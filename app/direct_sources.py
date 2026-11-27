"""不依赖 akShare 的直连数据源。

云端沙箱里 akShare 走 requests 会遇到证书链问题（datacenter-web.eastmoney.com
直接抛 SSLError），而 urllib 用系统 CA 是通的；生意社那个接口更是逐日抓取、
非交易日还会 sleep(3) 重试 5 次，25 天窗口能跑到几分钟。

所以这里用 urllib 直连同一批数据源，akShare 降级为备选路径。
纯标准库 + pandas（pandas 本来就是 akShare 的依赖，一定在）。
"""

from __future__ import annotations

import io
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

try:
    from .source_names import SHANGSHE_NAME as _SHANGSHE_NAME
except ImportError:  # 直接当脚本跑
    from source_names import SHANGSHE_NAME as _SHANGSHE_NAME

# 生意社页面标签 -> 项目品种代码
_SHANGSHE_CODE_OF = {v: k for k, v in _SHANGSHE_NAME.items()}

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
REFERER_SINA = "https://finance.sina.com.cn/"

# ---------------------------------------------------------------- 工具


def _get(url: str, *, timeout: int = 7, headers: dict | None = None, encoding: str = "utf-8",
         retries: int = 2) -> str | None:
    # Accept 必须是 text/html 开头：生意社对 `Accept: */*` 会返回 JS 反爬挑战页
    # （几百字节的混淆脚本，解析不出任何表格），换成 text/html 立刻正常。
    hdrs = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            break
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries - 1:
                time.sleep(0.6 * (attempt + 1))
    else:
        return None
    for enc in (encoding, "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def _get_json(url: str, params: dict, *, timeout: int = 7) -> dict | None:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    text = _get(full, timeout=timeout, headers={"Accept": "application/json, text/plain, */*"})
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _read_html_tables(text: str):
    import pandas as pd

    try:
        return pd.read_html(io.StringIO(text))
    except Exception:
        return []


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        s = str(value).strip().replace(",", "")
        if s in ("", "-", "--", "nan", "None"):
            return None
        # 生意社的现期差列会写成 "-15-2.31%"，取第一段数字
        m = re.match(r"^-?\d+(?:\.\d+)?", s)
        return float(m.group()) if m else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 新浪·会员持仓


def sina_position(contract: str, date: str) -> dict | None:
    """新浪财经会员持仓排名。date 形如 2026-11-25。"""
    url = (
        "https://vip.stock.finance.sina.com.cn/q/view/vFutures_Positions_cjcc.php"
        f"?t_breed={urllib.parse.quote(contract)}&t_date={date}"
    )
    text = _get(url, timeout=7, headers={"Referer": REFERER_SINA}, encoding="gbk")
    if not text:
        return None
    tables = _read_html_tables(text)
    if len(tables) < 5:
        return None

    def pick(idx: int, qty_col: str) -> list[dict]:
        t = tables[idx]
        rows = []
        for _, r in t.iterrows():
            member = str(r.get("会员简称") or r.get("会员名称") or "").strip()
            qty = _to_float(r.get(qty_col))
            if not member or qty is None:
                continue
            rows.append({
                "rank": int(_to_float(r.get("名次")) or 0),
                "member": member,
                "qty": qty,
                "chg": _to_float(r.get("比上交易增减")) or 0.0,
            })
        return rows

    longs = pick(3, "多单持仓")
    shorts = pick(4, "空单持仓")
    if not longs and not shorts:
        return None
    return {"long": longs, "short": shorts}


# ---------------------------------------------------------------- 东方财富·期货库存

_EM_INVENTORY_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def eastmoney_inventory(symbol: str) -> dict | None:
    """东财期货库存。返回 {"date": [...], "value": [...]}（按日期升序）。"""
    meta = _get_json(
        _EM_INVENTORY_URL,
        {
            "reportName": "RPT_FUTU_POSITIONCODE",
            "columns": "TRADE_MARKET_CODE,TRADE_CODE,TRADE_TYPE",
            "filter": '(IS_MAINCODE="1")',
            "pageNumber": "1",
            "pageSize": "500",
            "source": "WEB",
            "client": "WEB",
        },
    )
    if not meta:
        return None
    rows = ((meta.get("result") or {}).get("data")) or []
    # 接口给的是「中文名 → 品种代码」：{"TRADE_TYPE": "铁矿石", "TRADE_CODE": "I"}
    name_to_code = {str(r.get("TRADE_TYPE") or ""): str(r.get("TRADE_CODE") or "") for r in rows}
    # 反查表要大小写不敏感：东财里工业硅/碳酸锂/多晶硅是小写代码（si/lc/ps）
    code_lookup = {str(v).upper(): str(v) for v in name_to_code.values()}
    # SECURITY_CODE 过滤用的是「代码」，不是中文名
    product_id = ""
    if symbol in name_to_code:
        product_id = name_to_code[symbol]
    elif symbol.upper() in code_lookup:
        product_id = code_lookup[symbol.upper()]
    if not product_id:
        return None

    data = _get_json(
        _EM_INVENTORY_URL,
        {
            "reportName": "RPT_FUTU_STOCKDATA",
            "columns": "SECURITY_CODE,TRADE_DATE,ON_WARRANT_NUM,ADDCHANGE",
            "filter": f'(SECURITY_CODE="{product_id}")(TRADE_DATE>=\'2020-10-28\')',
            "pageNumber": "1",
            "pageSize": "500",
            "sortTypes": "-1",
            "sortColumns": "TRADE_DATE",
            "source": "WEB",
            "client": "WEB",
        },
    )
    if not data:
        return None
    items = ((data.get("result") or {}).get("data")) or []
    dates, values = [], []
    for r in items:
        d = str(r.get("TRADE_DATE") or "")[:10]
        v = _to_float(r.get("ON_WARRANT_NUM"))
        if not d or v is None:
            continue
        dates.append(d)
        values.append(v)
    if not dates:
        return None
    # 接口按日期倒序返回，翻成升序
    order = sorted(range(len(dates)), key=lambda i: dates[i])
    return {"date": [dates[i] for i in order], "value": [values[i] for i in order]}


# ---------------------------------------------------------------- 生意社·现货与基差

_SPOT_URL = "https://www.100ppi.com/sf/day-{date}.html"


def shangshe_spot_day(name: str, date: str, code: str = "") -> dict | None:
    """生意社单日现货与基差。date 形如 2026-11-25。

    name 可以是页面上的品种标签（如「热轧卷板」），也允许传空——此时返回
    该日全部品种（配合 shangshe_spot_day_all 做多品种批量）。

    注意：页面上的「现期差」列不可直接用 —— 它把数字和百分比拼在一起
    （"72667.63%"），精度也错。这里只取现货价与合约价，基差自己算：
        基差 = 合约价 - 现货价 ；基差率 = 基差 / 现货价
    与 akShare 的口径一致。
    """
    text = _get(_SPOT_URL.format(date=date), timeout=7)
    if not text:
        return None
    tables = _read_html_tables(text)
    if not tables:
        # 生意社的反爬挑战页：拿到的是几百字节的混淆 JS，没有任何表格
        return None
    page_date = _page_date(tables, date)
    for t in tables:
        if t.shape[0] < 4 or t.shape[1] < 8:
            continue
        body = t.iloc[2:]
        for _, r in body.iterrows():
            label = str(r.iloc[0]).strip()
            if name and label != name:
                continue
            spot = _to_float(r.iloc[1])
            if spot is None:
                continue
            near_price = _to_float(r.iloc[3])
            dom_price = _to_float(r.iloc[6])
            near_code = str(r.iloc[2] or "").strip()
            dom_code = str(r.iloc[5] or "").strip()
            prefix = (code or "").upper()
            near_basis = (near_price - spot) if (near_price is not None and spot) else None
            dom_basis = (dom_price - spot) if (dom_price is not None and spot) else None
            return {
                "date": page_date or date,
                "requested": date,
                "label": label,
                "spot": spot,
                "near_contract": f"{prefix}{near_code}" if prefix and near_code else near_code,
                "near_price": near_price,
                "near_basis": near_basis,
                "dom_contract": f"{prefix}{dom_code}" if prefix and dom_code else dom_code,
                "dom_price": dom_price,
                "dom_basis": dom_basis,
                "dom_basis_rate": (dom_basis / spot) if (dom_basis is not None and spot) else None,
            }
    return None


def shangshe_spot_day_all(date: str) -> dict:
    """抓一天生意社日页，返回 {品种代码: 行数据}（覆盖页面全部品种）。

    一次 HTTP 请求就能拿到全部 ~44 个品种的现货与基差，比逐品种抓快一个量级，
    也避免多次请求触发生意社的频率限制。
    """
    text = _get(_SPOT_URL.format(date=date), timeout=7)
    if not text:
        return {}
    tables = _read_html_tables(text)
    if not tables:
        return {}
    page_date = _page_date(tables, date)
    out: dict[str, dict] = {}
    for t in tables:
        if t.shape[0] < 4 or t.shape[1] < 8:
            continue
        for _, r in t.iloc[2:].iterrows():
            label = str(r.iloc[0]).strip()
            code = _SHANGSHE_CODE_OF.get(label)
            if not code:
                continue
            spot = _to_float(r.iloc[1])
            if spot is None:
                continue
            near_price = _to_float(r.iloc[3])
            dom_price = _to_float(r.iloc[6])
            near_code = str(r.iloc[2] or "").strip()
            dom_code = str(r.iloc[5] or "").strip()
            dom_basis = (dom_price - spot) if (dom_price is not None and spot) else None
            out[code] = {
                "date": page_date or date,
                "requested": date,
                "label": label,
                "spot": spot,
                "near_contract": f"{code}{near_code}" if near_code else "",
                "near_price": near_price,
                "near_basis": (near_price - spot) if (near_price is not None and spot) else None,
                "dom_contract": f"{code}{dom_code}" if dom_code else "",
                "dom_price": dom_price,
                "dom_basis": dom_basis,
                "dom_basis_rate": (dom_basis / spot) if (dom_basis is not None and spot) else None,
            }
        break
    return out


def _page_date(tables, fallback: str) -> str | None:
    """从页面第一个表格里抠出真实日期。

    生意社在非交易日会把请求重定向到最近有数据的那天，
    不做这个去重就会把同一天的数据当成多天的序列。
    """
    try:
        cell = tables[0].iloc[1, 1]
    except Exception:
        return None
    digits = "".join(re.findall(r"[0-9]", str(cell)))
    if len(digits) >= 8:
        ymd = digits[3:11]
        if len(ymd) == 8 and ymd.isdigit():
            return f"{ymd[0:4]}-{ymd[4:6]}-{ymd[6:8]}"
    return fallback


def recent_trading_days(count: int = 20) -> list[str]:
    """最近 count 个工作日（跳过周末）。日期格式 YYYY-MM-DD。"""
    out: list[str] = []
    day = datetime.now()
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day.strftime("%Y-%m-%d"))
        day -= timedelta(days=1)
    return out


# ---------------------------------------------------------------- 上期所·注册仓单

# 上期所（含能源中心 INE）仓单日报。akShare 的 get_shfe_receipt_3 走 requests，
# 云端沙箱里可能因证书链失败；这个 URL 用 urllib 直连是通的。
# 页面结构：每个品种一张表，第 0 列是仓库名，最后一行「总计」是仓单量与增减。
_SHFE_STOCK_URL = (
    "https://www.shfe.com.cn/data/tradedata/future/stockdata/dailystock_{ymd}/ZH/all.html"
)

# 页面上的中文品种名 -> 项目品种代码（上期所口径）
# 注意：仓单日报里多数品种分「(仓库)」和「(厂库)」两张表，
# 这里按剥掉后缀后的基名归并，两张表的量相加才是该品种的仓单总量。
_SHFE_STOCK_NAME = {
    "铜": "CU", "铝": "AL", "锌": "ZN", "铅": "PB", "镍": "NI", "锡": "SN",
    "不锈钢": "SS", "氧化铝": "AO", "螺纹钢": "RB", "热轧卷板": "HC",
    "黄金": "AU", "白银": "AG", "燃料油": "FU", "石油沥青": "BU",
    "中质含硫原油": "SC", "天然橡胶": "RU", "纸浆": "SP", "20号胶": "NR",
    "低硫燃料油": "LU", "丁二烯橡胶": "BR", "铸造铝合金": "AD",
    "胶版印刷纸": "OP", "线材": "WR",
}


def shfe_receipt_series(code: str, days: int = 22) -> list[dict]:
    """上期所仓单序列（逐日抓，取最近若干个有数据的工作日）。

    用途：当东财库存序列不覆盖某品种时（如原油 SC），
    仓单本身就是**交割口径库存**——它是真实的、可核验的库存读数，
    比留空更好，但口径不同，必须在展示时标注清楚。
    """
    code = (code or "").upper()
    if not code:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for day in recent_trading_days(days + 6):
        iso = day
        try:
            page = shfe_receipt_day(iso)
        except Exception:
            page = {}
        hit = page.get(code)
        if not hit or iso in seen:
            continue
        seen.add(iso)
        out.append({"date": iso, "value": hit["total"], "delta": hit.get("delta") or 0.0})
        if len(out) >= days:
            break
    out.sort(key=lambda r: r["date"])
    return out


def shfe_receipt_day(date: str) -> dict:
    """上期所仓单日报 → {品种代码: {"total": 手, "delta": 手}}。

    date 形如 2026-11-26。返回空字典表示当天没取到（周末/未发布）。
    同一品种的「仓库」与「厂库」两张表会合并求和。
    """
    ymd = date.replace("-", "")
    text = _get(_SHFE_STOCK_URL.format(ymd=ymd), timeout=10)
    if not text:
        return {}
    tables = _read_html_tables(text)
    out: dict[str, dict] = {}
    for t in tables:
        if t.shape[0] < 2 or t.shape[1] < 3:
            continue
        try:
            label = str(t.iloc[0, 1]).strip()
        except Exception:
            continue
        # 剥掉「(仓库)」「(厂库)」等后缀再匹配
        base = re.sub(r"\((仓库|厂库|保税|一般|完税)\)$", "", label).strip()
        code = _SHFE_STOCK_NAME.get(base)
        if not code:
            continue
        total = _to_float(t.iloc[-1, 2])
        if total is None:
            continue
        delta = _to_float(t.iloc[-1, 3]) or 0.0
        prev = out.get(code)
        if prev:
            prev["total"] += total
            prev["delta"] += delta
        else:
            out[code] = {"total": total, "delta": delta, "date": date}
    return out
