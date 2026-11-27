"""技术指标与信号引擎（纯标准库实现）。

输入：K 线序列（dict 列表）
输出：指标序列 + 信号评分 + 关键价位 + 分位统计

设计原则：
- 只描述"盘面行为"，不解释供需原因（对应 skill 中"行情不能证明供需逻辑"的约束）
- 每个信号都带可读说明，便于在界面上直接对照
"""

from __future__ import annotations

import math
from typing import Sequence


def _ma(values: Sequence[float], n: int) -> list[float | None]:
    out: list[float | None] = []
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        out.append(total / n if i >= n - 1 else None)
    return out


def _ema(values: Sequence[float], n: int) -> list[float | None]:
    out: list[float | None] = []
    k = 2.0 / (n + 1.0)
    prev: float | None = None
    for v in values:
        if prev is None:
            prev = v
            out.append(v)
        else:
            prev = v * k + prev * (1 - k)
            out.append(prev)
    return out


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    dif = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(ema_fast, ema_slow)
    ]
    valid = [d for d in dif if d is not None]
    dea_valid = _ema(valid, signal)
    dea: list[float | None] = [None] * (len(dif) - len(dea_valid)) + list(dea_valid)
    hist = [
        ((d - s) * 2) if (d is not None and s is not None) else None
        for d, s in zip(dif, dea)
    ]
    return dif, dea, hist


def kdj(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
         n: int = 9, k_period: int = 3, d_period: int = 3):
    rsv: list[float | None] = []
    k_list: list[float | None] = []
    d_list: list[float | None] = []
    j_list: list[float | None] = []
    prev_k = 50.0
    prev_d = 50.0
    for i in range(len(closes)):
        if i < n - 1:
            rsv.append(None)
            k_list.append(None)
            d_list.append(None)
            j_list.append(None)
            continue
        hh = max(highs[i - n + 1: i + 1])
        ll = min(lows[i - n + 1: i + 1])
        r = 50.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100.0
        prev_k = (prev_k * (k_period - 1) + r) / k_period
        prev_d = (prev_d * (d_period - 1) + prev_k) / d_period
        rsv.append(r)
        k_list.append(prev_k)
        d_list.append(prev_d)
        j_list.append(3 * prev_k - 2 * prev_d)
    return k_list, d_list, j_list, rsv


def rsi(closes: Sequence[float], n: int = 14) -> list[float | None]:
    """Wilder RSI。返回长度与 closes 一致，样本不足处为 None。"""
    out: list[float | None] = [None]
    if len(closes) < 2:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        if i < n:
            gains += gain
            losses += loss
            out.append(None)
        elif i == n:
            gains = (gains + gain) / n
            losses = (losses + loss) / n
            out.append(100.0 if losses == 0 else 100.0 - 100.0 / (1.0 + gains / losses))
        else:
            gains = (gains * (n - 1) + gain) / n
            losses = (losses * (n - 1) + loss) / n
            out.append(100.0 if losses == 0 else 100.0 - 100.0 / (1.0 + gains / losses))
    return out


def boll(closes: Sequence[float], n: int = 20, k: float = 2.0):
    mid = _ma(closes, n)
    upper: list[float | None] = []
    lower: list[float | None] = []
    for i in range(len(closes)):
        if mid[i] is None:
            upper.append(None)
            lower.append(None)
            continue
        window = closes[i - n + 1: i + 1]
        m = mid[i]
        var = sum((x - m) ** 2 for x in window) / n
        sd = math.sqrt(var)
        upper.append(m + k * sd)
        lower.append(m - k * sd)
    return upper, mid, lower


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        n: int = 14) -> list[float | None]:
    trs: list[float] = []
    for i in range(len(closes)):
        if i == 0:
            trs.append(highs[i] - lows[i])
        else:
            trs.append(max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            ))
    out: list[float | None] = []
    prev: float | None = None
    for i, tr in enumerate(trs):
        if i < n:
            out.append(sum(trs[: i + 1]) / (i + 1))
        elif i == n:
            prev = sum(trs[:n]) / n
            out.append(prev)
        else:
            prev = (prev * (n - 1) + tr) / n
            out.append(prev)
    return out


