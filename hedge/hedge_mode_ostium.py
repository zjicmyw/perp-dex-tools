import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from decimal import Decimal
from typing import Dict, Optional, Tuple

import requests
from lighter.signer_client import SignerClient

from ostium_python_sdk import OstiumSDK
from ostium_python_sdk.utils import get_trade_details, parse_limit_order_id


class HedgeBot:
    """Hedge bot: Ostium LIMIT maker -> Lighter market taker."""

    def __init__(
        self,
        ticker: str,
        order_quantity: Decimal,
        fill_timeout: int = 10,
        iterations: int = 20,
        sleep_time: int = 0,
        max_position: Decimal = Decimal("0"),
    ):
        self.ticker = ticker.upper()
        self.order_quantity = order_quantity
        self.fill_timeout = fill_timeout
        self.iterations = iterations
        self.sleep_time = sleep_time
        self.max_position = order_quantity if max_position == Decimal("0") else max_position

        self.stop_flag = False
        self.order_execution_complete = False

        self.ostium_sdk: Optional[OstiumSDK] = None
        self.ostium_pair_id: Optional[int] = None
        self.ostium_from: Optional[str] = None
        self.ostium_to: Optional[str] = None
        self.ostium_trader_address: Optional[str] = None

        self.lighter_client: Optional[SignerClient] = None
        self.lighter_market_index: Optional[int] = None
        self.base_amount_multiplier: Optional[int] = None
        self.price_multiplier: Optional[int] = None

        self.ostium_position = Decimal("0")
        self.lighter_position = Decimal("0")

        self.lighter_base_url = os.getenv(
            "LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"
        )
        self.account_index = int(os.getenv("LIGHTER_ACCOUNT_INDEX", "0"))
        self.api_key_index = int(os.getenv("LIGHTER_API_KEY_INDEX", "0"))

        self.ostium_leverage = Decimal(os.getenv("OSTIUM_LEVERAGE", "5"))
        self.ostium_price_offset_bps = Decimal(
            os.getenv("OSTIUM_PRICE_OFFSET_BPS", "5")
        )

        self.logger = self._setup_logger()

    def _setup_logger(self) -> logging.Logger:
        os.makedirs("logs", exist_ok=True)
        logger = logging.getLogger(f"hedge_ostium_{self.ticker}")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()

        file_handler = logging.FileHandler(f"logs/{self.ticker}_hedge_ostium.log")
        console_handler = logging.StreamHandler(sys.stdout)

        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)

        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
        logger.propagate = False
        return logger

    def setup_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)

    def shutdown(self, signum=None, frame=None) -> None:
        self.stop_flag = True
        self.logger.info("Stopping hedge bot...")

    def _parse_ticker(self) -> Tuple[str, str, str]:
        if "-" in self.ticker:
            base, quote = self.ticker.split("-", 1)
            lighter_symbol = f"{base}{quote}"
            return base, quote, lighter_symbol

        if len(self.ticker) == 6 and self.ticker.isalpha():
            base, quote = self.ticker[:3], self.ticker[3:]
            lighter_symbol = self.ticker
            return base, quote, lighter_symbol

        base, quote = self.ticker, "USD"
        lighter_symbol = self.ticker
        return base, quote, lighter_symbol

    async def initialize_ostium(self) -> None:
        private_key = os.getenv("PRIVATE_KEY")
        rpc_url = os.getenv("RPC_URL")
        if not private_key or not rpc_url:
            raise ValueError("PRIVATE_KEY and RPC_URL must be set for Ostium")

        self.ostium_sdk = OstiumSDK("mainnet", private_key=private_key, rpc_url=rpc_url)
        self.ostium_trader_address = self.ostium_sdk.ostium.get_public_address()

        base, quote, _ = self._parse_ticker()
        pairs = await self.ostium_sdk.subgraph.get_pairs()
        pair_id = None
        for pair in pairs:
            if pair.get("from") == base and pair.get("to") == quote:
                pair_id = int(pair.get("id"))
                break

        if pair_id is None:
            raise ValueError(f"Ostium pair not found for {base}-{quote}")

        self.ostium_pair_id = pair_id
        self.ostium_from = base
        self.ostium_to = quote

        self.logger.info(
            f"Ostium pair resolved: {self.ostium_from}-{self.ostium_to} (id={self.ostium_pair_id})"
        )

    def initialize_lighter(self) -> None:
        api_key_private_key = os.getenv("API_KEY_PRIVATE_KEY")
        if not api_key_private_key:
            raise ValueError("API_KEY_PRIVATE_KEY must be set for Lighter")

        self.lighter_client = SignerClient(
            url=self.lighter_base_url,
            account_index=self.account_index,
            api_private_keys={self.api_key_index: api_key_private_key},
        )

        err = self.lighter_client.check_client()
        if err is not None:
            raise ValueError(f"Lighter client check failed: {err}")

    def _extract_best_price(self, levels) -> Optional[Decimal]:
        if not levels:
            return None
        first = levels[0]
        if isinstance(first, (list, tuple)) and first:
            return Decimal(str(first[0]))
        if isinstance(first, dict) and "price" in first:
            return Decimal(str(first["price"]))
        return None

    def _fetch_lighter_order_book(self, symbol: str) -> Optional[Dict]:
        url = f"{self.lighter_base_url}/api/v1/orderBooks"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        order_books = data.get("order_books") or data.get("orderBooks") or []
        for item in order_books:
            if item.get("symbol") == symbol:
                return item
        return None

    def get_lighter_market_config(self, symbol: str) -> Tuple[int, int, int]:
        data = self._fetch_lighter_order_book(symbol)
        if not data:
            raise ValueError(f"Lighter symbol not found: {symbol}")

        market_id = int(data["market_id"])
        base_mult = 10 ** int(data["supported_size_decimals"])
        price_mult = 10 ** int(data["supported_price_decimals"])
        return market_id, base_mult, price_mult

    def get_lighter_bbo(self, symbol: str) -> Tuple[Decimal, Decimal]:
        data = self._fetch_lighter_order_book(symbol)
        if not data:
            raise ValueError(f"Lighter symbol not found: {symbol}")

        best_bid = self._extract_best_price(data.get("bids", []))
        best_ask = self._extract_best_price(data.get("asks", []))
        if best_bid is None or best_ask is None:
            raise ValueError("Lighter order book missing bids/asks")
        return best_bid, best_ask

    async def get_ostium_price(self) -> Tuple[Decimal, Decimal, Decimal]:
        if not self.ostium_sdk or not self.ostium_from or not self.ostium_to:
            raise ValueError("Ostium not initialized")
        price_data = await self.ostium_sdk.price.get_latest_price_json(
            self.ostium_from, self.ostium_to
        )
        bid = Decimal(str(price_data.get("bid", price_data.get("mid", 0))))
        ask = Decimal(str(price_data.get("ask", price_data.get("mid", 0))))
        mid = Decimal(str(price_data.get("mid", 0)))
        return bid, ask, mid

    def _calc_limit_price(self, side: str, bid: Decimal, ask: Decimal, mid: Decimal) -> Decimal:
        offset = self.ostium_price_offset_bps / Decimal("10000")
        if side == "buy":
            base = bid if bid > 0 else mid
            return base * (Decimal("1") - offset)
        base = ask if ask > 0 else mid
        return base * (Decimal("1") + offset)

    async def place_ostium_limit_order(self, side: str, quantity: Decimal) -> Optional[Dict]:
        if not self.ostium_sdk or self.ostium_pair_id is None:
            raise ValueError("Ostium not initialized")

        bid, ask, mid = await self.get_ostium_price()
        order_price = self._calc_limit_price(side, bid, ask, mid)
        notional = order_price * quantity
        collateral = notional / self.ostium_leverage

        trade_params = {
            "collateral": float(collateral),
            "leverage": float(self.ostium_leverage),
            "direction": side == "buy",
            "asset_type": int(self.ostium_pair_id),
            "order_type": "LIMIT",
            "tp": 0,
            "sl": 0,
        }

        self.logger.info(
            f"Ostium LIMIT {side} qty={quantity} price={order_price} collateral={collateral} lev={self.ostium_leverage}"
        )

        result = self.ostium_sdk.ostium.perform_trade(trade_params, float(order_price))
        order_id = result.get("order_id")
        if not order_id:
            self.logger.error("Ostium order_id missing from receipt")
            return None

        tracking = await self.ostium_sdk.ostium.track_order_and_trade(
            self.ostium_sdk.subgraph,
            order_id,
            polling_interval=1,
            max_attempts=self.fill_timeout,
        )

        order = tracking.get("order") if tracking else None
        trade = tracking.get("trade") if tracking else None

        if order and order.get("isPending", False):
            self.logger.warning("Ostium order still pending after timeout, cancelling")
            limit_id = order.get("limitID")
            if limit_id:
                pair_index, index = parse_limit_order_id(limit_id)
                self.ostium_sdk.ostium.cancel_limit_order(pair_index, index)
            return None

        if order and order.get("isCancelled", False):
            self.logger.warning("Ostium order was cancelled")
            return None

        if trade:
            return trade
        return None

    async def place_lighter_market_order(self, side: str, quantity: Decimal) -> None:
        if not self.lighter_client or self.lighter_market_index is None:
            raise ValueError("Lighter not initialized")

        best_bid, best_ask = self.get_lighter_bbo(self.lighter_symbol)

        if side == "buy":
            is_ask = False
            price = best_ask * Decimal("1.002")
        else:
            is_ask = True
            price = best_bid * Decimal("0.998")

        client_order_index = int(time.time() * 1000)
        tx, tx_hash, error = await self.lighter_client.create_order(
            market_index=self.lighter_market_index,
            client_order_index=client_order_index,
            base_amount=int(quantity * self.base_amount_multiplier),
            price=int(price * self.price_multiplier),
            is_ask=is_ask,
            order_type=self.lighter_client.ORDER_TYPE_LIMIT,
            time_in_force=self.lighter_client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            reduce_only=False,
            trigger_price=0,
        )
        if error is not None:
            raise ValueError(f"Lighter order error: {error}")

        self.logger.info(f"Lighter MARKET {side} qty={quantity} price={price} tx={tx_hash}")

        await self._wait_for_lighter_position_change(side, quantity)

    def get_lighter_position(self) -> Decimal:
        url = f"{self.lighter_base_url}/api/v1/account"
        params = {"by": "index", "value": self.account_index}
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        positions = data.get("accounts", [{}])[0].get("positions", [])
        for position in positions:
            if position.get("symbol") == self.lighter_symbol:
                return Decimal(position["position"]) * position["sign"]
        return Decimal("0")

    async def _wait_for_lighter_position_change(self, side: str, quantity: Decimal) -> None:
        start = time.time()
        initial = self.get_lighter_position()
        target_delta = quantity if side == "buy" else -quantity

        while time.time() - start < 30 and not self.stop_flag:
            current = self.get_lighter_position()
            if (current - initial) * target_delta > 0:
                return
            await asyncio.sleep(0.2)

        self.logger.warning("Lighter position did not update within timeout")

    async def get_ostium_position(self) -> Decimal:
        if not self.ostium_sdk or not self.ostium_trader_address:
            return Decimal("0")

        open_trades = await self.ostium_sdk.subgraph.get_open_trades(
            self.ostium_trader_address
        )
        total = Decimal("0")
        for trade in open_trades or []:
            if int(trade["pair"]["id"]) != int(self.ostium_pair_id):
                continue
            open_price, trade_notional, _, _, _, _, _, is_long, _, _ = get_trade_details(
                trade
            )
            if not open_price or not trade_notional:
                continue
            base_qty = Decimal(str(trade_notional)) / Decimal(str(open_price))
            total += base_qty if is_long else -base_qty
        return total

    async def trading_loop(self) -> None:
        self.setup_signal_handlers()

        await self.initialize_ostium()
        self.initialize_lighter()

        base, quote, lighter_symbol = self._parse_ticker()
        self.lighter_symbol = lighter_symbol
        self.lighter_market_index, self.base_amount_multiplier, self.price_multiplier = (
            self.get_lighter_market_config(lighter_symbol)
        )

        self.logger.info(
            f"Lighter symbol resolved: {lighter_symbol} (market_id={self.lighter_market_index})"
        )

        for iteration in range(self.iterations):
            if self.stop_flag:
                break

            self.logger.info(f"Iteration {iteration + 1}/{self.iterations}")

            self.ostium_position = await self.get_ostium_position()
            self.lighter_position = self.get_lighter_position()

            while self.ostium_position < self.max_position and not self.stop_flag:
                trade = await self.place_ostium_limit_order("buy", self.order_quantity)
                if trade:
                    base_qty = self.order_quantity
                    if trade.get("tradeNotional") and trade.get("openPrice"):
                        base_qty = Decimal(str(trade["tradeNotional"])) / Decimal(
                            str(trade["openPrice"])
                        )
                    await self.place_lighter_market_order("sell", abs(base_qty))

                self.ostium_position = await self.get_ostium_position()
                self.lighter_position = self.get_lighter_position()

                if abs(self.ostium_position + self.lighter_position) > self.order_quantity * 2:
                    self.logger.error("Position diff too large, stopping")
                    self.stop_flag = True
                    break

            if self.stop_flag:
                break

            if self.sleep_time > 0:
                await asyncio.sleep(self.sleep_time)

            while self.ostium_position > -self.max_position and not self.stop_flag:
                trade = await self.place_ostium_limit_order("sell", self.order_quantity)
                if trade:
                    base_qty = self.order_quantity
                    if trade.get("tradeNotional") and trade.get("openPrice"):
                        base_qty = Decimal(str(trade["tradeNotional"])) / Decimal(
                            str(trade["openPrice"])
                        )
                    await self.place_lighter_market_order("buy", abs(base_qty))

                self.ostium_position = await self.get_ostium_position()
                self.lighter_position = self.get_lighter_position()

                if abs(self.ostium_position + self.lighter_position) > self.order_quantity * 2:
                    self.logger.error("Position diff too large, stopping")
                    self.stop_flag = True
                    break

            if self.sleep_time > 0:
                await asyncio.sleep(self.sleep_time)

    async def run(self) -> None:
        try:
            await self.trading_loop()
        except Exception as exc:
            self.logger.error(f"Error: {exc}")
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ostium + Lighter hedge bot")
    parser.add_argument("--ticker", type=str, required=True)
    parser.add_argument("--size", type=str, required=True)
    parser.add_argument("--iter", type=int, default=20)
    parser.add_argument("--fill-timeout", type=int, default=10)
    parser.add_argument("--sleep", type=int, default=0)
    parser.add_argument("--max-position", type=Decimal, default=Decimal("0"))
    args = parser.parse_args()

    bot = HedgeBot(
        ticker=args.ticker,
        order_quantity=Decimal(args.size),
        fill_timeout=args.fill_timeout,
        iterations=args.iter,
        sleep_time=args.sleep,
        max_position=args.max_position,
    )
    sys.exit(asyncio.run(bot.run()))
