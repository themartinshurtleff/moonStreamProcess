"""
Multi-exchange Open Interest poller.

Fetches OI from Binance, Bybit, and OKX for each tracked symbol,
aggregates across exchanges, and delivers per-symbol updates via callback.

All OI values are normalized to BASE ASSET (e.g. BTC, not contracts or USD).

Exchange endpoints:
  - Binance: GET /fapi/v1/openInterest → float(openInterest)  (base asset)
  - Bybit:   GET /v5/market/open-interest → float(list[0].openInterest)  (base asset)
  - OKX:     GET /api/v5/public/open-interest → float(data[0].oiCcy)  (base currency)
             NOTE: use "oiCcy" NOT "oi" — "oi" is in contracts, "oiCcy" is base asset.

Rate budget: 3 symbols × 3 exchanges × every 15s = 36 req/min total.
Well within all exchange rate limits.
"""

import asyncio
import logging
import time
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

# Exchange REST base URLs
BINANCE_BASE = "https://fapi.binance.com"
BYBIT_BASE = "https://api.bybit.com"
OKX_BASE = "https://www.okx.com"

# Default poll interval (seconds)
DEFAULT_POLL_INTERVAL = 15.0

# HTTP timeout per request (seconds)
HTTP_TIMEOUT = 10.0


@dataclass
class OISnapshot:
    """Per-symbol aggregated OI snapshot."""
    symbol: str               # short symbol ("BTC", "ETH", "SOL")
    aggregated_oi: float      # sum of all exchange OIs (base asset)
    per_exchange: Dict[str, float]  # {"binance": 123.4, "bybit": 56.7, ...}
    ts: float                 # unix timestamp


