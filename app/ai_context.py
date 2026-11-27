"""AI 研判上下文构建。

把「自动基本面采集结果」「技术面信号」「人工判断」打包成一份**可追溯**的
证据清单，并给出两套提示词（基本面解读 / 综合诊断），供前端直连大模型使用。

设计原则（沿用 skill 的研究纪律）：
- 每条读数都带来源、等级、数据日期，模型不得引入清单之外的事实。
- 客观读数与人工判断分区呈现，不混为一谈。
- 数据缺口显式列出，不让模型用推测填补。
- 模型只做叙述与对照，不产出数值填补、不替代人工决策。
"""

from __future__ import annotations

import hashlib
import json

# ---------------------------------------------------------------- 提示词

COMMON_RULES = """你是一名中国商品期货的基本面研究员，正在为研究工作台生成研判文字。

必须遵守的纪律：
1. 只使用【数据】中给出的内容。任何数字都必须能在数据中找到出处，不得推算、不得补齐、不得引入外部知识里的行情。
2. 每条结论都要能追溯到具体读数；写结论时用"数据显示…因此…"的句式，把依据和判断分开。
3. 数据缺口、来源等级低的项，要在"局限"里明确写出，不要用推测填补。
4. 区分「事实」与「推断」：事实是数据本身，推断是基于事实的解释，推断要标注为推断。
5. 不要给出具体买卖点位、目标价或仓位建议；只描述供需与盘面状态。
6. 输出必须是合法 JSON，不要包裹 markdown 代码块，不要输出 JSON 以外的任何文字。
7. 【数据缺口】里列出的项一律写「未获取」。严禁把缺失写成 0、写成「无变化」或任何带数值/趋势的描述；
   清单之外的品种、地区、国家、机构、新闻事件一律不得出现（例如不要提到油脂、农产品或境外产地）。
8. 若【客观基本面评分】存在，结论里的倾向与评分必须逐字引用（如「偏多（评分 4）」）；
   严禁另造「中性」「评分 0」等与清单冲突的结论。若清单里没有评分，就写「未生成评分」。
9. 【唯一允许引用的数值】是本次采集的全部数值。逐条复述时必须用这些原值，
   不得改写、不得补零、不得把「未获取」换算成 0 手。"""

FUNDAMENTAL_SYSTEM = COMMON_RULES + """

本次任务：解读【自动基本面数据】。
输出 JSON 结构：
{
  "observed": "客观数据综述：逐条复述关键读数（库存/仓单/基差/持仓），带数值与数据日期",
  "analysis": "分析：这些读数指向怎样的供需状态，推理链要说清楚",
  "conclusion": "基本面倾向结论：偏多/偏空/中性，并说明置信度受什么限制",
  "caveats": "口径与局限：含税口径、数据缺口、来源等级、以及需要人工核验的点"
}"""

DIAGNOSE_SYSTEM = COMMON_RULES + """

本次任务：对【技术面 / 客观基本面 / 人工判断】做三方对照诊断。
输出 JSON 结构：
{
  "observed": "三方现状：技术面读到什么、客观基本面读到什么、人工判断是什么",
  "process": "分析过程：按步骤说明如何对照、哪里一致、哪里冲突、冲突说明什么",
  "conclusion": "综合结论：当前结论应如何表述（含是否需要降级）",
  "actions": ["行动建议1", "行动建议2", "行动建议3"],
  "caveats": "口径与局限：哪些地方证据不足、哪些必须人工核实"
}"""


# ---------------------------------------------------------------- 工具

def _f(v):
    """浮点数格式化，避免 1.0 这种尾巴。"""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(f - round(f)) < 1e-9:
        return f"{int(round(f)):,}"
    return f"{f:,.2f}"


def _block_head(blk: dict, title: str) -> str:
    return f"### {title}｜来源：{blk.get('source', '—')}｜等级：{blk.get('grade', '—')}｜数据日期：{blk.get('asof', '—')}"


def _receipt_text(blk: dict) -> str:
    v = blk["value"]
    lines = [_block_head(blk, "交割仓单")]
    lines.append(f"- 仓单总量 {_f(v.get('total'))} 手，日变化 {_f(v.get('delta'))} 手")
    if v.get("forecast") is not None:
        lines.append(f"- 有效预报 {_f(v.get('forecast'))} 手")
    if v.get("premium") is not None:
        lines.append(f"- 升贴水 {v.get('premium')}")
    detail = v.get("detail") or []
    if detail:
        top = sorted(detail, key=lambda d: (d.get("qty") or 0), reverse=True)[:5]
        seg = "、".join(f"{d.get('name')}（{d.get('region') or '—'}）{_f(d.get('qty'))} 手" for d in top)
        lines.append(f"- 主要交割库分布：{seg}")
    if blk.get("note"):
        lines.append(f"- 备注：{blk['note']}")
    return "\n".join(lines)


