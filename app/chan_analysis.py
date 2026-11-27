"""缠论(Chan Theory)分析适配层。

把工作台已有的 K 线喂给 vendored 的 chan.py 官方开源版
(Vespa314/chan.py, 见 vendor/chanpy/LICENSE)，再把算出来的
分型/笔/线段/中枢/形态学买卖点翻译成前端能直接画在 K 线上的坐标。

设计要点
--------
1. **不重复造数据**：K 线仍走工作台自己的新浪接口，只在 chan.py 侧做适配。
   chan.py 原本内置 baostock/akshare/ccxt/csv 四种源，前三种都不覆盖国内
   期货，csv 源要求文件落在 DataAPI 目录下 —— 都不好用，所以实现了
   CChanDataSource 继承 CCommonStockApi 直接喂内存数据。
2. **只取坐标，不取画图**：chan.py 自带的 matplotlib 绑图不适合嵌到网页，
   这里只把每个元素的 (K 线序号, 价格) 抽出来，交给前端 ECharts 渲染。
3. **降级不崩**：chan.py 对数据格式要求较严（时间不能出现 0 点 0 分、
   OHLC 顺序不能反），任何异常都吞掉并返回明确 error，前端显示原因。

坐标系约定（前端用）
--------------------
- `bi`：笔，{idx, dir(up/down), start_idx, end_idx, start_price, end_price, is_sure}
  画法：从 (start_idx, start_price) 到 (end_idx, end_price) 的直线
- `seg`：线段，同上结构
- `zs`：中枢，{idx, zg(上沿), zd(下沿), start_idx, end_idx, start_price,
  end_price, is_sure, bi_count} 画法：矩形，横向从 start_idx 到 end_idx，
  纵向从 zd 到 zg
- `bsp`：买卖点，{idx, is_buy, types:["1","2",...], price, klu_idx, time}
  画法：三角箭头 + 文字标签
"""

from __future__ import annotations

import os
import sys
import threading
from datetime import datetime

VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "chanpy")

# chan.py 内部用 `from Common.CEnum import ...` 这类平铺导入，
# 必须把它的根目录塞进 sys.path，否则无法作为包导入。
if VENDOR_DIR not in sys.path:
    sys.path.insert(0, VENDOR_DIR)

_IMPORT_ERROR: str | None = None
try:
    from Chan import CChan
    from ChanConfig import CChanConfig
    from Common.CEnum import AUTYPE, DATA_FIELD, DATA_SRC, KL_TYPE
    from Common.CTime import CTime
    from DataAPI.CommonStockAPI import CCommonStockApi
    from KLine.KLine_Unit import CKLine_Unit
except Exception as exc:  # pragma: no cover - 环境缺依赖时的兜底
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


# chan.py 级别枚举：日线
_KL_DAY = KL_TYPE.K_DAY if _IMPORT_ERROR is None else None


class CChanDataSource(CCommonStockApi):
    """把内存中的 K 线列表包装成 chan.py 能消费的数据源。

    chan.py 要求每个数据源实现 get_kl_data() 生成器，逐根吐出 CKLine_Unit。
    """

    # 由 _register_data_source 注入的当前数据（chan.py 实例化时会重新读取）
    CURRENT_ROWS: list = []
    # 当前这批数据是不是分钟级（决定 CTime 的 auto 行为）
    CURRENT_INTRADAY: bool = False

    def __init__(self, code="futures", k_type=None, begin_date=None,
                 end_date=None, autype=None):
        # 注意：不走 CCommonStockApi.__init__，因为它会调用 SetBasciInfo()，
        # 而我们不需要联网查基础信息。
        self.code = code
        self.name = code
        self.is_stock = False
        self.k_type = k_type if k_type is not None else _KL_DAY
        self.begin_date = begin_date
        self.end_date = end_date
        self.autype = autype
        self._rows = list(CChanDataSource.CURRENT_ROWS)

    def get_kl_data(self):
        intraday = CChanDataSource.CURRENT_INTRADAY
        for r in self._rows:
            yield CKLine_Unit({
                DATA_FIELD.FIELD_TIME: CTime(
                    r["year"], r["month"], r["day"], r.get("hour", 0), r.get("minute", 0),
                    # auto=True 会把 00:00 映射到当天 23:59。对日线正好（当天最后一刻），
                    # 但对分钟线的 00:00 会造成时间逆序 → chan.py 报 "kline time err"。
                    # 所以分钟线整批关掉 auto。
                    auto=not intraday),
                DATA_FIELD.FIELD_OPEN: r["open"],
                DATA_FIELD.FIELD_HIGH: r["high"],
                DATA_FIELD.FIELD_LOW: r["low"],
                DATA_FIELD.FIELD_CLOSE: r["close"],
            })

    def SetBasciInfo(self):  # noqa: N802 - 框架要求的方法名
        pass

    @classmethod
    def do_init(cls):
        pass

    @classmethod
    def do_close(cls):
        pass