class MultiExchangeOIPoller:
    """
    Polls open interest from Binance, Bybit, and OKX for multiple symbols.

    Constructor args:
        symbols:        List of short symbols, e.g. ["BTC", "ETH", "SOL"]
        on_oi_update:   Callback(symbol_short, aggregated_oi, per_exchange_dict)
        poll_interval:  Seconds between poll cycles (default 15)
    """

    def __init__(
        self,
        symbols: List[str],
        on_oi_update: Callable[[str, float, Dict[str, float]], None],
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ):
        self.symbols = symbols
        self.on_oi_update = on_oi_update
        self.poll_interval = poll_interval
        self.running = False
        self._session: Optional[aiohttp.ClientSession] = None

        # Latest snapshots per symbol — protected by _snapshot_lock for
        # thread-safe access between the poller thread and API thread.
        self.snapshots: Dict[str, OISnapshot] = {}
        self._snapshot_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Per-exchange fetch functions — return OI in BASE ASSET or None
    # ------------------------------------------------------------------

    async def _fetch_binance(self, symbol_short: str) -> Optional[float]:
        """Fetch OI from Binance Futures.

        GET /fapi/v1/openInterest?symbol={SYMBOL}USDT
        Response: {"openInterest": "12345.678", "symbol": "BTCUSDT", "time": ...}
        openInterest is already in base asset.
        """
        url = f"{BINANCE_BASE}/fapi/v1/openInterest"
        params = {"symbol": f"{symbol_short}USDT"}
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning(
                        "OI Binance %s: HTTP %d", symbol_short, resp.status
                    )
                    return None
                data = await resp.json()
                return float(data["openInterest"])
        except Exception as e:
            logger.warning("OI Binance %s: %s", symbol_short, e)
            return None

    async def _fetch_bybit(self, symbol_short: str) -> Optional[float]:
        """Fetch OI from Bybit V5.

        GET /v5/market/open-interest?category=linear&symbol={SYMBOL}USDT
            &intervalTime=5min&limit=1
        Response: {"result": {"list": [{"openInterest": "12345.678", ...}]}}
        openInterest is in base asset.
        """
        url = f"{BYBIT_BASE}/v5/market/open-interest"
        params = {
            "category": "linear",
            "symbol": f"{symbol_short}USDT",
            "intervalTime": "5min",
            "limit": "1",
        }
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning(
                        "OI Bybit %s: HTTP %d", symbol_short, resp.status
                    )
                    return None
                data = await resp.json()
                items = data.get("result", {}).get("list", [])
                if not items:
                    logger.warning("OI Bybit %s: empty list", symbol_short)
                    return None
                return float(items[0]["openInterest"])
        except Exception as e:
            logger.warning("OI Bybit %s: %s", symbol_short, e)
            return None

    async def _fetch_okx(self, symbol_short: str) -> Optional[float]:
        """Fetch OI from OKX.

        GET /api/v5/public/open-interest?instId={SYMBOL}-USDT-SWAP
        Response: {"data": [{"instId": "BTC-USDT-SWAP", "oi": "...", "oiCcy": "12345.67", ...}]}
        CRITICAL: use "oiCcy" (base currency) NOT "oi" (contracts).
        """
        url = f"{OKX_BASE}/api/v5/public/open-interest"
        params = {"instId": f"{symbol_short}-USDT-SWAP"}
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning(
                        "OI OKX %s: HTTP %d", symbol_short, resp.status
                    )
                    return None
                data = await resp.json()
                items = data.get("data", [])
                if not items:
                    logger.warning("OI OKX %s: empty data", symbol_short)
                    return None
                return float(items[0]["oiCcy"])
        except Exception as e:
            logger.warning("OI OKX %s: %s", symbol_short, e)
            return None

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def _poll_symbol(self, symbol_short: str):
        """Fetch OI from all 3 exchanges for one symbol, aggregate, callback."""
        results = await asyncio.gather(
            self._fetch_binance(symbol_short),
            self._fetch_bybit(symbol_short),
            self._fetch_okx(symbol_short),
            return_exceptions=True,
        )

        per_exchange: Dict[str, float] = {}
        exchange_names = ["binance", "bybit", "okx"]

        for name, result in zip(exchange_names, results):
            if isinstance(result, Exception):
                logger.warning("OI %s %s: exception %s", name, symbol_short, result)
            elif result is not None:
                per_exchange[name] = result

        # Skip callback if ALL exchanges failed — do not deliver stale 0.0
        if not per_exchange:
            logger.warning("OI %s: all exchanges failed, skipping callback", symbol_short)
            return

        aggregated = sum(per_exchange.values())

        # Store snapshot (thread-safe write)
        with self._snapshot_lock:
            self.snapshots[symbol_short] = OISnapshot(
                symbol=symbol_short,
                aggregated_oi=aggregated,
                per_exchange=dict(per_exchange),
                ts=time.time(),
            )

        # Deliver to callback
        try:
            self.on_oi_update(symbol_short, aggregated, per_exchange)
        except Exception as e:
            logger.warning("OI callback error for %s: %s", symbol_short, e)

    async def _poll_loop(self):
        """Main poll loop — all symbols every poll_interval seconds."""
        while self.running:
            try:
                # Poll all symbols concurrently
                await asyncio.gather(
                    *(self._poll_symbol(sym) for sym in self.symbols),
                    return_exceptions=True,
                )
            except Exception as e:
                logger.warning("OI poll loop error: %s", e)

            await asyncio.sleep(self.poll_interval)

    async def start(self):
        """Start the polling loop (async entry point)."""
        self.running = True
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
        self._session = aiohttp.ClientSession(timeout=timeout)
        try:
            await self._poll_loop()
        finally:
            await self._session.close()
            self._session = None

    def stop(self):
        """Signal the poll loop to stop."""
        self.running = False

    def get_snapshot(self, symbol: str) -> Optional[OISnapshot]:
        """Get the latest OI snapshot for a symbol (thread-safe read)."""
        with self._snapshot_lock:
            return self.snapshots.get(symbol)


class OIPollerThread:
    """Thread-safe wrapper that runs MultiExchangeOIPoller in a daemon thread."""

    def __init__(
        self,
        symbols: List[str],
        on_oi_update: Callable[[str, float, Dict[str, float]], None],
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ):
        self.poller = MultiExchangeOIPoller(
            symbols=symbols,
            on_oi_update=on_oi_update,
            poll_interval=poll_interval,
        )
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self.poller.start())
        except Exception as e:
            logger.warning("OI poller thread error: %s", e)
        finally:
            self._loop.close()

    def start(self):
        """Start the poller in a background daemon thread."""
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="oi-poller"
        )
        self._thread.start()

    def stop(self):
        """Stop the poller and join the thread."""
        self.poller.stop()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                logger.warning("OI poller thread did not terminate within 5s")

    def get_snapshot(self, symbol: str) -> Optional[OISnapshot]:
        """Get latest OI snapshot (thread-safe)."""
        return self.poller.get_snapshot(symbol)
