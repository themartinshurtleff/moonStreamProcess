"""
Websocket connectors for real-time exchange data.
Supports Binance, Bybit, and OKX perpetual streams.
"""

import asyncio
import json
import logging
import random
import time
import websockets
from typing import Callable, Dict, Any, List, Optional
from dataclasses import dataclass, field
from collections import deque

logger = logging.getLogger(__name__)

# Backoff constants for reconnect loops
_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 30.0
_BACKOFF_JITTER_LOW = 0.8
_BACKOFF_JITTER_HIGH = 1.2
_REPEATED_ERROR_THRESHOLD = 5  # Downgrade to debug after this many consecutive identical errors

# Default symbols for multi-symbol connectors (Bybit, OKX).
# Binance uses !forceOrder@arr which already gets all symbols.
DEFAULT_SYMBOLS: List[str] = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


@dataclass
class StreamConfig:
    """Configuration for a websocket stream."""
    exchange: str
    instrument: str
    ins_type: str
    obj: str
    url: str
    subscribe_msg: Optional[dict] = None


class BinanceConnector:
    """
    Connects to Binance Futures websocket streams for BTC perpetuals.
    Streams: depth, trades, liquidations, markPrice (funding+OI), account ratios
    """

    BASE_WS = "wss://fstream.binance.com/ws"
    BASE_WS_STREAM = "wss://fstream.binance.com/stream?streams="

    def __init__(self, on_message: Callable[[str], None], btc_price_getter: Callable[[], float]):
        self.on_message = on_message
        self.get_btc_price = btc_price_getter
        self.running = False
        self.ws_connections: Dict[str, Any] = {}
        self.message_counts: Dict[str, int] = {}
        self.last_messages: Dict[str, float] = {}
        # Per-stream backoff state: {stream_name: (attempt_count, last_error_str)}
        self._backoff: Dict[str, list] = {}

    def _wrap_message(self, data: dict, exchange: str, instrument: str,
                      ins_type: str, obj: str) -> str:
        """Wrap raw exchange data in the format expected by lookups."""
        wrapped = {
            "exchange": exchange,
            "instrument": instrument,
            "insType": ins_type,
            "obj": obj,
            "btc_price": self.get_btc_price(),
            "timestamp": time.time(),
            "data": data
        }
        return json.dumps(wrapped)

    async def _connect_stream(self, name: str, url: str,
                              wrapper_params: tuple,
                              subscribe_msg: Optional[dict] = None):
        """Connect to a single websocket stream with exponential backoff."""
        exchange, instrument, ins_type, obj = wrapper_params
        self._backoff.setdefault(name, [0, ""])

        while self.running:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    self.ws_connections[name] = ws
                    self.message_counts[name] = 0
                    # Reset backoff on successful connection
                    self._backoff[name] = [0, ""]

                    if subscribe_msg:
                        await ws.send(json.dumps(subscribe_msg))

                    async for message in ws:
                        if not self.running:
                            break

                        try:
                            data = json.loads(message)
                            # Handle combined stream format
                            if "stream" in data:
                                data = data["data"]

                            wrapped = self._wrap_message(
                                data, exchange, instrument, ins_type, obj
                            )
                            self.message_counts[name] = self.message_counts.get(name, 0) + 1
                            self.last_messages[name] = time.time()
                            self.on_message(wrapped)
                        except json.JSONDecodeError as e:
                            logger.warning("Binance %s JSON decode error: %s", name, e)

            except websockets.exceptions.ConnectionClosed as e:
                if self.running:
                    err_str = str(e)
                    attempts, last_err = self._backoff[name]
                    attempts += 1
                    if err_str == last_err and attempts > _REPEATED_ERROR_THRESHOLD:
                        logger.debug("Binance %s connection closed: %s", name, e)
                    else:
                        logger.error("Binance %s connection closed: %s", name, e)
                    if err_str != last_err:
                        attempts = 1
                    self._backoff[name] = [attempts, err_str]
                    delay = min(_BACKOFF_INITIAL * (2 ** (attempts - 1)), _BACKOFF_MAX)
                    delay *= random.uniform(_BACKOFF_JITTER_LOW, _BACKOFF_JITTER_HIGH)
                    logger.info("Binance %s reconnecting in %.1fs (attempt %d)", name, delay, attempts)
                    await asyncio.sleep(delay)
            except Exception as e:
                if self.running:
                    err_str = str(e)
                    attempts, last_err = self._backoff[name]
                    attempts += 1
                    if err_str == last_err and attempts > _REPEATED_ERROR_THRESHOLD:
                        logger.debug("Binance %s error: %s", name, e)
                    else:
                        logger.error("Binance %s error: %s", name, e, exc_info=True)
                    if err_str != last_err:
                        attempts = 1
                    self._backoff[name] = [attempts, err_str]
                    delay = min(_BACKOFF_INITIAL * (2 ** (attempts - 1)), _BACKOFF_MAX)
                    delay *= random.uniform(_BACKOFF_JITTER_LOW, _BACKOFF_JITTER_HIGH)
                    logger.info("Binance %s reconnecting in %.1fs (attempt %d)", name, delay, attempts)
                    await asyncio.sleep(delay)

    async def connect_all(self):
        """Connect to all Binance BTC perpetual streams."""
        self.running = True

        # Define all streams
        streams = [
            # Depth stream (orderbook)
            ("depth_btcusdt",
             f"{self.BASE_WS}/btcusdt@depth@100ms",
             ("binance", "btcusdt", "perpetual", "depth"), None),

            # Trades streams (all tracked symbols)
            ("trades_btcusdt",
             f"{self.BASE_WS}/btcusdt@aggTrade",
             ("binance", "btcusdt", "perpetual", "trades"), None),

            ("trades_ethusdt",
             f"{self.BASE_WS}/ethusdt@aggTrade",
             ("binance", "ethusdt", "perpetual", "trades"), None),

            ("trades_solusdt",
             f"{self.BASE_WS}/solusdt@aggTrade",
             ("binance", "solusdt", "perpetual", "trades"), None),

            # Liquidations stream (all symbols, we filter for BTC)
            ("liquidations",
             f"{self.BASE_WS}/!forceOrder@arr",
             ("binance", "btcusdt", "perpetual", "liquidations"), None),

            # Mark price streams (includes funding rate) — all tracked symbols
            ("markprice_btcusdt",
             f"{self.BASE_WS}/btcusdt@markPrice@1s",
             ("binance", "btcusdt", "perpetual", "markprice"), None),

            ("markprice_ethusdt",
             f"{self.BASE_WS}/ethusdt@markPrice@1s",
             ("binance", "ethusdt", "perpetual", "markprice"), None),

            ("markprice_solusdt",
             f"{self.BASE_WS}/solusdt@markPrice@1s",
             ("binance", "solusdt", "perpetual", "markprice"), None),
        ]

        # Start all stream connections
        tasks = [
            self._connect_stream(name, url, params, sub_msg)
            for name, url, params, sub_msg in streams
        ]

        await asyncio.gather(*tasks, return_exceptions=True)

    def stop(self):
        """Stop all connections."""
        self.running = False


