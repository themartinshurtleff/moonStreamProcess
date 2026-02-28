"""
Binance REST API pollers for OI and trader ratios.
Runs alongside websocket streams to fill data gaps.

Endpoints:
- Open Interest: GET /fapi/v1/openInterest
- Top Trader Position Ratio: GET /futures/data/topLongShortPositionRatio
- Top Trader Account Ratio: GET /futures/data/topLongShortAccountRatio
- Global Long/Short Ratio: GET /futures/data/globalLongShortAccountRatio
"""

import asyncio
import aiohttp
import time
import random
import json
from dataclasses import dataclass, field
from typing import Dict, Optional, Callable, List
from datetime import datetime


# Binance Futures REST base
BINANCE_FAPI_BASE = "https://fapi.binance.com"

# Default polling intervals (seconds)
DEFAULT_OI_INTERVAL = 10  # OI updates frequently
DEFAULT_RATIO_INTERVAL = 60  # Ratios update every 5min anyway

# Ratio period: try "1m" first, fallback to "5m" if rejected
RATIO_PERIOD_PRIMARY = "1m"
RATIO_PERIOD_FALLBACK = "5m"

# Rate limit backoff settings
INITIAL_BACKOFF = 5
MAX_BACKOFF = 300  # 5 minutes
BACKOFF_MULTIPLIER = 2

# Jitter range (±10%)
JITTER_RANGE = 0.10


@dataclass
class OIData:
    """Open Interest data for a symbol."""
    symbol: str
    oi_value: float  # In USD for USDT-M
    oi_qty: float    # In contracts
    timestamp: float
    source: str = "binance_rest"


@dataclass
class RatioData:
    """Trader ratio data for a symbol."""
    symbol: str
    long_ratio: float
    short_ratio: float
    long_short_ratio: float
    timestamp: float
    ratio_type: str  # "TTA", "TTP", or "GTA"


@dataclass
class PollerState:
    """State for the REST pollers."""
    # OI data per symbol
    oi_data: Dict[str, OIData] = field(default_factory=dict)
    prev_oi: Dict[str, float] = field(default_factory=dict)

    # Ratio data per symbol
    tta_ratio: Dict[str, RatioData] = field(default_factory=dict)
    ttp_ratio: Dict[str, RatioData] = field(default_factory=dict)
    gta_ratio: Dict[str, RatioData] = field(default_factory=dict)

    # Aggregated values
    total_oi: float = 0.0
    oi_change: float = 0.0

    # Weighted ratios (by OI)
    weighted_tta: float = 0.0
    weighted_ttp: float = 0.0
    weighted_gta: float = 0.0

    # Status
    last_oi_update: float = 0.0
    last_ratio_update: float = 0.0
    errors: List[str] = field(default_factory=list)


