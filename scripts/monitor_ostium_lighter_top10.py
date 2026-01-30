import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 以 python scripts/xxx.py 运行时，把项目根加入 path，才能 import helpers
_script_dir = Path(__file__).resolve().parent
if _script_dir.name == "scripts":
    _project_root = _script_dir.parent
    if str(_project_root) not in sys.path:
        sys.path.insert(0, str(_project_root))

import requests
import websockets

from ostium_python_sdk import OstiumSDK
from helpers.telegram_bot import TelegramBot


ORACLE_FEE_USD = Decimal("0.10")
KNOWN_LIGHTER_SYMBOLS = {
    "AAPL",
    "ADA",
    "AMZN",
    "BMNR",
    "BNB",
    "BTC",
    "COIN",
    "CRCL",
    "ETH",
    "HOOD",
    "HYPE",
    "LINK",
    "META",
    "MSFT",
    "MSTR",
    "NVDA",
    "PLTR",
    "SOL",
    "SPX",
    "TRX",
    "TSLA",
    "XAG",
    "XAU",
    "XRP",
    "AUDUSD",
    "EURUSD",
    "GBPUSD",
    "NZDUSD",
    "USDCAD",
    "USDCHF",
    "USDJPY",
}

CRYPTO_SYMBOLS = {"BTC", "ETH", "SOL", "BNB", "ADA", "TRX", "XRP", "LINK", "HYPE"}
INDEX_SYMBOLS = {"SPX"}
METAL_SYMBOLS = {"XAU", "XAG"}
FOREX_SYMBOLS = {"AUDUSD", "EURUSD", "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY"}
STOCK_SYMBOLS = {
    "AAPL",
    "AMZN",
    "BMNR",
    "COIN",
    "CRCL",
    "HOOD",
    "META",
    "MSFT",
    "MSTR",
    "NVDA",
    "PLTR",
    "TSLA",
}


@dataclass
class Candidate:
    symbol: str
    direction: str
    net_bps: Decimal
    gross_bps: Decimal
    cost_bps: Decimal
    ostium_fee_bps: Decimal
    oracle_fee_bps: Decimal
    funding_bps: Decimal
    funding_pnl_bps: Decimal
    spread_bps: Decimal
    depth_bid: Decimal
    depth_ask: Decimal
    depth_quote_bid: Decimal
    depth_quote_ask: Decimal
    min_net_bps: Decimal
    ostium_price: Decimal
    lighter_price: Decimal


def _normalize_symbol(value: str) -> str:
    return value.strip().upper().replace("/", "-").replace("_", "-")


def _parse_lighter_symbol(symbol: str) -> Tuple[str, str]:
    symbol = _normalize_symbol(symbol)
    if "-" in symbol:
        base, quote = symbol.split("-", 1)
        return base, quote
    if len(symbol) == 6 and symbol.isalpha():
        return symbol[:3], symbol[3:]
    return symbol, "USD"


def _fetch_lighter_order_books(base_url: str, timeout: int) -> Dict[str, Dict]:
    url = f"{base_url.rstrip('/')}/api/v1/orderBooks"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    order_books = data.get("order_books") or data.get("orderBooks") or []
    return {ob.get("symbol"): ob for ob in order_books if ob.get("symbol")}


def _derive_ws_url(base_url: str) -> str:
    if base_url.startswith("https://"):
        return "wss://" + base_url[len("https://") :].rstrip("/") + "/stream"
    if base_url.startswith("http://"):
        return "ws://" + base_url[len("http://") :].rstrip("/") + "/stream"
    return base_url.rstrip("/") + "/stream"


