#!/usr/bin/env python3
"""
Embedded API Server for Full Metrics Viewer

This module provides FastAPI endpoints that share buffers directly with the
collector (full_metrics_viewer.py), eliminating the disk sync issue where
the standalone API would load frames once at startup and never refresh.

Per-symbol snapshot files are read from disk every 5 seconds:
  liq_api_snapshot_{SYM}.json   (V1, per symbol)
  liq_api_snapshot_v2_{SYM}.json (V2, per symbol)

Per-symbol binary history files store frame history:
  liq_heatmap_v1_{SYM}.bin  (V1 history, per symbol)
  liq_heatmap_v2_{SYM}.bin  (V2 history, per symbol)

Usage:
    # In full_metrics_viewer.py:
    from embedded_api import create_embedded_app, start_api_thread

    app = create_embedded_app(
        ob_buffer=processor.ob_heatmap_buffer,
        snapshot_dir=POC_DIR,
        engine_manager=processor.engine_manager
    )
    api_thread = start_api_thread(app, host="127.0.0.1", port=8899)
"""

import copy
import json
import os
import struct
import time
import threading
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional, Any, Tuple
import logging

logger = logging.getLogger(__name__)

# Try to import FastAPI
HAS_FASTAPI = False
try:
    from fastapi import FastAPI, Query, HTTPException
    from fastapi.responses import JSONResponse, Response
    import uvicorn
    HAS_FASTAPI = True
except ImportError:
    pass

# Try to import orderbook heatmap module
HAS_OB_HEATMAP = False
try:
    from ob_heatmap import (
        OrderbookHeatmapBuffer, OrderbookFrame,
        build_unified_grid as ob_build_unified_grid,
        resample_frame_to_grid,
        frame_to_binary, frames_to_binary,
        DEFAULT_STEP, DEFAULT_RANGE_PCT,
        FRAME_INTERVAL_MS, FRAME_INTERVAL_SEC
    )
    HAS_OB_HEATMAP = True
except ImportError:
    pass

# Try to import engine manager for V3 zone access
HAS_ENGINE_MANAGER = False
try:
    from engine_manager import EngineManager, FULL_TO_SHORT
    HAS_ENGINE_MANAGER = True
except ImportError:
    pass

# API version
API_VERSION = "3.0.0"  # V3 with zone lifecycle endpoints

# Valid symbols for all endpoints
VALID_SYMBOLS = {"BTC", "ETH", "SOL"}

# Default grid parameters
DEFAULT_STEP = 20.0
DEFAULT_BAND_PCT = 0.08


def _step_ndigits(step: float) -> int:
    """Compute decimal precision digits from a step size."""
    return max(0, -Decimal(str(step)).as_tuple().exponent)


def _build_price_grid(price_min: float, price_max: float, step: float) -> List[float]:
    """Build an index-based price grid with exact rounding.

    Avoids incremental ``p += step`` which causes floating-point drift
    for sub-integer steps (e.g. step=0.1).
    """
    ndigits = _step_ndigits(step)
    price_min = round(price_min, ndigits)
    price_max = round(price_max, ndigits)
    n_buckets = int(round((price_max - price_min) / step)) + 1
    return [round(price_min + i * step, ndigits) for i in range(n_buckets)]


def _get_fallback_price_range(symbol: str):
    """Returns (price_min, price_max) fallback range for a symbol when no data exists."""
    symbol = symbol.upper().replace("USDT", "")
    ranges = {
        "BTC": (85000, 115000),
        "ETH": (1500, 5000),
        "SOL": (50, 400),
    }
    return ranges.get(symbol, (100, 10000))  # wide default for unknown symbols

# History buffer size (12 hours at 1-minute resolution)
HISTORY_MINUTES = 720

# Liquidation heatmap persistence constants
LIQ_FRAME_RECORD_SIZE = 4096  # Fixed 4KB per frame for fast seek
LIQ_MAX_BUCKETS = 1000  # Max price buckets per frame
LIQ_HEADER_FORMAT = '<dddddI'
LIQ_HEADER_SIZE = struct.calcsize(LIQ_HEADER_FORMAT)  # 48 bytes


class ResponseCache:
    """
    Thread-safe in-memory response cache for API endpoints.

    Allows hundreds of concurrent users to receive the same cached response
    instead of each triggering separate data reads. Cache entries expire
    after a configurable TTL.

    Usage:
        cache = ResponseCache()
        cached = cache.get("liq_heatmap?symbol=BTC", ttl=5.0)
        if cached is not None:
            return cached
        # ... generate response ...
        cache.set("liq_heatmap?symbol=BTC", response)
        return response
    """

    def __init__(self):
        self._cache: Dict[str, Tuple[Any, float]] = {}
        self._lock = threading.Lock()
        self._sets_count: int = 0

    def get(self, key: str, ttl: float) -> Optional[JSONResponse]:
        """Return cached JSONResponse if within TTL, else None."""
        with self._lock:
            entry = self._cache.get(key)
            if entry and (time.time() - entry[1]) < ttl:
                return entry[0]
        return None

    def set(self, key: str, response: JSONResponse) -> None:
        """Store a JSONResponse in the cache."""
        with self._lock:
            self._cache[key] = (response, time.time())
            self._sets_count += 1
            if self._sets_count % 100 == 0:
                self._evict_expired()

    def _evict_expired(self) -> None:
        """Remove entries older than max_ttl to prevent unbounded growth. Caller must hold _lock."""
        now = time.time()
        max_ttl = 60.0  # nothing should be cached longer than this
        expired = [k for k, (_, ts) in self._cache.items() if (now - ts) > max_ttl]
        for k in expired:
            del self._cache[k]


@dataclass
class HistoryFrame:
    """A single frame of heatmap history (1 minute)."""
    ts: float
    src: float
    price_min: float
    price_max: float
    long_intensity: bytes  # u8 encoded
    short_intensity: bytes  # u8 encoded
    prices: List[float]