class OKXConnector:
    """Connects to OKX websocket streams for perpetuals (multi-symbol).

    The liquidation-orders subscription uses instType=SWAP which receives
    liquidations for ALL SWAP instruments (BTC, ETH, SOL, etc.) in a
    single subscription — no per-symbol filtering needed.
    """

    WS_URL = "wss://ws.okx.com:8443/ws/v5/public"

    # OKX uses "BTC-USDT-SWAP" format. Map from our BTCUSDT to OKX instId.
    _SYMBOL_TO_OKX_INST = {
        "BTCUSDT": "BTC-USDT-SWAP",
        "ETHUSDT": "ETH-USDT-SWAP",
        "SOLUSDT": "SOL-USDT-SWAP",
    }

    def __init__(self, on_message: Callable[[str], None],
                 btc_price_getter: Callable[[], float],
                 symbols: Optional[List[str]] = None):
        self.on_message = on_message
        self.get_btc_price = btc_price_getter
        self.symbols = symbols or list(DEFAULT_SYMBOLS)
        self.running = False
        self.message_counts: Dict[str, int] = {}
        self.last_messages: Dict[str, float] = {}
        self._attempt: int = 0
        self._last_err: str = ""

    def _wrap_message(self, data: dict, exchange: str, instrument: str,
                      ins_type: str, obj: str) -> str:
        wrapped = {
            "exchange": exchange,
            "instrument": instrument,
            "insType": ins_type,
            "obj": obj,
            "btc_price": self.get_btc_price(),
            "timestamp": time.time(),
            "data": data
        }
        return json.dumps(wrapped)

    @staticmethod
    def _extract_instrument(data: dict) -> str:
        """Extract instrument from OKX message data.

        For liquidation-orders: data["data"][0]["instId"] → "BTC-USDT-SWAP" → "btcusdt"
        For per-instrument channels: data["arg"]["instId"] → same conversion.
        Falls back to "unknown".
        """
        # Try data[0].instId first (liquidation-orders, etc.)
        items = data.get("data")
        if items and isinstance(items, list) and len(items) > 0:
            inst_id = items[0].get("instId", "")
            if inst_id:
                # "BTC-USDT-SWAP" → "btcusdt"
                return inst_id.replace("-SWAP", "").replace("-", "").lower()

        # Try arg.instId (per-instrument subscriptions)
        inst_id = data.get("arg", {}).get("instId", "")
        if inst_id:
            return inst_id.replace("-SWAP", "").replace("-", "").lower()

        return "unknown"

    async def connect_all(self):
        """Connect to OKX perpetual streams for all configured symbols."""
        self.running = True

        while self.running:
            try:
                async with websockets.connect(self.WS_URL, ping_interval=20) as ws:
                    self._attempt = 0
                    self._last_err = ""
                    # Build subscription args
                    args = []
                    # BTC-specific data streams (orderbook, trades, funding, OI)
                    args.append({"channel": "books5", "instId": "BTC-USDT-SWAP"})
                    args.append({"channel": "trades", "instId": "BTC-USDT-SWAP"})
                    args.append({"channel": "funding-rate", "instId": "BTC-USDT-SWAP"})
                    args.append({"channel": "open-interest", "instId": "BTC-USDT-SWAP"})
                    # Liquidation-orders for ALL SWAP instruments (one subscription
                    # covers BTC, ETH, SOL, and everything else — no per-symbol
                    # filtering needed)
                    args.append({"channel": "liquidation-orders", "instType": "SWAP"})

                    subscribe = {"op": "subscribe", "args": args}
                    await ws.send(json.dumps(subscribe))

                    async for message in ws:
                        if not self.running:
                            break

                        try:
                            data = json.loads(message)
                            if "event" in data:  # Skip subscription confirmations
                                continue

                            channel = data.get("arg", {}).get("channel", "")
                            obj_map = {
                                "books5": "depth",
                                "trades": "trades",
                                "funding-rate": "funding",
                                "open-interest": "oi",
                                "liquidation-orders": "liquidations"
                            }
                            obj = obj_map.get(channel, channel)

                            instrument = self._extract_instrument(data)

                            wrapped = self._wrap_message(
                                data, "okx", instrument, "perpetual", obj
                            )
                            self.message_counts[obj] = self.message_counts.get(obj, 0) + 1
                            self.last_messages[obj] = time.time()
                            self.on_message(wrapped)
                        except json.JSONDecodeError as e:
                            logger.warning("OKX JSON decode error: %s", e)

            except websockets.exceptions.ConnectionClosed as e:
                if self.running:
                    err_str = str(e)
                    self._attempt += 1
                    if err_str == self._last_err and self._attempt > _REPEATED_ERROR_THRESHOLD:
                        logger.debug("OKX connection closed: %s", e)
                    else:
                        logger.error("OKX connection closed: %s", e)
                    if err_str != self._last_err:
                        self._attempt = 1
                    self._last_err = err_str
                    delay = min(_BACKOFF_INITIAL * (2 ** (self._attempt - 1)), _BACKOFF_MAX)
                    delay *= random.uniform(_BACKOFF_JITTER_LOW, _BACKOFF_JITTER_HIGH)
                    logger.info("OKX reconnecting in %.1fs (attempt %d)", delay, self._attempt)
                    await asyncio.sleep(delay)
            except Exception as e:
                if self.running:
                    err_str = str(e)
                    self._attempt += 1
                    if err_str == self._last_err and self._attempt > _REPEATED_ERROR_THRESHOLD:
                        logger.debug("OKX error: %s", e)
                    else:
                        logger.error("OKX error: %s", e, exc_info=True)
                    if err_str != self._last_err:
                        self._attempt = 1
                    self._last_err = err_str
                    delay = min(_BACKOFF_INITIAL * (2 ** (self._attempt - 1)), _BACKOFF_MAX)
                    delay *= random.uniform(_BACKOFF_JITTER_LOW, _BACKOFF_JITTER_HIGH)
                    logger.info("OKX reconnecting in %.1fs (attempt %d)", delay, self._attempt)
                    await asyncio.sleep(delay)

    def stop(self):
        self.running = False