class BinanceRESTPoller:
    """
    Async poller for Binance REST endpoints.
    Handles OI and trader ratios with direct HTTPS requests.
    """

    def __init__(
        self,
        symbols: List[str] = None,
        oi_interval: float = DEFAULT_OI_INTERVAL,
        ratio_interval: float = DEFAULT_RATIO_INTERVAL,
        on_update: Callable[[PollerState], None] = None,
        price_getter: Callable[[str], float] = None,
        debug: bool = False
    ):
        self.symbols = symbols or ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        self.oi_interval = oi_interval
        self.ratio_interval = ratio_interval
        self.on_update = on_update
        self.price_getter = price_getter  # Callback to get current price for a symbol
        self.debug = debug

        self.state = PollerState()
        self.running = False
        self.session: Optional[aiohttp.ClientSession] = None

        # Per-endpoint backoff tracking
        self._backoff = {
            "oi": INITIAL_BACKOFF,
            "tta": INITIAL_BACKOFF,
            "ttp": INITIAL_BACKOFF,
            "gta": INITIAL_BACKOFF,
        }
        self._last_error_time = {}

        # Ratio period with fallback
        self._ratio_period = RATIO_PERIOD_PRIMARY
        self._ratio_period_fallback_logged = False

        # Per-minute consolidated log tracking
        self._last_log_minute: int = -1

    def _add_jitter(self, interval: float) -> float:
        """Add ±10% jitter to interval."""
        jitter = interval * JITTER_RANGE
        return interval + random.uniform(-jitter, jitter)

    def _log(self, msg: str):
        """Debug log."""
        if self.debug:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[REST {ts}] {msg}")

    async def _request(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """Make a direct REST request with error handling."""
        url = f"{BINANCE_FAPI_BASE}{endpoint}"

        try:
            async with self.session.get(url, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status in (418, 429):
                    # Rate limited - backoff will be applied
                    self._log(f"Rate limited ({resp.status}): {endpoint}, backing off")
                    return None
                elif resp.status in (403, 451):
                    # Forbidden / unavailable in region
                    self._log(f"HTTP {resp.status}: {endpoint}, region restricted or forbidden")
                    return None
                else:
                    self._log(f"HTTP {resp.status}: {endpoint}")
                    return None
        except aiohttp.ClientError as e:
            self._log(f"Request error: {endpoint} - {e}")
            return None
        except Exception as e:
            self._log(f"Unexpected error: {endpoint} - {e}")
            return None

    async def poll_oi(self, symbol: str) -> Optional[OIData]:
        """Poll open interest for a symbol."""
        data = await self._request("/fapi/v1/openInterest", {"symbol": symbol})

        if data:
            try:
                oi_qty = float(data.get("openInterest", 0))
                # For USDT-M, we need to multiply by mark price to get USD value
                # The endpoint returns contracts, we'll estimate USD value later

                oi_data = OIData(
                    symbol=symbol,
                    oi_value=0,  # Will be computed with price
                    oi_qty=oi_qty,
                    timestamp=time.time()
                )

                self._log(f"OI [{symbol}]: qty={oi_qty:,.2f}")
                return oi_data
            except (KeyError, ValueError, TypeError) as e:
                self._log(f"OI parse error [{symbol}]: {e}")

        return None

    async def poll_ratio(self, symbol: str, ratio_type: str) -> Optional[RatioData]:
        """Poll trader ratio for a symbol with period fallback."""
        endpoint_map = {
            "TTA": "/futures/data/topLongShortAccountRatio",
            "TTP": "/futures/data/topLongShortPositionRatio",
            "GTA": "/futures/data/globalLongShortAccountRatio",
        }

        endpoint = endpoint_map.get(ratio_type)
        if not endpoint:
            return None

        # Try current period
        data = await self._request(endpoint, {
            "symbol": symbol,
            "period": self._ratio_period,
            "limit": 1
        })

        # If primary period fails with empty/error, try fallback
        if (data is None or (isinstance(data, list) and len(data) == 0)) and self._ratio_period == RATIO_PERIOD_PRIMARY:
            # Try fallback period
            data = await self._request(endpoint, {
                "symbol": symbol,
                "period": RATIO_PERIOD_FALLBACK,
                "limit": 1
            })
            if data and isinstance(data, list) and len(data) > 0:
                # Fallback worked, switch permanently
                self._ratio_period = RATIO_PERIOD_FALLBACK
                if not self._ratio_period_fallback_logged:
                    self._log(f"Ratio period '{RATIO_PERIOD_PRIMARY}' not supported, using '{RATIO_PERIOD_FALLBACK}'")
                    self._ratio_period_fallback_logged = True

        if data and isinstance(data, list) and len(data) > 0:
            try:
                item = data[0]
                long_ratio = float(item.get("longAccount", item.get("longPosition", 0)))
                short_ratio = float(item.get("shortAccount", item.get("shortPosition", 0)))
                ls_ratio = float(item.get("longShortRatio", 0))

                ratio_data = RatioData(
                    symbol=symbol,
                    long_ratio=long_ratio,
                    short_ratio=short_ratio,
                    long_short_ratio=ls_ratio,
                    timestamp=time.time(),
                    ratio_type=ratio_type
                )

                self._log(f"{ratio_type} [{symbol}]: L={long_ratio:.4f} S={short_ratio:.4f} L/S={ls_ratio:.4f}")
                return ratio_data
            except (KeyError, ValueError, TypeError) as e:
                self._log(f"{ratio_type} parse error [{symbol}]: {e}")

        return None

    def _update_aggregates(self):
        """Update aggregated values from per-symbol data."""
        # Total OI (in USD)
        total_oi = 0.0
        price_missing = []

        for symbol, oi_data in self.state.oi_data.items():
            # Get price from callback if available
            price = 0.0
            if self.price_getter:
                try:
                    price = self.price_getter(symbol)
                except Exception:
                    pass

            # Convert OI contracts to USD
            if oi_data.oi_qty > 0 and price > 0:
                oi_usd = oi_data.oi_qty * price
                oi_data.oi_value = oi_usd
                total_oi += oi_usd
            elif oi_data.oi_qty > 0 and price <= 0:
                price_missing.append(symbol)
            elif oi_data.oi_value > 0:
                total_oi += oi_data.oi_value

        # OI change
        prev_total = self.state.total_oi
        self.state.oi_change = total_oi - prev_total if prev_total > 0 else 0
        self.state.total_oi = total_oi

        # Weighted ratios (by OI)
        # For now, use simple average if OI weights not available
        def avg_ratio(ratio_dict: Dict[str, RatioData]) -> float:
            if not ratio_dict:
                return 0.0
            ratios = [r.long_short_ratio for r in ratio_dict.values() if r.long_short_ratio > 0]
            return sum(ratios) / len(ratios) if ratios else 0.0

        self.state.weighted_tta = avg_ratio(self.state.tta_ratio)
        self.state.weighted_ttp = avg_ratio(self.state.ttp_ratio)
        self.state.weighted_gta = avg_ratio(self.state.gta_ratio)

        # Per-minute consolidated log (once per minute)
        self._log_minute_summary(price_missing)

    def _log_minute_summary(self, price_missing: List[str]):
        """Log consolidated summary once per minute."""
        current_minute = datetime.now().minute
        if current_minute == self._last_log_minute:
            return
        self._last_log_minute = current_minute

        ts = datetime.now().strftime("%H:%M:%S")

        for symbol in self.symbols:
            oi_data = self.state.oi_data.get(symbol)
            tta = self.state.tta_ratio.get(symbol)
            ttp = self.state.ttp_ratio.get(symbol)
            gta = self.state.gta_ratio.get(symbol)

            # OI info
            if oi_data:
                oi_usd = oi_data.oi_value
                oi_qty = oi_data.oi_qty
                if oi_usd > 0:
                    oi_str = f"OI=${oi_usd:,.0f}"
                else:
                    oi_str = f"OI={oi_qty:,.0f}contracts (price=0)"
            else:
                oi_str = "OI=N/A"

            # OI change
            oi_chg = self.state.oi_change
            oi_chg_str = f"Δ={oi_chg:+,.0f}" if oi_chg != 0 else ""

            # Ratios
            tta_str = f"TTA={tta.long_short_ratio:.3f}" if tta else "TTA=N/A"
            ttp_str = f"TTP={ttp.long_short_ratio:.3f}" if ttp else "TTP=N/A"
            gta_str = f"GTA={gta.long_short_ratio:.3f}" if gta else "GTA=N/A"

            # Price warning
            price_warn = " [PRICE=0]" if symbol in price_missing else ""

            print(f"[REST {ts}] {symbol}: {oi_str} {oi_chg_str} | {tta_str} {ttp_str} {gta_str}{price_warn}")

    async def _oi_loop(self):
        """OI polling loop."""
        while self.running:
            try:
                for symbol in self.symbols:
                    if not self.running:
                        break

                    oi_data = await self.poll_oi(symbol)
                    if oi_data:
                        # Store previous for change calculation
                        if symbol in self.state.oi_data:
                            self.state.prev_oi[symbol] = self.state.oi_data[symbol].oi_qty
                        self.state.oi_data[symbol] = oi_data
                        self.state.last_oi_update = time.time()

                        # Reset backoff on success
                        self._backoff["oi"] = INITIAL_BACKOFF

                    # Small delay between symbols
                    await asyncio.sleep(0.2)

                # Update aggregates
                self._update_aggregates()

                # Notify callback
                if self.on_update:
                    self.on_update(self.state)

            except Exception as e:
                self._log(f"OI loop error: {e}")
                self._backoff["oi"] = min(self._backoff["oi"] * BACKOFF_MULTIPLIER, MAX_BACKOFF)

            # Sleep with jitter
            sleep_time = self._add_jitter(self.oi_interval)
            await asyncio.sleep(sleep_time)

    async def _ratio_loop(self):
        """Ratio polling loop."""
        while self.running:
            try:
                for symbol in self.symbols:
                    if not self.running:
                        break

                    # Poll all three ratio types
                    for ratio_type in ["TTA", "TTP", "GTA"]:
                        ratio_data = await self.poll_ratio(symbol, ratio_type)
                        if ratio_data:
                            target = getattr(self.state, f"{ratio_type.lower()}_ratio")
                            target[symbol] = ratio_data
                            self.state.last_ratio_update = time.time()

                        await asyncio.sleep(0.3)  # Rate limit between requests

                # Update aggregates
                self._update_aggregates()

                # Notify callback
                if self.on_update:
                    self.on_update(self.state)

            except Exception as e:
                self._log(f"Ratio loop error: {e}")

            # Sleep with jitter
            sleep_time = self._add_jitter(self.ratio_interval)
            await asyncio.sleep(sleep_time)

    async def self_test(self) -> Dict[str, bool]:
        """
        Run a startup self-test: one REST poll per endpoint.
        Returns dict of endpoint -> success.
        """
        results = {}

        if not self.session:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )

        print("\n=== Binance REST Poller Self-Test ===")

        # Test OI endpoint
        symbol = self.symbols[0] if self.symbols else "BTCUSDT"

        print(f"Testing OI endpoint for {symbol}...")
        try:
            data = await self._request("/fapi/v1/openInterest", {"symbol": symbol})
            if data:
                oi = float(data.get("openInterest", 0))
                print(f"  ✓ OI: {oi:,.2f} contracts")
                results["openInterest"] = True
            else:
                print(f"  ✗ OI: No data returned")
                results["openInterest"] = False
        except Exception as e:
            print(f"  ✗ OI: {e}")
            results["openInterest"] = False

        # Test ratio endpoints
        for ratio_type, endpoint in [
            ("TTA", "/futures/data/topLongShortAccountRatio"),
            ("TTP", "/futures/data/topLongShortPositionRatio"),
            ("GTA", "/futures/data/globalLongShortAccountRatio"),
        ]:
            print(f"Testing {ratio_type} endpoint for {symbol}...")
            try:
                data = await self._request(endpoint, {
                    "symbol": symbol,
                    "period": "5m",
                    "limit": 1
                })
                if data and isinstance(data, list) and len(data) > 0:
                    item = data[0]
                    ls_ratio = float(item.get("longShortRatio", 0))
                    print(f"  ✓ {ratio_type}: L/S ratio = {ls_ratio:.4f}")
                    results[ratio_type] = True
                else:
                    print(f"  ✗ {ratio_type}: No data returned")
                    results[ratio_type] = False
            except Exception as e:
                print(f"  ✗ {ratio_type}: {e}")
                results[ratio_type] = False

        success_count = sum(results.values())
        total = len(results)
        print(f"\nSelf-test: {success_count}/{total} endpoints OK")

        if success_count == 0:
            print("\n⚠️  All endpoints failed. Check network connectivity.")

        print("=" * 40 + "\n")

        return results

    async def start(self):
        """Start the poller loops."""
        self.running = True

        # Create session
        timeout = aiohttp.ClientTimeout(total=30)
        self.session = aiohttp.ClientSession(timeout=timeout)

        # Run self-test first
        await self.self_test()

        # Start polling loops
        self._log("Starting OI and ratio polling loops...")

        await asyncio.gather(
            self._oi_loop(),
            self._ratio_loop(),
            return_exceptions=True
        )

    async def stop(self):
        """Stop the poller."""
        self.running = False
        if self.session:
            await self.session.close()
            self.session = None

    def get_state(self) -> PollerState:
        """Get current poller state."""
        return self.state

    def get_oi_for_symbol(self, symbol: str) -> float:
        """Get OI value for a specific symbol (in contracts)."""
        if symbol in self.state.oi_data:
            return self.state.oi_data[symbol].oi_qty
        return 0.0

    def get_oi_change(self) -> float:
        """Get OI change since last update."""
        return self.state.oi_change


# Synchronous wrapper for use in threaded context
class BinanceRESTPollerThread:
    """
    Thread-safe wrapper that runs the async poller in a dedicated thread.
    """

    def __init__(
        self,
        symbols: List[str] = None,
        oi_interval: float = DEFAULT_OI_INTERVAL,
        ratio_interval: float = DEFAULT_RATIO_INTERVAL,
        on_update: Callable[[PollerState], None] = None,
        price_getter: Callable[[str], float] = None,
        debug: bool = False
    ):
        import threading

        self.poller = BinanceRESTPoller(
            symbols=symbols,
            oi_interval=oi_interval,
            ratio_interval=ratio_interval,
            on_update=on_update,
            price_getter=price_getter,
            debug=debug
        )

        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _run_loop(self):
        """Run the async event loop in a thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            self._loop.run_until_complete(self.poller.start())
        except Exception as e:
            print(f"REST poller thread error: {e}")
        finally:
            self._loop.close()

    def start(self):
        """Start the poller in a background thread."""
        import threading

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the poller thread and join."""
        if self._loop:
            self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self.poller.stop()))
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "REST poller thread did not terminate within 5s"
                )

    def get_state(self) -> PollerState:
        """Get current poller state (thread-safe read)."""
        return self.poller.get_state()