class LiquidationHeatmapBuffer:
    """
    Persistent ring buffer for liquidation heatmap history.
    Stores frames to disk for persistence across restarts.
    """

    def __init__(self, persistence_path: str, max_frames: int = HISTORY_MINUTES):
        self.persistence_path = persistence_path
        self.max_frames = max_frames
        self._lock = threading.RLock()
        self._frames: deque = deque(maxlen=max_frames)
        self._last_ts: float = 0

        # Load existing frames on init
        self._load_from_disk()

    def _load_from_disk(self):
        """Load frames from persistence file on startup."""
        if not os.path.exists(self.persistence_path):
            return

        file_size = os.path.getsize(self.persistence_path)
        total_frames = file_size // LIQ_FRAME_RECORD_SIZE

        if total_frames == 0:
            return

        frames_to_load = min(total_frames, self.max_frames)
        start_offset = (total_frames - frames_to_load) * LIQ_FRAME_RECORD_SIZE

        loaded = 0
        skipped = 0
        try:
            with open(self.persistence_path, 'rb') as f:
                f.seek(start_offset)
                for _ in range(frames_to_load):
                    record = f.read(LIQ_FRAME_RECORD_SIZE)
                    if len(record) < LIQ_FRAME_RECORD_SIZE:
                        break
                    try:
                        frame = self._frame_from_bytes(record)
                        if frame is not None:
                            self._frames.append(frame)
                            self._last_ts = frame.ts
                            loaded += 1
                        else:
                            skipped += 1
                    except Exception as e:
                        skipped += 1
                        logger.warning("Skipped corrupt frame in %s: %s",
                                       self.persistence_path, e)
        except Exception as e:
            logger.error("Failed to load history file %s: %s",
                         self.persistence_path, e, exc_info=True)
        if loaded > 0 or skipped > 0:
            logger.info("Loaded %d frames, skipped %d corrupt frames from %s",
                        loaded, skipped, self.persistence_path)

    def _frame_from_bytes(self, data: bytes) -> Optional[HistoryFrame]:
        """Deserialize frame from binary record."""
        if len(data) < LIQ_HEADER_SIZE:
            return None

        header = struct.unpack(LIQ_HEADER_FORMAT, data[:LIQ_HEADER_SIZE])
        ts, src, price_min, price_max, step, n_buckets = header

        if ts <= 0 or n_buckets == 0:
            return None

        long_start = LIQ_HEADER_SIZE
        short_start = LIQ_HEADER_SIZE + LIQ_MAX_BUCKETS
        long_intensity = data[long_start:long_start + n_buckets]
        short_intensity = data[short_start:short_start + n_buckets]

        prices = _build_price_grid(price_min, price_max, step)
        # Trim to match binary record bucket count
        if len(prices) > n_buckets:
            prices = prices[:n_buckets]

        return HistoryFrame(
            ts=ts,
            src=src,
            price_min=price_min,
            price_max=price_max,
            long_intensity=long_intensity,
            short_intensity=short_intensity,
            prices=prices
        )

    def add_frame(self, snapshot: Dict) -> bool:
        """Add a frame from a snapshot dict."""
        ts = snapshot.get('ts', 0)
        if ts <= 0:
            return False

        ts_minute = int(ts // 60)

        with self._lock:
            last_minute = int(self._last_ts // 60) if self._last_ts > 0 else -1
            if ts_minute <= last_minute:
                return False

            long_raw = snapshot.get('long_intensity', [])
            short_raw = snapshot.get('short_intensity', [])

            long_u8 = bytes([min(255, max(0, int(v * 255))) for v in long_raw])
            short_u8 = bytes([min(255, max(0, int(v * 255))) for v in short_raw])

            frame = HistoryFrame(
                ts=ts,
                src=snapshot.get('src', 0),
                price_min=snapshot.get('price_min', 0),
                price_max=snapshot.get('price_max', 0),
                long_intensity=long_u8,
                short_intensity=short_u8,
                prices=snapshot.get('prices', [])
            )

            self._frames.append(frame)
            self._last_ts = ts
            return True

    def get_frames(self, minutes: int = 60, stride: int = 1) -> List[HistoryFrame]:
        """Get recent frames with optional downsampling."""
        with self._lock:
            n_frames = min(minutes, len(self._frames))
            if n_frames == 0:
                return []
            frames = list(self._frames)[-n_frames:]
            if stride > 1:
                frames = frames[::stride]
            return frames

    def frame_count(self) -> int:
        with self._lock:
            return len(self._frames)

    def get_time_range(self) -> Tuple[float, float]:
        with self._lock:
            if not self._frames:
                return (0, 0)
            return (self._frames[0].ts, self._frames[-1].ts)

    def get_stats(self) -> dict:
        with self._lock:
            frames_in_memory = len(self._frames)
            last_ts = self._frames[-1].ts if self._frames else 0
            oldest_ts = self._frames[0].ts if self._frames else 0

        file_size = 0
        frames_on_disk = 0
        if os.path.exists(self.persistence_path):
            file_size = os.path.getsize(self.persistence_path)
            frames_on_disk = file_size // LIQ_FRAME_RECORD_SIZE

        return {
            "frames_in_memory": frames_in_memory,
            "frames_on_disk": frames_on_disk,
            "max_frames": self.max_frames,
            "last_ts": last_ts,
            "oldest_ts": oldest_ts,
            "file_size_bytes": file_size
        }


def build_unified_grid(
    frames: List[HistoryFrame],
    current_src: float,
    step: float = DEFAULT_STEP,
    band_pct: float = DEFAULT_BAND_PCT
) -> Tuple[List[float], float, float]:
    """Build a unified price grid that covers all frames."""
    if current_src <= 0:
        if frames:
            current_src = frames[-1].src
        else:
            return ([], 0, 0)

    ndigits = _step_ndigits(step)
    price_min = round(round((current_src * (1 - band_pct)) / step) * step, ndigits)
    price_max = round(round((current_src * (1 + band_pct)) / step) * step, ndigits)

    prices = _build_price_grid(price_min, price_max, step)

    return (prices, price_min, price_max)


def map_frame_to_grid(
    frame: HistoryFrame,
    target_prices: List[float],
    step: float
) -> Tuple[List[int], List[int]]:
    """Map a frame's intensities to a target grid."""
    frame_lookup_long = {}
    frame_lookup_short = {}

    for i, price in enumerate(frame.prices):
        if i < len(frame.long_intensity):
            frame_lookup_long[price] = frame.long_intensity[i]
        if i < len(frame.short_intensity):
            frame_lookup_short[price] = frame.short_intensity[i]

    long_out = []
    short_out = []

    for target_price in target_prices:
        best_long = frame_lookup_long.get(target_price, 0)
        best_short = frame_lookup_short.get(target_price, 0)
        long_out.append(best_long)
        short_out.append(best_short)

    return (long_out, short_out)


class EmbeddedAPIState:
    """
    Holds shared references to buffers and files.

    Per-symbol snapshot caches and history buffers (CLAUDE.md Rule #8:
    per-symbol buffers must be independent, never shared).
    """

    def __init__(
        self,
        ob_buffer: Optional['OrderbookHeatmapBuffer'] = None,
        snapshot_dir: Optional[str] = None,
        engine_manager: Optional['EngineManager'] = None,
        oi_poller: Optional[Any] = None,
        # Legacy params — accepted but ignored (kept for caller compat during transition)
        snapshot_file: Optional[str] = None,
        snapshot_v2_file: Optional[str] = None,
        liq_heatmap_v1_file: Optional[str] = None,
        liq_heatmap_v2_file: Optional[str] = None,
    ):
        self.ob_buffer = ob_buffer
        self.engine_manager = engine_manager
        self.oi_poller = oi_poller  # MultiExchangeOIPoller or OIPollerThread
        self.start_time = time.time()

        # Resolve snapshot directory: explicit param > inferred from legacy path > cwd
        if snapshot_dir:
            self._snapshot_dir = snapshot_dir
        elif snapshot_file:
            self._snapshot_dir = os.path.dirname(snapshot_file)
        else:
            self._snapshot_dir = os.path.dirname(__file__)

        # 5s minimum — CLAUDE.md rule: no disk I/O refresh intervals under 5s
        self._cache_ttl: float = 5.0

        # Per-symbol snapshot caches (CLAUDE.md Rule #8: independent per symbol)
        self._v1_caches: Dict[str, Optional[Dict]] = {sym: None for sym in VALID_SYMBOLS}
        self._v2_caches: Dict[str, Optional[Dict]] = {sym: None for sym in VALID_SYMBOLS}

        # Per-symbol history buffers with per-symbol binary files
        self.v1_histories: Dict[str, LiquidationHeatmapBuffer] = {}
        self.v2_histories: Dict[str, LiquidationHeatmapBuffer] = {}
        for sym in VALID_SYMBOLS:
            v1_bin = os.path.join(self._snapshot_dir, f"liq_heatmap_v1_{sym}.bin")
            v2_bin = os.path.join(self._snapshot_dir, f"liq_heatmap_v2_{sym}.bin")
            self.v1_histories[sym] = LiquidationHeatmapBuffer(v1_bin)
            self.v2_histories[sym] = LiquidationHeatmapBuffer(v2_bin)

        # Start background refresh thread
        self._running = True
        self._refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._refresh_thread.start()

    def _snapshot_path(self, version: str, symbol: str) -> str:
        """Build per-symbol snapshot file path."""
        if version == "v1":
            return os.path.join(self._snapshot_dir, f"liq_api_snapshot_{symbol}.json")
        return os.path.join(self._snapshot_dir, f"liq_api_snapshot_v2_{symbol}.json")

    def _refresh_loop(self):
        """Background loop to refresh per-symbol caches and add to history."""
        while self._running:
            for sym in VALID_SYMBOLS:
                self._refresh_symbol_cache(sym, "v1")
                self._refresh_symbol_cache(sym, "v2")
            time.sleep(self._cache_ttl)

    def _refresh_symbol_cache(self, symbol: str, version: str):
        """Refresh cache for one symbol+version. Never crashes the thread."""
        path = self._snapshot_path(version, symbol)
        if not os.path.exists(path):
            return  # File may not exist during warmup — normal
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            if version == "v1":
                self._v1_caches[symbol] = data
                self.v1_histories[symbol].add_frame(data)
            else:
                self._v2_caches[symbol] = data
                self.v2_histories[symbol].add_frame(data)
        except Exception as e:
            # Keep last good cached snapshot — never crash the refresh thread
            logger.warning("Cache refresh error (%s/%s): %s", version, symbol, e)

    def stop(self):
        self._running = False

    def get_v1_snapshot(self, symbol: str = "BTC") -> Optional[Dict]:
        return self._v1_caches.get(symbol)

    def get_v2_snapshot(self, symbol: str = "BTC") -> Optional[Dict]:
        return self._v2_caches.get(symbol)

    def get_v1_history(self, symbol: str = "BTC") -> Optional[LiquidationHeatmapBuffer]:
        return self.v1_histories.get(symbol)

    def get_v2_history(self, symbol: str = "BTC") -> Optional[LiquidationHeatmapBuffer]:
        return self.v2_histories.get(symbol)


def create_embedded_app(
    ob_buffer: Optional['OrderbookHeatmapBuffer'] = None,
    snapshot_dir: Optional[str] = None,
    engine_manager: Optional['EngineManager'] = None,
    oi_poller: Optional[Any] = None,
    # Legacy params — accepted for caller compat during transition, passed through
    snapshot_file: Optional[str] = None,
    snapshot_v2_file: Optional[str] = None,
    liq_heatmap_v1_file: Optional[str] = None,
    liq_heatmap_v2_file: Optional[str] = None,
) -> 'FastAPI':
    """
    Create FastAPI application with shared buffer references.

    Args:
        snapshot_dir: Directory containing per-symbol snapshot JSON and binary history files.
                      If not provided, inferred from legacy snapshot_file path or __file__.
    """
    if not HAS_FASTAPI:
        raise ImportError("FastAPI not installed. Install with: pip install fastapi uvicorn")

    # Create shared state
    state = EmbeddedAPIState(
        ob_buffer=ob_buffer,
        snapshot_dir=snapshot_dir,
        engine_manager=engine_manager,
        oi_poller=oi_poller,
        snapshot_file=snapshot_file,
        snapshot_v2_file=snapshot_v2_file,
        liq_heatmap_v1_file=liq_heatmap_v1_file,
        liq_heatmap_v2_file=liq_heatmap_v2_file,
    )

    # Response cache — shared across all concurrent requests
    cache = ResponseCache()

    app = FastAPI(
        title="Liquidation Heatmap API (Embedded)",
        description="API for liquidation zone visualization - embedded mode with shared buffers",
        version=API_VERSION
    )

    def _validate_symbol(symbol: str) -> str:
        """Validate and normalize symbol. Returns uppercase short symbol or raises 400."""
        sym = symbol.strip().upper().replace("USDT", "")
        if sym not in VALID_SYMBOLS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid symbol '{symbol}'. Valid symbols: {', '.join(sorted(VALID_SYMBOLS))}"
            )
        return sym

    @app.on_event("startup")
    async def startup():
        ob_frames = 0
        if state.ob_buffer:
            ob_stats = state.ob_buffer.get_stats()
            ob_frames = ob_stats.get('frames_in_memory', 0)

        print(f"[EMBEDDED_API] Started with shared buffer: {ob_frames} OB frames")
        print(f"[EMBEDDED_API] Snapshot dir: {state._snapshot_dir}")
        for sym in sorted(VALID_SYMBOLS):
            v1_frames = state.v1_histories[sym].frame_count()
            v2_frames = state.v2_histories[sym].frame_count()
            print(f"[EMBEDDED_API] {sym}: V1 history={v1_frames} frames, V2 history={v2_frames} frames")

    @app.on_event("shutdown")
    async def shutdown():
        state.stop()

    @app.get("/health")
    @app.get("/v1/health")  # backwards compat alias
    async def health():
        """Health check endpoint."""
        ob_stats = {}
        if state.ob_buffer:
            ob_stats = state.ob_buffer.get_stats()

        symbols_status = {}
        for sym in sorted(VALID_SYMBOLS):
            v1_snap = state.get_v1_snapshot(sym)
            v2_snap = state.get_v2_snapshot(sym)
            symbols_status[sym] = {
                "v1_available": v1_snap is not None,
                "v1_ts": v1_snap.get('ts', 0) if v1_snap else 0,
                "v2_available": v2_snap is not None,
                "v2_ts": v2_snap.get('ts', 0) if v2_snap else 0,
                "v1_history_frames": state.v1_histories[sym].frame_count(),
                "v2_history_frames": state.v2_histories[sym].frame_count(),
            }

        return {
            "ok": True,
            "version": API_VERSION,
            "mode": "embedded",
            "uptime_s": round(time.time() - state.start_time, 1),
            "ob_buffer": {
                "frames_in_memory": ob_stats.get('frames_in_memory', 0),
                "last_ts": ob_stats.get('last_ts', 0),
                "shared": state.ob_buffer is not None
            },
            "symbols": symbols_status,
        }

    @app.get("/oi")
    async def open_interest(
        symbol: str = Query(default="BTC", description="Symbol (BTC, ETH, SOL)")
    ):
        """
        Get aggregated open interest across Binance, Bybit, and OKX.

        Returns per-exchange breakdown and aggregate (in base asset).
        """
        symbol_short = _validate_symbol(symbol)
        cache_key = f"oi?symbol={symbol_short}"
        cached = cache.get(cache_key, ttl=5.0)
        if cached is not None:
            return cached

        if not state.oi_poller:
            raise HTTPException(
                status_code=503,
                detail="OI poller not available"
            )

        snapshot = state.oi_poller.get_snapshot(symbol_short)
        if not snapshot:
            raise HTTPException(
                status_code=404,
                detail=f"No OI data available for {symbol_short}. Poller may still be initializing."
            )

        payload = {
            "symbol": snapshot.symbol,
            "aggregated_oi": snapshot.aggregated_oi,
            "per_exchange": snapshot.per_exchange,
            "ts": snapshot.ts,
        }
        response = JSONResponse(content=payload)
        cache.set(cache_key, response)
        return response

    @app.get("/liq_heatmap")
    @app.get("/v1/liq_heatmap")  # backwards compat alias
    async def liq_heatmap(
        symbol: str = Query(default="BTC", description="Symbol to query (BTC, ETH, SOL)")
    ):
        """Get current V1 liquidation heatmap snapshot."""
        sym = _validate_symbol(symbol)

        cache_key = f"liq_heatmap?symbol={sym}"
        cached = cache.get(cache_key, ttl=5.0)
        if cached is not None:
            return cached

        snapshot = state.get_v1_snapshot(sym)
        if not snapshot:
            raise HTTPException(
                status_code=503,
                detail=f"Data for {sym} is warming up, try again shortly"
            )

        ts = snapshot.get('ts', 0)
        age = time.time() - ts
        if age > 120:
            snapshot = snapshot.copy()
            snapshot["_warning"] = f"Data is {age:.0f}s old, viewer may be stopped"

        response = JSONResponse(content=snapshot)
        cache.set(cache_key, response)
        return response

    @app.get("/liq_heatmap_history")
    @app.get("/v1/liq_heatmap_history")  # backwards compat alias
    async def liq_heatmap_history(
        symbol: str = Query(default="BTC", description="Symbol to query (BTC, ETH, SOL)"),
        minutes: int = Query(default=720, description="Minutes of history (clamped 5..720)"),  # was 360 (6h), changed to 720 (12h) 2026-02-15
        stride: int = Query(default=1, description="Downsample stride (clamped 1..30)")
    ):
        """Get historical V1 liquidation heatmap data."""
        sym = _validate_symbol(symbol)
        minutes = max(5, min(720, minutes))
        stride = max(1, min(30, stride))

        cache_key = f"liq_heatmap_history?symbol={sym}&minutes={minutes}&stride={stride}"
        cached = cache.get(cache_key, ttl=30.0)
        if cached is not None:
            return cached

        v1_hist = state.get_v1_history(sym)
        if not v1_hist:
            raise HTTPException(status_code=503, detail=f"V1 history not available for {sym}")

        current = state.get_v1_snapshot(sym)
        frames = v1_hist.get_frames(minutes=minutes, stride=stride)

        if not frames:
            if current:
                step = current.get('step', DEFAULT_STEP)
                src = current.get('src', 100000)
                fb_ndigits = _step_ndigits(step)
                price_min = round(round((src * (1 - DEFAULT_BAND_PCT)) / step) * step, fb_ndigits)
                price_max = round(round((src * (1 + DEFAULT_BAND_PCT)) / step) * step, fb_ndigits)
                prices = _build_price_grid(price_min, price_max, step)
            else:
                step = DEFAULT_STEP
                fb_min, fb_max = _get_fallback_price_range(sym)
                prices = [float(p) for p in range(int(fb_min), int(fb_max) + int(DEFAULT_STEP), int(DEFAULT_STEP))]

            response = JSONResponse(content={
                "t": [], "prices": prices, "long": [], "short": [],
                "step": step, "scale": 255, "_mode": "embedded"
            })
            cache.set(cache_key, response)
            return response

        if current:
            current_src = current.get('src', 0)
            step = current.get('step', DEFAULT_STEP)
        else:
            current_src = frames[-1].src
            step = DEFAULT_STEP

        prices, price_min, price_max = build_unified_grid(frames, current_src, step, DEFAULT_BAND_PCT)

        if not prices:
            fb_min, fb_max = _get_fallback_price_range(sym)
            prices = [float(p) for p in range(int(fb_min), int(fb_max) + int(DEFAULT_STEP), int(DEFAULT_STEP))]

        t_arr = []
        long_flat = []
        short_flat = []

        for frame in frames:
            t_arr.append(int(frame.ts * 1000))
            long_row, short_row = map_frame_to_grid(frame, prices, step)
            long_flat.extend(long_row)
            short_flat.extend(short_row)

        print(f"[EMBEDDED_API] v1/liq_heatmap_history ({sym}): frames={len(t_arr)} prices={len(prices)}")

        response = JSONResponse(content={
            "t": t_arr, "prices": prices, "long": long_flat, "short": short_flat,
            "step": step, "scale": 255, "_mode": "embedded"
        })
        cache.set(cache_key, response)
        return response

    @app.get("/liq_heatmap_v2")
    @app.get("/v2/liq_heatmap")  # backwards compat alias
    async def liq_heatmap_v2(
        symbol: str = Query(default="BTC", description="Symbol to query (BTC, ETH, SOL)"),
        min_notional: float = Query(default=0, description="Minimum USD notional to include")
    ):
        """Get current V2 liquidation heatmap (tape + inference)."""
        sym = _validate_symbol(symbol)

        cache_key = f"liq_heatmap_v2?symbol={sym}&min_notional={min_notional}"
        cached = cache.get(cache_key, ttl=5.0)
        if cached is not None:
            return cached

        snapshot = state.get_v2_snapshot(sym)
        if not snapshot:
            raise HTTPException(
                status_code=503,
                detail=f"Data for {sym} is warming up, try again shortly"
            )

        if min_notional > 0:
            snapshot = snapshot.copy()
            snapshot['long_levels'] = [
                lvl for lvl in snapshot.get('long_levels', [])
                if lvl.get('notional_usd', 0) >= min_notional
            ]
            snapshot['short_levels'] = [
                lvl for lvl in snapshot.get('short_levels', [])
                if lvl.get('notional_usd', 0) >= min_notional
            ]

        ts = snapshot.get('ts', 0)
        age = time.time() - ts
        if age > 120:
            snapshot = snapshot.copy() if min_notional == 0 else snapshot
            snapshot["_warning"] = f"Data is {age:.0f}s old, viewer may be stopped"

        response = JSONResponse(content=snapshot)
        cache.set(cache_key, response)
        return response

    @app.get("/liq_heatmap_v2_history")
    @app.get("/v2/liq_heatmap_history")  # backwards compat alias
    async def liq_heatmap_history_v2(
        symbol: str = Query(default="BTC", description="Symbol to query (BTC, ETH, SOL)"),
        minutes: int = Query(default=720, description="Minutes of history (clamped 5..720)"),  # was 360 (6h), changed to 720 (12h) 2026-02-15
        stride: int = Query(default=1, description="Downsample stride (clamped 1..30)")
    ):
        """Get historical V2 liquidation heatmap data."""
        sym = _validate_symbol(symbol)
        minutes = max(5, min(720, minutes))
        stride = max(1, min(30, stride))

        cache_key = f"liq_heatmap_v2_history?symbol={sym}&minutes={minutes}&stride={stride}"
        cached = cache.get(cache_key, ttl=30.0)
        if cached is not None:
            return cached

        v2_hist = state.get_v2_history(sym)
        if not v2_hist:
            raise HTTPException(status_code=503, detail=f"V2 history not available for {sym}")

        current = state.get_v2_snapshot(sym)
        frames = v2_hist.get_frames(minutes=minutes, stride=stride)

        if not frames:
            if current:
                step = current.get('step', DEFAULT_STEP)
                src = current.get('src', 100000)
                fb_ndigits = _step_ndigits(step)
                price_min = round(round((src * (1 - DEFAULT_BAND_PCT)) / step) * step, fb_ndigits)
                price_max = round(round((src * (1 + DEFAULT_BAND_PCT)) / step) * step, fb_ndigits)
                prices = _build_price_grid(price_min, price_max, step)
            else:
                step = DEFAULT_STEP
                fb_min, fb_max = _get_fallback_price_range(sym)
                prices = [float(p) for p in range(int(fb_min), int(fb_max) + int(DEFAULT_STEP), int(DEFAULT_STEP))]

            response = JSONResponse(content={
                "t": [], "prices": prices, "long": [], "short": [],
                "step": step, "scale": 255, "_mode": "embedded"
            })
            cache.set(cache_key, response)
            return response

        if current:
            current_src = current.get('src', 0)
            step = current.get('step', DEFAULT_STEP)
        else:
            current_src = frames[-1].src
            step = DEFAULT_STEP

        prices, price_min, price_max = build_unified_grid(frames, current_src, step, DEFAULT_BAND_PCT)

        if not prices:
            fb_min, fb_max = _get_fallback_price_range(sym)
            prices = [float(p) for p in range(int(fb_min), int(fb_max) + int(DEFAULT_STEP), int(DEFAULT_STEP))]

        t_arr = []
        long_flat = []
        short_flat = []

        for frame in frames:
            t_arr.append(int(frame.ts * 1000))
            long_row, short_row = map_frame_to_grid(frame, prices, step)
            long_flat.extend(long_row)
            short_flat.extend(short_row)

        print(f"[EMBEDDED_API] v2/liq_heatmap_history ({sym}): frames={len(t_arr)} prices={len(prices)}")

        response = JSONResponse(content={
            "t": t_arr, "prices": prices, "long": long_flat, "short": short_flat,
            "step": step, "scale": 255, "_mode": "embedded"
        })
        cache.set(cache_key, response)
        return response

    @app.get("/liq_stats")
    @app.get("/v2/liq_stats")  # backwards compat alias
    async def liq_stats_v2(
        symbol: str = Query(default="BTC", description="Symbol to query (BTC, ETH, SOL)")
    ):
        """Get V2 heatmap statistics."""
        sym = _validate_symbol(symbol)

        cache_key = f"liq_stats?symbol={sym}"
        cached = cache.get(cache_key, ttl=5.0)
        if cached is not None:
            return cached

        snapshot = state.get_v2_snapshot(sym)
        if not snapshot:
            raise HTTPException(
                status_code=503,
                detail=f"Data for {sym} is warming up, try again shortly"
            )

        # Copy stats to avoid mutating the cached snapshot dict in-place
        stats = copy.deepcopy(snapshot.get('stats', {}))
        stats['symbol'] = sym
        stats['snapshot_ts'] = snapshot.get('ts', 0)
        stats['snapshot_age_s'] = round(time.time() - snapshot.get('ts', 0), 1)

        v2_hist = state.get_v2_history(sym)
        if v2_hist:
            stats['history_frames'] = v2_hist.frame_count()

        response = JSONResponse(content=stats)
        cache.set(cache_key, response)
        return response

    # =========================================================================
    # Orderbook Heatmap Endpoints (30s DoM - uses shared buffer directly)
    # BTC-only: orderbook data is only available for BTC (other symbols lack depth streams)
    # =========================================================================

    def _require_btc_orderbook(symbol: str) -> None:
        """Reject non-BTC symbols for orderbook endpoints (BTC-only data)."""
        sym = symbol.strip().upper().replace("USDT", "")
        if sym != "BTC":
            raise HTTPException(
                status_code=400,
                detail=f"Orderbook data is only available for BTC. Got: '{symbol}'"
            )

    @app.get("/orderbook_heatmap")
    @app.get("/v2/orderbook_heatmap_30s")  # backwards compat alias
    async def orderbook_heatmap_30s(
        symbol: str = Query(default="BTC", description="Symbol to query (BTC only)"),
        range_pct: float = Query(default=0.10, description="Price range as decimal"),
        step: float = Query(default=20.0, description="Price bucket size"),
        price_min: float = Query(default=None, description="Override minimum price"),
        price_max: float = Query(default=None, description="Override maximum price"),
        format: str = Query(default="json", description="Response format: json or bin")
    ):
        """
        Get latest 30-second orderbook heatmap frame.

        Time metadata fields:
        - frame_ts_ms: Start of 30s window (milliseconds)
        - frame_interval_ms: 30000 (fixed 30s interval)
        - valid_from_ts_ms: Same as frame_ts_ms
        - valid_to_ts_ms: frame_ts_ms + 30000
        - server_send_ts_ms: Server timestamp when response was sent
        - data_is_stale: True if frame is older than 2x interval (60s)

        Returns the most recently WRITTEN frame (never synthesized/rewritten).
        """
        _require_btc_orderbook(symbol)

        # Validate user-provided grid parameters
        if step <= 0:
            raise HTTPException(status_code=400, detail="step must be positive")
        if range_pct <= 0 or range_pct > 1.0:
            raise HTTPException(status_code=400, detail="range_pct must be between 0 and 1.0")

        if not HAS_OB_HEATMAP or not state.ob_buffer:
            raise HTTPException(
                status_code=503,
                detail="Orderbook heatmap not available (no shared buffer)"
            )

        frame = state.ob_buffer.get_latest()
        if not frame:
            raise HTTPException(
                status_code=404,
                detail="No orderbook frames available yet"
            )

        # Compute time metadata
        frame_ts_ms = int(frame.ts * 1000)
        frame_interval_ms = FRAME_INTERVAL_MS  # 30000
        valid_from_ts_ms = frame_ts_ms
        valid_to_ts_ms = frame_ts_ms + frame_interval_ms

        # Data is stale if frame is older than 2x interval (60s)
        now_ms = int(time.time() * 1000)
        data_is_stale = (now_ms - frame_ts_ms) > (2 * frame_interval_ms)

        # Binary format response (not cached — different response type)
        if format == "bin":
            binary_data = frame_to_binary(frame, frame_ts_ms)
            return Response(
                content=binary_data,
                media_type="application/octet-stream",
                headers={
                    "X-Frame-Ts-Ms": str(frame_ts_ms),
                    "X-Frame-Interval-Ms": str(frame_interval_ms),
                    "X-Data-Is-Stale": "true" if data_is_stale else "false",
                    "X-Server-Send-Ts-Ms": str(int(time.time() * 1000))
                }
            )

        # JSON format — check cache
        cache_key = f"orderbook_heatmap?symbol={symbol}&range_pct={range_pct}&step={step}&price_min={price_min}&price_max={price_max}"
        cached = cache.get(cache_key, ttl=5.0)
        if cached is not None:
            return cached

        server_recv_ts_ms = int(time.time() * 1000)

        # JSON format response
        if price_min is not None or price_max is not None:
            p_min = price_min if price_min is not None else frame.price_min
            p_max = price_max if price_max is not None else frame.price_max
            prices, p_min, p_max = ob_build_unified_grid([frame], p_min, p_max, step)
            bid_u8, ask_u8 = resample_frame_to_grid(frame, prices, step)
        else:
            prices = frame.get_prices()
            bid_u8 = list(frame.bid_u8)
            ask_u8 = list(frame.ask_u8)
            p_min = frame.price_min
            p_max = frame.price_max

        server_send_ts_ms = int(time.time() * 1000)

        response = {
            "symbol": symbol,
            # Time metadata for client synchronization
            "frame_ts_ms": frame_ts_ms,
            "frame_interval_ms": frame_interval_ms,
            "valid_from_ts_ms": valid_from_ts_ms,
            "valid_to_ts_ms": valid_to_ts_ms,
            "server_recv_ts_ms": server_recv_ts_ms,
            "server_send_ts_ms": server_send_ts_ms,
            "data_is_stale": data_is_stale,
            # Legacy ts field (epoch seconds) for backward compatibility
            "ts": frame.ts,
            "src": frame.src,
            "step": frame.step,
            "price_min": p_min,
            "price_max": p_max,
            "prices": prices,
            "bid_u8": bid_u8,
            "ask_u8": ask_u8,
            # Combined scaling stats
            "norm_p50": frame.norm_p50,
            "norm_p90": frame.norm_p90,
            "norm_p95": frame.norm_p95,
            "norm_p99": frame.norm_p99,
            "norm_max": frame.norm_max,
            # Per-side scaling stats for asymmetric colormaps
            "bid_p50": frame.bid_p50,
            "bid_p95": frame.bid_p95,
            "bid_max": frame.bid_max,
            "ask_p50": frame.ask_p50,
            "ask_p95": frame.ask_p95,
            "ask_max": frame.ask_max,
            # Totals
            "total_bid_notional": frame.total_bid_notional,
            "total_ask_notional": frame.total_ask_notional,
            "scale": 255,
            "_mode": "shared_buffer"
        }

        json_response = JSONResponse(content=response)
        cache.set(cache_key, json_response)
        return json_response

    @app.get("/orderbook_heatmap_history")
    @app.get("/v2/orderbook_heatmap_30s_history")  # backwards compat alias
    async def orderbook_heatmap_30s_history(
        symbol: str = Query(default="BTC", description="Symbol to query (BTC only)"),
        minutes: int = Query(default=720, description="Minutes of history (clamped 5..720)"),  # was 360 (6h), changed to 720 (12h) 2026-02-15
        stride: int = Query(default=1, description="Downsample stride (clamped 1..60)"),
        step: float = Query(default=20.0, description="Price bucket size"),
        price_min: float = Query(default=None, description="Override minimum price"),
        price_max: float = Query(default=None, description="Override maximum price"),
        format: str = Query(default="json", description="Response format: json or bin")
    ):
        """
        Get historical 30-second orderbook heatmap frames.

        Time metadata fields:
        - frame_interval_ms: 30000 (fixed 30s interval)
        - first_frame_ts_ms: Timestamp of oldest frame in response
        - last_frame_ts_ms: Timestamp of newest frame in response
        - server_send_ts_ms: Server timestamp when response was sent
        - data_is_stale: True if last frame is older than 2x interval

        Binary format (format=bin) returns compact blob for low-latency backfill.
        """
        _require_btc_orderbook(symbol)

        # Validate user-provided grid parameters
        if step <= 0:
            raise HTTPException(status_code=400, detail="step must be positive")

        if not HAS_OB_HEATMAP or not state.ob_buffer:
            raise HTTPException(
                status_code=503,
                detail="Orderbook heatmap not available (no shared buffer)"
            )

        minutes = max(5, min(720, minutes))
        stride = max(1, min(60, stride))

        frames = state.ob_buffer.get_frames(minutes=minutes, stride=stride)

        if not frames:
            return JSONResponse(content={
                "t": [], "prices": [], "bid_u8": [], "ask_u8": [],
                "frame_interval_ms": FRAME_INTERVAL_MS,
                "step": step, "scale": 255, "norm_method": "p50_p95", "_mode": "shared_buffer"
            })

        prices, p_min, p_max = ob_build_unified_grid(
            frames, price_min=price_min, price_max=price_max, step=step
        )

        # Time metadata
        first_frame_ts_ms = int(frames[0].ts * 1000)
        last_frame_ts_ms = int(frames[-1].ts * 1000)
        now_ms = int(time.time() * 1000)
        data_is_stale = (now_ms - last_frame_ts_ms) > (2 * FRAME_INTERVAL_MS)

        # Binary format response (not cached — different response type)
        if format == "bin":
            binary_data = frames_to_binary(frames, prices, step)
            return Response(
                content=binary_data,
                media_type="application/octet-stream",
                headers={
                    "X-First-Frame-Ts-Ms": str(first_frame_ts_ms),
                    "X-Last-Frame-Ts-Ms": str(last_frame_ts_ms),
                    "X-Frame-Count": str(len(frames)),
                    "X-Price-Count": str(len(prices)),
                    "X-Frame-Interval-Ms": str(FRAME_INTERVAL_MS),
                    "X-Data-Is-Stale": "true" if data_is_stale else "false",
                    "X-Server-Send-Ts-Ms": str(int(time.time() * 1000))
                }
            )

        # JSON format — check cache
        cache_key = f"orderbook_heatmap_history?symbol={symbol}&minutes={minutes}&stride={stride}&step={step}&price_min={price_min}&price_max={price_max}"
        cached = cache.get(cache_key, ttl=30.0)
        if cached is not None:
            return cached

        server_recv_ts_ms = int(time.time() * 1000)

        # JSON format response
        t_arr = []
        bid_flat = []
        ask_flat = []

        for frame in frames:
            t_arr.append(int(frame.ts * 1000))
            bid_row, ask_row = resample_frame_to_grid(frame, prices, step)
            bid_flat.extend(bid_row)
            ask_flat.extend(ask_row)

        server_send_ts_ms = int(time.time() * 1000)

        print(f"[EMBEDDED_API] ob_heatmap_30s_history: frames={len(t_arr)} prices={len(prices)}")

        response = JSONResponse(content={
            # Time metadata
            "frame_interval_ms": FRAME_INTERVAL_MS,
            "first_frame_ts_ms": first_frame_ts_ms,
            "last_frame_ts_ms": last_frame_ts_ms,
            "server_recv_ts_ms": server_recv_ts_ms,
            "server_send_ts_ms": server_send_ts_ms,
            "data_is_stale": data_is_stale,
            # Frame data
            "t": t_arr,
            "prices": prices,
            "bid_u8": bid_flat,
            "ask_u8": ask_flat,
            "step": step,
            "price_min": p_min,
            "price_max": p_max,
            "scale": 255,
            "norm_method": "p50_p95",
            "_mode": "shared_buffer"
        })
        cache.set(cache_key, response)
        return response

    @app.get("/orderbook_heatmap_stats")
    @app.get("/v2/orderbook_heatmap_30s_stats")  # backwards compat alias
    async def orderbook_heatmap_30s_stats():
        """
        Get orderbook heatmap buffer and reconstructor statistics.

        Includes:
        - Buffer stats: frames in memory/disk, write timing
        - Reconstructor stats: state, resyncs, gaps, update IDs
        - Timing: last frame age, staleness indicator
        """
        cache_key = "orderbook_heatmap_stats"
        cached = cache.get(cache_key, ttl=10.0)
        if cached is not None:
            return cached

        if not state.ob_buffer:
            raise HTTPException(
                status_code=503,
                detail="Orderbook heatmap not available (no shared buffer)"
            )

        stats = state.ob_buffer.get_stats()
        now = time.time()
        now_ms = int(now * 1000)

        # Time metadata
        stats["current_time"] = now
        stats["current_time_ms"] = now_ms
        stats["frame_interval_ms"] = FRAME_INTERVAL_MS
        stats["mode"] = "shared_buffer"

        if stats["last_ts"] > 0:
            last_frame_age_s = now - stats["last_ts"]
            stats["last_frame_age_s"] = round(last_frame_age_s, 1)
            stats["last_frame_ts_ms"] = int(stats["last_ts"] * 1000)
            # Stale if > 2x frame interval
            stats["data_is_stale"] = last_frame_age_s > (2 * FRAME_INTERVAL_SEC)
        else:
            stats["last_frame_age_s"] = None
            stats["last_frame_ts_ms"] = 0
            stats["data_is_stale"] = True

        # Try to load reconstructor stats from file (written by full_metrics_viewer)
        recon_stats_file = os.path.join(os.path.dirname(__file__), "ob_recon_stats.json")
        if os.path.exists(recon_stats_file):
            try:
                with open(recon_stats_file, 'r') as f:
                    recon_stats = json.load(f)
                stats["reconstructor"] = recon_stats
                # Add stats file age
                if "written_at" in recon_stats:
                    stats["reconstructor"]["stats_age_s"] = round(
                        now - recon_stats["written_at"], 1
                    )
            except Exception as e:
                stats["reconstructor"] = {"error": f"failed to read: {e}"}
        else:
            stats["reconstructor"] = {"error": "stats file not found"}

        response = JSONResponse(content=stats)
        cache.set(cache_key, response)
        return response

    @app.get("/orderbook_heatmap_debug")
    @app.get("/v2/orderbook_heatmap_30s_debug")  # backwards compat alias
    async def orderbook_heatmap_30s_debug():
        """Debug endpoint for orderbook heatmap system."""
        cache_key = "orderbook_heatmap_debug"
        cached = cache.get(cache_key, ttl=10.0)
        if cached is not None:
            return cached

        debug_info = {
            "has_ob_heatmap_module": HAS_OB_HEATMAP,
            "ob_buffer_shared": state.ob_buffer is not None,
            "mode": "embedded"
        }

        if state.ob_buffer is not None:
            try:
                debug_info["ob_buffer_stats"] = state.ob_buffer.get_stats()
            except Exception as e:
                debug_info["ob_buffer_stats_error"] = str(e)

        response = JSONResponse(content=debug_info)
        cache.set(cache_key, response)
        return response

    # =========================================================================
    # V3 Zone Lifecycle Endpoints (via EngineManager — live in-memory data)
    # =========================================================================

    # _resolve_symbol removed — V3 zone endpoints now use _validate_symbol() for
    # consistent normalization (upper-case, USDT-strip, VALID_SYMBOLS check).

    def _get_zone_manager(symbol_short: str):
        """Get the zone_manager for a symbol from the engine_manager."""
        if not state.engine_manager:
            return None
        engine = state.engine_manager.get_engine(symbol_short)
        if not engine:
            return None
        return getattr(engine.heatmap_v2, 'zone_manager', None)

    @app.get("/liq_zones")
    @app.get("/v3/liq_zones")  # backwards compat alias
    async def liq_zones_v3(
        symbol: str = Query(default="BTC", description="Symbol to query"),
        side: str = Query(default=None, description="Filter by side: 'long' or 'short'"),
        min_leverage: int = Query(default=None, description="Minimum leverage tier to include"),
        max_leverage: int = Query(default=None, description="Maximum leverage tier to include"),
        min_weight: float = Query(default=None, description="Minimum zone weight to include")
    ):
        """
        Get V3 active liquidation zones from zone lifecycle manager.

        Returns persistent zones with lifecycle tracking:
        - CREATED: New zones from inference or tape
        - REINFORCED: Zones strengthened by repeated predictions
        - (Zones are removed when SWEPT by price or EXPIRED by time/decay)
        """
        cache_key = f"liq_zones?symbol={symbol}&side={side}&min_leverage={min_leverage}&max_leverage={max_leverage}&min_weight={min_weight}"
        cached = cache.get(cache_key, ttl=5.0)
        if cached is not None:
            return cached

        symbol_short = _validate_symbol(symbol)

        if not state.engine_manager:
            raise HTTPException(
                status_code=503,
                detail="Engine manager not available"
            )

        zone_mgr = _get_zone_manager(symbol_short)
        if not zone_mgr:
            raise HTTPException(
                status_code=404,
                detail=f"No zone manager available for {symbol}. Is the engine registered?"
            )

        # Get active zones with filters
        zones = zone_mgr.get_active_zones(
            side=side,
            min_leverage=min_leverage,
            max_leverage=max_leverage
        )

        # Apply min_weight filter if specified
        if min_weight is not None and min_weight > 0:
            zones = [z for z in zones if z.get('weight', 0) >= min_weight]

        # Get summary stats
        summary = zone_mgr.get_summary()

        response = JSONResponse(content={
            "symbol": symbol_short,
            "ts": time.time(),
            "zones": zones,
            "zones_count": len(zones),
            "summary": summary,
            "filters": {
                "side": side,
                "min_leverage": min_leverage,
                "max_leverage": max_leverage,
                "min_weight": min_weight
            }
        })
        cache.set(cache_key, response)
        return response

    @app.get("/liq_zones_summary")
    @app.get("/v3/liq_zones_summary")  # backwards compat alias
    async def liq_zones_summary_v3(
        symbol: str = Query(default="BTC", description="Symbol to query")
    ):
        """
        Get V3 zone lifecycle summary statistics.

        Returns counts and totals for zone lifecycle tracking.
        """
        cache_key = f"liq_zones_summary?symbol={symbol}"
        cached = cache.get(cache_key, ttl=5.0)
        if cached is not None:
            return cached

        symbol_short = _validate_symbol(symbol)

        if not state.engine_manager:
            raise HTTPException(
                status_code=503,
                detail="Engine manager not available"
            )

        zone_mgr = _get_zone_manager(symbol_short)
        if not zone_mgr:
            raise HTTPException(
                status_code=404,
                detail=f"No zone manager available for {symbol}. Is the engine registered?"
            )

        summary = zone_mgr.get_summary()
        summary["symbol"] = symbol_short
        summary["ts"] = time.time()

        response = JSONResponse(content=summary)
        cache.set(cache_key, response)
        return response

    @app.get("/liq_zones_heatmap")
    @app.get("/v3/liq_heatmap")  # backwards compat alias
    async def liq_heatmap_v3(
        symbol: str = Query(default="BTC", description="Symbol to query"),
        min_notional: float = Query(default=0, description="Minimum USD notional to include"),
        min_leverage: int = Query(default=None, description="Minimum leverage tier to include (5-250)"),
        max_leverage: int = Query(default=None, description="Maximum leverage tier to include (5-250)"),
        min_weight: float = Query(default=None, description="Minimum zone weight to include")
    ):
        """
        Get V3 liquidation heatmap with full leverage filtering.

        Uses the zone lifecycle manager for filtering by leverage tier.
        Unlike V2, this supports filtering zones based on which leverage tiers
        contributed to them.
        """
        cache_key = f"liq_zones_heatmap?symbol={symbol}&min_notional={min_notional}&min_leverage={min_leverage}&max_leverage={max_leverage}&min_weight={min_weight}"
        cached = cache.get(cache_key, ttl=5.0)
        if cached is not None:
            return cached

        symbol_short = _validate_symbol(symbol)

        if not state.engine_manager:
            raise HTTPException(
                status_code=503,
                detail="Engine manager not available. Use /v2/liq_heatmap for snapshot-based data."
            )

        zone_mgr = _get_zone_manager(symbol_short)
        if not zone_mgr:
            raise HTTPException(
                status_code=404,
                detail=f"No zone manager available for {symbol}. Is the engine registered?"
            )

        # Get zones with leverage filtering
        zones = zone_mgr.get_active_zones(
            side=None,  # Get both sides
            min_leverage=min_leverage,
            max_leverage=max_leverage
        )

        # Apply additional filters
        if min_weight is not None and min_weight > 0:
            zones = [z for z in zones if z.get('weight', 0) >= min_weight]

        # Separate into long and short levels
        long_levels = []
        short_levels = []

        for z in zones:
            # Estimate notional from weight (inverse of normalization)
            estimated_notional = z['weight'] * 100000.0

            if min_notional > 0 and estimated_notional < min_notional:
                continue

            level = {
                "price": z['price'],
                "weight": z['weight'],
                "notional_usd": round(estimated_notional, 2),
                "tier_contributions": z.get('tier_contributions', {}),
                "reinforcement_count": z.get('reinforcement_count', 0),
                "source": z.get('source', 'unknown'),
                "created_at": z.get('created_at'),
                "last_reinforced_at": z.get('last_reinforced_at')
            }

            if z['side'] == 'long':
                long_levels.append(level)
            else:
                short_levels.append(level)

        # Sort by weight descending
        long_levels.sort(key=lambda x: -x['weight'])
        short_levels.sort(key=lambda x: -x['weight'])

        # Get summary for meta
        summary = zone_mgr.get_summary()

        response = JSONResponse(content={
            "symbol": symbol_short,
            "ts": time.time(),
            "long_levels": long_levels,
            "short_levels": short_levels,
            "meta": {
                "min_leverage": min_leverage,
                "max_leverage": max_leverage,
                "min_weight": min_weight,
                "min_notional": min_notional,
                "filtered": bool(min_leverage or max_leverage or min_weight or min_notional),
                "long_pools_count": len(long_levels),
                "short_pools_count": len(short_levels),
                "total_long_weight": summary.get('total_weight_long', 0),
                "total_short_weight": summary.get('total_weight_short', 0),
                "zones_created": summary.get('zones_created_total', 0),
                "zones_swept": summary.get('zones_swept_total', 0)
            }
        })
        cache.set(cache_key, response)
        return response

    return app


def start_api_thread(
    app: 'FastAPI',
    host: str = "127.0.0.1",
    port: int = 8899,
    log_level: str = "warning"
) -> threading.Thread:
    """Start the API server in a background daemon thread."""
    if not HAS_FASTAPI:
        raise ImportError("FastAPI not installed")

    def run_server():
        config = uvicorn.Config(
            app, host=host, port=port, log_level=log_level, access_log=False
        )
        server = uvicorn.Server(config)
        server.run()

    thread = threading.Thread(target=run_server, daemon=True, name="EmbeddedAPI")
    thread.start()

    print(f"[EMBEDDED_API] Server started on http://{host}:{port}")
    print(f"[EMBEDDED_API] Endpoints (clean paths, old /vN/ aliases still work):")
    print(f"  GET http://{host}:{port}/health")
    print(f"  GET http://{host}:{port}/liq_heatmap")
    print(f"  GET http://{host}:{port}/liq_heatmap_history")
    print(f"  GET http://{host}:{port}/liq_heatmap_v2")
    print(f"  GET http://{host}:{port}/liq_heatmap_v2_history")
    print(f"  GET http://{host}:{port}/liq_stats")
    print(f"  GET http://{host}:{port}/orderbook_heatmap")
    print(f"  GET http://{host}:{port}/orderbook_heatmap_history")
    print(f"  GET http://{host}:{port}/orderbook_heatmap_stats")
    print(f"  GET http://{host}:{port}/orderbook_heatmap_debug")
    print(f"  GET http://{host}:{port}/liq_zones")
    print(f"  GET http://{host}:{port}/liq_zones_summary")
    print(f"  GET http://{host}:{port}/liq_zones_heatmap")

    return thread
