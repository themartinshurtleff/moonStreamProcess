#!/usr/bin/env python3
"""
Liquidation Heatmap API Server

Lightweight HTTP API serving liquidation zone data for terminal integration.
Reads from a snapshot file written by full_metrics_viewer.py.

Supports:
- Live snapshots via /v1/liq_heatmap
- Historical data via /v1/liq_heatmap_history (12h ring buffer)

Usage:
    python -m poc.liq_api --host 127.0.0.1 --port 8899
"""

import argparse
import json
import os
import sys
import time
import threading
from collections import deque
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from fastapi import FastAPI, Query, HTTPException
    from fastapi.responses import JSONResponse
    import uvicorn
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False
    print("FastAPI not installed. Install with: pip install fastapi uvicorn")

# Directory where this script lives
POC_DIR = os.path.dirname(os.path.abspath(__file__))

# Snapshot file written by viewer
SNAPSHOT_FILE = os.path.join(POC_DIR, "liq_api_snapshot.json")

# API version
API_VERSION = "1.1.0"

# Cache refresh interval
CACHE_REFRESH_INTERVAL = 1.0  # seconds

# History buffer size (12 hours at 1-minute resolution)
HISTORY_MINUTES = 720

# Default grid parameters
DEFAULT_STEP = 20.0
DEFAULT_BAND_PCT = 0.08  # ±8% around src


@dataclass
class HistoryFrame:
    """A single frame of heatmap history (1 minute)."""
    ts: float
    src: float
    price_min: float
    price_max: float
    # Store intensities as bytes (0-255) for memory efficiency
    long_intensity: bytes  # u8 encoded
    short_intensity: bytes  # u8 encoded
    # Original prices array for this frame
    prices: List[float]