async def _fetch_lighter_bbo_ws(
    symbols: List[str],
    base_url: str,
    timeout: int,
    ws_url: Optional[str] = None,
    debug: bool = False,
    target_quote: Decimal = Decimal("10000"),
) -> Dict[str, Dict[str, Decimal]]:
    markets = _fetch_lighter_order_books(base_url, timeout=timeout)
    symbol_to_market = {
        sym: markets[sym]["market_id"]
        for sym in symbols
        if sym in markets and "market_id" in markets[sym]
    }
    market_to_symbol = {int(v): k for k, v in symbol_to_market.items()}
    pending = set(market_to_symbol.keys())
    bbo_map: Dict[str, Dict[str, Decimal]] = {}

    if not pending:
        return bbo_map

    ws_url = ws_url or _derive_ws_url(base_url)

    async with websockets.connect(ws_url) as ws:
        for market_id in pending:
            await ws.send(
                json.dumps({"type": "subscribe", "channel": f"order_book/{market_id}"})
            )

        start = time.time()
        while pending and (time.time() - start) < timeout:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=1)
            except asyncio.TimeoutError:
                continue

            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                continue

            if data.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong"}))
                continue

            if data.get("type") != "subscribed/order_book":
                continue

            channel = data.get("channel", "")
            market_id = None
            if ":" in channel:
                try:
                    market_id = int(channel.split(":")[-1])
                except ValueError:
                    market_id = None

            order_book = data.get("order_book", {})
            bids_raw = order_book.get("bids", [])
            asks_raw = order_book.get("asks", [])
            best_bid = _extract_best(bids_raw)
            best_ask = _extract_best(asks_raw)

            if market_id is None or best_bid is None or best_ask is None:
                continue

            symbol = market_to_symbol.get(market_id)
            if symbol:
                bids = _parse_levels(bids_raw)
                asks = _parse_levels(asks_raw)
                vwap_bid, bid_depth, bid_quote = _compute_vwap_by_quote(
                    bids, target_quote=target_quote
                )
                vwap_ask, ask_depth, ask_quote = _compute_vwap_by_quote(
                    asks, target_quote=target_quote
                )
                bbo_map[symbol] = {
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "vwap_bid": vwap_bid,
                    "vwap_ask": vwap_ask,
                    "bid_depth": bid_depth,
                    "ask_depth": ask_depth,
                    "bid_quote": bid_quote,
                    "ask_quote": ask_quote,
                }
                pending.discard(market_id)

        if debug and pending:
            print(f"debug: lighter ws missing markets={sorted(pending)}")

    return bbo_map


def _extract_best(levels) -> Optional[Decimal]:
    if not levels:
        return None
    first = levels[0]
    if isinstance(first, (list, tuple)) and first:
        return Decimal(str(first[0]))
    if isinstance(first, dict) and "price" in first:
        return Decimal(str(first["price"]))
    return None


def _parse_levels(levels: list) -> List[Tuple[Decimal, Decimal]]:
    parsed: List[Tuple[Decimal, Decimal]] = []
    for level in levels or []:
        if isinstance(level, (list, tuple)) and len(level) >= 2:
            price = Decimal(str(level[0]))
            size = Decimal(str(level[1]))
        elif isinstance(level, dict):
            price = Decimal(str(level.get("price", 0)))
            size = Decimal(str(level.get("size", 0)))
        else:
            continue
        if price > 0 and size > 0:
            parsed.append((price, size))
    return parsed


def _compute_vwap_by_quote(
    levels: List[Tuple[Decimal, Decimal]], target_quote: Decimal
) -> Tuple[Decimal, Decimal, Decimal]:
    if not levels:
        return Decimal("0"), Decimal("0"), Decimal("0")
    total_qty = Decimal("0")
    total_px_qty = Decimal("0")
    total_quote = Decimal("0")
    for price, size in levels:
        quote = price * size
        total_qty += size
        total_px_qty += quote
        total_quote += quote
        if total_quote >= target_quote:
            break
    if total_qty <= 0:
        return Decimal("0"), Decimal("0"), Decimal("0")
    return total_px_qty / total_qty, total_qty, total_quote


def _oracle_fee_bps(notional_usd: Decimal) -> Decimal:
    if notional_usd <= 0:
        return Decimal("0")
    return (ORACLE_FEE_USD / notional_usd) * Decimal("10000")


def _ostium_fee_bps_by_symbol(symbol: str, is_maker: bool) -> Decimal:
    if symbol in CRYPTO_SYMBOLS:
        return Decimal("3") if is_maker else Decimal("10")
    if symbol in METAL_SYMBOLS:
        return Decimal("3") if symbol == "XAU" else Decimal("15")
    if symbol in INDEX_SYMBOLS:
        return Decimal("5")
    if symbol in FOREX_SYMBOLS:
        return Decimal("3")
    return Decimal("5")


