import argparse
import asyncio
import json
import os
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Optional, Tuple

import requests
import websockets
import dotenv

from ostium_python_sdk import OstiumSDK
from ostium_python_sdk.utils import parse_limit_order_id
from lighter.signer_client import SignerClient


@dataclass
class ExecResult:
    symbol: str
    direction: str
    ostium_price: Decimal
    lighter_price: Decimal
    gross_bps: Decimal
    cost_bps: Decimal
    funding_bps: Decimal
    net_bps: Decimal


CRYPTO_SYMBOLS = {"BTC", "ETH", "SOL", "BNB", "ADA", "TRX", "XRP", "LINK", "HYPE"}
FOREX_SYMBOLS = {"AUDUSD", "EURUSD", "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY"}
METAL_SYMBOLS = {"XAU", "XAG"}
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


def _ostium_fee_bps_by_symbol(symbol: str, is_maker: bool) -> Decimal:
    if symbol in CRYPTO_SYMBOLS:
        return Decimal("3") if is_maker else Decimal("10")
    if symbol in METAL_SYMBOLS:
        return Decimal("3") if symbol == "XAU" else Decimal("15")
    if symbol in FOREX_SYMBOLS:
        return Decimal("3")
    return Decimal("5")


def _derive_ws_url(base_url: str) -> str:
    if base_url.startswith("https://"):
        return "wss://" + base_url[len("https://") :].rstrip("/") + "/stream"
    if base_url.startswith("http://"):
        return "ws://" + base_url[len("http://") :].rstrip("/") + "/stream"
    return base_url.rstrip("/") + "/stream"


def _fetch_lighter_markets(base_url: str, timeout: int) -> Dict[str, Dict]:
    url = f"{base_url.rstrip('/')}/api/v1/orderBooks"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    order_books = data.get("order_books") or data.get("orderBooks") or []
    return {ob.get("symbol"): ob for ob in order_books if ob.get("symbol")}


def _parse_levels(levels: list):
    parsed = []
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


def _vwap_by_quote(levels, target_quote: Decimal):
    total_qty = Decimal("0")
    total_quote = Decimal("0")
    total_px_qty = Decimal("0")
    for price, size in levels:
        quote = price * size
        total_qty += size
        total_px_qty += quote
        total_quote += quote
        if total_quote >= target_quote:
            break
    if total_qty <= 0:
        return Decimal("0"), Decimal("0")
    return total_px_qty / total_qty, total_quote


async def _fetch_lighter_vwap(symbol: str, base_url: str, target_quote: Decimal, timeout: int) -> Optional[Dict]:
    markets = _fetch_lighter_markets(base_url, timeout=timeout)
    if symbol not in markets:
        return None
    market_id = markets[symbol]["market_id"]
    ws_url = _derive_ws_url(base_url)
    async with websockets.connect(ws_url) as ws:
        await ws.send(
            json.dumps({"type": "subscribe", "channel": f"order_book/{market_id}"})
        )
        start = time.time()
        while time.time() - start < timeout:
            msg = await ws.recv()
            data = json.loads(msg)
            if data.get("type") != "subscribed/order_book":
                continue
            book = data.get("order_book", {})
            bids = _parse_levels(book.get("bids", []))
            asks = _parse_levels(book.get("asks", []))
            vwap_bid, bid_quote = _vwap_by_quote(bids, target_quote)
            vwap_ask, ask_quote = _vwap_by_quote(asks, target_quote)
            return {
                "vwap_bid": vwap_bid,
                "vwap_ask": vwap_ask,
                "bid_quote": bid_quote,
                "ask_quote": ask_quote,
                "market_id": market_id,
            }
    return None


def _parse_symbol(symbol: str) -> Tuple[str, str]:
    if symbol in FOREX_SYMBOLS and len(symbol) == 6:
        return symbol[:3], symbol[3:]
    return symbol, "USD"


def _calc_limit_price(mid: Decimal, side: str, offset_bps: Decimal) -> Decimal:
    offset = offset_bps / Decimal("10000")
    if side == "buy":
        return mid * (Decimal("1") - offset)
    return mid * (Decimal("1") + offset)


