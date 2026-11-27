"""实时行情快照（内盘）与 K 线合并。

数据源：新浪财经实时行情接口
    GET https://hq.sinajs.cn/list=nf_<品种代码>0
响应为 GBK 编码的 JS 赋值语句，例如：
    var hq_str_nf_RB0="螺纹钢连续,092655,3065.000,...";

内盘连续合约字段布局（44 段，已逐字段核对）：
    0  名称            1  时间 HHMMSS        2  开盘价      3  最高价
    4  最低价          5  昨收盘价          6  买价        7  卖价
    8  最新价          9  结算价(盘中为 0)  10 昨结算价   11 买量
    12 卖量            13 持仓量            14 成交量      15 交易所
    16 品种名          17 日期              18-26 保留     27 均价
    28-35 买1-4档(价,量)                   36-43 卖1-4档(价,量)

涨跌幅按国内期货惯例以「昨结算价」为基准，而非昨收盘价。
外盘品种暂不接入本模块（见 server.py 的 /api/stream）。

实时快照记录的是「整个交易日」的累计状态，而非自然日切片。已实测：
    11-26 09:33 快照 O=3065 H=3071 L=3051 V=330331
    其中 O/H/L 与 11-25 21:05 夜盘首根 K 线一致（O=3065 H=3071 L=3051），
    而 11-26 09:00 日盘首根为 O=3054。
    即夜盘 21:00 开盘价属于「下一交易日」，成交量为夜盘+日盘累计。

新浪日线接口在盘中不发布当日 K 线（09:30 时最后一根仍是上一交易日），
因此日线视图必须用本模块的快照合成「当日进行中 K 线」。
"""

from __future__ import annotations

import datetime as dt
import threading
import urllib.request

SPOT_URL = "https://hq.sinajs.cn/list={code}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
REFERER = "https://finance.sina.com.cn/"

# 日盘时段（北京时间，分钟数）：09:00-10:15, 10:30-11:30, 13:30-15:00
DAY_SESSIONS = ((9 * 60, 10 * 60 + 15), (10 * 60 + 30, 11 * 60 + 30), (13 * 60 + 30, 15 * 60))
# 夜盘起点 21:00，部分品种延续至次日 02:30
NIGHT_START = 21 * 60
NIGHT_END = 2 * 60 + 30

SPOT_FIELDS = (
    "name time open high low prev_close bid ask last settlement prev_settlement "
    "bid_volume ask_volume open_interest volume exchange variety date"
).split()


def _to_float(seg: str | None) -> float | None:
    try:
        v = float(seg)
    except (TypeError, ValueError):
        return None
    return v


def _to_int(seg: str | None) -> int | None:
    try:
        return int(float(seg))
    except (TypeError, ValueError):
        return None


def _is_weekday(d: dt.date) -> bool:
    return d.weekday() < 5


_calendar_cache: dict | None = None
_calendar_lock = threading.Lock()


def trade_calendar() -> list[str] | None:
    """交易日历（ISO 日期字符串升序）。取自新浪历史交易日历，可用则精确到节假日。"""
    global _calendar_cache
    with _calendar_lock:
        if _calendar_cache is not None:
            return _calendar_cache
        try:
            import akshare as ak

            df = ak.calendar_trade_date_hist_sina()
            col = df.columns[0]
            days = sorted({str(v)[:10] for v in df[col]})
            _calendar_cache = days
        except Exception:
            _calendar_cache = None
        return _calendar_cache


def next_trade_day(d: dt.date) -> dt.date:
    """d 之后的下一个交易日。

    优先用真实交易日历（能正确跳过春节、国庆等法定假期）；
    日历覆盖范围之外（实测截至 2026-12-31）退化为「跳过周末」，
    并在跨年边界保留日历之外的日期，避免误判为长假。
    """
    cal = trade_calendar()
    if cal:
        last = dt.date.fromisoformat(cal[-1])
        if d < last:
            for i in range(1, 40):
                cand = d + dt.timedelta(days=i)
                if cand.isoformat() in cal:
                    return cand
            return d + dt.timedelta(days=1)
        # 已超出日历覆盖范围（年末/次年初），退回工作日推断
    nxt = d + dt.timedelta(days=1)
    while not _is_weekday(nxt):
        nxt += dt.timedelta(days=1)
    return nxt


