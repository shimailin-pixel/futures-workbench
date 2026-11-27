"""基本面自动采集层 —— 把公开数据源接进研究工作台。

覆盖能力
--------
| 数据           | 来源                        | 等级 | 覆盖            |
|----------------|-----------------------------|------|-----------------|
| 交割仓单日报   | 上期所 / 郑商所 / 广期所官方 | S    | SHFE CZCE GFEX  |
| 期货库存序列   | 东方财富数据中心            | B    | 30/44 品种      |
| 现货价与基差   | 生意社 100ppi              | B    | 39/44 品种      |
| 会员持仓排名   | 新浪财经                    | B    | 全部（需主力合约）|

大商所（dce.com.cn）对程序访问启用了反爬验证（HTTP 412，首页同样被拦），
其仓单日报与成交持仓排名在本机取不到。已用「东财库存 + 新浪持仓排名」覆盖
其核心变量，缺口会写进返回结果的 gaps，前端显式提示，**不做任何数值伪造**。

纪律约定
--------
1. 每块数据都带 source / grade / asof，事实必须有来源和时点。
2. 采集失败的块返回 ok=False + error，其余块照常返回，互不拖累。
3. akShare 是可选依赖：未安装时全部源降级为"不可用"，工作台其余部分照常工作。
"""

from __future__ import annotations

import inspect
import io
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import direct_sources as ds  # noqa: E402
import source_names as sn  # noqa: E402
CACHE_FILE = APP_DIR / "data" / "fund_cache.json"

# ---------------------------------------------------------------- 来源等级
GRADE_SHFE = "S"   # 交易所官方一手数据
GRADE_B = "B"      # 第三方平台整理
GRADE_NA = "-"

# ---------------------------------------------------------------- 仓单品种名映射
# 上期所 / 能源中心：仓单日报用中文品种名（部分带"仓库/厂库"后缀）
RECEIPT_NAME_SHFE = {
    "RB": "螺纹钢", "HC": "热轧卷板", "CU": "铜", "AL": "铝", "ZN": "锌",
    "PB": "铅", "NI": "镍", "SN": "锡", "AO": "氧化铝", "SS": "不锈钢",
    "BU": "石油沥青", "RU": "天然橡胶", "SP": "纸浆", "AU": "黄金", "AG": "白银",
    "SC": "中质含硫原油", "FU": "燃料油", "NR": "20号胶",
}

# 郑商所：仓单日报与持仓排名表都用品种代码，其中 PTA 的代码是 "PTA" 而非 "TA"
RECEIPT_NAME_CZCE = {
    "SF": "SF", "SM": "SM", "TA": "PTA", "MA": "MA", "SA": "SA", "FG": "FG",
    "UR": "UR", "RM": "RM", "OI": "OI", "SR": "SR", "CF": "CF",
    "AP": "AP", "CJ": "CJ",
}

# 郑商所持仓排名表的品种列同样用 "PTA"
CZCE_VARIETY = dict(RECEIPT_NAME_CZCE)

# 广期所：仓单日报用中文品种名
RECEIPT_NAME_GFEX = {
    "LC": "碳酸锂", "SI": "工业硅", "PS": "多晶硅",
}

EXCHANGE_RECEIPT_SOURCE = {
    "SHFE": ("上期所仓单日报", GRADE_SHFE),
    "INE": ("上期所仓单日报（含能源品种）", GRADE_SHFE),
    "CZCE": ("郑商所仓单日报", GRADE_SHFE),
    "GFEX": ("广期所仓单日报", GRADE_SHFE),
}

# 增值税税率：农产品多为 9%，工业品多为 13%。用于把含税现货折回不含税口径。
VAT_RATE = {
    "M": 0.09, "Y": 0.09, "P": 0.09, "C": 0.09, "CS": 0.09, "OI": 0.09,
    "RM": 0.09, "SR": 0.09, "CF": 0.09, "AP": 0.09, "CJ": 0.09, "LH": 0.09,
    "JD": 0.09, "PK": 0.09,
}

_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}
_disk_cache: dict[str, dict] = {}

# 缓存格式版本。改动采集结果的结构或日期格式时递增，
# 旧缓存会自动失效，避免"改了代码却读到历史旧格式"的坑。
_CACHE_VERSION = "v6"

# 整体采集预算。反向代理一般 120 秒超时，这里留出余量：
# 超时后未完成的块标成「采集中」先返回，已完成的照常展示。
_COLLECT_TIMEOUT = 55.0


# ---------------------------------------------------------------- 基础工具
def _ak():
    """懒加载 akShare。未安装返回 None，调用方自行降级。"""
    try:
        import akshare as ak  # noqa: PLC0415
        return ak
    except Exception:
        return None


_AK_ARG_ALIASES = {
    # akshare 不同版本对同一语义的参数命名不一致：
    #   1.20.x 用 start_date/end_date，1.18.x 用 start_day/end_day
    "start_date": ("start_day", "begin_date"),
    "end_date": ("end_day", "finish_date"),
    "date": ("trade_date", "day"),
    # 品种列表参数：1.20.x 用 symbols=（字符串），1.18.x 用 vars_list=（列表）
    "symbols": ("vars_list", "symbol_list"),
    "vars_list": ("symbols", "symbol_list"),
}

# 这两个参数互为别名，同一语义只能传一个（两个都传会报重复参数）
_ALIAS_EXCLUSIVE = {"symbols", "vars_list"}


def _find_ak_function(ak, name: str):
    """在 akshare 的子模块里找一个函数（顶层没有时用）。

    例：get_shfe_receipt_3 只存在于 akshare.futures.futures_receipt。
    先按已知路径找，再退回遍历所有子模块。
    """
    known_paths = (
        "akshare.futures.futures_receipt",
        "akshare.futures.futures_warehouse_receipt",
        "akshare.futures.futures_inventory_em",
    )
    for path in known_paths:
        try:
            mod = __import__(path, fromlist=["*"])
        except Exception:
            continue
        fn = getattr(mod, name, None)
        if callable(fn):
            return fn

    import pkgutil

    try:
        import akshare
    except Exception:
        return None
    for mod_info in pkgutil.iter_modules(akshare.__path__, prefix="akshare."):
        try:
            mod = __import__(mod_info.name, fromlist=["*"])
        except Exception:
            continue
        fn = getattr(mod, name, None)
        if callable(fn):
            return fn
    return None


