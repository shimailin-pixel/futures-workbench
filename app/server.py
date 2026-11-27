"""期货技术面 + 基本面联合分析工作台 —— 后端服务。

行情与技术面部分仅依赖标准库；基本面自动采集部分需要可选依赖 akShare
（pip install akshare）。未安装时 /api/fundamentals 会逐项返回"源不可用"，
工作台其余功能不受影响。

启动：
    python app/server.py --port 8765

API：
    GET  /                         前端页面
    GET  /api/symbols             品种清单（含关键变量与否决权变量）
    GET  /api/kline                K 线原始数据
    GET  /api/analysis             技术面指标 + 信号 + 关键价位
    GET  /api/fundamentals         基本面自动采集（仓单/库存/基差/持仓）
    GET  /api/term-structure       期限结构与月差
    GET  /api/chan                 缠论笔/线段/中枢/买卖点标注
    GET  /api/research?symbol=     读取基本面研究记录
    POST /api/research             保存基本面研究记录
    GET  /api/quote                多品种快速快照
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent
SKILL_SCRIPTS = REPO_ROOT / "skills" / "futures-fundamental-research" / "scripts"
STATIC_DIR = APP_DIR / "static"
DATA_DIR = APP_DIR / "data"
RESEARCH_FILE = DATA_DIR / "research.json"

for candidate in (str(APP_DIR), str(SKILL_SCRIPTS)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import indicators  # noqa: E402
import symbols as symtab  # noqa: E402
import fundamentals as fund  # noqa: E402
import realtime as rt  # noqa: E402
import ai_context as aictx  # noqa: E402
import netcheck  # noqa: E402
import chan_analysis as chan  # noqa: E402

try:
    import futures_history as fh  # 复用 skill 的行情脚本
except Exception as exc:  # pragma: no cover
    fh = None
    _FH_ERROR = str(exc)
else:
    _FH_ERROR = ""

DATA_DIR.mkdir(parents=True, exist_ok=True)

_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()
CACHE_TTL = 120.0
# 实时快照缓存：行情推送频率高，单独设一个很短的 TTL
REALTIME_TTL = 1.5


def cached(key: str, producer, ttl: float | None = None):
    now = time.time()
    limit = CACHE_TTL if ttl is None else ttl
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < limit:
            return hit[1]
    value = producer()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def invalidate_cache(prefix: str):
    """清除某个前缀下的缓存（用于强制刷新）。"""
    with _cache_lock:
        for key in [k for k in _cache if k.startswith(prefix)]:
            _cache.pop(key, None)


def _fetch(market: str, symbol: str, interval: str, count: int) -> list[dict]:
    if fh is None:
        raise RuntimeError(f"行情模块不可用: {_FH_ERROR}")
    duration = fh.parse_duration(interval)
    bars = fh.fetch_history(market, symbol, duration, count=count)
    rows = fh.serialize_bars(bars, duration)
    return rows


def kline_rows(market: str, symbol: str, interval: str, count: int,
               force: bool = False) -> list[dict]:
    key = f"k|{market}|{symbol}|{interval}|{count}"
    if force:
        invalidate_cache(key)
    return cached(key, lambda: _fetch(market, symbol, interval, count))


def spot_snapshot(code: str, force: bool = False) -> dict:
    """实时行情快照。code 为内盘品种字母，如 RB。"""
    key = f"spot|{code}"
    if force:
        invalidate_cache(key)
    return cached(key, lambda: rt.fetch_spot(code), REALTIME_TTL)


def contract_candidates(code: str, months: list[int], ahead: int = 12, back: int = 3) -> list[str]:
    """按交割月份规则生成候选合约代码（含 back 个月前的旧主力，便于识别独立近月）。"""
    today = datetime.now()
    out: list[str] = []

    # 回溯 back 个月（独立变量，避免与前推共用导致重复）
    year, month = today.year, today.month
    for _ in range(back):
        month -= 1
        if month < 1:
            month = 12
            year -= 1
        if month in months:
            out.append(f"{code}{year % 100:02d}{month:02d}")

    # 从今天往前推 ahead 个月
    year, month = today.year, today.month
    for _ in range(ahead):
        month += 1
        if month > 12:
            month = 1
            year += 1
        if month in months:
            out.append(f"{code}{year % 100:02d}{month:02d}")

    # 去重并保持顺序
    seen: set[str] = set()
    unique: list[str] = []
    for c in out:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def _code_of(symbol: str) -> str | None:
    """从新浪合约代码反推内盘品种字母。

    nf_RB0 / RB0 / rb2701 → RB
    """
    if not symbol:
        return None
    s = symbol.strip()
    if s.lower().startswith("nf_"):
        s = s[3:]
    s = s.upper()
    head = "".join(ch for ch in s if ch.isalpha())
    return head or None


def _normalize_symbol(market: str, symbol: str) -> str:
    """把品种代码归一到行情接口可接受的写法。

    行情接口只认 RB0 / nf_RB0 / rb2701 这类带连续标记或月份的代码，
    裸字母（RB）会直接返回空 JSONP。而 /api/realtime、/api/term-structure
    等接口都以裸字母为入参，这里统一补全，避免同一套参数在不同接口表现不一致。
    """
    s = (symbol or "").strip()
    if not s or market != "domestic":
        return s
    if s.lower().startswith("nf_"):
        return s
    if s.isalpha():
        meta = symtab.find_symbol(s)
        return meta["continuous"] if meta else f"{s}0"
    return s


def _sina_contract_volumes(contracts: list[str], timeout: int = 8) -> dict[str, float]:
    """一次请求取回多个合约的实时成交量。

    新浪 hq.sinajs.cn 支持逗号分隔的批量查询，每个合约的行情串里
    第 13 个字段是成交量。用它来找主力合约，比逐个合约抓完整历史快几十倍
    （原来的做法要给每个候选合约拉一遍全量日线，云端直接被代理掐超时）。
    """
    if not contracts:
        return {}
    out: dict[str, float] = {}
    symbols = ",".join(f"nf_{c.upper()}" for c in contracts)
    req = urllib.request.Request(
        f"https://hq.sinajs.cn/list={symbols}",
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://finance.sina.com.cn/",
            "Accept": "*/*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("gbk", errors="replace")
    except Exception:
        return out

    for line in text.splitlines():
        if "=" not in line:
            continue
        head, _, payload = line.partition("=")
        name = head.strip().replace("var hq_str_nf_", "").strip()
        payload = payload.strip().strip(';').strip('"')
        parts = payload.split(",")
        if len(parts) < 14:
            continue
        try:
            volume = float(parts[13] or 0)
        except (TypeError, ValueError):
            volume = 0.0
        out[name] = volume
    return out


def main_contract_of(code: str, months: list[int]) -> str | None:
    """探测成交量最大的合约代码，供基本面持仓排名查询使用。

    快路径：一次新浪批量行情拿到各候选合约的成交量，取最大的。
    慢路径（批量行情不可用时）：退回逐合约查日线成交量。
    """
    candidates = contract_candidates(code, months)
    if not candidates:
        return None

    volumes = _sina_contract_volumes(candidates)
    if volumes:
        best = max(volumes.items(), key=lambda kv: kv[1])
        if best[1] > 0:
            return best[0]

    def probe(contract: str) -> tuple[str, float]:
        try:
            rows = _fetch("domestic", contract, "1d", 3)
        except Exception:
            return contract, 0.0
        if not rows:
            return contract, 0.0
        return contract, float(rows[-1].get("volume") or 0)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(probe, candidates))
    best = max(results, key=lambda x: x[1])
    return best[0] if best[1] > 0 else None


def _months_between(a_ym: int, b_ym: int) -> int:
    """两个合约年月之间相差的月数（2701 → 2705 = 4）。"""
    a = (a_ym // 100) * 12 + (a_ym % 100)
    b = (b_ym // 100) * 12 + (b_ym % 100)
    return b - a


def term_structure(code: str, months: list[int], spot: str) -> dict:
    """并发抓取候选合约，识别主力/近月/远月并计算月差。"""
    candidates = contract_candidates(code, months)
    if not candidates:
        return {"error": "该品种无可用合约月份配置"}

    def probe(contract: str) -> dict | None:
        try:
            rows = _fetch("domestic", contract, "1d", 6)
        except Exception:
            return None
        if not rows:
            return None
        last = rows[-1]
        # 合约年月排序键（品种字母 + 年月数字）
        digits = "".join(ch for ch in contract if ch.isdigit())
        ym = int(digits[-4:]) if len(digits) >= 4 else 0
        return {
            "contract": contract,
            "ym": ym,
            "date": last.get("datetime"),
            "close": float(last.get("close") or 0),
            "volume": float(last.get("volume") or 0),
            "open_interest": float(last.get("open_interest") or 0),
        }

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(probe, candidates))
    rows = [r for r in results if r and r["volume"] > 0]
    if not rows:
        return {"error": "未取到有效合约行情"}
    rows.sort(key=lambda r: r["ym"])

    # 已摘牌合约（交割月早于当前月）只留最后一笔陈价，挂在曲线上会把形态彻底带偏
    # ——例如 CU2610 停在 109,600（2026-09 的旧价），而 CU2701 才 87,950。
    # 这类合约退出曲线计算，表格里仍保留并标注。
    now = datetime.now()
    cur_ym = (now.year % 100) * 100 + now.month
    for r in rows:
        r["expired"] = r["ym"] < cur_ym
    live = [r for r in rows if not r["expired"]] or rows

    main = max(live, key=lambda r: r["volume"])

    # 流动性门槛：近月/远月必须有真实成交，否则月差是失真噪音
    min_vol = max(main["volume"] * 0.01, 1000.0)
    liquid = [r for r in live if r["volume"] >= min_vol]
    if not liquid:                       # 全部无成交时退回原始集合，避免无解
        liquid = [r for r in rows if r["volume"] >= min_vol]

    for r in rows:
        r["liquid"] = r["volume"] >= min_vol

    # 曲线端点：
    #   front    = 最早仍在交易的活跃合约（"近月"）
    #   deferred = 主力之后较远的活跃合约（"远月"）
    # 早先版本把 near 定义成"主力前一个合约"，而多数品种主力就是第一个活跃合约，
    # 于是 near 恒为 None、形态标签全部退化成"无独立近月"，把最有价值的
    # 近月↔远月升贴水信号整个丢掉。改为按曲线端点判断。
    front = liquid[0] if liquid else main
    after = [r for r in liquid if r["ym"] > main["ym"]]
    if after:
        deferred = after[1] if len(after) >= 2 else after[0]
    else:
        deferred = liquid[-1] if liquid and liquid[-1]["ym"] != main["ym"] else main

    # 主力和 front 重合时（常见），"近月-主力"没有信息量，主看远月端点
    near = front if front["ym"] != main["ym"] else None
    far = deferred

    spot_row = None
    try:
        srows = _fetch("domestic", spot, "1d", 3)
        if srows:
            spot_row = {
                "date": srows[-1].get("datetime"),
                "close": float(srows[-1].get("close") or 0),
            }
    except Exception:
        pass

    def spread(a: dict | None, b: dict | None) -> float | None:
        if not a or not b or not b["close"]:
            return None
        return round(a["close"] - b["close"], 2)

    near_spread = spread(near, main)
    far_spread = spread(main, far)
    # 曲线形态的核心指标：近端 vs 远端
    curve_spread = spread(front, deferred)
    curve_pct = None
    annualized = None
    months_gap = _months_between(front["ym"], deferred["ym"]) if front and deferred else 0
    if curve_spread is not None and deferred and deferred["close"]:
        curve_pct = round(curve_spread / deferred["close"] * 100, 3)
        if months_gap > 0:
            annualized = round(curve_pct * 12 / months_gap, 2)

    # 形态判定：以年化幅度为准（不同品种跨月数差异大，裸百分比不可比）
    # 年化 |x| < 2% 视为平坦，2%~10% 为温和偏紧/偏松，>10% 为显著。
    if curve_spread is None:
        shape = "曲线数据不足"
        shape_kind = "unknown"
        strength = "unknown"
    else:
        ann = annualized if annualized is not None else 0.0
        if ann > 2.0:
            shape_kind = "backwardation"
            strength = "显著" if ann > 10 else ("温和" if ann > 5 else "轻微")
        elif ann < -2.0:
            shape_kind = "contango"
            strength = "显著" if ann < -10 else ("温和" if ann < -5 else "轻微")
        else:
            shape_kind = "flat"
            strength = "基本平坦"
        if shape_kind == "backwardation":
            shape = f"近月升水（Backwardation）· {strength}偏紧"
        elif shape_kind == "contango":
            shape = f"近月贴水（Contango）· {strength}偏松"
        else:
            # 平坦时仍标出方向（略微升水/略微贴水），避免把有倾向的曲线说成"中性"
            if curve_spread and curve_spread > 0:
                shape = "曲线基本平坦（Flat）· 略微升水，方向不明确"
            elif curve_spread and curve_spread < 0:
                shape = "曲线基本平坦（Flat）· 略微贴水，方向不明确"
            else:
                shape = "曲线基本平坦（Flat）· 期限结构中性"

    # 曲线是否整体单调（V 形/扭曲的辅助判断）
    curve_prices = [r["close"] for r in liquid]
    monotone_down = all(a >= b for a, b in zip(curve_prices, curve_prices[1:]))
    monotone_up = all(a <= b for a, b in zip(curve_prices, curve_prices[1:]))
    if shape_kind == "backwardation" and not monotone_down:
        shape += " · 但曲线非单调（远端有翘尾）"
    if shape_kind == "contango" and not monotone_up:
        shape += " · 但曲线非单调（近端有下折）"

    # 通俗解释：先讲概念，再把数字代入
    fc, dc = front["close"], deferred["close"]
    if shape_kind == "backwardation":
        concept = (
            "这条线是「近月贵、远月便宜」，叫【升水（Backwardation）】。\n"
            "打个比方：现在鸡蛋紧张、大家抢着买，先要货的人出价高；"
            "等几个月后新产能上来、货多了，价格反而回落。"
            "期货市场把这种「眼下货紧、以后会松」的预期画成这条向下倾斜的线。"
        )
        reading = (
            f"{front['contract']} {fc:,.0f} 高于 {deferred['contract']} {dc:,.0f}，"
            f"价差 {curve_spread:+,.0f} 元（{curve_pct:+.2f}%，跨 {months_gap} 个月，"
            f"年化约 {annualized:+.1f}%）。\n"
            f"含义：市场认为{code}眼下偏紧——现货不好买、贸易商不愿把货放到未来，"
            f"期货价先被拉高；预期的紧张会随时间缓解。"
        )
    elif shape_kind == "contango":
        concept = (
            "这条线是「近月便宜、远月贵」，叫【贴水（Contango）】。\n"
            "打个比方：仓库里货堆着不紧张，把货存到未来反而更划算"
            "（省得现在卖亏钱，还占着仓储费）。"
            "期货市场把这种「现在货松、以后会紧」的预期画成这条向上倾斜的线。"
        )
        reading = (
            f"{front['contract']} {fc:,.0f} 低于 {deferred['contract']} {dc:,.0f}，"
            f"价差 {curve_spread:+,.0f} 元（{curve_pct:+.2f}%，跨 {months_gap} 个月，"
            f"年化约 {annualized:+.1f}%）。\n"
            f"含义：市场认为{code}眼下不紧张——货主不急着卖，期货价被压低；"
            f"预期未来会消耗掉库存、价格走高。"
        )
    else:
        tilt = "略偏升水" if (curve_spread or 0) > 0 else ("略偏贴水" if (curve_spread or 0) < 0 else "完全走平")
        concept = (
            "这条线几乎是平的，叫【平坦（Flat）】。\n"
            "打个比方：近月和远月的价格差不多，市场对「眼下松紧」没有明显分歧，"
            "定价主要在交易别的东西（资金成本、季节性、宏观预期）。"
            f"当前这条线{tilt}，但幅度太小，不足以当方向用。"
        )
        reading = (
            f"{front['contract']} {fc:,.0f} 与 {deferred['contract']} {dc:,.0f} "
            f"相差 {curve_spread:+,.0f} 元（{curve_pct:+.2f}%，跨 {months_gap} 个月，"
            f"年化约 {annualized:+.1f}%）。\n"
            f"含义：期限结构本身没给出方向性信息，别从这条线上找多空理由。"
            f"若该品种本应有明显季节性（如农产品），那么走平本身反而是个信号——"
            f"说明市场暂时没在交易那个季节。"
        )

    caution = (
        "注意：期货曲线反映的是【预期】，不是已经发生的事实。"
        "月差大不等于一定会收敛——预期落空时曲线自己会改。"
        "另外本工作台只算价差，仓单与有效预报需人工到交易所查；"
        "换月移仓期间的价格跳空会让月差失真，读数前先确认合约是否已完成移仓换月。"
    )

    note = "月差基于新浪收盘价计算；仓单与有效预报需人工在基本面面板补充。"
    if near is None:
        note += " 主力即最近活跃合约，无独立近月；近月↔远月升贴水以主力为起点计算。"
    expired = [r["contract"] for r in rows if r.get("expired")]
    if expired:
        note += f" 已摘牌合约（陈价）已排除出曲线计算：{'、'.join(expired)}。"
    illiquid = [r["contract"] for r in rows if not r.get("liquid")]
    if illiquid:
        note += f" 流动性不足已排除月差计算：{'、'.join(illiquid)}。"

    return {
        "rows": rows,
        "curve": liquid,
        "main": main,
        "near": near,
        "far": far,
        "front": front,
        "deferred": deferred,
        "spot": spot_row,
        "near_spread": near_spread,
        "far_spread": far_spread,
        "curve_spread": curve_spread,
        "curve_pct": curve_pct,
        "months_gap": months_gap,
        "annualized_pct": annualized,
        "shape": shape,
        "shape_kind": shape_kind,
        "concept": concept,
        "reading": reading,
        "caution": caution,
        "note": note,
    }
def load_research() -> dict:
    if not RESEARCH_FILE.exists():
        return {}
    try:
        return json.loads(RESEARCH_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_research(payload: dict) -> dict:
    store = load_research()
    key = payload.get("key") or f"{payload.get('symbol', 'UNKNOWN')}@{payload.get('date', '')}"
    payload = dict(payload)
    payload["key"] = key
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    store[key] = payload
    RESEARCH_FILE.write_text(
        json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def diagnose(tech: dict, research: dict) -> dict:
    """技术面 vs 基本面的冲突诊断：直接对应 skill 的结论降级规则。"""
    if not research:
        return {
            "status": "no_input",
            "title": "尚未录入基本面判断",
            "detail": "技术面已就绪。补录决策面板（倾向、时间边界、否决权变量状态）后，这里会给出两者的冲突诊断与降级建议。",
            "severity": "info",
            "actions": [],
        }

    tech_label = tech.get("label", "中性震荡")
    tech_score = tech.get("score", 0)
    bias = research.get("bias", "")
    veto_status = research.get("veto_status", {}) or {}
    conflicts = [k for k, v in veto_status.items() if v == "冲突"]
    unverified = research.get("unverified") or []

    bullish_tech = tech_score >= 7
    bearish_tech = tech_score <= -7
    bull_fund = bias in ("偏多", "中性偏多", "看多")
    bear_fund = bias in ("偏空", "中性偏空", "看空")

    actions: list[str] = []
    severity = "info"
    title = "技术面与基本面一致"
    detail = ""

    if bullish_tech and bear_fund:
        severity = "warn"
        title = "方向冲突：技术面偏多 vs 基本面偏空"
        detail = (
            f"盘面信号为{tech_label}（评分 {tech_score}），但基本面倾向记为「{bias}」。"
            "按研究纪律，此种背离通常意味着市场在交易尚未兑现的预期或资金推动，"
            "不宜用单一方向下注。"
        )
        actions = [
            "优先核对：盘面是否已提前交易了未兑现的预期",
            "检查否决权变量是否支持盘面方向",
            "考虑降级为「只看结构」或跨期表达",
            "确认时间边界一致（日内波动 vs 中期供需不可混用）",
        ]
    elif bearish_tech and bull_fund:
        severity = "warn"
        title = "方向冲突：技术面偏空 vs 基本面偏多"
        detail = (
            f"盘面信号为{tech_label}（评分 {tech_score}），基本面倾向记为「{bias}」。"
            "这通常是基本面预期已在价格中提前反映，或存在未识别的否决权变量。"
        )
        actions = [
            "核对基差与现货：基本面转好但盘面走弱，通常是现货没有跟上",
            "检查是否出现结构性因素（仓单、交割、逼仓）压制盘面",
            "确认时间边界是否错配",
        ]
    elif conflicts:
        severity = "warn"
        title = "否决权变量冲突：结论需要降级"
        detail = (
            f"已标记为「冲突」的否决权变量：{', '.join(conflicts)}。"
            "否决权变量一旦与主叙事冲突，按纪律必须先降级结论。"
        )
        actions = [
            "优先核实冲突变量的最新数据与来源等级",
            "冲突未解除前，结论降级为「观望」或「只看结构」",
            "明确写出失效条件与改判触发点",
        ]
    elif unverified:
        severity = "warn"
        title = "存在未验证信息，结论暂不能进入主链"
        detail = f"以下信息尚未验证：{', '.join(unverified[:5])}。未核验信息不得直接进入主结论。"
        actions = ["先做源头核验与异类交叉验证", "核验前只能作为线索处理"]
    elif (bullish_tech and bull_fund) or (bearish_tech and bear_fund):
        title = "技术面与基本面同向"
        detail = (
            f"盘面{tech_label}（评分 {tech_score}）与基本面倾向「{bias}」方向一致。"
            "同向时仍需检查时间边界是否一致，以及盘面是否已充分定价。"
        )
        actions = ["检查当前价格分位，避免在情绪极端处加仓", "设定失效条件与止盈止损"]
    else:
        title = "技术面中性，与基本面无明显冲突"
        detail = (
            f"盘面信号为{tech_label}（评分 {tech_score}），未与基本面倾向「{bias or '未填'}」形成反向。"
            "盘面信号弱时，基本面主导方向，但需降低操作频率与置信度。"
        )
        actions = ["等待技术面信号确认或基本面出现边际变化"]

    confidence = research.get("confidence", "")
    if confidence == "低" and severity == "info":
        severity = "warn"
        title = "置信度偏低：结论按低置信度处理"
        detail = "基本面记录标记为低置信度，输出应强调条件触发与失效条件，而非直接给方向。"

    return {
        "status": "done",
        "title": title,
        "detail": detail,
        "severity": severity,
        "actions": actions,
        "tech_label": tech_label,
        "tech_score": tech_score,
        "bias": bias,
        "conflicts": conflicts,
    }


def _latest_research(symbol: str) -> dict:
    """取该品种最近一条已保存的研究记录。"""
    store = load_research()
    items = [v for v in store.values() if v.get("symbol", "").upper() == symbol.upper()]
    if not items:
        return {}
    items.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return items[0]


def build_ai_context(payload: dict) -> dict:
    """组装 AI 研判所需的上下文与提示词。

    真正的模型调用发生在浏览器端（云服务 LLM 走前端直连），
    后端只负责把「可追溯的证据清单」整理成提示词，避免前后端两套口径。
    """
    symbol = (payload.get("symbol") or "RB").strip().upper()
    scope = (payload.get("scope") or "fundamental").strip().lower()
    meta = symtab.find_symbol(symbol)
    if not meta:
        raise ValueError(f"未知品种 {symbol}")

    # 技术面信号：与 /api/analysis 同一套算法，避免两处口径漂移
    sig = None
    if scope == "diagnose":
        rows = kline_rows("domestic", meta["continuous"], "1d", 300)
        ind = indicators.compute_indicators(rows)
        sig = indicators.build_signals(rows, ind)

    # 基本面采集结果：优先复用「面板当前显示的那一份」，避免二次采集漂移。
    # 之前这里会重新跑一遍 fund.collect()，如果这次某块超时/失败（云端常见），
    # 模型拿到的读数就与用户眼前看到的面板不一致，是最容易造成误解的路径。
    fund_data = None
    fund_source = "panel"
    panel_fund = payload.get("fund")
    if (isinstance(panel_fund, dict)
            and isinstance(panel_fund.get("blocks"), dict)
            and str(panel_fund.get("symbol") or symbol).upper() == symbol.upper()):
        fund_data = panel_fund
    else:
        fund_source = "recollect"
        try:
            mainc = cached(f"mainc|{symbol}", lambda: main_contract_of(symbol, meta.get("months") or []), 1800)
            fund_data = fund.collect(meta, mainc)
        except Exception as exc:
            fund_data = {"error": f"基本面采集失败：{exc}", "blocks": {}, "gaps": [str(exc)[:200]]}

    rec = payload.get("rec") or _latest_research(symbol)
    ctx = aictx.context_payload(scope, symbol, sig or {}, fund_data or {}, rec or {}, meta)
    ctx["symbol"] = symbol
    ctx["name"] = meta.get("name")
    ctx["model_hint"] = "space-bunny"
    ctx["fundamentals_error"] = (fund_data or {}).get("error")
    ctx["fundamentals_source"] = fund_source
    # 面板传来的这份数据自带指纹（服务端用原始 Python 对象算的）。
    # 后端对 JSON 反序列化后的对象重算，可能因 numpy/date 等类型序列化差异得出不同值，
    # 因此统一以「用户眼前那份数据」的指纹为准，保证 AI 研判与面板严格绑定。
    if fund_source == "panel":
        panel_fp = panel_fund.get("data_fingerprint")
        if panel_fp:
            ctx["computed_fingerprint"] = ctx.get("data_fingerprint")
            ctx["fingerprint_consistent"] = (ctx.get("data_fingerprint") == panel_fp)
            ctx["data_fingerprint"] = panel_fp
    return ctx


class Handler(BaseHTTPRequestHandler):
    server_version = "FuturesLab/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默常规日志
        pass

    def handle_one_request(self):
        # 实时推送连接被浏览器关闭时，Windows 会抛 ConnectionAbortedError，
        # 这里统一吞掉，避免刷屏式堆栈。
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def _send(self, code: int, body: bytes, ctype: str, cache: bool = False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, payload, code: int = 200):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _sse_start(self):
        """开始一个 SSE 长连接响应。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _sse_write(self, event: str, data: dict):
        payload = json.dumps(data, ensure_ascii=False, default=str)
        chunk = f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
        self.wfile.write(chunk)
        self.wfile.flush()

    def _stream_quotes(self, codes: list[str], interval: str = "1d"):
        """SSE 行情推送：按固定节奏推送快照与合并后的 K 线末根。

        交易时段内推送更频繁，休市时降频，避免无谓请求。
        """
        try:
            self._sse_start()
        except (BrokenPipeError, ConnectionAbortedError):
            return

        deadline = time.time() + 3600
        while time.time() < deadline:
            try:
                session = rt.session_state()
                # 盘中 2 秒、休市 20 秒
                gap = 2.0 if session["trading"] else 20.0
                for code in codes:
                    meta = symtab.find_symbol(code)
                    if not meta or meta.get("market") != "domestic":
                        continue
                    try:
                        spot = spot_snapshot(code, force=True)
                    except Exception as exc:
                        self._sse_write("error", {"symbol": code, "error": str(exc)[:200]})
                        continue
                    if not spot.get("ok"):
                        self._sse_write("error", {"symbol": code, "error": spot.get("error")})
                        continue

                    payload = {
                        "symbol": code,
                        "name": meta["name"],
                        "spot": spot,
                        "session": session,
                        "server_time": datetime.now().isoformat(timespec="seconds"),
                    }

                    # 日线模式下同步推送合并后的 K 线末根，让图表最后一根实时跳动
                    if interval == "1d":
                        try:
                            rows = kline_rows("domestic", meta["continuous"], "1d", 300)
                            merged, note = rt.merge_daily(rows, spot)
                            payload["last_bar"] = rt.bar_to_payload(merged[-1])
                            payload["merge_note"] = note
                        except Exception:
                            pass

                    self._sse_write("quote", payload)

                time.sleep(gap)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return
            except Exception as exc:  # 推送线程内部异常不应中断连接
                try:
                    self._sse_write("error", {"error": f"{type(exc).__name__}: {str(exc)[:200]}"})
                except (BrokenPipeError, ConnectionAbortedError):
                    return
                time.sleep(5)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._serve_static("index.html")

        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/"):])

        if path == "/api/symbols":
            return self._json({
                "groups": symtab.GROUPS,
                "symbols": symtab.symbol_table(),
                "server_time": datetime.now().isoformat(timespec="seconds"),
            })

        if path == "/api/kline":
            market = (query.get("market", ["domestic"])[0]).lower()
            symbol = _normalize_symbol(market, query.get("symbol", ["RB0"])[0])
            interval = query.get("interval", ["1d"])[0]
            count = int(query.get("count", ["300"])[0])
            try:
                rows = kline_rows(market, symbol, interval, count)
            except Exception as exc:
                return self._json({"error": str(exc)}, 200)
            return self._json({"rows": rows, "symbol": symbol, "interval": interval})

        if path == "/api/analysis":
            market = (query.get("market", ["domestic"])[0]).lower()
            symbol = _normalize_symbol(market, query.get("symbol", ["RB0"])[0])
            interval = query.get("interval", ["1d"])[0]
            count = int(query.get("count", ["300"])[0])
            force = query.get("force", ["0"])[0] in ("1", "true", "yes")
            try:
                rows = kline_rows(market, symbol, interval, count, force=force)
            except Exception as exc:
                return self._json({"error": str(exc)}, 200)
            if not rows:
                return self._json({"error": "无行情数据"}, 200)

            # 日线在盘中滞后一个交易日，用实时快照补出「当日进行中 K 线」
            spot = None
            merge_note = ""
            code = _code_of(symbol)
            if market == "domestic" and code:
                try:
                    spot = spot_snapshot(code)
                    if interval == "1d" and spot.get("ok"):
                        rows, merge_note = rt.merge_daily(rows, spot)
                except Exception as exc:
                    merge_note = f"实时快照获取失败：{exc}"

            ind = indicators.compute_indicators(rows)
            sig = indicators.build_signals(rows, ind)
            series = {
                "datetime": [r["datetime"] for r in rows],
                "open": [r["open"] for r in rows],
                "high": [r["high"] for r in rows],
                "low": [r["low"] for r in rows],
                "close": [r["close"] for r in rows],
                "volume": [r.get("volume") for r in rows],
                "open_interest": [r.get("open_interest") for r in rows],
                "settlement_price": [r.get("settlement_price") for r in rows],
                "ma5": ind["ma5"], "ma10": ind["ma10"], "ma20": ind["ma20"], "ma60": ind["ma60"],
                "dif": ind["dif"], "dea": ind["dea"], "hist": ind["hist"],
                "kdj_k": ind["kdj_k"], "kdj_d": ind["kdj_d"], "kdj_j": ind["kdj_j"],
                "rsi14": ind["rsi14"],
                "boll_upper": ind["boll_upper"], "boll_mid": ind["boll_mid"], "boll_lower": ind["boll_lower"],
                "atr14": ind["atr14"], "vol_ma5": ind["vol_ma5"], "oi_delta": ind["oi_delta"],
            }
            payload = {
                "symbol": symbol,
                "interval": interval,
                "series": series,
                "signals": sig,
                "data_asof": rows[-1]["datetime"],
                "merge_note": merge_note,
            }
            if spot:
                payload["spot"] = spot
            return self._json(payload)

        if path == "/api/realtime":
            code = (query.get("symbol", ["RB"])[0]).strip().upper()
            force = query.get("force", ["0"])[0] in ("1", "true", "yes")
            meta = symtab.find_symbol(code)
            if not meta:
                return self._json({"error": f"未知品种 {code}"}, 200)
            if meta.get("market") != "domestic":
                return self._json({"error": "外盘品种暂不支持实时推送"}, 200)
            try:
                spot = spot_snapshot(code, force=force)
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {str(exc)[:200]}"}, 200)
            spot["session"] = rt.session_state(spot.get("minute_of_day"))
            spot["server_time"] = datetime.now().isoformat(timespec="seconds")
            return self._json(spot)

        if path == "/api/net-check":
            endpoint = query.get("endpoint", [netcheck.DEFAULT_ENDPOINT])[0].strip()
            try:
                return self._json(netcheck.check_cloud_endpoint(endpoint))
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {str(exc)[:200]}"}, 200)

        if path == "/api/stream":
            raw = query.get("codes", ["RB"])[0]
            codes = [c.strip().upper() for c in raw.split(",") if c.strip()]
            if not codes:
                return self._json({"error": "缺少 codes 参数"}, 200)
            interval = query.get("interval", ["1d"])[0]
            # 逐个校验品种，只允许内盘（外盘行情未接入）
            valid = []
            for code in codes:
                meta = symtab.find_symbol(code)
                if not meta:
                    return self._json({"error": f"未知品种 {code}"}, 200)
                if meta.get("market") != "domestic":
                    return self._json({"error": "外盘品种暂不支持实时推送"}, 200)
                valid.append(code)
            return self._stream_quotes(valid, interval)

        if path == "/api/term-structure":
            code = (query.get("code", ["RB"])[0]).upper()
            meta = symtab.find_symbol(code)
            if not meta:
                return self._json({"error": f"未知品种 {code}"}, 200)
            try:
                data = term_structure(code, meta.get("months", symtab.ALL_MONTHS), meta["continuous"])
            except Exception as exc:
                return self._json({"error": str(exc)}, 200)
            return self._json(data)

        if path == "/api/chan":
            code = (query.get("symbol", ["RB"])[0]).strip().upper()
            meta = symtab.find_symbol(code)
            if not meta:
                return self._json({"error": f"未知品种 {code}"}, 200)
            interval = query.get("interval", ["1d"])[0]
            count = int(query.get("count", ["300"])[0])
            force = query.get("force", ["0"])[0] in ("1", "true", "yes")
            if not chan.available():
                return self._json({"error": f"缠论模块不可用：{chan._IMPORT_ERROR}"}, 200)
            try:
                rows = kline_rows(meta["market"], meta["continuous"], interval, count, force=force)
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {str(exc)[:200]}"}, 200)
            # 缠论按原始 K 线数组算，返回时带上原始序列供前端对齐坐标轴
            result = chan.analyze_cached(
                rows, key=f"{code}|{interval}|{count}|{rows[-1].get('datetime', '') if rows else ''}")
            if result.get("error"):
                return self._json(result, 200)
            result["symbol"] = code
            result["interval"] = interval
            result["kline_count"] = len(rows)
            return self._json(result)

        if path == "/api/fundamentals":
            code = query.get("symbol", ["RB"])[0].strip().upper()
            meta = symtab.find_symbol(code)
            if not meta:
                return self._json({"error": f"未知品种 {code}"}, 200)

            # 整体硬超时：反向代理一般 120 秒就掐，这里留 20 秒余量。
            # 用 daemon 线程而不是 ThreadPoolExecutor —— executor 的 worker 是
            # 非守护线程，一旦某个 akshare 调用卡死，进程退出会被拖住。
            box: dict = {}

            def _work() -> None:
                try:
                    box["data"] = _fundamentals_payload(code, meta)
                except Exception as exc:  # noqa: BLE001
                    box["err"] = exc

            _t0 = time.time()
            th = threading.Thread(target=_work, daemon=True, name=f"fund-{code}")
            th.start()
            th.join(timeout=65)
            if "data" in box:
                data = box["data"]
                data["_耗时秒"] = round(time.time() - _t0, 2)
            elif "err" in box:
                exc = box["err"]
                data = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            else:
                data = {
                    "error": "基本面数据采集超时（数据源响应较慢），请稍后刷新重试",
                    "symbol": code,
                    "blocks": {},
                    "gaps": ["采集超时"],
                }
            return self._json(data)

        if path == "/api/env":
            # 环境体检：akshare 版本、接口签名、各数据源连通性、缓存统计
            # 加 ?deep=1 会真的调一次 akshare，看它到底能不能出网
            deep = query.get("deep", ["0"])[0] in ("1", "true", "yes")
            try:
                return self._json(fund.env_report(deep=deep))
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {str(exc)[:200]}"}, 200)

        if path == "/api/research":
            symbol = (query.get("symbol", ["RB"])[0]).upper()
            store = load_research()
            items = [v for v in store.values() if v.get("symbol", "").upper() == symbol]
            items.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
            return self._json({"items": items, "total": len(store)})

        if path == "/api/quote":
            codes = query.get("codes", ["RB,CU,SC,M,I,AU"])[0].split(",")
            codes = [c.strip() for c in codes if c.strip()]

            def one(code: str):
                meta = symtab.find_symbol(code)
                if not meta:
                    return {"code": code, "error": "未知品种"}
                try:
                    rows = _fetch(meta["market"], meta["continuous"], "1d", 3)
                    if not rows:
                        return {"code": code, "error": "无数据"}
                    last = rows[-1]
                    prev_close = float(rows[-2]["close"]) if len(rows) >= 2 else float(last["open"])
                    close = float(last["close"])
                    return {
                        "code": code,
                        "name": meta["name"],
                        "close": close,
                        "change": round(close - prev_close, 2),
                        "change_pct": round((close / prev_close - 1) * 100, 2) if prev_close else None,
                        "date": last.get("datetime"),
                    }
                except Exception as exc:
                    return {"code": code, "error": str(exc)}

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(one, codes))
            return self._json({"rows": results})

        self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._json({"error": "invalid json"}, 400)

        if parsed.path == "/api/research":
            try:
                saved = save_research(payload)
            except Exception as exc:
                return self._json({"error": str(exc)}, 500)
            return self._json({"ok": True, "record": saved})

        if parsed.path == "/api/ai-context":
            try:
                return self._json(build_ai_context(payload))
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {str(exc)[:300]}"}, 200)

        self._json({"error": "not found"}, 404)

    def _serve_static(self, rel: str):
        rel = rel.replace("\\", "/").lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            return self._json({"error": "invalid path"}, 403)
        if not target.is_file():
            return self._json({"error": "not found"}, 404)
        ext = target.suffix.lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(ext, "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)


def _maybe_open_browser(url: str) -> None:
    """本地双击启动时自动开浏览器；云端部署不设 OPEN_BROWSER，所以不会触发。"""
    if not os.environ.get("OPEN_BROWSER"):
        return

    def _run() -> None:
        time.sleep(1.2)
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


def _fundamentals_payload(code: str, meta: dict) -> dict:
    """解析主力合约并采集基本面四块数据。"""
    def resolve_main():
        return main_contract_of(code, meta.get("months") or [])

    try:
        mainc = cached(f"mainc|{code}", resolve_main, 1800)
    except Exception:
        mainc = None
    result = fund.collect(meta, mainc)
    # 与 /api/ai-context 用同一套指纹：前端可据此判断 AI 研判是否基于当前面板数据
    try:
        result["data_fingerprint"] = aictx.data_fingerprint(result)
    except Exception:
        pass
    return result


def _ak_probe() -> None:
    """后台预检 akShare 是否可用（只打日志，不影响服务启动）。"""
    try:
        if fund._ak() is None:
            print("[提示] 未安装 akShare，基本面自动采集不可用（pip install akshare）")
            print("       技术面、期限结构、研究记录等功能不受影响。", flush=True)
    except Exception as exc:
        print(f"[提示] akShare 预检异常: {type(exc).__name__}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="期货技术面 + 基本面联合分析工作台")
    # 云端部署时 PORT 由平台注入，必须绑定 0.0.0.0 才会被反向代理看到；
    # 本地运行（无 PORT 环境变量）保持 127.0.0.1 不变，避免无意暴露到局域网。
    env_port = os.environ.get("PORT")
    default_port = int(env_port) if env_port else 8765
    default_host = "0.0.0.0" if env_port else "127.0.0.1"
    ap.add_argument("--port", type=int, default=default_port)
    ap.add_argument("--host", default=default_host)
    args = ap.parse_args()

    if fh is None:
        print(f"[警告] 行情模块不可用: {_FH_ERROR}")
    # akShare 是可选增强，import 它要好几秒（会拉起 py_mini_racer 等依赖）。
    # 云端部署要求「服务立刻监听 PORT」，所以这里放后台线程预检，不阻塞启动。
    threading.Thread(target=_ak_probe, daemon=True).start()

    class SingleInstanceServer(ThreadingHTTPServer):
        # 关闭端口复用：否则第二个实例会静默绑定同一端口，导致请求命中旧进程、改代码不生效
        allow_reuse_address = False

    try:
        srv = SingleInstanceServer((args.host, args.port), Handler)
    except OSError as exc:
        print(f"[错误] 无法绑定 {args.host}:{args.port} —— {exc}")
        print("端口可能已被占用。请先结束占用进程，或用 --port 换一个端口。")
        return 1

    print(f"期货分析工作台已启动: http://{args.host}:{args.port}", flush=True)
    print(f"静态目录: {STATIC_DIR}", flush=True)
    _maybe_open_browser(f"http://127.0.0.1:{args.port}/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