class HistoryBuffer:
    """
    Ring buffer for heatmap history.

    Stores up to HISTORY_MINUTES frames with u8-encoded intensities.

    Memory estimate:
    - 720 frames × ~800 buckets × 2 sides × 1 byte = ~1.15 MB
    - Plus metadata ~50 bytes/frame = ~36 KB
    - Total: ~1.2 MB
    """

    def __init__(self, max_frames: int = HISTORY_MINUTES):
        self.max_frames = max_frames
        self._frames: deque = deque(maxlen=max_frames)
        self._lock = threading.Lock()
        self._last_ts: float = 0

    def add_frame(self, snapshot: Dict) -> bool:
        """
        Add a frame from a snapshot dict.
        Returns True if frame was added (new timestamp).
        """
        ts = snapshot.get('ts', 0)
        if ts <= 0:
            return False

        # Only add if we're in a new minute (floor to minute)
        ts_minute = int(ts // 60)
        with self._lock:
            last_minute = int(self._last_ts // 60) if self._last_ts > 0 else -1
            if ts_minute <= last_minute:
                return False

            # Extract and encode intensities
            long_raw = snapshot.get('long_intensity', [])
            short_raw = snapshot.get('short_intensity', [])

            # Convert float 0..1 to u8 0..255
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

    def get_frames(
        self,
        minutes: int = 60,
        stride: int = 1
    ) -> List[HistoryFrame]:
        """
        Get recent frames with optional downsampling.

        Args:
            minutes: Number of minutes to return
            stride: Return every Nth frame (1 = all frames)
        """
        with self._lock:
            # Get last N frames
            n_frames = min(minutes, len(self._frames))
            if n_frames == 0:
                return []

            frames = list(self._frames)[-n_frames:]

            # Apply stride
            if stride > 1:
                frames = frames[::stride]

            return frames

    def frame_count(self) -> int:
        """Get number of stored frames."""
        with self._lock:
            return len(self._frames)

    def get_time_range(self) -> Tuple[float, float]:
        """Get (oldest_ts, newest_ts) or (0, 0) if empty."""
        with self._lock:
            if not self._frames:
                return (0, 0)
            return (self._frames[0].ts, self._frames[-1].ts)


def build_unified_grid(
    frames: List[HistoryFrame],
    current_src: float,
    step: float = DEFAULT_STEP,
    band_pct: float = DEFAULT_BAND_PCT
) -> Tuple[List[float], float, float]:
    """
    Build a unified price grid that covers all frames.

    Uses current src ±band_pct as the grid bounds.
    Returns (prices, price_min, price_max).
    """
    if current_src <= 0:
        # Fallback: use most recent frame's src
        if frames:
            current_src = frames[-1].src
        else:
            return ([], 0, 0)

    price_min = round((current_src * (1 - band_pct)) / step) * step
    price_max = round((current_src * (1 + band_pct)) / step) * step

    prices = []
    p = price_min
    while p <= price_max:
        prices.append(p)
        p += step

    return (prices, price_min, price_max)


def map_frame_to_grid(
    frame: HistoryFrame,
    target_prices: List[float],
    step: float
) -> Tuple[List[int], List[int]]:
    """
    Map a frame's intensities to a target grid.

    Returns (long_u8, short_u8) as lists of integers.
    Out-of-range prices get 0 intensity.
    """
    # Build lookup from frame's prices
    frame_lookup_long = {}
    frame_lookup_short = {}

    for i, price in enumerate(frame.prices):
        if i < len(frame.long_intensity):
            frame_lookup_long[price] = frame.long_intensity[i]
        if i < len(frame.short_intensity):
            frame_lookup_short[price] = frame.short_intensity[i]

    # Map to target grid
    long_out = []
    short_out = []

    for target_price in target_prices:
        # Find closest price in frame (within half step)
        best_long = 0
        best_short = 0

        # Direct lookup first
        if target_price in frame_lookup_long:
            best_long = frame_lookup_long[target_price]
        if target_price in frame_lookup_short:
            best_short = frame_lookup_short[target_price]

        long_out.append(best_long)
        short_out.append(best_short)

    return (long_out, short_out)


class SnapshotCache:
    """Thread-safe cache for heatmap snapshots with history buffer."""

    def __init__(
        self,
        snapshot_file: str,
        refresh_interval: float = 1.0,
        history_minutes: int = HISTORY_MINUTES
    ):
        self.snapshot_file = snapshot_file
        self.refresh_interval = refresh_interval
        self._cache: Optional[Dict] = None
        self._cache_time: float = 0
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # History buffer
        self.history = HistoryBuffer(max_frames=history_minutes)

    def start(self):
        """Start background refresh thread."""
        self._running = True
        self._thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop background refresh thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)

    def _refresh_loop(self):
        """Background loop to refresh cache."""
        while self._running:
            self._load_snapshot()
            time.sleep(self.refresh_interval)

    def _load_snapshot(self):
        """Load snapshot from file and add to history."""
        if not os.path.exists(self.snapshot_file):
            return

        try:
            with open(self.snapshot_file, 'r') as f:
                data = json.load(f)

            with self._lock:
                self._cache = data
                self._cache_time = time.time()

            # Add to history buffer (handles deduplication internally)
            self.history.add_frame(data)

        except Exception:
            # Don't crash on read errors, keep stale cache
            pass

    def get(self, symbol: str = "BTC") -> Optional[Dict]:
        """Get cached snapshot for symbol."""
        with self._lock:
            if self._cache is None:
                return None
            if self._cache.get('symbol') != symbol:
                return None
            return self._cache.copy()

    def get_age(self) -> float:
        """Get cache age in seconds."""
        with self._lock:
            if self._cache_time == 0:
                return float('inf')
            return time.time() - self._cache_time


# Global cache instance
_cache: Optional[SnapshotCache] = None
_start_time: float = 0


def create_app() -> FastAPI:
    """Create FastAPI application."""
    app = FastAPI(
        title="Liquidation Heatmap API",
        description="API for liquidation zone visualization with history support",
        version=API_VERSION
    )

    @app.on_event("startup")
    async def startup():
        global _cache, _start_time
        _start_time = time.time()
        _cache = SnapshotCache(SNAPSHOT_FILE, CACHE_REFRESH_INTERVAL, HISTORY_MINUTES)
        _cache.start()
        print(f"API started, reading from: {SNAPSHOT_FILE}")
        print(f"History buffer: {HISTORY_MINUTES} minutes ({HISTORY_MINUTES / 60:.1f} hours)")

    @app.on_event("shutdown")
    async def shutdown():
        global _cache
        if _cache:
            _cache.stop()

    @app.get("/v1/health")
    async def health():
        """Health check endpoint."""
        cache_age = _cache.get_age() if _cache else float('inf')
        snapshot_exists = os.path.exists(SNAPSHOT_FILE)
        history_frames = _cache.history.frame_count() if _cache else 0
        time_range = _cache.history.get_time_range() if _cache else (0, 0)

        return {
            "ok": snapshot_exists and cache_age < 120,
            "version": API_VERSION,
            "uptime_s": round(time.time() - _start_time, 1),
            "snapshot_file": SNAPSHOT_FILE,
            "snapshot_exists": snapshot_exists,
            "cache_age_s": round(cache_age, 1) if cache_age < float('inf') else None,
            "history": {
                "frames": history_frames,
                "max_frames": HISTORY_MINUTES,
                "oldest_ts": time_range[0] if time_range[0] > 0 else None,
                "newest_ts": time_range[1] if time_range[1] > 0 else None
            }
        }

    @app.get("/v1/liq_heatmap")
    async def liq_heatmap(
        symbol: str = Query(default="BTC", description="Symbol to query"),
        window_minutes: int = Query(default=60, description="Window for normalization (unused)")
    ):
        """
        Get current liquidation heatmap snapshot.

        Returns intensity values for long/short liquidation zones.
        """
        if not _cache:
            raise HTTPException(status_code=503, detail="Cache not initialized")

        snapshot = _cache.get(symbol)
        if not snapshot:
            raise HTTPException(
                status_code=404,
                detail=f"No snapshot available for {symbol}. Is the viewer running?"
            )

        # Check staleness
        cache_age = _cache.get_age()
        if cache_age > 120:
            snapshot["_warning"] = f"Data is {cache_age:.0f}s old, viewer may be stopped"

        return JSONResponse(content=snapshot)

    @app.get("/v1/liq_heatmap_history")
    async def liq_heatmap_history(
        symbol: str = Query(default="BTC", description="Symbol to query"),
        minutes: int = Query(default=360, description="Minutes of history (clamped 5..720)"),
        stride: int = Query(default=1, description="Downsample stride (clamped 1..30)")
    ):
        """
        Get historical liquidation heatmap data.

        Returns time-series of intensity values for backfilling charts.
        Intensities are u8 encoded (0-255) for efficiency.

        Response format:
        - t: array of timestamps
        - src: array of source prices
        - prices: unified price grid
        - long: 2D array [frames x prices] of u8 intensities
        - short: 2D array [frames x prices] of u8 intensities
        """
        # Clamp parameters
        minutes = max(5, min(720, minutes))
        stride = max(1, min(30, stride))

        if not _cache:
            raise HTTPException(status_code=503, detail="Cache not initialized")

        # Get current snapshot for reference (needed for grid params)
        current = _cache.get(symbol)

        # Get historical frames
        frames = _cache.history.get_frames(minutes=minutes, stride=stride)

        # Debug output
        frame_count = len(frames)
        total_frames = _cache.history.frame_count()
        print(f"[liq_heatmap_history] symbol={symbol} minutes={minutes} stride={stride}")
        print(f"[liq_heatmap_history] frames_returned={frame_count} total_in_buffer={total_frames}")

        # If no frames yet, return empty response (not 404)
        if not frames:
            # Use current snapshot for grid params if available
            if current:
                step = current.get('step', DEFAULT_STEP)
                src = current.get('src', 100000)
                price_min = round((src * (1 - DEFAULT_BAND_PCT)) / step) * step
                price_max = round((src * (1 + DEFAULT_BAND_PCT)) / step) * step
                prices = []
                p = price_min
                while p <= price_max:
                    prices.append(p)
                    p += step
            else:
                step = DEFAULT_STEP
                price_min = 92000.0
                price_max = 108000.0
                prices = list(range(int(price_min), int(price_max) + 1, int(step)))

            print(f"[liq_heatmap_history] returning empty: prices={len(prices)} buckets")

            return JSONResponse(content={
                "schema_version": 1,
                "symbol": symbol,
                "step": step,
                "price_min": price_min,
                "price_max": price_max,
                "minutes": 0,
                "stride": stride,
                "t": [],
                "src": [],
                "prices": prices,
                "long": [],
                "short": [],
                "encoding": "u8",
                "scale": 255
            })

        # Build unified grid based on current src (or most recent frame)
        if current:
            current_src = current.get('src', 0)
            step = current.get('step', DEFAULT_STEP)
        else:
            current_src = frames[-1].src
            step = DEFAULT_STEP

        prices, price_min, price_max = build_unified_grid(
            frames, current_src, step, DEFAULT_BAND_PCT
        )

        if not prices:
            # Fallback grid
            price_min = 92000.0
            price_max = 108000.0
            step = DEFAULT_STEP
            prices = list(range(int(price_min), int(price_max) + 1, int(step)))

        # Build response arrays
        t_arr = []
        src_arr = []
        long_matrix = []
        short_matrix = []

        for frame in frames:
            t_arr.append(frame.ts)
            src_arr.append(frame.src)

            # Map frame to unified grid
            long_row, short_row = map_frame_to_grid(frame, prices, step)
            long_matrix.append(long_row)
            short_matrix.append(short_row)

        print(f"[liq_heatmap_history] response: frames={len(t_arr)} prices={len(prices)} long_shape={len(long_matrix)}x{len(long_matrix[0]) if long_matrix else 0}")

        response = {
            "schema_version": 1,
            "symbol": symbol,
            "step": step,
            "price_min": price_min,
            "price_max": price_max,
            "minutes": len(frames),
            "stride": stride,
            "t": t_arr,
            "src": src_arr,
            "prices": prices,
            "long": long_matrix,
            "short": short_matrix,
            "encoding": "u8",
            "scale": 255
        }

        return JSONResponse(content=response)

    return app


def main():
    """Main entry point."""
    if not HAS_FASTAPI:
        print("ERROR: FastAPI required. Install with:")
        print("  pip install fastapi uvicorn")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Liquidation Heatmap API Server")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8899, help="Port to bind to")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")

    args = parser.parse_args()

    # Memory estimate
    buckets_estimate = int(100000 * 0.16 / DEFAULT_STEP)  # ~800 buckets at $100k
    memory_mb = (HISTORY_MINUTES * buckets_estimate * 2) / (1024 * 1024)

    print("=" * 60)
    print("Liquidation Heatmap API Server")
    print("=" * 60)
    print(f"Host: {args.host}")
    print(f"Port: {args.port}")
    print(f"Snapshot file: {SNAPSHOT_FILE}")
    print()
    print(f"History buffer: {HISTORY_MINUTES} minutes ({HISTORY_MINUTES / 60:.1f} hours)")
    print(f"Memory estimate: ~{memory_mb:.1f} MB")
    print()
    print("Endpoints:")
    print(f"  GET http://{args.host}:{args.port}/v1/health")
    print(f"  GET http://{args.host}:{args.port}/v1/liq_heatmap?symbol=BTC")
    print(f"  GET http://{args.host}:{args.port}/v1/liq_heatmap_history?symbol=BTC&minutes=60&stride=1")
    print()
    print("Make sure full_metrics_viewer.py is running to generate snapshots.")
    print("=" * 60)

    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