class BybitConnector:
    """Connects to Bybit V5 websocket streams for perpetuals (multi-symbol)."""

    WS_URL = "wss://stream.bybit.com/v5/public/linear"

    def __init__(self, on_message: Callable[[str], None],
                 btc_price_getter: Callable[[], float],
                 symbols: Optional[List[str]] = None):
        self.on_message = on_message
        self.get_btc_price = btc_price_getter
        self.symbols = symbols or list(DEFAULT_SYMBOLS)
        self.running = False
        self.message_counts: Dict[str, int] = {}
        self.last_messages: Dict[str, float] = {}
        self._attempt: int = 0
        self._last_err: str = ""

    def _wrap_message(self, data: dict, exchange: str, instrument: str,
                      ins_type: str, obj: str) -> str:
        wrapped = {
            "exchange": exchange,
            "instrument": instrument,
            "insType": ins_type,
            "obj": obj,
            "btc_price": self.get_btc_price(),
            "timestamp": time.time(),
            "data": data
        }
        return json.dumps(wrapped)

    @staticmethod
    def _extract_instrument(data: dict) -> str:
        """Extract instrument from Bybit message data.

        For liquidation messages, the symbol is in data["data"][0]["s"].
        For other messages, parse from the topic string.
        Falls back to "unknown".
        """
        topic = data.get("topic", "")

        # Liquidation messages: allLiquidation.BTCUSDT → data[].s
        if "Liquidation" in topic or "liquidation" in topic:
            items = data.get("data")
            if items and isinstance(items, list) and len(items) > 0:
                sym = items[0].get("s", "")
                if sym:
                    return sym.lower()

        # Other topics: "orderbook.50.BTCUSDT" → BTCUSDT
        parts = topic.split(".")
        if len(parts) >= 2:
            return parts[-1].lower()

        return "unknown"

    async def connect_all(self):
        """Connect to Bybit perpetual streams for all configured symbols."""
        self.running = True

        while self.running:
            try:
                async with websockets.connect(self.WS_URL, ping_interval=20) as ws:
                    # Reset backoff on successful connection
                    self._attempt = 0
                    self._last_err = ""
                    # Build subscription args for all symbols
                    args = []
                    # BTC-specific data streams (orderbook, trades, tickers)
                    args.append("orderbook.50.BTCUSDT")
                    args.append("publicTrade.BTCUSDT")
                    args.append("tickers.BTCUSDT")
                    # Liquidation streams for ALL configured symbols
                    for sym in self.symbols:
                        args.append(f"allLiquidation.{sym}")

                    subscribe = {"op": "subscribe", "args": args}
                    await ws.send(json.dumps(subscribe))

                    async for message in ws:
                        if not self.running:
                            break

                        try:
                            data = json.loads(message)
                            if "success" in data:  # Skip subscription confirmations
                                continue

                            topic = data.get("topic", "")
                            if "orderbook" in topic:
                                obj = "depth"
                            elif "Trade" in topic:
                                obj = "trades"
                            elif "tickers" in topic:
                                obj = "oifunding"
                            elif "Liquidation" in topic or "liquidation" in topic:
                                obj = "liquidations"
                            else:
                                obj = topic

                            instrument = self._extract_instrument(data)

                            wrapped = self._wrap_message(
                                data, "bybit", instrument, "perpetual", obj
                            )
                            self.message_counts[obj] = self.message_counts.get(obj, 0) + 1
                            self.last_messages[obj] = time.time()
                            self.on_message(wrapped)
                        except json.JSONDecodeError as e:
                            logger.warning("Bybit JSON decode error: %s", e)

            except websockets.exceptions.ConnectionClosed as e:
                if self.running:
                    err_str = str(e)
                    self._attempt += 1
                    if err_str == self._last_err and self._attempt > _REPEATED_ERROR_THRESHOLD:
                        logger.debug("Bybit connection closed: %s", e)
                    else:
                        logger.error("Bybit connection closed: %s", e)
                    if err_str != self._last_err:
                        self._attempt = 1
                    self._last_err = err_str
                    delay = min(_BACKOFF_INITIAL * (2 ** (self._attempt - 1)), _BACKOFF_MAX)
                    delay *= random.uniform(_BACKOFF_JITTER_LOW, _BACKOFF_JITTER_HIGH)
                    logger.info("Bybit reconnecting in %.1fs (attempt %d)", delay, self._attempt)
                    await asyncio.sleep(delay)
            except Exception as e:
                if self.running:
                    err_str = str(e)
                    self._attempt += 1
                    if err_str == self._last_err and self._attempt > _REPEATED_ERROR_THRESHOLD:
                        logger.debug("Bybit error: %s", e)
                    else:
                        logger.error("Bybit error: %s", e, exc_info=True)
                    if err_str != self._last_err:
                        self._attempt = 1
                    self._last_err = err_str
                    delay = min(_BACKOFF_INITIAL * (2 ** (self._attempt - 1)), _BACKOFF_MAX)
                    delay *= random.uniform(_BACKOFF_JITTER_LOW, _BACKOFF_JITTER_HIGH)
                    logger.info("Bybit reconnecting in %.1fs (attempt %d)", delay, self._attempt)
                    await asyncio.sleep(delay)

    def stop(self):
        self.running = False