def _ak_call(name: str, **kwargs):
    """调用 akshare 接口，屏蔽版本间的签名差异。

    akshare 迭代很快，同一个接口名在不同版本里可能：
      1. 参数名不同（start_date ↔ start_day）→ 自动改写成本版本认识的叫法；
      2. 参数集不同（新增/删除）→ 只传当前版本认识的参数；
      3. 从「函数」变成「按市场分组的函数列表」→ 取第一个可调用成员。

    线上沙箱预装的是 akshare 1.18.22，本机是 1.20.70，两边参数名对不上。
    早期版本直接写死 start_date 会在云端报
    "got an unexpected keyword argument 'start_date'"，
    而如果只是把参数丢掉不传，就会拿到接口的默认日期区间（2021 年），
    界面上会显示一堆过期数据 —— 所以日期参数映射不上时宁可报错。
    """
    ak = _ak()
    if ak is None:
        raise RuntimeError("akshare 未安装")
    fn = getattr(ak, name, None)
    if fn is None:
        # 有些接口不在顶层（如 get_shfe_receipt_3 在 akshare.futures.futures_receipt），
        # 逐个子模块找一遍。
        fn = _find_ak_function(ak, name)
    if fn is None:
        raise AttributeError(f"akshare 没有接口 {name}")
    if not callable(fn):
        members = [f for f in fn if callable(f)] if isinstance(fn, (list, tuple)) else []
        if not members:
            raise TypeError(f"akshare.{name} 不是可调用接口")
        fn = members[0]
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = None
    if params is None or not kwargs:
        return fn(**kwargs)

    has_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    if has_var_kw:
        return fn(**kwargs)

    allowed: dict = {}
    dropped_dates = 0
    used_exclusive: set[str] = set()

    def _put(target: str, value) -> None:
        """写入 allowed，同语义别名只保留第一个。"""
        if target in _ALIAS_EXCLUSIVE:
            if used_exclusive:
                return
            used_exclusive.add(target)
            # 值类型跟着参数名变：新版 symbols= 收字符串，旧版 vars_list= 收列表
            if target == "symbols" and isinstance(value, (list, tuple)):
                value = ",".join(str(v) for v in value)
            elif target == "vars_list" and isinstance(value, str):
                value = [v.strip() for v in value.split(",") if v.strip()]
        allowed[target] = value

    for key, value in kwargs.items():
        if key in params:
            _put(key, value)
            continue
        alias = next((a for a in _AK_ARG_ALIASES.get(key, ()) if a in params), None)
        if alias:
            _put(alias, value)
            continue
        if key in ("start_date", "end_date", "date"):
            # 日期参数映射不上：宁可不取这份数据，也不要用接口默认的过期区间
            dropped_dates += 1
        # 其它参数直接丢弃（该版本不认识）

    if dropped_dates:
        raise TypeError(
            f"akshare.{name} 的日期参数与本版本不兼容"
            f"（已知参数：{', '.join(params)}）"
        )
    try:
        out = fn(**allowed) if allowed else fn()
    except TypeError as exc:
        # akShare 内部对"源返回空"一律用 data_json["result"]["data"] 解引用，
        # 结果抛 "'NoneType' object is not subscriptable"。这不是代码 bug，
        # 而是"该品种/该日期在源上没有数据"，转成明确信号交给上层处理。
        if "not subscriptable" in str(exc):
            return None
        raise
    return out


def _err_text(exc: Exception, limit: int = 120) -> str:
    """把原始异常转成人话。

    原始的 SSL/urllib 堆栈对非技术同事完全不可读（"SSLError:
    HTTPSConnectionPool(host='datacenter-web.eastmoney.com'...)"），
    看着像工具坏了。这里把网络类异常翻译成一句能看懂的说明；
    代码类异常（TypeError 等）保留原文，便于排查。
    """
    name = type(exc).__name__
    raw = str(exc)
    low = raw.lower()
    lname = name.lower()
    if "ssl" in lname or "ssl" in low:
        return "数据源暂时连接失败（可能是网络波动或证书策略拦截），刷新可重试"
    if "timeout" in lname or "timed out" in low or "timeout" in low:
        return "数据源响应超时，该板块暂不可用"
    if ("connection" in lname or "connection" in low) and (
        "refused" in low or "reset" in low or "aborted" in low
    ):
        return "数据源连接被拒绝，该板块暂不可用"
    if "http error" in low:
        return "数据源接口不可用或已变更"
    if "412" in low:
        return "数据源对程序访问启用了反爬验证"
    return f"{name}: {raw[:limit]}"


def _load_disk_cache():
    global _disk_cache
    with _lock:
        if _disk_cache:
            return _disk_cache
        try:
            if CACHE_FILE.is_file():
                raw = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            else:
                raw = {}
        except Exception:
            raw = {}
        # 只保留当前版本前缀的条目，旧版本自动清理
        prefix = f"{_CACHE_VERSION}|"
        _disk_cache = {k: v for k, v in raw.items() if str(k).startswith(prefix)}
        return _disk_cache


def _save_disk_cache():
    with _lock:
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            prefix = f"{_CACHE_VERSION}|"
            payload = {k: v for k, v in _disk_cache.items() if str(k).startswith(prefix)}
            CACHE_FILE.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass


def _memo(key: str, ttl: float, producer, fail_ttl: float = 60.0, stale_ok: bool = False):
    """内存 + 磁盘两级缓存。磁盘层让服务重启后不必重新打外网。

    失败结果（producer 返回 None）只缓存 fail_ttl 秒，成功结果才走完整 ttl。
    早期版本两者共用 ttl，导致「一次网络抖动 → 该板块失败被写进磁盘缓存 →
    之后整整一小时刷新都是同样的失败」，线上看起来就像数据源彻底坏了。

    stale_ok=True 时，采集失败会回退到过期缓存：那是真实的历史数据，
    不是推测值；配合各块自带的 asof 字段，用户能看到数据实际是哪天的。
    """
    key = f"{_CACHE_VERSION}|{key}"
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit:
            # 内存缓存同样要区分成功/失败：失败结果只活 fail_ttl 秒，
            # 否则一次抖动会在内存里把该板块锁死整个 ttl。
            stored_t, stored_v = hit
            is_empty = stored_v is None or (
                isinstance(stored_v, (dict, list, tuple, set)) and len(stored_v) == 0
            )
            eff = fail_ttl if is_empty else ttl
            if now - stored_t < eff:
                return stored_v

    disk = _load_disk_cache()
    entry = disk.get(key)
    if entry:
        eff_ttl = fail_ttl if entry.get("f") else ttl
        if now - entry.get("t", 0) < eff_ttl:
            with _lock:
                _cache[key] = (entry["t"], entry["v"])
            return entry["v"]

    value = producer()
    # 空结果（None / {} / []）等同于失败：早期版本只把 None 当失败，
    # 而「空字典」会被当成成功结果按完整 TTL（1 小时）写进磁盘缓存——
    # 于是一次网络抖动就把该板块锁死一小时，之后一直返回空表。
    # 这是线上「基本面数据没有了」最难查的一个原因。
    empty = value is None or (
        isinstance(value, (dict, list, tuple, set)) and len(value) == 0
    )
    if empty and stale_ok:
        old = disk.get(key)
        if old and old.get("v") is not None and not (
            isinstance(old.get("v"), (dict, list, tuple, set)) and len(old["v"]) == 0
        ):
            return old["v"]

    now = time.time()
    with _lock:
        _cache[key] = (now, value)
    disk[key] = {"t": now, "v": value, "f": 1 if empty else 0}
    _save_disk_cache()
    return value