def parse_spot(payload: str) -> dict | None:
    """解析新浪实时行情的一行 payload。返回 None 表示无有效数据。"""
    parts = payload.split(",")
    if len(parts) < 18:
        return None
    time_raw = parts[1].strip()
    if len(time_raw) < 4:
        return None
    try:
        hh = int(time_raw[0:2])
        mm = int(time_raw[2:4])
    except ValueError:
        return None

    last = _to_float(parts[8])
    if last is None or last <= 0:
        return None

    date_raw = parts[17].strip()
    try:
        day = dt.date.fromisoformat(date_raw)
    except ValueError:
        day = dt.date.today()

    prev_settlement = _to_float(parts[10])
    prev_close = _to_float(parts[5])
    # 盘中结算价字段为 0，昨结算价才是有效的涨跌基准
    reference = prev_settlement or prev_close
    change = (last - reference) if reference else None
    change_pct = (change / reference * 100) if (change is not None and reference) else None

    bids = []
    asks = []
    for i in range(28, 36, 2):
        price = _to_float(parts[i]) if i < len(parts) else None
        volume = _to_int(parts[i + 1]) if i + 1 < len(parts) else None
        if price:
            bids.append({"price": price, "volume": volume})
    for i in range(36, 44, 2):
        price = _to_float(parts[i]) if i < len(parts) else None
        volume = _to_int(parts[i + 1]) if i + 1 < len(parts) else None
        if price:
            asks.append({"price": price, "volume": volume})

    return {
        "ok": True,
        "name": parts[0].strip(),
        "variety": parts[16].strip() if len(parts) > 16 else "",
        "exchange": parts[15].strip() if len(parts) > 15 else "",
        "datetime": f"{day.isoformat()} {time_raw[0:2]}:{time_raw[2:4]}:{time_raw[4:6] if len(time_raw) >= 6 else '00'}",
        "date": day.isoformat(),
        "time": f"{time_raw[0:2]}:{time_raw[2:4]}:{time_raw[4:6] if len(time_raw) >= 6 else '00'}",
        "minute_of_day": hh * 60 + mm,
        "open": _to_float(parts[2]),
        "high": _to_float(parts[3]),
        "low": _to_float(parts[4]),
        "last": last,
        "bid": _to_float(parts[6]),
        "ask": _to_float(parts[7]),
        "avg_price": _to_float(parts[27]) if len(parts) > 27 else None,
        "reference": reference,
        "reference_kind": "昨结算价" if prev_settlement else "昨收盘价",
        "change": round(change, 4) if change is not None else None,
        "change_pct": round(change_pct, 3) if change_pct is not None else None,
        "volume": _to_int(parts[14]),
        "open_interest": _to_int(parts[13]),
        "bids": bids,
        "asks": asks,
        "source": "新浪财经 hq.sinajs.cn 实时行情",
    }