def _inventory_text(blk: dict) -> str:
    v = blk["value"]
    lines = [_block_head(blk, "期货库存")]
    lines.append(f"- 最新库存 {_f(v.get('latest'))} 手（数据日 {v.get('asof', '—')}）")
    lines.append(f"- 5 日变化 {_f(v.get('chg_5d'))} 手，20 日变化 {_f(v.get('chg_20d'))} 手")
    ser = v.get("series") or []
    if len(ser) >= 2:
        lines.append(f"- 序列区间：{ser[0].get('date')} → {ser[-1].get('date')}，共 {len(ser)} 个观测")
    return "\n".join(lines)


def _basis_text(blk: dict) -> str:
    v = blk["value"]
    lines = [_block_head(blk, "现货与基差")]
    lines.append(f"- 现货价 {_f(v.get('spot'))} 元/吨（生意社口径，含增值税）")
    rate = v.get("dom_basis_rate_pct")
    rate_s = "—" if rate is None else f"{float(rate):+.2f}%"
    lines.append(
        f"- 主力 {v.get('dom_contract', '—')} 基差 {_f(v.get('dom_basis'))}（基差率 {rate_s}）"
    )
    lines.append(f"- 除税参考基差 {_f(v.get('dom_basis_ex_vat'))}")
    z = v.get("basis_z20")
    if z is not None:
        lines.append(f"- 基差率相对 20 日均值偏离 z = {float(z):+.2f}σ")
    if blk.get("note"):
        lines.append(f"- 备注：{blk['note']}")
    return "\n".join(lines)


def _position_text(blk: dict) -> str:
    v = blk["value"]
    lines = [_block_head(blk, "会员持仓结构")]
    r = v.get("long_short_ratio")
    lines.append(f"- 前 20 多空比 {r if r is None else round(r, 3)}")
    lines.append(f"- 前 20 多头合计 {_f(v.get('long_sum'))} 手，空头合计 {_f(v.get('short_sum'))} 手")
    lines.append(f"- 净持仓 {_f(v.get('net'))} 手（数据日 {v.get('asof', '—')}）")
    if blk.get("note"):
        lines.append(f"- 备注：{blk['note']}")
    return "\n".join(lines)


_BLOCK_RENDERERS = {
    "receipt": ("交割仓单", _receipt_text),
    "inventory": ("期货库存", _inventory_text),
    "basis": ("现货与基差", _basis_text),
    "position": ("会员持仓结构", _position_text),
}


def _fundamentals_section(fund: dict) -> tuple[str, list[str]]:
    """把采集到的四个数据块渲染成给模型看的文本。返回（正文, 缺口列表）。"""
    blocks = (fund or {}).get("blocks") or {}
    parts: list[str] = []
    gaps: list[str] = []
    for key, (title, fn) in _BLOCK_RENDERERS.items():
        blk = blocks.get(key)
        if not blk:
            gaps.append(f"{title}：数据块缺失")
            continue
        if not blk.get("ok"):
            gaps.append(f"{title}：{blk.get('error') or '不可用'}")
            continue
        try:
            parts.append(fn(blk))
        except Exception as exc:  # 单块渲染失败不应拖垮整体
            gaps.append(f"{title}：解析失败 {exc}")
    for g in (fund or {}).get("gaps") or []:
        gaps.append(str(g))
    return "\n\n".join(parts), gaps


def _auto_signal_text(auto: dict) -> str:
    if not auto:
        return "（客观基本面评分未生成）"
    lines = [f"客观基本面倾向：**{auto.get('label')}**（评分 {auto.get('score')}）"]
    for it in auto.get("items") or []:
        lines.append(f"- [{it.get('points', 0):+d}] {it.get('label')}：{it.get('detail')}")
    if auto.get("note"):
        lines.append(f"（{auto['note']}）")
    return "\n".join(lines)