def percentile_rank(values: Sequence[float], value: float) -> float | None:
    if not values:
        return None
    below = sum(1 for v in values if v < value)
    equal = sum(1 for v in values if v == value)
    return round((below + 0.5 * equal) / len(values) * 100, 1)


def compute_indicators(bars: list[dict]) -> dict:
    """计算全部技术指标序列。"""
    closes = [float(b["close"]) for b in bars]
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    opens = [float(b["open"]) for b in bars]
    volumes = [float(b.get("volume") or 0) for b in bars]
    ois = [float(b.get("open_interest") or 0) for b in bars]

    ma5 = _ma(closes, 5)
    ma10 = _ma(closes, 10)
    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60)
    dif, dea, hist = macd(closes)
    k, d, j, _ = kdj(highs, lows, closes)
    rsi14 = rsi(closes)
    bu, bm, bl = boll(closes)
    atr14 = atr(highs, lows, closes)

    vol_ma5 = _ma(volumes, 5)
    oi_delta = [
        (ois[i] - ois[i - 1]) if (i > 0 and ois[i] and ois[i - 1]) else None
        for i in range(len(ois))
    ]

    return {
        "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
        "dif": dif, "dea": dea, "hist": hist,
        "kdj_k": k, "kdj_d": d, "kdj_j": j,
        "rsi14": rsi14,
        "boll_upper": bu, "boll_mid": bm, "boll_lower": bl,
        "atr14": atr14,
        "vol_ma5": vol_ma5,
        "oi_delta": oi_delta,
    }


def _last(seq, idx: int):
    if idx < 0 or idx >= len(seq):
        return None
    return seq[idx]