class _CChanWithMemorySource(CChan):
    """CChan 子类：把数据源固定为内存版。

    chan.py 的自定义源要求 `custom:DataAPI模块名.类名`，必须把类文件放进
    vendor 目录下的 DataAPI/ 里 —— 那会污染第三方代码。改为覆写
    GetStockAPI() 直接返回我们的类，效果等价且不动 vendor。
    """

    def GetStockAPI(self):  # noqa: N802 - 框架要求的方法名
        return CChanDataSource


def _register_data_source(rows: list[dict], intraday: bool = False) -> None:
    """把当前 K 线注入数据源类，供 chan.py 实例化时读取。"""
    CChanDataSource.CURRENT_ROWS = list(rows)
    CChanDataSource.CURRENT_INTRADAY = intraday


def _parse_datetime(text: str) -> tuple[int, int, int, int, int]:
    """解析工作台 K 线的时间字段。

    工作台的时间格式：
      - 日线："2026-11-25"
      - 分钟线："2026-11-25 21:30:00"（也可能带毫秒）

    ⚠️ 分钟线必须保留时分——chan.py 会校验时间递增，同一天多根 K 线
    如果时间戳相同会直接报 "kline time err"。日线则强制归零时分，
    避免日线里残留 21:30 这类时间戳。
    """
    s = (text or "").strip()
    if not s:
        raise ValueError("K 线缺少时间字段")

    date_part, time_part = s, ""
    if " " in s:
        date_part, time_part = s.split(" ", 1)
    elif "T" in s:
        date_part, time_part = s.split("T", 1)
        time_part = time_part.replace("Z", "")

    parts = date_part.split("-")
    if len(parts) < 3:
        raise ValueError(f"无法解析时间: {text}")
    year, month, day = int(parts[0]), int(parts[1]), int(parts[2])

    hour = minute = 0
    if time_part:
        tp = time_part.split(":")
        try:
            hour = int(tp[0])
            minute = int(tp[1]) if len(tp) > 1 else 0
        except ValueError:
            hour = minute = 0

    # 日线：时间统一归零；分钟线：保留时分
    if hour == 0 and minute == 0:
        return year, month, day, 0, 0
    return year, month, day, hour, minute


def _is_intraday(rows: list[dict]) -> bool:
    """判断这批 K 线是不是分钟级（时间字段带时分）。"""
    for r in rows[:5]:
        if " " in (r.get("datetime") or "") or "T" in (r.get("datetime") or ""):
            return True
    return False


def _to_chan_rows(rows: list[dict]) -> list[dict]:
    """把工作台 K 线行转成 chan.py 需要的时间分量形式。"""
    out = []
    intraday = _is_intraday(rows)
    seen_times = set()
    for r in rows:
        y, mo, d, h, mi = _parse_datetime(r.get("datetime", ""))
        if not intraday:
            h = mi = 0
        # chan.py 校验时间严格递增：同一时刻出现两根 K 线会直接报错。
        # 新浪接口偶尔返回重复时间戳（夜盘切日等边界），这里按时间去重，
        # 保留后出现的那根（通常是数据更完整的一根）。
        tkey = (y, mo, d, h, mi)
        if tkey in seen_times:
            dup = True
        else:
            seen_times.add(tkey)
            dup = False
        o = float(r.get("open") or 0)
        hi = float(r.get("high") or 0)
        lo = float(r.get("low") or 0)
        c = float(r.get("close") or 0)
        if not (o and hi and lo and c):
            continue
        # chan.py 内部要求 high >= max(open, close) >= min(open, close) >= low
        hi = max(hi, o, c)
        lo = min(lo, o, c)
        item = {
            "year": y, "month": mo, "day": d, "hour": h, "minute": mi,
            "open": o, "high": hi, "low": lo, "close": c,
        }
        if dup:
            # 覆盖前一根同时间的记录，保持顺序不变
            for i in range(len(out) - 1, -1, -1):
                if (out[i]["year"], out[i]["month"], out[i]["day"],
                        out[i]["hour"], out[i]["minute"]) == tkey:
                    out[i] = item
                    break
            continue
        out.append(item)
    return out