class MultiExchangeConnector:
    """Manages connections to multiple exchanges.

    Args:
        on_message: Callback receiving wrapped JSON message strings.
        symbols:    List of symbols for multi-symbol connectors (Bybit, OKX).
                    Defaults to DEFAULT_SYMBOLS (BTCUSDT, ETHUSDT, SOLUSDT).
                    Binance uses !forceOrder@arr which already gets all symbols.
    """

    def __init__(self, on_message: Callable[[str], None],
                 symbols: Optional[List[str]] = None):
        self.btc_price = 0.0
        self.on_message = on_message
        self.symbols = symbols or list(DEFAULT_SYMBOLS)

        self.binance = BinanceConnector(on_message, lambda: self.btc_price)
        self.okx = OKXConnector(on_message, lambda: self.btc_price,
                                symbols=self.symbols)
        self.bybit = BybitConnector(on_message, lambda: self.btc_price,
                                    symbols=self.symbols)

        self.connectors = {
            "binance": self.binance,
            "okx": self.okx,
            "bybit": self.bybit,
        }

    def update_btc_price(self, price: float):
        """Update the current BTC price for all connectors."""
        if price > 0:
            self.btc_price = price

    async def connect_all(self, exchanges: list = None):
        """Connect to specified exchanges (default: all)."""
        if exchanges is None:
            exchanges = list(self.connectors.keys())

        tasks = []
        for name in exchanges:
            if name in self.connectors:
                tasks.append(self.connectors[name].connect_all())

        await asyncio.gather(*tasks, return_exceptions=True)

    def stop(self):
        """Stop all connections."""
        for connector in self.connectors.values():
            connector.stop()

    def get_stats(self) -> Dict[str, Dict]:
        """Get message statistics from all connectors."""
        stats = {}
        for name, connector in self.connectors.items():
            stats[name] = {
                "counts": connector.message_counts.copy(),
                "last": connector.last_messages.copy()
            }
        return stats