async def _fetch_ostium_funding_map(
    sdk: OstiumSDK,
    pair_ids: Dict[str, int],
    period_hours: int,
    cache: Dict[str, Tuple[float, Decimal]],
    cache_seconds: int,
) -> Dict[str, Decimal]:
    now = time.time()
    result: Dict[str, Decimal] = {}

    async def fetch(symbol: str, pair_id: int) -> None:
        cached = cache.get(symbol)
        if cached and now - cached[0] < cache_seconds:
            result[symbol] = cached[1]
            return

        try:
            _, _, funding_rate_percent, _ = await sdk.get_funding_rate_for_pair_id(
                pair_id, period_hours=period_hours
            )
            funding_bps = Decimal(str(funding_rate_percent)) * Decimal("100")
            cache[symbol] = (now, funding_bps)
            result[symbol] = funding_bps
        except Exception:
            # If funding fetch fails, treat as 0 for this cycle
            result[symbol] = Decimal("0")

    tasks = [fetch(sym, pair_id) for sym, pair_id in pair_ids.items()]
    if tasks:
        await asyncio.gather(*tasks)
    return result


async def _fetch_ostium_pairs(sdk: OstiumSDK) -> List[Dict]:
    return await sdk.subgraph.get_pairs()


async def _fetch_ostium_prices(sdk: OstiumSDK) -> List[Dict]:
    return await sdk.price.get_latest_prices()


def _build_price_map(prices: List[Dict]) -> Dict[str, Dict]:
    price_map = {}
    for p in prices or []:
        key = f"{p.get('from')}-{p.get('to')}"
        price_map[key] = p
    return price_map


def _calc_limit_price(
    direction: str, bid: Decimal, ask: Decimal, mid: Decimal, offset_bps: Decimal
) -> Decimal:
    offset = offset_bps / Decimal("10000")
    if direction == "buy":
        base = bid if bid > 0 else mid
        return base * (Decimal("1") - offset)
    base = ask if ask > 0 else mid
    return base * (Decimal("1") + offset)


def _calc_spread_bps(
    direction: str,
    ostium_price: Decimal,
    lighter_price: Decimal,
    mid: Decimal,
) -> Decimal:
    if mid <= 0:
        return Decimal("0")
    if direction == "buy":
        return (lighter_price - ostium_price) / mid * Decimal("10000")
    return (ostium_price - lighter_price) / mid * Decimal("10000")


def _format_candidate_row(rank: int, c: Candidate, open_side: str) -> str:
    return (
        f"{rank:>2}  {c.symbol:<7}  {open_side:<12}  "
        f"{c.gross_bps:>10.4f}  {c.cost_bps:>10.4f}  {c.net_bps:>10.4f}  "
        f"{c.ostium_price:>12.6f}  {c.lighter_price:>12.6f}"
    )


def _format_process_row(c: Candidate, notional_usd: Decimal) -> str:
    return (
        "    "
        f"利润={c.gross_bps:.4f}bps  "
        f"综合成本={c.cost_bps:.4f}bps  "
        f"资金费率={c.funding_bps:.4f}bps(负为收)  "
        f"资金费率收益={c.funding_pnl_bps:.4f}bps  "
        f"净利润(含资金)={c.net_bps:.4f}bps  "
        f"盘口spread={c.spread_bps:.2f}bps  "
        f"深度USD bid={c.depth_quote_bid:.0f} ask={c.depth_quote_ask:.0f}  "
        f"动态阈值={c.min_net_bps:.2f}bps  "
        f"名义=${notional_usd}"
    )


def _print_header(max_items: int, min_net_bps: Decimal) -> None:
    print(f"Top{max_items} 候选（最小净利 {min_net_bps} bps）")
    print(
        "序  标的     开多/开空平台      利润bps      成本bps      净利润bps(含资金)  Ostium价      Lighter价"
    )
    print("-" * 110)


def _format_alert_line(c: Candidate) -> str:
    open_side = "Ostium多 / Lighter空" if c.direction == "buy" else "Ostium空 / Lighter多"
    return (
        f"{c.symbol} | {open_side} | 利润 {c.gross_bps:.2f}bps | "
        f"综合成本 {c.cost_bps:.2f}bps | 资金费率 {c.funding_bps:.2f}bps | "
        f"净利润 {c.net_bps:.2f}bps"
    )