def _num(value) -> float | None:
    """清洗 akshare 返回的数值：支持 '1,234'、'652'、'--'、NaN。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return None if value != value else float(value)
    text = str(value).strip().replace(",", "").replace("%", "")
    if text in ("", "-", "--", "nan", "NaN", "None"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _recent_trading_day(offset: int = 0) -> str:
    """粗略取最近交易日（跳过周末）。交易所节假日仍可能取到空，调用方需容错。"""
    day = datetime.now() - timedelta(days=offset)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day.strftime("%Y%m%d")


def _find_day(dirs: list[str], offset: int = 0) -> str | None:
    """从最近的日子往前找，直到某个仓单接口真的有数据。"""
    for i in range(offset, offset + 8):
        day = _recent_trading_day(i)
        if day in dirs:
            return day
    return None


def _iso(day: str) -> str:
    """YYYYMMDD → YYYY-MM-DD"""
    return f"{day[:4]}-{day[4:6]}-{day[6:]}" if len(day) == 8 else day


# ---------------------------------------------------------------- 仓单日报
def _shfe_receipt_map(date: str) -> dict:
    """上期所仓单日报 → {品种名: {total, delta, detail[]}}"""
    ak = _ak()
    df = _ak_call("futures_warehouse_receipt_shfe", date=date)
    detail = df[df["行类型"].astype(str) == "明细"].copy()
    detail["base"] = (
        detail["品种"].astype(str)
        .str.replace("仓库$", "", regex=True)
        .str.replace("厂库$", "", regex=True)
    )
    out: dict[str, dict] = {}
    for base, sub in detail.groupby("base"):
        rows = []
        for _, r in sub.iterrows():
            qty = _num(r.get("期货仓单"))
            if qty:
                rows.append({
                    "name": str(r.get("仓库") or ""),
                    "region": str(r.get("地区") or ""),
                    "qty": qty,
                    "delta": _num(r.get("增减")) or 0.0,
                })
        rows.sort(key=lambda x: -x["qty"])
        out[str(base)] = {
            "total": sum(r["qty"] for r in rows),
            "delta": sum(r["delta"] for r in rows),
            "detail": rows[:12],
        }
    return out


def _czce_receipt_map(date: str) -> dict:
    """郑商所仓单日报 → {品种代码: {total, delta, forecast, premium, detail[]}}"""
    ak = _ak()
    df = _ak_call("futures_warehouse_receipt_czce", date=date)
    df = df.copy()
    # 注意：必须先把两列转成字符串再过滤。groupby 传原始 Series 会按索引对齐，
    # 与过滤后的子集索引错位，导致部分品种整组丢失。
    df["品种"] = df["品种"].astype(str)
    df["仓库编号"] = df["仓库编号"].astype(str)
    detail = df[df["仓库编号"] != "小计"]
    subtotal = df[df["仓库编号"] == "小计"]

    forecast_by_code: dict[str, float] = {}
    for code, sub in subtotal.groupby("品种"):
        val = sum((_num(v) or 0.0) for v in sub.get("有效预报", []))
        forecast_by_code[str(code)] = val

    out: dict[str, dict] = {}
    for code, sub in detail.groupby("品种"):
        if code in ("None", "nan"):
            continue
        rows = []
        for _, r in sub.iterrows():
            qty = _num(r.get("仓单数量"))
            if qty is None:
                continue
            rows.append({
                "name": str(r.get("仓库简称") or r.get("仓库编号") or ""),
                "region": "",
                "qty": qty,
                "delta": _num(r.get("当日增减")) or 0.0,
                "premium": _num(r.get("升贴水")),
            })
        rows.sort(key=lambda x: -x["qty"])
        premiums = [r["premium"] for r in rows if r["premium"] is not None]
        out[str(code)] = {
            "total": sum(r["qty"] for r in rows),
            "delta": sum(r["delta"] for r in rows),
            "forecast": forecast_by_code.get(str(code), 0.0),
            "premium": premiums[0] if premiums else None,
            "detail": rows[:12],
        }
    return out


def _gfex_receipt_map(date: str) -> dict:
    """广期所仓单日报 → {品种名: {total, delta, detail[]}}"""
    ak = _ak()
    df = _ak_call("futures_warehouse_receipt_gfex", date=date)
    df = df.copy()
    df["品种"] = df["品种"].astype(str)
    df["仓库/分库"] = df["仓库/分库"].astype(str)
    detail = df[df["仓库/分库"] != "总计"]
    out: dict[str, dict] = {}
    for name, sub in detail.groupby("品种"):
        rows = []
        for _, r in sub.iterrows():
            qty = _num(r.get("今日仓单量"))
            if qty is None:
                continue
            rows.append({
                "name": str(r.get("仓库/分库") or ""),
                "region": "",
                "qty": qty,
                "delta": _num(r.get("增减")) or 0.0,
            })
        rows.sort(key=lambda x: -x["qty"])
        out[str(name)] = {
            "total": sum(r["qty"] for r in rows),
            "delta": sum(r["delta"] for r in rows),
            "detail": rows[:12],
        }
    return out


def _receipt_via_akshare(code: str, exchange: str = "") -> dict | None:
    """跨交易所仓单备选。

    两条路径：
    1. `futures_receipt` —— 跨交易所汇总，但内部会遍历大商所，
       大商所 412 会让**整个接口**抛 DceBlockedError，所以单独用不可靠。
    2. `get_shfe_receipt_3` —— 只走上期所自己的仓单汇总源，不经大商所，
       云端沙箱里是唯一能取到上期所仓单的路径（汇总口径，无仓库明细）。

    参数名在 akshare 版本间不同：新版是 symbols=，旧版是 vars_list=，
    交给 _ak_call 的别名机制处理。
    """
    code = (code or "").upper()
    if not code:
        return None

    def producer():
        # 路径 0：上期所官网直连（urllib，不经 requests，云端证书链稳）
        #        只覆盖上期所 + 能源中心品种
        if exchange in ("SHFE", "INE"):
            for offset in range(0, 8):
                day = _recent_trading_day(offset)
                iso = day[0:4] + "-" + day[4:6] + "-" + day[6:8]
                try:
                    page = ds.shfe_receipt_day(iso)
                except Exception:
                    page = {}
                hit = page.get(code)
                if hit:
                    return hit

        # 路径 2：上期所仓单汇总（不经大商所）
        for offset in range(0, 8):
            day = _recent_trading_day(offset)
            iso = day[0:4] + "-" + day[4:6] + "-" + day[6:8]
            df = _ak_call("get_shfe_receipt_3", date=day, vars_list=[code])
            if df is None or not len(df):
                continue
            for _, r in df.iterrows():
                sym = str(r.get("var") or "").strip().upper()
                if sym != code:
                    continue
                total = _num(r.get("receipt"))
                if total is None:
                    continue
                return {
                    "total": total,
                    "delta": _num(r.get("receipt_chg")) or 0.0,
                    "date": iso,
                }

        # 路径 1：跨交易所汇总（可能因大商所 412 整体失败）
        for offset in range(0, 8):
            day = _recent_trading_day(offset)
            iso = day[0:4] + "-" + day[4:6] + "-" + day[6:8]
            df = _ak_call(
                "futures_receipt",
                start_date=day,
                end_date=day,
                symbols=code,
            )
            if df is None or not len(df):
                continue
            for _, r in df.iterrows():
                sym = str(r.get("var") or "").strip().upper()
                if sym != code:
                    continue
                total = _num(r.get("receipt"))
                if total is None:
                    continue
                return {
                    "total": total,
                    "delta": _num(r.get("receipt_chg")) or 0.0,
                    "date": iso,
                }
        return None

    try:
        return _memo(f"receipt|ak|{code}", 3600, producer)
    except Exception:
        return None


def _receipt_block(code: str, exchange: str) -> dict:
    """取单品种的仓单块。DCE 无源，返回带说明的失败块。"""
    source, grade = EXCHANGE_RECEIPT_SOURCE.get(
        exchange, ("交易所仓单日报", GRADE_SHFE)
    )

    if exchange == "DCE":
        return {
            "ok": False,
            "error": "大商所官网对程序访问启用了反爬验证（HTTP 412），仓单日报无法自动获取",
            "source": "大商所仓单日报",
            "grade": GRADE_SHFE,
            "manual_hint": "请人工查 dce.com.cn → 仓单日报，或用交易所日报 PDF",
        }

    name = (
        RECEIPT_NAME_SHFE.get(code)
        or RECEIPT_NAME_CZCE.get(code)
        or RECEIPT_NAME_GFEX.get(code)
    )
    if not name:
        return {
            "ok": False,
            "error": f"{exchange} 仓单日报中未收录该品种（{code}）",
            "source": source,
            "grade": grade,
        }

    fetcher = {
        "SHFE": _shfe_receipt_map,
        "INE": _shfe_receipt_map,
        "CZCE": _czce_receipt_map,
        "GFEX": _gfex_receipt_map,
    }[exchange]

    try:
        cache_map = _memo(
            f"receipt|{fetcher.__name__}", 3 * 3600, lambda: _fetch_receipt_map(fetcher)
        )
    except Exception as exc:
        cache_map = None
        primary_err = _err_text(exc, 140)

    data = (cache_map or {}).get(name)
    if not data:
        # 备选路径：akShare 的 futures_receipt 是跨交易所的仓单汇总接口
        # （只给品种总量与增减，没有仓库明细）。云端沙箱里
        # futures_warehouse_receipt_shfe 缺失，只有这条能取到上期所仓单。
        data = _receipt_via_akshare(code, exchange)
        if data:
            return {
                "ok": True,
                "value": {
                    "total": data["total"],
                    "delta": data["delta"],
                    "forecast": None,
                    "premium": None,
                    "detail": [],
                    "unit": "手",
                },
                "source": "akShare·期货仓单汇总",
                "grade": GRADE_B,
                "asof": data.get("date"),
                "unit": "手",
                "note": "仓单汇总口径（无仓库明细），来自交易所日报",
            }

    if not data:
        return {
            "ok": False,
            "error": f"{source} 未找到品种「{name}」的仓单记录（当日可能无仓单或尚未发布）",
            "source": source,
            "grade": grade,
        }

    return {
        "ok": True,
        "value": {
            "total": data["total"],
            "delta": data["delta"],
            "forecast": data.get("forecast"),
            "premium": data.get("premium"),
            "detail": data.get("detail", []),
            "unit": "手",
        },
        "source": source,
        "grade": grade,
        "asof": cache_map.get("__date__") if isinstance(cache_map, dict) else None,
        "unit": "手",
        "note": "郑商所另含有效预报与升贴水，是交割压力的直接读数",
    }


_RECEIPT_DATE_HINT: dict[str, str] = {}


def _fetch_receipt_map(fetcher) -> dict:
    """尝试最近若干个交易日，直到某天有数据。返回带 __date__ 的映射。"""
    for offset in range(0, 8):
        day = _recent_trading_day(offset)
        try:
            data = fetcher(day)
        except Exception:
            continue
        if data:
            data["__date__"] = _iso(day)
            return data
    return {}


# ---------------------------------------------------------------- 库存
def _inventory_block(code: str, name: str = "") -> dict:
    """期货库存。直连优先（不依赖 akShare），失败再退回 akShare。

    东财的品种标识是它自己的中文名（沪铜/热卷/锰硅…），跟项目内部的
    name 完全对不上，所以一律按「代码」经 source_names 换算，
    直接用 meta["name"] 去查永远查不到。
    """

    def producer():
        # 直连：东财 JSON 接口，urllib 走系统 CA，云端不会被 requests 的证书链坑到
        em_name = sn.eastmoney_name(code) or name or code
        if code.upper() in sn.EAST_MONEY_MISSING:
            # 源本身不覆盖该品种（原油），直接走备选，别去撞 requests 的证书问题
            em_name = ""
        data = ds.eastmoney_inventory(em_name) if em_name else None
        if data and data.get("date"):
            rows = []
            for i, d in enumerate(data["date"]):
                val = data["value"][i]
                rows.append({"date": d, "value": val, "delta": 0.0})
            # 用相邻差值补出增减
            for i in range(len(rows) - 1, 0, -1):
                rows[i]["delta"] = rows[i]["value"] - rows[i - 1]["value"]
            return rows
        # 备选：akShare
        if code.upper() in sn.EAST_MONEY_MISSING:
            # 该品种两个库存源都没有，别去撞 akShare 的 requests 证书问题
            return None
        df = _ak_call("futures_inventory_em", symbol=code)
        if df is None or not len(df):
            return None
        rows = []
        for _, r in df.iterrows():
            val = _num(r.get("库存"))
            if val is None:
                continue
            rows.append({
                "date": str(r.get("日期")),
                "value": val,
                "delta": _num(r.get("增减")) or 0.0,
            })
        return rows or None

    try:
        rows = _memo(f"inv|{code}", 3600, producer, stale_ok=True)
    except Exception as exc:
        return {
            "ok": False,
            "error": _err_text(exc, 120),
            "source": "东方财富·期货库存",
            "grade": GRADE_B,
        }
    if not rows:
        # 回退：仓单序列也是真实的库存读数（交割口径），
        # 总比留空强 —— 但口径不同，展示时必须标明来源。
        if code.upper() in sn.EAST_MONEY_MISSING:
            try:
                alt = _memo(
                    f"inv|receipt|{code}",
                    3600,
                    lambda: ds.shfe_receipt_series(code, 22) or None,
                    stale_ok=True,
                )
            except Exception:
                alt = None
            if alt:
                latest = alt[-1]
                return {
                    "ok": True,
                    "value": {
                        "latest": latest["value"],
                        "delta": latest.get("delta") or 0.0,
                        "asof": latest["date"],
                        "chg_5d": (alt[-1]["value"] - alt[-6]["value"]) if len(alt) > 5 else None,
                        "chg_20d": (alt[-1]["value"] - alt[0]["value"]) if len(alt) > 1 else None,
                        "series": alt[-120:],
                    },
                    "source": "上期所·注册仓单（交割口径库存）",
                    "grade": GRADE_B,
                    "asof": latest["date"],
                    "unit": "手",
                    "note": "该品种无第三方库存序列，此处以交易所注册仓单替代（交割口径，非厂库/社会库存）",
                }
        missing = sn.EAST_MONEY_MISSING.get(code.upper())
        return {
            "ok": False,
            "error": missing or "该品种无库存序列（郑商所品种与原油暂不覆盖）",
            "source": "东方财富·期货库存",
            "grade": GRADE_B,        }

    latest = rows[-1]
    def ago(n: int):
        return rows[-1 - n]["value"] if len(rows) > n else None

    v5, v20 = ago(5), ago(20)
    return {
        "ok": True,
        "value": {
            "latest": latest["value"],
            "delta": latest["delta"],
            "asof": latest["date"],
            "chg_5d": (latest["value"] - v5) if v5 is not None else None,
            "chg_20d": (latest["value"] - v20) if v20 is not None else None,
            "series": rows[-120:],
        },
        "source": "东方财富·期货库存",
        "grade": GRADE_B,
        "asof": latest["date"],
        "unit": "手",
    }


# ---------------------------------------------------------------- 现货与基差
def _basis_table_direct(code: str, name: str, days: int = 22) -> dict:
    """直连生意社抓基差序列。

    关键改动：生意社日页本身就含全部 ~44 个品种，用「一次请求取整页」
    的方式（shangshe_spot_day_all）代替逐日逐品种地抓：
      - HTTP 请求数从 days × 品种数 降到 days
      - 生意社的频率限制按请求数算，请求数降下来就不再触发反爬
      - 品种名统一走 source_names 的「代码 → 页面标签」映射

    akShare 那个接口是逐日循环、且非交易日 sleep(3) 重试 5 次，
    22 个工作日在云端能跑到几分钟；这里直连串行抓工作日，快一个量级。
    """
    code = (code or "").upper()
    if not code:
        return {}
    dates = ds.recent_trading_days(days)

    def to_row(r: dict) -> dict:
        return {
            "date": r.get("date"),
            "spot": r.get("spot"),
            "near_contract": r.get("near_contract") or "",
            "near_basis": r.get("near_basis"),
            "dom_contract": r.get("dom_contract") or "",
            "dom_basis": r.get("dom_basis"),
            "dom_basis_rate": r.get("dom_basis_rate"),
        }

    # 先抓一天做探针，确认源可用（反爬挑战页会让 tables 为空）
    probe_day = ds.shangshe_spot_day_all(dates[0])
    if not probe_day:
        return {}
    rows: dict[str, dict] = {}
    if code in probe_day:
        r = probe_day[code]
        rows[r.get("date") or dates[0]] = to_row(r)

    # 串行抓剩余日期：生意社对并发访问会返回 JS 反爬挑战页，
    # 并发 3 路时能随机把某些品种整批打空。串行 22 天约 7 秒，稳。
    # 连续失败 6 天就放弃剩下的：源被限流时死磕只会拖满整个采集预算。
    fails = 0
    for day in dates[1:]:
        page = ds.shangshe_spot_day_all(day)
        r = page.get(code)
        if not r or r.get("spot") is None:
            fails += 1
            if fails >= 6:
                break
            continue
        fails = 0
        rows[r.get("date") or r.get("requested")] = to_row(r)

    if not rows:
        return {}
    ordered = [rows[k] for k in sorted(rows)]
    return {code: ordered}


def _basis_table(code: str = "", name: str = "", days: int = 25) -> dict:
    """拉现货与基差表。

    :param code: 品种代码（用于定位行）
    :param name: 品种中文名（生意社表里用的是中文名）
    :param days: 回看天数。20 日基差率窗口需要至少 20 天，留 5 天缓冲。
    """
    ak = _ak()
    if ak is None:
        return {}
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    end = datetime.now().strftime("%Y%m%d")
    kwargs: dict = {"start_date": start, "end_date": end}
    if code:
        # vars_list 只在较新的 akshare 里有，_ak_call 会自动忽略旧版本不认识的关键字
        kwargs["vars_list"] = [code.upper()]
    df = _ak_call("futures_spot_price_daily", **kwargs)
    table: dict[str, list[dict]] = {}
    for _, r in df.iterrows():
        sym = str(r.get("symbol") or "").upper()
        if not sym:
            continue

        def pick(*names):
            for n in names:
                try:
                    v = r.get(n)
                except Exception:
                    v = None
                if v is not None:
                    return v
            return None

        spot = _num(pick("spot_price"))
        dom_contract = str(pick("dominant_contract", "dom_contract") or "")
        dom_price = _num(pick("dominant_contract_price", "dom_contract_price"))
        dom_basis = _num(pick("dom_basis", "dominant_basis"))
        dom_rate = _num(pick("dom_basis_rate", "dominant_basis_rate"))
        if dom_rate is None and dom_basis is not None and spot:
            # 旧版 akshare 没有基差率列，用「基差 ÷ 现货价」补齐（与新版口径一致）
            dom_rate = dom_basis / spot
        table.setdefault(sym, []).append({
            "date": _iso(str(pick("date") or "")),
            "spot": spot,
            "near_contract": str(pick("near_contract") or ""),
            "near_basis": _num(pick("near_basis")),
            "dom_contract": dom_contract,
            "dom_basis": dom_basis,
            "dom_price": dom_price,
            "dom_basis_rate": dom_rate,
        })
    return table


def _basis_block(code: str, name: str = "") -> dict:
    ak = _ak()
    if ak is None and not name:
        return {"ok": False, "error": "未安装 akShare，现货源不可用", "source": "生意社·现货与基差", "grade": GRADE_B}

    def producer():
        # 直连优先：urllib + pandas，云端不依赖 akShare 的 requests 证书链
        table = _basis_table_direct(code, name)
        if table.get(code.upper()):
            return table
        # 备选：akShare
        return _basis_table(code)

    try:
        table = _memo(f"basis|{code.upper()}", 3600, producer, stale_ok=True)
    except Exception as exc:
        return {
            "ok": False,
            "error": _err_text(exc, 120),
            "source": "生意社·现货与基差",
            "grade": GRADE_B,
        }

    if not table:
        missing = sn.SHANGSHE_MISSING.get(code.upper())
        return {
            "ok": False,
            "error": missing or "现货源返回空表（生意社限流或日页结构变更，点击「重新采集」重试）",
            "source": "生意社·现货与基差",
            "grade": GRADE_B,
        }

    rows = table.get(code.upper())
    if not rows:
        missing = sn.SHANGSHE_MISSING.get(code.upper())
        return {
            "ok": False,
            "error": missing or "该品种无现货报价（生意社现货页未收录该品种）",
            "source": "生意社·现货与基差",
            "grade": GRADE_B,
        }

    rows = [r for r in rows if r["spot"] is not None]
    if not rows:
        return {
            "ok": False,
            "error": "现货价缺失",
            "source": "生意社·现货与基差",
            "grade": GRADE_B,
        }

    latest = rows[-1]
    rate = latest["dom_basis_rate"]
    vat = VAT_RATE.get(code.upper(), 0.13)
    spot_ex = latest["spot"] / (1 + vat)
    dom_price = latest["spot"] - latest["dom_basis"] if latest["dom_basis"] is not None else None

    # 基差率的 20 日统计：不同品种的含税口径差异是常数项，
    # 只有"偏离自身历史均值"才是跨品种可比的信号。
    window = [r["dom_basis_rate"] for r in rows[-20:] if r["dom_basis_rate"] is not None]
    z20 = None
    if len(window) >= 5 and rate is not None:
        mean = sum(window) / len(window)
        var = sum((x - mean) ** 2 for x in window) / len(window)
        std = var ** 0.5
        if std > 1e-9:
            z20 = (rate - mean) / std

    return {
        "ok": True,
        "value": {
            "spot": latest["spot"],
            "spot_ex_vat": round(spot_ex, 2),
            "vat_rate": vat,
            "near_contract": latest["near_contract"],
            "near_basis": latest["near_basis"],
            "dom_contract": latest["dom_contract"],
            "dom_price": dom_price,
            "dom_basis": latest["dom_basis"],
            "dom_basis_rate": rate,
            "dom_basis_rate_pct": (rate * 100) if rate is not None else None,
            "dom_basis_ex_vat": (round(spot_ex - dom_price, 2)
                                 if dom_price is not None else None),
            "basis_z20": z20,
            "asof": latest["date"],
            "series": rows[-60:],
        },
        "source": "生意社·现货与基差",
        "grade": GRADE_B,
        "asof": latest["date"],
        "unit": "元/吨",
        "note": (
            f"现货为含税价（VAT {vat:.0%}），已同时给出除税参考基差；"
            "各品种现货口径不完全一致，绝对基差仅供趋势参考，请以交易所现货核验"
        ),
    }


# ---------------------------------------------------------------- 会员持仓
def _position_czce(code: str) -> dict | None:
    """郑商所官方持仓排名表：按品种取汇总行的前 20 会员多空持仓。

    该表同时含各合约明细与品种汇总行（symbol 不带月份），汇总行更稳健，
    不需要预判主力合约。
    """
    ak = _ak()
    variety = CZCE_VARIETY.get(code, code)
    for offset in range(0, 6):
        day = _recent_trading_day(offset)
        try:
            df = _ak_call("futures_rank_table_czce", date=day)
        except Exception:
            continue
        if df is None or not len(df):
            continue
        sub = df[df["variety"].astype(str) == variety]
        if not len(sub):
            continue
        # 优先用品种汇总行（symbol == variety），否则取成交量最大的合约
        summary = sub[sub["symbol"].astype(str) == variety]
        if len(summary) >= 20:
            picked = summary
        else:
            vol = sub.groupby(sub["symbol"].astype(str))["vol"].sum()
            if not len(vol):
                continue
            picked = sub[sub["symbol"].astype(str) == vol.idxmax()]
        picked = picked.sort_values("rank").head(20)
        longs = [
            {
                "rank": int(_num(r.get("rank")) or 0),
                "member": str(r.get("long_party_name") or ""),
                "qty": _num(r.get("long_open_interest")) or 0.0,
                "chg": _num(r.get("long_open_interest_chg")) or 0.0,
            }
            for _, r in picked.iterrows()
        ]
        shorts = [
            {
                "rank": int(_num(r.get("rank")) or 0),
                "member": str(r.get("short_party_name") or ""),
                "qty": _num(r.get("short_open_interest")) or 0.0,
                "chg": _num(r.get("short_open_interest_chg")) or 0.0,
            }
            for _, r in picked.iterrows()
        ]
        return {
            "long": longs,
            "short": shorts,
            "date": _iso(day),
            "contract": variety,
        }
    return None


def _position_block(code: str, exchange: str, main_contract: str | None) -> dict:
    ak = _ak()

    if exchange == "CZCE":
        czce_err = "郑商所持仓排名表暂不可用"

        def czce_producer():
            return _position_czce(code)

        try:
            data = _memo(f"pos|czce|{code}", 3600, czce_producer)
        except Exception as exc:
            data = None
            czce_err = _err_text(exc, 120)
        if not data:
            return {
                "ok": False,
                "error": czce_err,
                "source": "郑商所持仓排名表",
                "grade": GRADE_B,
            }
        return _pack_position(data, "郑商所持仓排名表", "品种汇总口径前 20 会员")

    if not main_contract:
        return {
            "ok": False,
            "error": "缺少主力合约代码，无法查询持仓排名",
            "source": "新浪财经·会员持仓",
            "grade": GRADE_B,
        }

    def producer():
        # 直连优先：新浪持仓排名页面用 urllib 取，pandas 解析表格
        contract = main_contract.upper()
        for offset in range(0, 6):
            day = _recent_trading_day(offset)
            iso = f"{day[0:4]}-{day[4:6]}-{day[6:8]}"
            try:
                data = ds.sina_position(contract, iso)
            except Exception:
                data = None
            if data and data.get("long"):
                data["date"] = iso
                data["contract"] = contract
                return data

        # 备选：akShare
        out: dict[str, list[dict]] = {}
        for kind, key in (("多单持仓", "long"), ("空单持仓", "short")):
            for offset in range(0, 6):
                day = _recent_trading_day(offset)
                try:
                    df = _ak_call("futures_hold_pos_sina",
                        symbol=kind, contract=contract, date=day
                    )
                except Exception:
                    continue
                if df is not None and len(df):
                    out[key] = [
                        {
                            "rank": int(_num(r.get("名次")) or 0),
                            "member": str(r.get("会员简称") or ""),
                            "qty": _num(r.get(kind)) or 0.0,
                            "chg": _num(r.get("比上交易增减")) or 0.0,
                        }
                        for _, r in df.head(20).iterrows()
                    ]
                    out["date"] = _iso(day)
                    out["contract"] = main_contract.upper()
                    break
        return out or None

    try:
        data = _memo(f"pos|sina|{main_contract.upper()}", 3600, producer, stale_ok=True)
    except Exception as exc:
        return {
            "ok": False,
            "error": _err_text(exc, 120),
            "source": "新浪财经·会员持仓",
            "grade": GRADE_B,
        }
    if not data or not data.get("long"):
        # 已知新浪持仓源不收录的品种（实测页面返回空模板，非合约代码问题）：
        # 原油(SC)、国际铜(BC)、集运指数(EC)。FU/LU/NR 等同为能源中心品种但有数据。
        uncovered = {"SC", "BC", "EC"}
        if code.upper() in uncovered:
            return {
                "ok": False,
                "error": f"新浪财经持仓排名未收录 {code.upper()}（上期能源中心该品种数据缺失）",
                "source": "新浪财经·会员持仓",
                "grade": GRADE_B,
                "manual_hint": "可人工查上期能源交易所 ine.cn → 每日持仓排名",
            }
        return {
            "ok": False,
            "error": "新浪财经未覆盖该合约的持仓排名（郑商所品种请走郑商所官方表）",
            "source": "新浪财经·会员持仓",
            "grade": GRADE_B,
        }
    return _pack_position(data, "新浪财经·会员持仓", "按主力合约统计前 20 会员")


def _pack_position(data: dict, source: str, note: str) -> dict:
    longs = data.get("long", [])
    shorts = data.get("short", [])
    l_sum = sum(r["qty"] for r in longs)
    s_sum = sum(r["qty"] for r in shorts)
    return {
        "ok": True,
        "value": {
            "contract": data.get("contract", ""),
            "asof": data.get("date"),
            "long_sum": l_sum,
            "short_sum": s_sum,
            "long_short_ratio": (l_sum / s_sum) if s_sum else None,
            "net": l_sum - s_sum,
            "top_long": longs[:5],
            "top_short": shorts[:5],
        },
        "source": source,
        "grade": GRADE_B,
        "asof": data.get("date"),
        "unit": "手",
        "note": note,
    }


# ---------------------------------------------------------------- 客观基本面信号
def auto_signal(blocks: dict) -> dict:
    """把采集到的客观数据折算成一个基本面倾向评分。

    纪律：只用可量化的读数（库存变化、基差偏离、仓单增减、会员多空），
    每条都必须能追溯到具体数据块；缺数据不参与打分，也不猜测方向。

    口径提醒：基差绝对值因品种含税口径不一致而不可横比，
    因此只用「基差率相对自身 20 日均值的偏离」这一口径无关的读数。
    """
    items: list[dict] = []
    score = 0

    inv = blocks.get("inventory")
    if inv and inv.get("ok"):
        v = inv["value"]
        series = [s["value"] for s in (v.get("series") or []) if s.get("value") is not None]
        latest = v.get("latest") or (series[-1] if series else None)
        # ① 库存分位：当前值在过去 60 个观测中的位置。比首尾百分比稳健，
        #    不会因个别极值把相对变化放大到 -200% 这种无意义读数。
        if series and latest is not None and len(series) >= 20:
            window = series[-60:] if len(series) >= 60 else series
            rank = sum(1 for x in window if x <= latest) / len(window) * 100
            if rank <= 25:
                score += 1
                items.append({
                    "label": "库存分位",
                    "points": 1,
                    "detail": f"库存 {latest:,.0f} 手处于近 {len(window)} 日 {rank:.0f}% 分位（偏低），现货偏紧",
                })
            elif rank >= 75:
                score -= 1
                items.append({
                    "label": "库存分位",
                    "points": -1,
                    "detail": f"库存 {latest:,.0f} 手处于近 {len(window)} 日 {rank:.0f}% 分位（偏高），供应宽松",
                })
        # ② 20 日变化方向：用「前值」做分母，并对相对幅度设上限
        if series and len(series) >= 21:
            prev = series[-21]
            chg = latest - prev
            if prev > 0 and chg != 0:
                pct = chg / prev * 100
                capped = max(-100.0, min(100.0, pct))
                if abs(capped) >= 3:
                    if chg < 0:
                        score += 1
                        pts = 1
                        add = f"20 日库存去化 {capped:.1f}%（{chg:+,.0f} 手），显性供应收缩"
                    else:
                        score -= 1
                        pts = -1
                        add = f"20 日库存累积 {capped:.1f}%（{chg:+,.0f} 手），供应压力上升"
                    items.append({"label": "库存趋势", "points": pts, "detail": add})

    basis = blocks.get("basis")
    if basis and basis.get("ok"):
        v = basis["value"]
        z = v.get("basis_z20")
        if z is not None and abs(z) >= 1.0:
            if z > 0:
                score += 1
                pts = 1
                add = f"基差率偏离 20 日均值 {z:+.2f}σ（现货相对走强）"
            else:
                score -= 1
                pts = -1
                add = f"基差率偏离 20 日均值 {z:+.2f}σ（现货相对走弱）"
            items.append({"label": "基差结构", "points": pts, "detail": add})

        dom_basis = v.get("dom_basis")
        spot = v.get("spot")
        if dom_basis is not None and spot:
            rate = dom_basis / spot * 100
            if abs(rate) >= 4.0:
                if dom_basis > 0:
                    score += 1
                    add = f"除税前现货升水期货 {rate:.1f}%，现货偏强"
                    pts = 1
                else:
                    score -= 1
                    add = f"期货升水现货 {-rate:.1f}%，期货端偏贵"
                    pts = -1
                items.append({"label": "现货升贴水", "points": pts, "detail": add})

    rc = blocks.get("receipt")
    if rc and rc.get("ok"):
        v = rc["value"]
        delta = v.get("delta") or 0
        total = v.get("total") or 0
        if delta and total and abs(delta) / total >= 0.02:
            if delta < 0:
                score += 1
                pts = 1
                add = f"仓单减少 {abs(delta):,.0f} 手（-{abs(delta)/total*100:.1f}%），显性供应收缩"
            else:
                score -= 1
                pts = -1
                add = f"仓单增加 {delta:,.0f} 手（+{delta/total*100:.1f}%），交割资源累积"
            items.append({"label": "交割仓单", "points": pts, "detail": add})

    ps = blocks.get("position")
    if ps and ps.get("ok"):
        v = ps["value"]
        r = v.get("long_short_ratio")
        if r is not None and abs(r - 1) >= 0.04:
            if r > 1:
                score += 1
                pts = 1
                add = f"前 20 会员多空比 {r:.3f}，多头持仓略占优"
            else:
                score -= 1
                pts = -1
                add = f"前 20 会员多空比 {r:.3f}，空头持仓略占优"
            items.append({"label": "持仓结构", "points": pts, "detail": add})

    if score >= 3:
        label = "偏多"
    elif score >= 1:
        label = "弱偏多"
    elif score <= -3:
        label = "偏空"
    elif score <= -1:
        label = "弱偏空"
    else:
        label = "中性"

    return {
        "score": score,
        "label": label,
        "items": items,
        "note": "仅由客观读数折算，不含主观判断；数据缺失项不参与计分",
    }


# ---------------------------------------------------------------- 汇总入口
def collect(meta: dict, main_contract: str | None = None) -> dict:
    """采集单品种的全部可用基本面数据。

    :param meta: symbols.find_symbol() 返回的品种元数据
    :param main_contract: 主力合约代码（如 RB2701），用于持仓排名

    四个数据块并发采集：串行时总耗时是各块之和（云端可达 2 分钟以上，
    会被反向代理的 120 秒超时掐成 504）；并发后是其中的最大值。
    再加一层整体预算，超时就把没跑完的块标成「采集中」先返回，
    不要让整个请求挂死 —— 已完成的块照常展示。
    """
    code = meta.get("code", "")
    exchange = meta.get("exchange", "")
    gaps: list[str] = []

    jobs = {
        "receipt": lambda: _receipt_block(code, exchange),
        "inventory": lambda: _inventory_block(code, meta.get("name", "")),
        "basis": lambda: _basis_block(code, meta.get("name", "")),
        "position": lambda: _position_block(code, exchange, main_contract),
    }

    blocks: dict[str, dict] = {}
    executor = ThreadPoolExecutor(max_workers=4)
    try:
        futures = {key: executor.submit(fn) for key, fn in jobs.items()}
        # 注意：wait() 内部会 set(fs)，传 dict 会迭代出 key（字符串）而不是 future
        done, _pending = wait(list(futures.values()), timeout=_COLLECT_TIMEOUT)
        for key, fut in futures.items():
            if fut in done:
                try:
                    blocks[key] = fut.result()
                except Exception as exc:
                    blocks[key] = {
                        "ok": False,
                        "error": _err_text(exc, 140),
                        "source": "—",
                        "grade": GRADE_NA,
                    }
            else:
                blocks[key] = {
                    "ok": False,
                    "error": "采集中（数据源响应较慢，稍后刷新重试）",
                    "source": "—",
                    "grade": GRADE_NA,
                    "pending": True,
                }
    finally:
        # 不等待卡住的线程，让 HTTP 响应先返回
        executor.shutdown(wait=False, cancel_futures=True)

    for key, block in blocks.items():
        if not block.get("ok"):
            gaps.append(f"{key}: {block.get('error', '不可用')}")

    return {
        "symbol": code,
        "name": meta.get("name", ""),
        "exchange": exchange,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "main_contract": main_contract,
        "blocks": blocks,
        "auto_signal": auto_signal(blocks),
        "gaps": gaps,
    }


# ---------------------------------------------------------------- 环境体检
_NET_PROBES = {
    "新浪·实时行情": "https://hq.sinajs.cn/list=nf_I0",
    "新浪·会员持仓": "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_p=/InnerFuturesNewService.getFewDayPosition?symbol=I2701&type=0&day=20261125",
    "新浪·历史行情": "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_t=/InnerFuturesNewService.getDailyKLine?symbol=I2701",
    "东方财富·库存": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_FUTURES_STOCK&columns=ALL&pageSize=1",
    "生意社·现货": "https://www.100ppi.com/sf/",
    # 生意社日页才是真正解析数据的页面。首页 HTTP 200 不代表日页能拿到表格
    # （被限流时返回的是几百字节的 JS 挑战页，也是 HTTP 200），所以要单独探。
    "生意社·日页": "https://www.100ppi.com/sf/day-{today}.html",
    "大商所官网": "http://www.dce.com.cn/",
    "上期所官网": "https://www.shfe.com.cn/",
    "郑商所官网": "http://www.czce.com.cn/",
}

_API_PROBES = (
    "futures_spot_price_daily",
    "futures_hold_pos_sina",
    "futures_inventory_em",
    "futures_receipt",
    "futures_warehouse_receipt_dce",
    "futures_warehouse_receipt_shfe",
    "futures_warehouse_receipt_czce",
    "futures_rank_table_dce",
    "futures_rank_table_czce",
)


def _ak_live_probe() -> dict:
    """用 akshare 实打实地调一次关键接口，返回耗时与错误类型。

    只看签名和裸 HTTP 连通性不够：akshare 走 requests、还带自己的
    headers/编码逻辑，裸 urllib 通不代表它能通。这一项才是真实证据。
    """
    out: dict = {}

    def run(label, fn):
        t0 = time.time()
        try:
            df = fn()
            out[label] = {
                "ok": True,
                "秒": round(time.time() - t0, 1),
                "行数": len(df) if df is not None else 0,
                "列": list(df.columns)[:8] if hasattr(df, "columns") else [],
            }
        except Exception as exc:
            out[label] = {
                "ok": False,
                "秒": round(time.time() - t0, 1),
                "错误": type(exc).__name__,
                "信息": str(exc)[:160],
            }

    ak = _ak()
    if ak is None:
        return {"错误": "akshare 未安装"}
    d = datetime.now().strftime("%Y%m%d")
    prev = (datetime.now() - timedelta(days=3)).strftime("%Y%m%d")
    run("现货基差", lambda: _ak_call("futures_spot_price_daily", start_date=prev, end_date=d, vars_list=["I"]))
    run("会员持仓", lambda: _ak_call("futures_hold_pos_sina", symbol="多单持仓", contract="I2701", date=prev))
    run("期货库存", lambda: _ak_call("futures_inventory_em", symbol="I"))
    run("通用仓单", lambda: _ak_call("futures_receipt", start_date=prev, end_date=d))
    return out


def env_report(deep: bool = False) -> dict:
    """环境与数据源连通性体检。

    线上（云端）出口网络与本机往往不同：同一个接口本机通、线上被拦的情况非常常见。
    这个接口把「当前环境到底连得到哪些源、akshare 是哪个版本、接口签名长什么样」
    一次性摊开，省掉每次都靠猜的排查成本。
    """
    import urllib.request

    out: dict = {
        "python": sys.version.split()[0],
        "akshare": None,
        "apis": {},
        "net": {},
        "cache": {},
    }

    ak = _ak()
    if ak is not None:
        out["akshare"] = getattr(ak, "__version__", "?")
        for nm in _API_PROBES:
            fn = getattr(ak, nm, None)
            if fn is None:
                out["apis"][nm] = "缺失"
                continue
            if not callable(fn):
                out["apis"][nm] = f"列表形态(len={len(fn)})"
                continue
            try:
                out["apis"][nm] = str(inspect.signature(fn))
            except (TypeError, ValueError):
                out["apis"][nm] = "签名未知"

    for name, url in _NET_PROBES.items():
        probe_url = url.format(today=datetime.now().strftime("%Y-%m-%d"))
        try:
            req = urllib.request.Request(
                probe_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Referer": "https://finance.sina.com.cn/",
                    # 生意社对 Accept: */* 返回 JS 反爬挑战页，必须声明要 HTML
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                status = f"HTTP {resp.status}"
            # 生意社日页：能解析出表格才算真正可用
            if "100ppi" in probe_url and "/sf/day-" in probe_url:
                try:
                    import pandas as pd

                    n = len(pd.read_html(io.StringIO(raw.decode("utf-8", "replace"))))
                    status += f" · 表格{n} · {len(raw)}字节"
                except Exception:
                    status += f" · 无表格 · {len(raw)}字节"
            out["net"][name] = status
        except Exception as exc:
            out["net"][name] = type(exc).__name__

    try:
        disk = _load_disk_cache()
        out["cache"] = {
            "条目数": len(disk),
            "失败条目数": sum(1 for v in disk.values() if v.get("f")),
        }
    except Exception:
        out["cache"] = {}

    if deep or os.environ.get("ENV_DEEP"):
        # 深挖模式：真的调一次 akshare，看它到底能不能出去（耗时较长）
        out["akshare实测"] = _ak_live_probe()
    # 直连探针独立于 deep：它快（几秒），且直连才是生产路径的实际来源
    out["直连实测"] = _direct_live_probe()

    return out


def _direct_live_probe() -> dict:
    """直连数据源逐步诊断：把「取页 → 解码 → 解析表格 → 取行」每一步的结果摊开。

    排查云端基差空表时用：HTTP 200 不代表能解析出数据，
    可能是编码、pandas 解析器或行匹配任一环出了问题。
    """
    out: dict = {}
    code = "I"
    day = ds.recent_trading_days(1)[0]

    # 1) 原始请求
    try:
        text = ds._get(ds._SPOT_URL.format(date=day), timeout=10)
    except Exception as exc:
        text = None
        out["取页异常"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    out["取页"] = f"{'OK' if text else 'FAIL'} · {len(text) if text else 0}字符"

    if text:
        # 2) 表格解析
        try:
            tables = ds._read_html_tables(text)
        except Exception as exc:
            tables = []
            out["解析异常"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        out["表格数"] = len(tables)

        # 3) 行匹配
        try:
            page = ds.shangshe_spot_day_all(day)
        except Exception as exc:
            page = {}
            out["行解析异常"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        out["命中品种数"] = len(page)
        out["目标品种"] = f"{code} -> {'命中' if code in page else '未命中'}"

    # 4) 其它两个源
    try:
        inv = ds.eastmoney_inventory(sn.eastmoney_name(code) or code)
        out["东财库存"] = f"{'OK' if inv else 'FAIL'} · {len(inv['date']) if inv else 0}条"
    except Exception as exc:
        out["东财库存"] = f"异常 {type(exc).__name__}: {str(exc)[:100]}"
    try:
        pos = ds.sina_position("I2701", "2026-11-25")
        out["新浪持仓"] = f"{'OK' if pos else 'FAIL'}"
    except Exception as exc:
        out["新浪持仓"] = f"异常 {type(exc).__name__}: {str(exc)[:100]}"

    # 5) 生产路径实测：基差块实际会串行抓 N 天，直接测它能不能出数
    try:
        t0 = time.time()
        table = _basis_table_direct(code, "铁矿石", days=22)
        rows = table.get(code) or []
        out["基差块"] = (
            f"{'OK' if rows else 'EMPTY'} · {len(rows)}行 · "
            f"{time.time() - t0:.1f}秒 · 样本{len(rows[-20:])}"
        )
    except Exception as exc:
        out["基差块"] = f"异常 {type(exc).__name__}: {str(exc)[:120]}"

    # 6) 仓单备选路径实测
    try:
        ak = _ak()
        avail = {}
        for nm in ("get_shfe_receipt_3", "futures_receipt", "futures_warehouse_receipt_shfe"):
            fn = getattr(ak, nm, None) if ak else None
            if fn is None:
                fn = _find_ak_function(ak, nm) if ak else None
            avail[nm] = "有" if fn is not None else "无"
        out["仓单接口可用性"] = avail
    except Exception as exc:
        out["仓单接口可用性"] = f"异常 {type(exc).__name__}"
    try:
        t0 = time.time()
        rc = _receipt_via_akshare(code)
        out["仓单备选"] = (
            f"{'OK ' + str(rc['total']) if rc else 'EMPTY'} · {time.time() - t0:.1f}秒"
        )
    except Exception as exc:
        out["仓单备选"] = f"异常 {type(exc).__name__}: {str(exc)[:120]}"
    try:
        t0 = time.time()
        df = _ak_call(
            "futures_receipt",
            start_date=_recent_trading_day(0),
            end_date=_recent_trading_day(0),
            symbols=code,
        )
        out["akShare仓单原始"] = (
            f"{'OK ' + str(len(df)) + '行' if df is not None else 'None'} · "
            f"{time.time() - t0:.1f}秒"
        )
    except Exception as exc:
        out["akShare仓单原始"] = f"异常 {type(exc).__name__}: {str(exc)[:120]}"

    # 7) 上期所仓单直连实测（云端可能被限流）
    try:
        t0 = time.time()
        page = ds.shfe_receipt_day("2026-11-26")
        out["上期所直连"] = (
            f"{'OK ' + str(len(page)) + '品种' if page else 'EMPTY'} · "
            f"{time.time() - t0:.1f}秒"
        )
    except Exception as exc:
        out["上期所直连"] = f"异常 {type(exc).__name__}: {str(exc)[:120]}"

    # 8) pandas / 解析器环境
    try:
        import pandas as pd

        out["pandas"] = pd.__version__
    except Exception:
        out["pandas"] = "缺失"
    for mod in ("lxml", "bs4", "html5lib"):
        try:
            __import__(mod)
            out[mod] = "已安装"
        except Exception:
            out[mod] = "缺失"

    return out