def _klu_index_map(kl_list, intraday: bool = False) -> dict:
    """建立 原始K线idx -> 时间字符串 的映射，供前端对齐坐标轴。

    注意 chan.py 有两层结构：
      - CKLine_Unit：原始 K 线（前端画的就是这个）
      - CKLine：按"包含关系"合并后的 K 线
    kl_list.lst 里存的是合并 K 线，原始 K 线藏在每根合并 K 线的 .lst 里，
    必须展开一层才能拿到原始 K 线的 idx 与时间。
    笔/线段/中枢引用的多是合并 K 线的 idx，所以坐标一律用
    bi.get_begin_klu() / get_end_klu() 换算回原始 K 线 idx，
    否则前端画的位置会整体错位。
    """
    out = {}
    for klc in kl_list.lst:
        try:
            for klu in klc.lst:
                t = klu.time
                # 分钟线必须带时分：夜盘会有 00:00 的 bar（1h/30m 周期常见），
                # 若这里只输出日期，前端按时间对齐时就找不到这根 K 线，
                # 落在它上面的笔/线段/买卖点会被整段丢弃。
                if intraday:
                    out[int(klu.idx)] = f"{t.year:04d}-{t.month:02d}-{t.day:02d} {t.hour:02d}:{t.minute:02d}"
                else:
                    out[int(klu.idx)] = f"{t.year:04d}-{t.month:02d}-{t.day:02d}"
        except Exception:
            continue
    return out


def _bi_payload(bi, kl_time_map: dict) -> dict:
    """笔 -> 前端可画的线段坐标（索引落在原始 K 线上）。"""
    try:
        begin_klu = bi.get_begin_klu()
        end_klu = bi.get_end_klu()
        start_idx = int(begin_klu.idx)
        end_idx = int(end_klu.idx)
        start_price = float(bi.get_begin_val())
        end_price = float(bi.get_end_val())
    except Exception:
        return {}
    return {
        "idx": int(getattr(bi, "idx", 0)),
        "dir": "up" if bi.is_up() else "down",
        "start_idx": start_idx,
        "end_idx": end_idx,
        "start_price": start_price,
        "end_price": end_price,
        "start_time": kl_time_map.get(start_idx),
        "end_time": kl_time_map.get(end_idx),
        "is_sure": bool(getattr(bi, "is_sure", True)),
        "amp": round(float(bi.amp()), 4),
    }


def _seg_payload(seg, kl_time_map: dict) -> dict:
    """线段 -> 前端可画的线段坐标（索引落在原始 K 线上）。"""
    try:
        begin_klu = seg.get_begin_klu()
        end_klu = seg.get_end_klu()
        start_idx = int(begin_klu.idx)
        end_idx = int(end_klu.idx)
        start_price = float(seg.get_begin_val())
        end_price = float(seg.get_end_val())
    except Exception:
        return {}
    return {
        "idx": int(getattr(seg, "idx", 0)),
        "dir": "up" if seg.is_up() else "down",
        "start_idx": start_idx,
        "end_idx": end_idx,
        "start_price": start_price,
        "end_price": end_price,
        "start_time": kl_time_map.get(start_idx),
        "end_time": kl_time_map.get(end_idx),
        "is_sure": bool(getattr(seg, "is_sure", True)),
    }


def _zs_payload(zs, kl_time_map: dict, zs_idx: int) -> dict:
    """中枢 -> 前端可画的矩形（横向索引落在原始 K 线上）。"""
    try:
        begin_klu = zs.begin_bi.get_begin_klu()
        end_klu = zs.end_bi.get_end_klu()
        start_idx = int(begin_klu.idx)
        end_idx = int(end_klu.idx)
        zg = float(zs.high)
        zd = float(zs.low)
        bi_count = int(zs.end_bi.idx - zs.begin_bi.idx + 1)
    except Exception:
        return {}
    return {
        "idx": zs_idx,
        "zg": zg,
        "zd": zd,
        "mid": round((zg + zd) / 2, 4),
        "start_idx": start_idx,
        "end_idx": end_idx,
        "start_time": kl_time_map.get(start_idx),
        "end_time": kl_time_map.get(end_idx),
        "bi_count": bi_count,
        "is_sure": bool(getattr(zs, "is_sure", True)),
    }


def _bsp_payload(bsp, kl_time_map: dict) -> dict:
    """买卖点 -> 箭头标注。"""
    try:
        klu = bsp.klu
        idx = int(klu.idx)
        # 买点标在当根最低价、卖点标在最高价，视觉上更贴近真实成交位置
        price = float(klu.low) if bsp.is_buy else float(klu.high)
    except Exception:
        return {}
    types = [t.value for t in (bsp.type or [])]
    return {
        "types": types,
        "is_buy": bool(bsp.is_buy),
        "label": ("B" if bsp.is_buy else "S") + ("".join(types) if types else ""),
        "klu_idx": idx,
        "price": price,
        "time": kl_time_map.get(idx),
        "is_segbsp": bool(getattr(bsp, "is_segbsp", False)),
    }


