#!/usr/bin/env python3
"""新浪期货历史数据抓取脚本。

用法：
    python3 futures_history.py MARKET SYMBOL DURATION [--count N]
    python3 futures_history.py MARKET SYMBOL DURATION [--format csv|json]

示例：
    python3 futures_history.py intl hf_OIL 5m
    python3 futures_history.py intl hf_GC 1d --format json
    python3 futures_history.py domestic rb2509 5m
    python3 futures_history.py domestic V2609 2d --count 200

symbol 规则：
    - MARKET 目前必须显式指定为 "intl" 或 "domestic"。
    - 外盘期货建议使用标准的 "hf_*" 代码，例如 "hf_OIL"、"hf_GC"。
      脚本也接受去掉 "hf_" 前缀后的原始代码，例如 "OIL"、"GC"。
    - 内盘期货请传新浪使用的合约代码，通常是“品种 + 交割年月”形式，
      例如 "rb2509"、"V2609"、"au2506"。
    - 内盘也接受可选的 "nf_" 前缀；若传入，脚本会在请求前自动去掉。

支持的外盘期货代码：
    - hf_CL: 纽约原油
    - hf_NG: 美国天然气
    - hf_OIL: 布伦特原油
    - hf_GC: 纽约黄金
    - hf_SI: 纽约白银
    - hf_XAU: 伦敦金（现货黄金）
    - hf_XAG: 伦敦银（现货白银）
    - hf_XPT: 纽约铂金
    - hf_XPD: 纽约钯金
    - hf_CAD: 伦铜
    - hf_AHD: 伦铝
    - hf_NID: 伦镍
    - hf_PBD: 伦铅
    - hf_SND: 伦锡
    - hf_ZSD: 伦锌
    - hf_HG: 美铜
    - hf_S: 美国大豆
    - hf_W: 美国小麦
    - hf_C: 美国玉米
    - hf_SM: 美黄豆粉
    - hf_BO: 美黄豆油
    - hf_RS: 美国原糖
    - hf_CT: 美国棉花
    - hf_LHC: 美瘦猪肉
    - hf_BTC: 比特币期货
    - hf_EUA: 欧洲碳排放
    - hf_FEF: 新加坡铁矿石
    - hf_FCPO: 马棕油
    - hf_RSS3: 日橡胶

输出说明：
    - 默认输出格式为 CSV，可用 "--format json" 切换为 JSON。
    - 结果中不输出 "id" 字段。
    - "datetime" 输出为字符串。
    - 日线及以上周期使用 "YYYY-MM-DD"。
    - 分钟/小时周期使用 "YYYY-MM-DD HH:MM:SS"。
    - 若整批结果里的 "open_interest" 全部为 0，则不输出该列。
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime as dt
import json
import re
import sys
import urllib.parse
import urllib.request
from collections import OrderedDict, defaultdict
from typing import Callable


SECONDS_PER_DAY = 86_400
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


@dataclasses.dataclass(frozen=True)
class KlineBar:
    id: int
    datetime: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    open_interest: float
    settlement_price: float | None = None

    @classmethod
    def from_values(
        cls,
        time_text: str,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        volume: float,
        open_interest: float,
        settlement_price: float | None = None,
        duration_seconds: int = SECONDS_PER_DAY,
    ) -> "KlineBar":
        datetime_ns = parse_datetime(time_text)
        bar_id = compute_bucket_id(datetime_ns, duration_seconds)
        return cls(
            id=bar_id,
            datetime=bar_id * duration_ns(duration_seconds),
            open=float(open_price),
            high=float(high_price),
            low=float(low_price),
            close=float(close_price),
            volume=float(volume),
            open_interest=float(open_interest),
            settlement_price=(
                float(settlement_price) if settlement_price is not None else None
            ),
        )


def parse_duration(value: str) -> int:
    raw = value.strip().lower()
    if raw.isdigit():
        seconds = int(raw)
        if seconds <= 0:
            raise ValueError("duration seconds must be > 0")
        return seconds

    match = re.fullmatch(r"(\d+)([mhd])", raw)
    if match is None:
        raise ValueError(
            "duration must be raw seconds or <n><unit> where unit is m, h, or d"
        )

    amount = int(match.group(1))
    if amount <= 0:
        raise ValueError("duration amount must be > 0")

    unit = match.group(2)
    if unit == "m":
        return amount * 60
    if unit == "h":
        return amount * 3_600
    return amount * SECONDS_PER_DAY


def parse_datetime(time_text: str) -> int:
    datetime_formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
    )
    for fmt in datetime_formats:
        try:
            parsed = dt.datetime.strptime(time_text, fmt)
        except ValueError:
            continue
        return int(parsed.replace(tzinfo=dt.timezone.utc).timestamp() * 1_000_000_000)

    date_formats = ("%Y-%m-%d", "%Y/%m/%d")
    for fmt in date_formats:
        try:
            parsed_date = dt.datetime.strptime(time_text, fmt).date()
        except ValueError:
            continue
        parsed = dt.datetime.combine(parsed_date, dt.time.min, tzinfo=dt.timezone.utc)
        return int(parsed.timestamp() * 1_000_000_000)

    normalized = time_text.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"unsupported datetime format: {time_text}") from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    else:
        parsed = parsed.astimezone(dt.timezone.utc)

    return int(parsed.timestamp() * 1_000_000_000)


def duration_ns(duration_seconds: int) -> int:
    return duration_seconds * 1_000_000_000


def compute_bucket_id(datetime_ns: int, duration_seconds: int) -> int:
    if duration_seconds <= 0:
        return 0
    return datetime_ns // duration_ns(duration_seconds)


def extract_json_array(text: str) -> str:
    start = text.find("=([")
    if start < 0:
        raise ValueError("no =([ pattern found in JSONP payload")

    array_start = start + 3
    depth = 1
    index = array_start

    while index < len(text):
        current = text[index]
        if current == "[":
            depth += 1
        elif current == "]":
            depth -= 1

        if depth == 0:
            break
        index += 1

    if depth != 0:
        raise ValueError("unmatched brackets in JSONP payload")

    return text[array_start:index]


def normalize_international_symbol(symbol: str) -> str:
    if symbol.startswith("hf_"):
        return symbol[3:]
    return symbol.upper()


def normalize_domestic_symbol(symbol: str) -> str:
    if symbol.startswith("nf_"):
        return symbol[3:]
    return symbol.upper()


def parse_optional_float(value: str | None) -> float | None:
    if value is None:
        return None
    return float(value)


def parse_international_minute_records(
    records: list[dict[str, str]], duration_seconds: int
) -> list[KlineBar]:
    bars: list[KlineBar] = []
    for record in records:
        try:
            datetime_ns = parse_datetime(record["d"])
            bar_id = compute_bucket_id(datetime_ns, duration_seconds)
            bars.append(
                KlineBar(
                    id=bar_id,
                    datetime=bar_id * duration_ns(duration_seconds),
                    open=float(record["o"]),
                    high=float(record["h"]),
                    low=float(record["l"]),
                    close=float(record["c"]),
                    volume=float(record["v"]),
                    open_interest=float(record.get("p", "0")),
                    settlement_price=None,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return bars


def parse_international_daily_records(records: list[dict[str, str]]) -> list[KlineBar]:
    bars: list[KlineBar] = []
    for record in records:
        try:
            datetime_ns = parse_datetime(record["date"])
            bar_id = compute_bucket_id(datetime_ns, SECONDS_PER_DAY)
            bars.append(
                KlineBar(
                    id=bar_id,
                    datetime=bar_id * duration_ns(SECONDS_PER_DAY),
                    open=float(record["open"]),
                    high=float(record["high"]),
                    low=float(record["low"]),
                    close=float(record["close"]),
                    volume=float(record["volume"]),
                    open_interest=float(record.get("position", "0")),
                    settlement_price=parse_optional_float(record.get("s")),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue

    bars.sort(key=lambda bar: bar.id)
    return bars


def parse_domestic_minute_records(
    records: list[dict[str, str]], duration_seconds: int
) -> list[KlineBar]:
    return parse_international_minute_records(records, duration_seconds)


def parse_domestic_daily_records(records: list[dict[str, str]]) -> list[KlineBar]:
    bars: list[KlineBar] = []
    for record in records:
        try:
            datetime_ns = parse_datetime(record["d"])
            bar_id = compute_bucket_id(datetime_ns, SECONDS_PER_DAY)
            bars.append(
                KlineBar(
                    id=bar_id,
                    datetime=bar_id * duration_ns(SECONDS_PER_DAY),
                    open=float(record["o"]),
                    high=float(record["h"]),
                    low=float(record["l"]),
                    close=float(record["c"]),
                    volume=float(record["v"]),
                    open_interest=float(record.get("p", "0")),
                    settlement_price=parse_optional_float(record.get("s")),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue

    bars.sort(key=lambda bar: bar.id)
    return bars


def aggregate_daily_bars(bars: list[KlineBar], duration_seconds: int) -> list[KlineBar]:
    if duration_seconds < SECONDS_PER_DAY or duration_seconds % SECONDS_PER_DAY != 0:
        raise ValueError(
            "duration_seconds must be a multiple of 86400 for daily-or-higher periods"
        )

    if duration_seconds == SECONDS_PER_DAY:
        deduped: list[KlineBar] = []
        seen_ids: set[int] = set()
        for bar in sorted(bars, key=lambda item: item.id):
            if bar.id in seen_ids:
                continue
            seen_ids.add(bar.id)
            deduped.append(bar)
        return deduped

    duration_days = duration_seconds // SECONDS_PER_DAY
    buckets: dict[int, "OrderedDict[int, KlineBar]"] = defaultdict(OrderedDict)

    for bar in bars:
        bucket_id = compute_bucket_id(bar.datetime, duration_seconds)
        day_id = compute_bucket_id(bar.datetime, SECONDS_PER_DAY)
        buckets[bucket_id][day_id] = bar

    aggregated: list[KlineBar] = []
    for bucket_id, day_bars in sorted(buckets.items()):
        if len(day_bars) != duration_days:
            continue

        ordered_bars = [day_bars[key] for key in sorted(day_bars)]
        first = ordered_bars[0]
        last = ordered_bars[-1]
        aggregated.append(
            KlineBar(
                id=bucket_id,
                datetime=bucket_id * duration_ns(duration_seconds),
                open=first.open,
                high=max(bar.high for bar in ordered_bars),
                low=min(bar.low for bar in ordered_bars),
                close=last.close,
                volume=sum(bar.volume for bar in ordered_bars),
                open_interest=last.open_interest,
                settlement_price=None,
            )
        )

    return aggregated


FetchText = Callable[[str, dict[str, str]], str]


def default_fetch_text(url: str, headers: dict[str, str]) -> str:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_jsonp_records(
    url: str,
    headers: dict[str, str],
    max_attempts: int,
    fetch_text: FetchText,
) -> list[dict[str, str]]:
    for _ in range(max(max_attempts, 1)):
        text = fetch_text(url, headers)
        raw = extract_json_array(text)
        records = json.loads(f"[{raw}]")
        if records:
            return records
    return []


def format_output_datetime(datetime_ns: int, duration_seconds: int) -> str:
    timestamp = dt.datetime.fromtimestamp(
        datetime_ns / 1_000_000_000, tz=dt.timezone.utc
    )
    if duration_seconds >= SECONDS_PER_DAY:
        return timestamp.strftime("%Y-%m-%d")
    return timestamp.strftime("%Y-%m-%d %H:%M:%S")


def bar_to_dict(
    bar: KlineBar,
    duration_seconds: int,
    include_open_interest: bool = True,
) -> dict[str, str | float]:
    payload: dict[str, str | float] = {
        "datetime": format_output_datetime(bar.datetime, duration_seconds),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
    }
    if include_open_interest:
        payload["open_interest"] = bar.open_interest
    if bar.settlement_price is not None:
        payload["settlement_price"] = bar.settlement_price
    return payload


def serialize_bars(bars: list[KlineBar], duration_seconds: int) -> list[dict[str, str | float]]:
    include_open_interest = any(bar.open_interest != 0.0 for bar in bars)
    return [
        bar_to_dict(
            bar,
            duration_seconds,
            include_open_interest=include_open_interest,
        )
        for bar in bars
    ]


def print_csv(rows: list[dict[str, str | float]]) -> None:
    fieldnames = [
        "datetime",
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]
    if any("open_interest" in row for row in rows):
        fieldnames.append("open_interest")
    if any("settlement_price" in row for row in rows):
        fieldnames.append("settlement_price")

    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)


def fetch_history(
    market: str,
    symbol: str,
    duration_seconds: int,
    count: int | None = None,
    max_attempts: int = 3,
    fetch_text: FetchText | None = None,
) -> list[KlineBar]:
    if market not in {"intl", "domestic"}:
        raise ValueError("market must be one of: intl, domestic")

    fetch = fetch_text or default_fetch_text
    if market == "intl":
        bars = fetch_international_history(
            symbol=symbol,
            duration_seconds=duration_seconds,
            max_attempts=max_attempts,
            fetch_text=fetch,
        )
    else:
        bars = fetch_domestic_history(
            symbol=symbol,
            duration_seconds=duration_seconds,
            max_attempts=max_attempts,
            fetch_text=fetch,
        )

    if count is None:
        return bars
    if count < 0:
        raise ValueError("count must be >= 0")
    if count == 0:
        return []
    return bars[-count:]


def fetch_international_history(
    symbol: str,
    duration_seconds: int,
    max_attempts: int,
    fetch_text: FetchText,
) -> list[KlineBar]:
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be > 0")

    if duration_seconds >= SECONDS_PER_DAY:
        daily = fetch_international_daily_history(symbol, max_attempts, fetch_text)
        bars = aggregate_daily_bars(daily, duration_seconds)
        if not bars:
            raise ValueError("history data unavailable")
        return bars

    if duration_seconds % 60 != 0:
        raise ValueError("intraday duration_seconds must be a multiple of 60")

    period_minutes = duration_seconds // 60
    return fetch_international_mink_history(
        symbol=symbol,
        period_minutes=period_minutes,
        max_attempts=max_attempts,
        fetch_text=fetch_text,
    )


def fetch_international_mink_history(
    symbol: str,
    period_minutes: int,
    max_attempts: int,
    fetch_text: FetchText,
) -> list[KlineBar]:
    code = normalize_international_symbol(symbol)
    var_name = f"_{code}_{period_minutes}_{len(code)}"
    encoded_var_name = urllib.parse.quote(var_name, safe="")
    base_url = (
        "https://gu.sina.cn/ft/api/jsonp.php/"
        f"var%20{encoded_var_name}%3D/GlobalService.getMink"
    )
    query = urllib.parse.urlencode({"symbol": code, "type": str(period_minutes)})
    url = f"{base_url}?{query}"
    headers = {
        "Accept": "*/*",
        "Referer": f"https://finance.sina.com.cn/futures/quotes/{code}.shtml",
        "User-Agent": USER_AGENT,
    }

    duration_seconds = period_minutes * 60
    records = fetch_jsonp_records(url, headers, max_attempts, fetch_text)
    if records:
        bars = parse_international_minute_records(records, duration_seconds)
        if bars:
            return bars

    raise ValueError("history data unavailable")


def fetch_international_daily_history(
    symbol: str,
    max_attempts: int,
    fetch_text: FetchText,
) -> list[KlineBar]:
    code = normalize_international_symbol(symbol)
    now = dt.datetime.now(dt.timezone.utc).date()
    cache_buster = f"{now.year}_{now.month}_{now.day}"
    var_name = f"_{code}{cache_buster}"
    encoded_var_name = urllib.parse.quote(var_name, safe="")
    base_url = (
        "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
        f"var%20{encoded_var_name}=/GlobalFuturesService.getGlobalFuturesDailyKLine"
    )
    query = urllib.parse.urlencode(
        {"symbol": code, "_": cache_buster, "source": "web"}
    )
    url = f"{base_url}?{query}"
    headers = {
        "Accept": "*/*",
        "Referer": f"https://finance.sina.com.cn/futures/quotes/{code}.shtml",
        "User-Agent": USER_AGENT,
    }

    records = fetch_jsonp_records(url, headers, max_attempts, fetch_text)
    if records:
        bars = parse_international_daily_records(records)
        if bars:
            return bars

    raise ValueError("history data unavailable")


def fetch_domestic_history(
    symbol: str,
    duration_seconds: int,
    max_attempts: int,
    fetch_text: FetchText,
) -> list[KlineBar]:
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be > 0")

    if duration_seconds >= SECONDS_PER_DAY:
        daily = fetch_domestic_daily_history(symbol, max_attempts, fetch_text)
        bars = aggregate_daily_bars(daily, duration_seconds)
        if not bars:
            raise ValueError("history data unavailable")
        return bars

    if duration_seconds % 60 != 0:
        raise ValueError("intraday duration_seconds must be a multiple of 60")

    period_minutes = duration_seconds // 60
    return fetch_domestic_mink_history(
        symbol=symbol,
        period_minutes=period_minutes,
        max_attempts=max_attempts,
        fetch_text=fetch_text,
    )


def fetch_domestic_mink_history(
    symbol: str,
    period_minutes: int,
    max_attempts: int,
    fetch_text: FetchText,
) -> list[KlineBar]:
    code = normalize_domestic_symbol(symbol)
    nonce = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    var_name = f"_{code}_{period_minutes}_{nonce}"
    encoded_var_name = urllib.parse.quote(var_name, safe="")
    base_url = (
        "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
        f"var%20{encoded_var_name}=/InnerFuturesNewService.getFewMinLine"
    )
    query = urllib.parse.urlencode({"symbol": code, "type": str(period_minutes)})
    url = f"{base_url}?{query}"
    headers = {
        "Accept": "*/*",
        "Referer": f"https://finance.sina.com.cn/futures/quotes/{code}.shtml",
        "User-Agent": USER_AGENT,
    }

    duration_seconds = period_minutes * 60
    records = fetch_jsonp_records(url, headers, max_attempts, fetch_text)
    if records:
        bars = parse_domestic_minute_records(records, duration_seconds)
        if bars:
            return bars

    raise ValueError("history data unavailable")


def fetch_domestic_daily_history(
    symbol: str,
    max_attempts: int,
    fetch_text: FetchText,
) -> list[KlineBar]:
    code = normalize_domestic_symbol(symbol)
    now = dt.datetime.now(dt.timezone.utc).date()
    cache_buster = f"{now.year}_{now.month}_{now.day}"
    var_name = f"_{code}{cache_buster}"
    encoded_var_name = urllib.parse.quote(var_name, safe="")
    base_url = (
        "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
        f"var%20{encoded_var_name}=/InnerFuturesNewService.getDailyKLine"
    )
    query = urllib.parse.urlencode({"symbol": code, "_": cache_buster})
    url = f"{base_url}?{query}"
    headers = {
        "Accept": "*/*",
        "Referer": f"https://finance.sina.com.cn/futures/quotes/{code}.shtml",
        "User-Agent": USER_AGENT,
    }

    records = fetch_jsonp_records(url, headers, max_attempts, fetch_text)
    if records:
        bars = parse_domestic_daily_records(records)
        if bars:
            return bars

    raise ValueError("history data unavailable")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch Sina futures history for international and domestic markets."
    )
    parser.add_argument("market", choices=("intl", "domestic"))
    parser.add_argument("symbol", help="intl example: hf_OIL; domestic example: rb2501")
    parser.add_argument(
        "duration",
        help="raw seconds or compact period like 1m, 5m, 1h, 1d, 2d, 7d, 28d, 365d",
    )
    parser.add_argument("--count", type=int, default=None, help="latest bar count to keep")
    parser.add_argument("--format", choices=("csv", "json"), default="csv")
    parser.add_argument("--max-attempts", type=int, default=3)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        duration_seconds = parse_duration(args.duration)
        bars = fetch_history(
            market=args.market,
            symbol=args.symbol,
            duration_seconds=duration_seconds,
            count=args.count,
            max_attempts=args.max_attempts,
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    rows = serialize_bars(bars, duration_seconds)
    if args.format == "json":
        print(
            json.dumps(
                rows,
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print_csv(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