def _category_for_symbol(symbol: str) -> str:
    if symbol in FOREX_SYMBOLS:
        return "forex"
    if symbol in METAL_SYMBOLS:
        return "commodity"
    if symbol in STOCK_SYMBOLS:
        return "stocks"
    if symbol in CRYPTO_SYMBOLS:
        return "crypto"
    return "other"


def _print_rankings(
    candidates: List[Candidate],
    all_ranked: List[Candidate],
    max_items: int,
    min_net_bps: Decimal,
    notional_usd: Decimal,
) -> None:
    print(f"说明：Lighter 侧使用 ${notional_usd} 深度的 VWAP 买/卖价计算利润")
    print("\n[综合前10]")
    _print_header(max_items, min_net_bps)
    for idx, cand in enumerate(candidates[:max_items], start=1):
        open_side = "Ostium多 / Lighter空" if cand.direction == "buy" else "Ostium空 / Lighter多"
        print(_format_candidate_row(idx, cand, open_side))
        print(_format_process_row(cand, notional_usd))

    if not candidates:
        print("无标的满足阈值，输出磨损最小的 5 个标的。")
        fallback = all_ranked[:5]
        for idx, cand in enumerate(fallback, start=1):
            open_side = "Ostium多 / Lighter空" if cand.direction == "buy" else "Ostium空 / Lighter多"
            print(_format_candidate_row(idx, cand, open_side))
            print(_format_process_row(cand, notional_usd))

    def print_category(title: str, categories: Tuple[str, ...], limit: int) -> None:
        subset = [c for c in candidates if _category_for_symbol(c.symbol) in categories]
        if not subset:
            return
        print(f"\n[{title}]")
        _print_header(limit, min_net_bps)
        for idx, cand in enumerate(subset[:limit], start=1):
            open_side = "Ostium多 / Lighter空" if cand.direction == "buy" else "Ostium空 / Lighter多"
            print(_format_candidate_row(idx, cand, open_side))
            print(_format_process_row(cand, notional_usd))

    print_category("外汇前5", ("forex",), 5)
    print_category("股票/大宗商品前5", ("stocks", "commodity"), 5)
    print_category("加密前5", ("crypto",), 5)


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Monitor top10 Ostium/Lighter opportunities every minute"
    )
    parser.add_argument("--rpc-url", default=os.getenv("RPC_URL", ""))
    parser.add_argument("--lighter-base-url", default=os.getenv("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"))
    parser.add_argument("--interval", type=int, default=60, help="seconds between updates")
    parser.add_argument("--min-net-bps", type=Decimal, default=Decimal("0.01"))
    parser.add_argument("--alert-net-bps", type=Decimal, default=Decimal("0"))
    parser.add_argument("--alert-cooldown", type=int, default=300, help="seconds")
    parser.add_argument("--buffer-bps", type=Decimal, default=Decimal("0"))
    parser.add_argument("--notional-usd", type=Decimal, default=Decimal("10000"))
    parser.add_argument("--funding-hours", type=int, default=24)
    parser.add_argument("--funding-cache-seconds", type=int, default=300)
    parser.add_argument("--exclude-symbols", type=str, default="SPX")
    parser.add_argument("--depth-quote-usd", type=Decimal, default=Decimal("10000"))
    parser.add_argument("--min-depth-quote-usd", type=Decimal, default=Decimal("10000"))
    parser.add_argument("--max-spread-bps", type=Decimal, default=Decimal("50"))
    parser.add_argument("--spread-weight", type=Decimal, default=Decimal("0.2"))
    parser.add_argument("--max-dislocation-bps", type=Decimal, default=Decimal("500"))
    parser.add_argument("--ostium-leverage", type=Decimal, default=Decimal(os.getenv("OSTIUM_LEVERAGE", "5")))
    parser.add_argument("--offset-bps", type=Decimal, default=Decimal(os.getenv("OSTIUM_PRICE_OFFSET_BPS", "5")))
    parser.add_argument("--lighter-ws-url", type=str, default="")
    parser.add_argument("--max-items", type=int, default=10)
    parser.add_argument("--debug", action="store_true", help="print debug stats")
    parser.add_argument("--debug-symbol", type=str, default="", help="print raw order book for symbol")
    parser.add_argument("--once", action="store_true", help="run once and exit")
    args = parser.parse_args()

    if not args.rpc_url:
        print("RPC_URL is required for Ostium price feed.")
        return 2

    sdk = OstiumSDK("mainnet", private_key=None, rpc_url=args.rpc_url)
    print("监控已启动")
    sys.stdout.flush()
    funding_cache: Dict[str, Tuple[float, Decimal]] = {}
    alert_cache: Dict[str, float] = {}
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    tg_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    while True:
        try:
            try:
                pairs = await asyncio.wait_for(_fetch_ostium_pairs(sdk), timeout=30)
            except Exception as exc:
                print(f"fetch ostium pairs failed: {exc}")
                await asyncio.sleep(args.interval)
                continue

            try:
                prices = await asyncio.wait_for(_fetch_ostium_prices(sdk), timeout=30)
            except Exception as exc:
                print(f"fetch ostium prices failed: {exc}")
                await asyncio.sleep(args.interval)
                continue

            price_map = _build_price_map(prices)

            try:
                lighter_books = _fetch_lighter_order_books(args.lighter_base_url, timeout=15)
            except Exception as exc:
                print(f"fetch lighter books failed: {exc}")
                await asyncio.sleep(args.interval)
                continue

            bbo_map = await _fetch_lighter_bbo_ws(
                sorted(KNOWN_LIGHTER_SYMBOLS),
                args.lighter_base_url,
                timeout=15,
                ws_url=args.lighter_ws_url or None,
                debug=args.debug,
                target_quote=args.depth_quote_usd,
            )

            candidates: List[Candidate] = []
            all_ranked: List[Candidate] = []
            oracle_fee_bps = _oracle_fee_bps(args.notional_usd)
            # funding_cache reused across cycles to avoid heavy calls
            matched_pairs = 0
            excluded = {s.strip().upper() for s in args.exclude_symbols.split(",") if s.strip()}

            pair_id_map: Dict[str, int] = {}
            for pair in pairs:
                base = pair.get("from")
                quote = pair.get("to")
                pair_id = pair.get("id")
                if base and quote and pair_id is not None:
                    pair_id_map[f"{base}-{quote}"] = int(pair_id)

            funding_pair_ids: Dict[str, int] = {}
            for sym in CRYPTO_SYMBOLS:
                key = f"{sym}-USD"
                if key in pair_id_map:
                    funding_pair_ids[sym] = pair_id_map[key]

            funding_map = await _fetch_ostium_funding_map(
                sdk,
                funding_pair_ids,
                period_hours=args.funding_hours,
                cache=funding_cache,
                cache_seconds=args.funding_cache_seconds,
            )

            for symbol in sorted(KNOWN_LIGHTER_SYMBOLS):
                if symbol in excluded:
                    continue
                if symbol not in lighter_books:
                    continue
                if symbol not in bbo_map:
                    continue

                if symbol in FOREX_SYMBOLS:
                    base, quote = symbol[:3], symbol[3:]
                    price_key = f"{base}-{quote}"
                elif symbol in METAL_SYMBOLS or symbol in INDEX_SYMBOLS or symbol in CRYPTO_SYMBOLS:
                    price_key = f"{symbol}-USD"
                else:
                    price_key = f"{symbol}-USD"

                price_info = price_map.get(price_key)
                if not price_info:
                    continue

                matched_pairs += 1
                book = bbo_map[symbol]
                best_bid = book.get("best_bid", Decimal("0"))
                best_ask = book.get("best_ask", Decimal("0"))
                vwap_bid = book.get("vwap_bid", Decimal("0"))
                vwap_ask = book.get("vwap_ask", Decimal("0"))
                bid_depth = book.get("bid_depth", Decimal("0"))
                ask_depth = book.get("ask_depth", Decimal("0"))
                bid_quote = book.get("bid_quote", Decimal("0"))
                ask_quote = book.get("ask_quote", Decimal("0"))

                if bid_quote < args.min_depth_quote_usd or ask_quote < args.min_depth_quote_usd:
                    continue

                bid = Decimal(str(price_info.get("bid", price_info.get("mid", 0))))
                ask = Decimal(str(price_info.get("ask", price_info.get("mid", 0))))
                mid = Decimal(str(price_info.get("mid", 0)))
                if mid <= 0:
                    continue

                spread_bps = Decimal("0")
                if vwap_bid > 0 and vwap_ask > 0:
                    spread_bps = (vwap_ask - vwap_bid) / vwap_bid * Decimal("10000")

                if spread_bps > args.max_spread_bps:
                    continue

                for direction in ("buy", "sell"):
                    maker_ok = symbol in CRYPTO_SYMBOLS and args.ostium_leverage <= Decimal("20")
                    fee_bps = _ostium_fee_bps_by_symbol(symbol, maker_ok)
                    ostium_price = _calc_limit_price(direction, bid, ask, mid, args.offset_bps)
                    lighter_price = vwap_bid if direction == "buy" else vwap_ask
                    gross_bps = _calc_spread_bps(direction, ostium_price, lighter_price, mid)
                    cost_bps = fee_bps + oracle_fee_bps + args.buffer_bps
                    funding_rate_bps = funding_map.get(symbol, Decimal("0"))
                    funding_cost_bps = funding_rate_bps if direction == "buy" else -funding_rate_bps
                    funding_pnl_bps = Decimal("0") - funding_cost_bps
                    min_net_bps = args.min_net_bps + spread_bps * args.spread_weight
                    net_bps = gross_bps - cost_bps - funding_cost_bps

                    if abs(gross_bps) > args.max_dislocation_bps:
                        continue

                    item = Candidate(
                        symbol=symbol,
                        direction=direction,
                        net_bps=net_bps,
                        gross_bps=gross_bps,
                        cost_bps=cost_bps,
                        ostium_fee_bps=fee_bps,
                        oracle_fee_bps=oracle_fee_bps,
                        funding_bps=funding_cost_bps,
                        funding_pnl_bps=funding_pnl_bps,
                        spread_bps=spread_bps,
                        depth_bid=bid_depth,
                        depth_ask=ask_depth,
                        depth_quote_bid=bid_quote,
                        depth_quote_ask=ask_quote,
                        min_net_bps=min_net_bps,
                        ostium_price=ostium_price,
                        lighter_price=lighter_price,
                    )
                    all_ranked.append(item)

                    if net_bps < min_net_bps:
                        continue

                    candidates.append(item)

            candidates.sort(key=lambda c: c.net_bps, reverse=True)
            all_ranked.sort(key=lambda c: c.net_bps, reverse=True)
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            print(f"\n[{ts}]")
            _print_rankings(candidates, all_ranked, args.max_items, args.min_net_bps, args.notional_usd)
            if args.debug:
                print(
                    f"debug: ostium_pairs={len(pairs)} prices={len(prices)} "
                    f"price_map={len(price_map)} lighter_books={len(lighter_books)} "
                    f"bbo_map={len(bbo_map)} matched_pairs={matched_pairs} "
                    f"ranked={len(all_ranked)}"
                )

            # Telegram alert
            if args.alert_net_bps > 0 and tg_token and tg_chat_id:
                now_ts = time.time()
                alert_items: List[Candidate] = []
                for cand in candidates:
                    if cand.net_bps < args.alert_net_bps:
                        continue
                    key = f"{cand.symbol}:{cand.direction}"
                    last_ts = alert_cache.get(key, 0)
                    if now_ts - last_ts < args.alert_cooldown:
                        continue
                    alert_cache[key] = now_ts
                    alert_items.append(cand)

                if alert_items:
                    alert_items.sort(key=lambda c: c.net_bps, reverse=True)
                    lines = [
                        "Ostium/Lighter 监控提醒",
                        f"阈值: {args.alert_net_bps} bps",
                    ]
                    for item in alert_items[:5]:
                        lines.append(_format_alert_line(item))
                    message = "\n".join(lines)
                    try:
                        TelegramBot(tg_token, tg_chat_id).send_text(message, parse_mode="HTML")
                    except Exception as exc:
                        print(f"Telegram 提醒失败: {exc}")

            if args.debug:
                sample_pairs = [f"{p.get('from')}-{p.get('to')}" for p in pairs[:5]]
                sample_lighter = list(lighter_books.keys())[:5]
                print(f"debug: sample ostium pairs={sample_pairs}")
                print(f"debug: sample lighter symbols={sample_lighter}")
                if args.debug_symbol:
                    raw = lighter_books.get(args.debug_symbol)
                    print(f"debug: orderbook {args.debug_symbol}={raw}")

            sys.stdout.flush()

        except Exception as exc:
            print(f"Error during scan: {exc}")
            sys.stdout.flush()

        if args.once:
            return 0
        await asyncio.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