def analyze(rows: list[dict], min_bars: int = 60) -> dict:
    """对 K 线做缠论分析。

    返回 {"bi": [...], "seg": [...], "zs": [...], "bsp": [...], "stats": {...}}
    或 {"error": "..."}。
    """
    if _IMPORT_ERROR:
        return {"error": f"chan.py 导入失败：{_IMPORT_ERROR}"}

    chan_rows = _to_chan_rows(rows)
    if len(chan_rows) < min_bars:
        return {"error": f"K 线样本不足（需要至少 {min_bars} 根，当前 {len(chan_rows)} 根），缠论无法成笔"}

    # 背驰过滤放到较宽松的位置：期货日内波动大，默认 0.5 会把大部分点滤掉，
    # 导致"看不到买卖点"，反而失去了标注的意义。用 inf = 不筛背驰，全部标出，
    # 由用户自己在图上判断。
    config = CChanConfig({
        "bi_strict": True,
        "bi_fx_check": "strict",
        "bi_algo": "normal",
        "seg_algo": "chan",
        "zs_algo": "normal",
        "zs_combine": True,
        "divergence_rate": float("inf"),
        "min_zs_cnt": 0,
        "bs_type": "1,1p,2,2s,3a,3b",
        "bsp1_only_multibi_zs": False,
        "bsp2_follow_1": True,
        "bsp3_follow_1": True,
        "print_warning": False,
        "print_err_time": False,
    })

    _register_data_source(chan_rows, _is_intraday(rows))

    try:
        chan = _CChanWithMemorySource(
            code="futures",
            data_src=DATA_SRC.CSV,   # 占位，实际由 GetStockAPI 覆写决定
            lv_list=[_KL_DAY],
            config=config,
            autype=AUTYPE.NONE,
        )
    except Exception as exc:
        return {"error": f"chan.py 计算失败：{type(exc).__name__}: {exc}"}

    try:
        # chan[0] 依赖 lv_list 顺序，而 load() 内部会重写 lv_list（跳过非法级别），
        # 索引可能对不上。这里直接按 KL_TYPE 取 kl_datas，最稳。
        kl_list = chan.kl_datas[_KL_DAY]
    except Exception as exc:
        return {"error": f"chan.py 结果读取失败：{type(exc).__name__}: {exc}"}
    if kl_list is None:
        return {"error": "chan.py 未产出该级别的计算结果"}

    kl_time_map = _klu_index_map(kl_list, _is_intraday(rows))

    bi_list, seg_list, zs_list, bsp_list = [], [], [], []
    try:
        for bi in kl_list.bi_list:
            p = _bi_payload(bi, kl_time_map)
            if p:
                bi_list.append(p)
    except Exception:
        pass
    try:
        for seg in kl_list.seg_list:
            p = _seg_payload(seg, kl_time_map)
            if p:
                seg_list.append(p)
    except Exception:
        pass
    try:
        for i, zs in enumerate(kl_list.zs_list):
            p = _zs_payload(zs, kl_time_map, i)
            if p:
                zs_list.append(p)
    except Exception:
        pass
    try:
        # bs_point_lst 不是可迭代对象（只实现了 __len__），要用它自己的 bsp_iter()
        for bsp in kl_list.bs_point_lst.bsp_iter():
            p = _bsp_payload(bsp, kl_time_map)
            if p:
                bsp_list.append(p)
    except Exception:
        pass

    # 买卖点按 K 线序号排序，前端画图更稳
    bsp_list.sort(key=lambda x: x.get("klu_idx", 0))

    return {
        "bi": bi_list,
        "seg": seg_list,
        "zs": zs_list,
        "bsp": bsp_list,
        "stats": {
            "bars": len(chan_rows),
            "bi_count": len(bi_list),
            "seg_count": len(seg_list),
            "zs_count": len(zs_list),
            "bsp_count": len(bsp_list),
            "buy_count": sum(1 for b in bsp_list if b["is_buy"]),
            "sell_count": sum(1 for b in bsp_list if not b["is_buy"]),
            "first_time": kl_time_map.get(0),
            "last_time": kl_time_map.get(len(chan_rows) - 1),
        },
    }


# 缠论计算较重（300 根日线约 0.1~0.5 秒），加一层进程内缓存
_cache: dict = {}
_cache_lock = threading.Lock()
CACHE_TTL = 300  # 秒


def analyze_cached(rows: list[dict], key: str = "", min_bars: int = 60) -> dict:
    ck = key or f"{len(rows)}|{rows[-1].get('datetime', '') if rows else ''}"
    now = datetime.now().timestamp()
    with _cache_lock:
        hit = _cache.get(ck)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]

    result = analyze(rows, min_bars=min_bars)
    with _cache_lock:
        _cache[ck] = (now, result)
        if len(_cache) > 64:
            # 简单淘汰最旧的
            oldest = min(_cache.items(), key=lambda kv: kv[1][0])
            _cache.pop(oldest[0], None)
    return result


def available() -> bool:
    return _IMPORT_ERROR is None