def fetch_spot(code: str, timeout: int = 8) -> dict:
    """拉取单个内盘品种的实时快照。code 为品种字母，如 RB。"""
    symbol = f"nf_{code.upper()}0"
    req = urllib.request.Request(
        SPOT_URL.format(code=symbol),
        headers={
            "User-Agent": USER_AGENT,
            "Referer": REFERER,
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    text = raw.decode("gbk", errors="replace")
    payload = ""
    if '"' in text:
        payload = text.split('"')[1]
    if not payload.strip():
        return {"ok": False, "error": "新浪实时接口无该品种数据（可能非交易时段或代码不支持）"}
    spot = parse_spot(payload)
    if spot is None:
        return {"ok": False, "error": "新浪实时接口返回格式异常"}
    spot["symbol"] = symbol
    return spot


def session_state(minute_of_day: int | None = None) -> dict:
    """判断当前处于哪个交易时段（内盘口径）。

    日盘 09:00-10:15 / 10:30-11:30 / 13:30-15:00，夜盘 21:00-次日 02:30。
    """
    if minute_of_day is None:
        now = dt.datetime.now()
        minute_of_day = now.hour * 60 + now.minute
    for start, end in DAY_SESSIONS:
        if start <= minute_of_day < end:
            return {"state": "day", "label": "日盘", "trading": True}
    if minute_of_day >= NIGHT_START or minute_of_day <= NIGHT_END:
        return {"state": "night", "label": "夜盘", "trading": True}
    # 午休（10:15-13:30 之间且不在日盘时段）
    if 10 * 60 + 15 <= minute_of_day < 13 * 60 + 30:
        return {"state": "break", "label": "午间休市", "trading": False}
    if 15 * 60 <= minute_of_day < 21 * 60:
        return {"state": "closed", "label": "日盘休市", "trading": False}
    return {"state": "pre", "label": "开盘前", "trading": False}


def _spot_trade_date(spot: dict) -> dt.date | None:
    """由快照推导其所属交易日（夜盘归属下一交易日）。

    快照的 date 字段是自然日。夜盘 21:00 起属于下一交易日；
    凌晨（<=02:30）仍属于该夜盘对应的那个交易日。
    """
    try:
        natural = dt.date.fromisoformat(spot["date"])
    except (KeyError, ValueError):
        return None
    minute = spot.get("minute_of_day")
    if minute is None:
        return natural
    if minute >= NIGHT_START:
        return next_trade_day(natural)
    if minute <= NIGHT_END:
        # 跨零点的夜盘延续段，自然日已是交易日的次日时段
        return natural
    return natural


def merge_daily(rows: list[dict], spot: dict | None) -> tuple[list[dict], str]:
    """把实时快照合并进日线序列。

    日线接口在盘中滞后一个交易日，这里补出「当日进行中 K 线」：
    - 快照所属交易日 == 序列最后一日  → 替换最后一根（收盘后接口已更新）
    - 快照所属交易日 == 下一个交易日  → 追加一根新的进行中 K 线
    - 其余情况（跨节假日等）→ 不改动序列，避免出现重复或错位日期

    实测校验（2026-11-26 09:33）：
      快照 O=3065 H=3071 L=3051 与该交易日 5 分钟聚合结果完全一致（差 0.0），
      证明快照 OHLC 覆盖「夜盘 + 日盘」整个交易日，可直接作为日线 OHLC。
      但成交量口径不同（分钟接口逐根为增量、日线为全天总量），
      故进行中 K 线的成交量沿用接口原值，避免与历史序列口径冲突。

    返回 (新序列, 说明)。
    """
    if not spot or not spot.get("ok"):
        return rows, "实时快照不可用，日线维持接口数据"
    if not rows:
        return rows, "日线序列为空"

    trade_date = _spot_trade_date(spot)
    if trade_date is None:
        return rows, "无法确定快照所属交易日"

    last_date = None
    try:
        last_date = dt.date.fromisoformat(str(rows[-1]["datetime"])[:10])
    except (KeyError, ValueError):
        pass
    if last_date is None:
        return rows, "日线末根日期无法解析"

    live_bar = {
        "datetime": trade_date.isoformat(),
        "open": spot.get("open"),
        "high": spot.get("high"),
        "low": spot.get("low"),
        "close": spot.get("last"),
        "volume": None,           # 口径不同，保留接口原值
        "open_interest": spot.get("open_interest"),
        "settlement_price": None,
        "live": True,
    }

    if trade_date == last_date:
        merged = list(rows)
        merged[-1] = {**merged[-1], **{k: v for k, v in live_bar.items() if v is not None}}
        return merged, f"已用实时行情刷新 {trade_date} 的日线（接口数据已含当日）"

    expected = next_trade_day(last_date)
    if trade_date == expected:
        merged = list(rows) + [live_bar]
        return merged, f"已追加 {trade_date} 当日进行中 K 线（收盘价 {live_bar['close']}）"

    return rows, f"快照交易日 {trade_date} 与序列不连续（跨越节假日），未合并"


def bar_to_payload(bar: dict) -> dict:
    return {
        "datetime": bar.get("datetime"),
        "open": bar.get("open"),
        "high": bar.get("high"),
        "low": bar.get("low"),
        "close": bar.get("close"),
        "volume": bar.get("volume"),
        "open_interest": bar.get("open_interest"),
        "live": bool(bar.get("live")),
    }