def _tech_section(sig: dict) -> str:
    if not sig:
        return "（技术面信号未生成）"
    stats = sig.get("stats") or {}
    lines = [f"技术面倾向：**{sig.get('label')}**（评分 {sig.get('score')}）"]
    rows = [
        ("收盘价", _f(stats.get("close"))),
        ("近 20 日涨跌", f"{stats.get('ret20')}%" if stats.get("ret20") is not None else "—"),
        ("价格分位", f"{stats.get('percentile')}%" if stats.get("percentile") is not None else "—"),
        ("ATR", f"{_f(stats.get('atr'))}（{stats.get('atr_pct')}%）"),
        ("持仓变动", _f(stats.get("oi_delta"))),
    ]
    for k, v in rows:
        lines.append(f"- {k}：{v}")
    items = sig.get("items") or []
    if items:
        lines.append("信号明细：")
        for it in items:
            lines.append(f"- [{it.get('points', 0):+d}] {it.get('label')}：{it.get('detail')}")
    levels = sig.get("levels") or {}
    if levels:
        lv = "、".join(f"{k} {v}" for k, v in levels.items())
        lines.append(f"关键价位：{lv}")
    return "\n".join(lines)


def _research_section(rec: dict) -> str:
    if not rec:
        return "人工判断：**尚未录入**（决策面板未填写倾向、时间边界与否决权变量状态）"
    lines = [
        f"人工判断：倾向 **{rec.get('bias') or '未判定'}**",
        f"- 时间边界：{rec.get('horizon') or '未界定'}",
        f"- 置信度：{rec.get('confidence') or '未定'}",
    ]
    veto = rec.get("veto_status") or {}
    if veto:
        lines.append("- 否决权变量状态：" + "、".join(f"{k}={v}" for k, v in veto.items()))
    unv = rec.get("unverified") or []
    if unv:
        lines.append("- 未验证信息：" + "、".join(unv[:8]))
    notes = rec.get("notes")
    if notes:
        lines.append(f"- 人工备注：{notes}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 数据指纹
#
# AI 研判是浏览器端调模型生成的纯文本，和面板之间没有任何绑定：
# 面板重新采集后，旧研判会原样留在页面上，用户就会看到「两个地方数字对不上」。
# 因此这里对「数据内容本身」（数据日期 + 各块读数 + 客观评分）算一个稳定指纹，
# 前端把它贴在 AI 结果上，面板数据一变就能立刻判定这份研判已过期。
#
# 注意：指纹不能用 fetched_at（每次请求都是当前时间，会导致研判永远显示过期）。

_FP_FIELDS = {
    "receipt": ("total", "delta", "asof"),
    "inventory": ("latest", "chg_5d", "chg_20d", "asof"),
    "basis": ("spot", "dom_basis", "dom_basis_rate_pct", "basis_z20", "asof"),
    "position": ("net", "long_sum", "short_sum", "long_short_ratio", "asof"),
}


def _data_snapshot(fund: dict) -> dict:
    """本次采集的机器可读快照：模型唯一允许引用的数值都在这里。"""
    blocks = (fund or {}).get("blocks") or {}
    snap: dict = {"blocks": {}, "gaps": [], "auto_signal": None}
    for key, fields in _FP_FIELDS.items():
        blk = blocks.get(key) or {}
        val = blk.get("value") or {}
        if not blk.get("ok"):
            snap["blocks"][key] = {"ok": False, "reason": blk.get("error") or "未获取"}
            continue
        snap["blocks"][key] = {
            "ok": True,
            "asof": blk.get("asof"),
            "source": blk.get("source"),
            "grade": blk.get("grade"),
            "values": {k: val.get(k) for k in fields[:-1]},
        }
    auto = (fund or {}).get("auto_signal") or {}
    snap["auto_signal"] = {
        "label": auto.get("label"),
        "score": auto.get("score"),
        "items": [it.get("label") for it in (auto.get("items") or [])],
    }
    snap["gaps"] = [str(g) for g in ((fund or {}).get("gaps") or [])]
    return snap


def _data_fingerprint(fund: dict) -> tuple[str, dict]:
    raw = json.dumps(_data_snapshot(fund), ensure_ascii=False,
                     sort_keys=True, default=str)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12], _data_snapshot(fund)


def data_fingerprint(fund: dict) -> str:
    """对外接口：/api/fundamentals 也返回同一指纹，前端据此判定 AI 研判是否过期。"""
    return _data_fingerprint(fund)[0]