def build_signals(bars: list[dict], ind: dict) -> dict:
    """把指标转成可读信号列表 + 综合评分。"""
    n = len(bars)
    if n == 0:
        return {"score": 0, "label": "无数据", "items": [], "levels": {}, "stats": {}}

    closes = [float(b["close"]) for b in bars]
    vols = [float(b.get("volume") or 0) for b in bars]
    ois = [float(b.get("open_interest") or 0) for b in bars]

    i = n - 1
    close = float(bars[i]["close"])
    items: list[dict] = []
    score = 0

    def add(label: str, points: int, detail: str, kind: str = "趋势"):
        nonlocal score
        score += points
        items.append({
            "label": label,
            "points": points,
            "detail": detail,
            "kind": kind,
        })

    # 1. 均线结构
    ma5, ma10, ma20, ma60 = ind["ma5"], ind["ma10"], ind["ma20"], ind["ma60"]
    if all(v is not None for v in (ma5[i], ma10[i], ma20[i])):
        m5, m10, m20 = ma5[i], ma10[i], ma20[i]
        if m5 > m10 > m20:
            add("均线多头排列", 2, f"MA5 {m5:.1f} > MA10 {m10:.1f} > MA20 {m20:.1f}")
        elif m5 < m10 < m20:
            add("均线空头排列", -2, f"MA5 {m5:.1f} < MA10 {m10:.1f} < MA20 {m20:.1f}")
        else:
            add("均线缠绕", 0, f"MA5 {m5:.1f} / MA10 {m10:.1f} / MA20 {m20:.1f}", "震荡")
        if close > m20:
            add("价格站上 MA20", 1, f"收盘 {close:.1f} > MA20 {m20:.1f}")
        else:
            add("价格跌破 MA20", -1, f"收盘 {close:.1f} < MA20 {m20:.1f}")
    if ma60[i] is not None:
        if close > ma60[i]:
            add("价格站上 MA60", 1, f"收盘 {close:.1f} > MA60 {ma60[i]:.1f}", "趋势")
        else:
            add("价格跌破 MA60", -1, f"收盘 {close:.1f} < MA60 {ma60[i]:.1f}", "趋势")

    # 2. MACD
    dif, dea, hist = ind["dif"], ind["dea"], ind["hist"]
    if dif[i] is not None and dea[i] is not None:
        if dif[i] > dea[i]:
            add("MACD  DIF 上穿 DEA", 1, f"DIF {dif[i]:.2f} > DEA {dea[i]:.2f}", "动量")
        else:
            add("MACD  DIF 下穿 DEA", -1, f"DIF {dif[i]:.2f} < DEA {dea[i]:.2f}", "动量")
        if i > 0 and hist[i] is not None and hist[i - 1] is not None:
            if hist[i] > 0 and hist[i - 1] <= 0:
                add("MACD 红柱翻多", 1, "动能由弱转强", "动量")
            elif hist[i] < 0 and hist[i - 1] >= 0:
                add("MACD 绿柱翻空", -1, "动能由强转弱", "动量")
        if dif[i] < 0 and dea[i] < 0:
            add("MACD 零轴下方运行", -1, "中期动能仍弱", "动量")

    # 3. KDJ
    kk, dd, jj = ind["kdj_k"], ind["kdj_d"], ind["kdj_j"]
    if kk[i] is not None:
        if kk[i] > 80 and dd[i] > 80:
            add("KDJ 高位钝化", -1, f"K {kk[i]:.1f} / D {dd[i]:.1f}，追高风险", "震荡")
        elif kk[i] < 20 and dd[i] < 20:
            add("KDJ 低位钝化", 1, f"K {kk[i]:.1f} / D {dd[i]:.1f}，超卖区", "震荡")
        elif i > 0 and kk[i - 1] is not None and dd[i - 1] is not None:
            if kk[i - 1] <= dd[i - 1] and kk[i] > dd[i]:
                add("KDJ 金叉", 1, "短线动能转强", "动量")
            elif kk[i - 1] >= dd[i - 1] and kk[i] < dd[i]:
                add("KDJ 死叉", -1, "短线动能转弱", "动量")

    # 4. RSI
    r14 = ind["rsi14"]
    if r14[i] is not None:
        if r14[i] >= 70:
            add("RSI 超买", -1, f"RSI14 = {r14[i]:.1f}，回撤风险上升", "震荡")
        elif r14[i] <= 30:
            add("RSI 超卖", 1, f"RSI14 = {r14[i]:.1f}，存在修复空间", "震荡")
        else:
            add("RSI 中性", 0, f"RSI14 = {r14[i]:.1f}", "震荡")

    # 5. BOLL 位置
    bu, bm, bl = ind["boll_upper"], ind["boll_mid"], ind["boll_lower"]
    if bu[i] is not None:
        width = (bu[i] - bl[i]) / bm[i] * 100 if bm[i] else 0.0
        if close >= bu[i]:
            add("触及布林上轨", -1, "短期超买，追涨性价比下降", "震荡")
        elif close <= bl[i]:
            add("触及布林下轨", 1, "短期超卖", "震荡")
        else:
            pos = (close - bl[i]) / (bu[i] - bl[i]) * 100 if bu[i] != bl[i] else 50.0
            add("布林带内运行", 0, f"位于带内 {pos:.0f}% 位置，带宽 {width:.1f}%", "震荡")

    # 6. 量价配合
    vma5 = ind["vol_ma5"]
    if i >= 1 and vols[i] and vols[i - 1]:
        price_up = closes[i] > closes[i - 1]
        vol_up = vols[i] > vols[i - 1]
        if price_up and vol_up:
            add("价涨量增", 1, "资金推动向上，配合良好", "资金")
        elif not price_up and not vol_up:
            add("价跌量缩", 1, "抛压有限，止跌迹象", "资金")
        elif price_up and not vol_up:
            add("价涨量缩", 0, "上涨缺乏成交配合，动能存疑", "资金")
        else:
            add("价跌量增", -1, "放量下跌，抛压真实", "资金")
    if vma5[i] is not None and vols[i]:
        ratio = vols[i] / vma5[i] if vma5[i] else 0.0
        if ratio >= 1.5:
            add("成交量放大", 0, f"量为 5 日均量 {ratio:.2f} 倍，异动需核实原因", "资金")
        elif ratio <= 0.6:
            add("成交量萎缩", 0, f"量为 5 日均量 {ratio:.2f} 倍，市场分歧小", "资金")

    # 7. 持仓变化（配合价格）
    oi_delta = ind["oi_delta"]
    od = oi_delta[i]
    if od is not None and od != 0:
        oi_ratio = od / abs(ois[i]) * 100 if ois[i] else 0.0
        price_up = i > 0 and closes[i] > closes[i - 1]
        rollover = abs(oi_ratio) >= 10.0
        if rollover:
            add(
                "疑似移仓换月",
                0,
                f"持仓变动 {od:+.0f}（{oi_ratio:+.2f}%），幅度异常，多为主力换月导致，"
                "连续合约的持仓变化此时不可直接当作资金方向解读",
                "资金",
            )
        elif price_up and od < 0:
            add("价涨仓减", -1, f"持仓变动 {od:+.0f}（{oi_ratio:+.2f}%），上涨缺增仓支撑", "资金")
        elif (not price_up) and od > 0:
            add("价跌仓增", -1, f"持仓变动 {od:+.0f}（{oi_ratio:+.2f}%），下跌伴随增仓", "资金")
        elif price_up and od > 0:
            add("价涨仓增", 1, f"持仓变动 {od:+.0f}（{oi_ratio:+.2f}%），多头增仓推动", "资金")
        else:
            add("价跌仓减", 0, f"持仓变动 {od:+.0f}（{oi_ratio:+.2f}%），空头减仓离场", "资金")

    # 8. 关键价位
    recent = bars[-20:] if len(bars) >= 20 else bars
    highs20 = max(float(b["high"]) for b in recent)
    lows20 = min(float(b["low"]) for b in recent)
    highs60 = max(float(b["high"]) for b in bars[-60:]) if len(bars) >= 60 else highs20
    lows60 = min(float(b["low"]) for b in bars[-60:]) if len(bars) >= 60 else lows20

    closes_all = closes
    pct_all = percentile_rank(closes_all, close)
    pct_120 = percentile_rank(closes[-120:], close) if len(closes) >= 30 else pct_all

    atr_now = ind["atr14"][i]
    atr_pct = (atr_now / close * 100) if (atr_now and close) else None

    # 20 日涨跌幅
    ret20 = None
    if len(closes) >= 21:
        ret20 = (close / closes[-21] - 1) * 100
    ret5 = None
    if len(closes) >= 6:
        ret5 = (close / closes[-6] - 1) * 100

    if score >= 18:
        label = "偏多"
    elif score <= -18:
        label = "偏空"
    elif score >= 7:
        label = "弱偏多"
    elif score <= -7:
        label = "弱偏空"
    else:
        label = "中性震荡"

    stats = {
        "close": round(close, 2),
        "open": round(float(bars[i]["open"]), 2),
        "high": round(float(bars[i]["high"]), 2),
        "low": round(float(bars[i]["low"]), 2),
        "change": round(close - float(bars[i]["open"]), 2),
        "change_pct": round((close / float(bars[i]["open"]) - 1) * 100, 2),
        "ret5": round(ret5, 2) if ret5 is not None else None,
        "ret20": round(ret20, 2) if ret20 is not None else None,
        "percentile": pct_all,
        "percentile_120": pct_120,
        "atr": round(atr_now, 2) if atr_now else None,
        "atr_pct": round(atr_pct, 2) if atr_pct else None,
        "volume": vols[i],
        "volume_ratio5": round(vols[i] / vma5[i], 2) if (vma5[i] and vols[i]) else None,
        "open_interest": ois[i] if ois[i] else None,
        "oi_delta": od,
        "oi_delta_pct": round((od / abs(ois[i]) * 100) if (od is not None and ois[i]) else 0, 2),
        "bars": n,
    }

    levels = {
        "high20": round(highs20, 2),
        "low20": round(lows20, 2),
        "high60": round(highs60, 2),
        "low60": round(lows60, 2),
        "ma20": round(ma20[i], 2) if ma20[i] is not None else None,
        "ma60": round(ma60[i], 2) if ma60[i] is not None else None,
        "boll_upper": round(bu[i], 2) if bu[i] is not None else None,
        "boll_lower": round(bl[i], 2) if bl[i] is not None else None,
    }

    # 趋势持续性：近 5 日净值方向
    momentum_note = ""
    if i >= 5:
        net5 = (closes[i] / closes[i - 5] - 1) * 100
        momentum_note = f"近 5 日 {net5:+.2f}%"

    return {
        "score": score,
        "label": label,
        "items": items,
        "levels": levels,
        "stats": stats,
        "momentum": momentum_note,
    }