def _stock_market_open_fallback() -> bool:
    # Fallback: approximate US market hours 14:00-21:00 UTC on weekdays.
    now = time.gmtime()
    if now.tm_wday >= 5:
        return False
    return 14 <= now.tm_hour < 21


async def main() -> int:
    parser = argparse.ArgumentParser(description="Ostium/Lighter 套利脚本（自动执行）")
    parser.add_argument("--symbol", required=True, help="如 BTC 或 EURUSD")
    parser.add_argument("--size", default="", help="基础资产数量（可空，自动按名义金额计算）")
    parser.add_argument("--notional-usd", type=Decimal, default=Decimal("50"))
    parser.add_argument("--min-net-bps", type=Decimal, default=Decimal("1"))
    parser.add_argument("--depth-quote-usd", type=Decimal, default=Decimal("10000"))
    parser.add_argument("--min-notional-usd", type=Decimal, default=Decimal("20"))
    parser.add_argument("--max-notional-usd", type=Decimal, default=Decimal("200"))
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--execute", action="store_true", help="执行真实下单（默认仅计算）")
    parser.add_argument("--env-file", default=".env", help="env file path")
    args = parser.parse_args()

    dotenv.load_dotenv(args.env_file)

    rpc_url = os.getenv("RPC_URL")
    private_key = os.getenv("PRIVATE_KEY")
    if not rpc_url or not private_key:
        raise ValueError("RPC_URL / PRIVATE_KEY required")

    lighter_base_url = os.getenv("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai")
    api_key_private_key = os.getenv("API_KEY_PRIVATE_KEY")
    account_index = int(os.getenv("LIGHTER_ACCOUNT_INDEX", "0"))
    api_key_index = int(os.getenv("LIGHTER_API_KEY_INDEX", "0"))
    if not api_key_private_key:
        raise ValueError("API_KEY_PRIVATE_KEY required for Lighter")

    sdk = OstiumSDK("mainnet", private_key=private_key, rpc_url=rpc_url)
    lighter = None

    symbol = args.symbol.upper()
    base, quote = _parse_symbol(symbol)

    pairs = await sdk.subgraph.get_pairs()
    pair_id = None
    for pair in pairs:
        if pair.get("from") == base and pair.get("to") == quote:
            pair_id = int(pair.get("id"))
            break
    if pair_id is None:
        print(f"未找到 Ostium 交易对: {base}-{quote}")
        return 1

    price_mid, is_open, is_day_closed = await sdk.price.get_price(base, quote)
    if is_open is None:
        is_open = _stock_market_open_fallback()
    if is_day_closed is None:
        is_day_closed = False
    if symbol in STOCK_SYMBOLS and (not is_open or is_day_closed):
        print("股票交易时段关闭，跳过。")
        return 0

    price_mid = Decimal(str(price_mid))
    offset_bps = Decimal(os.getenv("OSTIUM_PRICE_OFFSET_BPS", "5"))

    markets = _fetch_lighter_markets(lighter_base_url, timeout=args.timeout)
    if symbol not in markets:
        print("Lighter 无该标的。")
        return 0
    market_info = markets[symbol]
    vwap = await _fetch_lighter_vwap(symbol, lighter_base_url, args.depth_quote_usd, args.timeout)
    if not vwap or vwap["bid_quote"] < args.depth_quote_usd or vwap["ask_quote"] < args.depth_quote_usd:
        print("Lighter 深度不足 $10k，跳过。")
        return 0

    vwap_bid = Decimal(str(vwap["vwap_bid"]))
    vwap_ask = Decimal(str(vwap["vwap_ask"]))

    # auto size with notional bounds
    if args.size:
        size = Decimal(args.size)
    else:
        size = args.notional_usd / price_mid

    notional = size * price_mid
    if notional < args.min_notional_usd:
        print(f"名义额 {notional:.2f} 低于最小值 {args.min_notional_usd}，已上调。")
        size = args.min_notional_usd / price_mid
        notional = size * price_mid
    if notional > args.max_notional_usd:
        print(f"名义额 {notional:.2f} 高于最大值 {args.max_notional_usd}，已下调。")
        size = args.max_notional_usd / price_mid
        notional = size * price_mid

    # funding (Ostium)
    if symbol in CRYPTO_SYMBOLS:
        try:
            _, _, funding_rate_percent, _ = await sdk.get_funding_rate_for_pair_id(pair_id, period_hours=24)
            funding_bps = Decimal(str(funding_rate_percent)) * Decimal("100")
        except Exception:
            funding_bps = Decimal("0")
    else:
        funding_bps = Decimal("0")

    # evaluate both directions
    def eval_side(side: str) -> ExecResult:
        ostium_price = _calc_limit_price(price_mid, side, offset_bps)
        lighter_price = vwap_bid if side == "buy" else vwap_ask
        gross_bps = (lighter_price - ostium_price) / price_mid * Decimal("10000") if side == "buy" else (
            ostium_price - lighter_price
        ) / price_mid * Decimal("10000")
        fee_bps = _ostium_fee_bps_by_symbol(symbol, is_maker=True)
        oracle_bps = (Decimal("0.10") / notional) * Decimal("10000")
        cost_bps = fee_bps + oracle_bps
        funding_cost = funding_bps if side == "buy" else -funding_bps
        net_bps = gross_bps - cost_bps - funding_cost
        return ExecResult(symbol, side, ostium_price, lighter_price, gross_bps, cost_bps, funding_cost, net_bps)

    long_side = eval_side("buy")
    short_side = eval_side("sell")
    best = long_side if long_side.net_bps >= short_side.net_bps else short_side

    open_side = "Ostium多 / Lighter空" if best.direction == "buy" else "Ostium空 / Lighter多"
    print(
        f"候选: {best.symbol} | {open_side} | 利润={best.gross_bps:.4f}bps "
        f"综合成本={best.cost_bps:.4f}bps | 资金费率={best.funding_bps:.4f}bps "
        f"| 净利润={best.net_bps:.4f}bps | size={size:.6f} notional=${notional:.2f}"
    )

    if best.net_bps < args.min_net_bps:
        print("未达到净利阈值，跳过。")
        return 0

    if not args.execute:
        print("未开启执行模式（--execute），仅计算。")
        return 0

    lighter = SignerClient(
        lighter_base_url,
        api_key_private_key,
        api_key_index,
        account_index,
    )

    # Place Ostium LIMIT order
    leverage = Decimal(os.getenv("OSTIUM_LEVERAGE", "5"))
    trade_params = {
        "collateral": float(notional / leverage),
        "leverage": float(leverage),
        "direction": best.direction == "buy",
        "asset_type": int(pair_id),
        "order_type": "LIMIT",
        "tp": 0,
        "sl": 0,
    }
    result = sdk.ostium.perform_trade(trade_params, float(best.ostium_price))
    order_id = result.get("order_id")
    if not order_id:
        print("Ostium 下单失败")
        return 1

    tracking = await sdk.ostium.track_order_and_trade(
        sdk.subgraph, order_id, polling_interval=1, max_attempts=args.timeout
    )
    order = tracking.get("order") if tracking else None
    trade = tracking.get("trade") if tracking else None

    if order and order.get("isPending", False):
        limit_id = order.get("limitID")
        if limit_id:
            pair_index, index = parse_limit_order_id(limit_id)
            sdk.ostium.cancel_limit_order(pair_index, index)
        print("Ostium 未成交，已撤单。")
        return 0

    if not trade:
        print("Ostium 成交状态未知，终止。")
        return 1

    # Lighter hedge
    client_order_index = int(time.time() * 1000)
    is_ask = best.direction == "buy"
    price = vwap_bid if is_ask else vwap_ask
    base_mult = 10 ** int(market_info.get("supported_size_decimals", 5))
    price_mult = 10 ** int(market_info.get("supported_price_decimals", 1))
    tx, tx_hash, error = await lighter.create_order(
        market_index=vwap["market_id"],
        client_order_index=client_order_index,
        base_amount=int(size * base_mult),
        price=int(price * price_mult),
        is_ask=is_ask,
        order_type=lighter.ORDER_TYPE_LIMIT,
        time_in_force=lighter.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
        reduce_only=False,
        trigger_price=0,
    )
    if error is not None:
        print(f"Lighter 下单失败: {error}")
        return 1

    print("已执行 Ostium + Lighter 对冲。")
    return 0


if __name__ == "__main__":
    logging.getLogger("web3").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("gql").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("websockets.client").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    raise SystemExit(asyncio.run(main()))