def _key_numbers(snap: dict) -> list[str]:
    """给前端做「模型有没有复述关键读数」校验用的数值串。

    同时给出带千分位与不带千分位两种写法，模型格式不同也能匹配。
    """
    out: list[str] = []

    def add(v):
        if v is None:
            return
        try:
            f = float(v)
        except (TypeError, ValueError):
            return
        s = f"{f:,.0f}" if abs(f) >= 1000 else (f"{int(round(f)):,}" if abs(f - round(f)) < 1e-9 else f"{f:g}")
        plain = s.replace(",", "")
        for x in (s, plain):
            if x not in out:
                out.append(x)

    for key in ("receipt", "inventory", "basis", "position"):
        blk = snap["blocks"].get(key) or {}
        if not blk.get("ok"):
            continue
        vals = blk.get("values") or {}
        for field in ("total", "latest", "chg_5d", "chg_20d", "spot",
                      "dom_basis", "net", "long_sum", "short_sum"):
            add(vals.get(field))
    return out


# ---------------------------------------------------------------- 上下文构建

def fundamental_context(symbol: str, fund: dict, meta: dict | None = None) -> dict:
    """自动基本面解读用的上下文。"""
    fund = fund or {}
    body, gaps = _fundamentals_section(fund)
    auto = fund.get("auto_signal")
    head = (
        f"品种：{fund.get('name') or (meta or {}).get('name') or symbol}"
        f"（{symbol}，{(meta or {}).get('exchange', '—')}）"
        f"，主力合约 {fund.get('main_contract') or '—'}，采集时间 {fund.get('fetched_at') or '—'}"
    )
    fingerprint, snap = _data_fingerprint(fund)
    parts = [f"【自动基本面数据】\n{head}", body]
    if auto:
        parts.append("【客观基本面评分（由上述读数折算）】\n" + _auto_signal_text(auto))
    if gaps:
        parts.append("【数据缺口（不得用推测填补）】\n" + "\n".join(f"- {g}" for g in gaps))
    else:
        parts.append("【数据缺口】\n- 四个数据块均已获取，无显式缺口")
    # 机器可读附录：把本次采集的全部数值钉死，模型不得改写、不得补零
    parts.append(
        "【唯一允许引用的数值（JSON，必须逐条复述，未获取的项写「未获取」而非 0）】\n"
        + json.dumps(snap, ensure_ascii=False, indent=2, default=str)
    )
    return {
        "scope": "fundamental",
        "system": FUNDAMENTAL_SYSTEM,
        "user": "\n\n".join(p for p in parts if p),
        "data_fingerprint": fingerprint,
        "data_snapshot": snap,
        "key_numbers": _key_numbers(snap),
        "fetched_at": fund.get("fetched_at"),
        "main_contract": fund.get("main_contract"),
    }


def diagnose_context(symbol: str, sig: dict, fund: dict, rec: dict,
                     meta: dict | None = None) -> dict:
    """综合诊断（三方对照）用的上下文。"""
    fund = fund or {}
    name = (meta or {}).get("name") or fund.get("name") or symbol
    auto = fund.get("auto_signal")
    body, gaps = _fundamentals_section(fund)

    parts = [
        f"【标的】{name}（{symbol}）",
        "【一、技术面（盘面行为）】\n" + _tech_section(sig),
    ]
    if body:
        parts.append("【二、客观基本面数据（可追溯读数）】\n" + body)
    if auto:
        parts.append("【三、客观基本面评分（由读数折算）】\n" + _auto_signal_text(auto))
    parts.append("【四、人工判断（决策面板）】\n" + _research_section(rec))
    if gaps:
        parts.append("【数据缺口（不得用推测填补）】\n" + "\n".join(f"- {g}" for g in gaps))

    return {
        "scope": "diagnose",
        "system": DIAGNOSE_SYSTEM,
        "user": "\n\n".join(parts),
    }


def context_payload(scope: str, symbol: str, sig: dict, fund: dict, rec: dict,
                    meta: dict | None = None) -> dict:
    """按 scope 分发，并统一附上数据指纹（两个 scope 都要能判定研判是否过期）。"""
    if scope == "diagnose":
        ctx = diagnose_context(symbol, sig, fund, rec, meta)
    else:
        ctx = fundamental_context(symbol, fund, meta)
    if "data_fingerprint" not in ctx:
        fp, snap = _data_fingerprint(fund or {})
        ctx.update({
            "data_fingerprint": fp,
            "data_snapshot": snap,
            "key_numbers": _key_numbers(snap),
            "fetched_at": (fund or {}).get("fetched_at"),
        })
    return ctx


def to_json_text(obj: dict) -> str:
    """给模型看的机器可读附录（部分模型对结构化输入更敏感）。"""
    return json.dumps(obj, ensure_ascii=False, indent=2)
