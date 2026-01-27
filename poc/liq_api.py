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
import struct
import threading
from collections import deque
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
import logging

logger = logging.getLogger(__name__)

# Add parent and current directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # Add poc/ for ob_heatmap

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

# V2 snapshot file (new tape+inference architecture)
SNAPSHOT_V2_FILE = os.path.join(POC_DIR, "liq_api_snapshot_v2.json")

# Orderbook heatmap persistence file
OB_HEATMAP_FILE = os.path.join(POC_DIR, "ob_heatmap_30s.bin")

# Orderbook reconstructor stats file
OB_RECON_STATS_FILE = os.path.join(POC_DIR, "ob_recon_stats.json")

# Liquidation heatmap persistence files (V1 and V2)
LIQ_HEATMAP_V1_FILE = os.path.join(POC_DIR, "liq_heatmap_v1.bin")
LIQ_HEATMAP_V2_FILE = os.path.join(POC_DIR, "liq_heatmap_v2.bin")

# Import orderbook heatmap module
HAS_OB_HEATMAP = False
OB_HEATMAP_IMPORT_ERROR = None
try:
    from ob_heatmap import (
        OrderbookHeatmapBuffer, OrderbookFrame,
        build_unified_grid as ob_build_unified_grid,
        resample_frame_to_grid,
        DEFAULT_STEP, DEFAULT_RANGE_PCT
    )
    HAS_OB_HEATMAP = True
except ImportError as e:
    OB_HEATMAP_IMPORT_ERROR = f"ImportError: {e}"
except Exception as e:
    OB_HEATMAP_IMPORT_ERROR = f"{type(e).__name__}: {e}"

# API version
API_VERSION = "2.0.0"

# Cache refresh interval
CACHE_REFRESH_INTERVAL = 1.0  # seconds

# History buffer size (12 hours at 1-minute resolution)
HISTORY_MINUTES = 720

# Default grid parameters
DEFAULT_STEP = 20.0
DEFAULT_BAND_PCT = 0.08  # ±8% around src

# Liquidation heatmap persistence constants
LIQ_FRAME_RECORD_SIZE = 4096  # Fixed 4KB per frame for fast seek
LIQ_MAX_BUCKETS = 1000  # Max price buckets per frame

# Binary record header format for liquidation heatmap (48 bytes)
# ts(d) + src(d) + price_min(d) + price_max(d) + step(d) + n_buckets(I)
LIQ_HEADER_FORMAT = '<dddddI'
LIQ_HEADER_SIZE = struct.calcsize(LIQ_HEADER_FORMAT)  # 48 bytes


class LiquidationHeatmapBuffer:
    """
    Persistent ring buffer for liquidation heatmap history.

    Stores frames to disk for persistence across restarts.
    Uses fixed-size binary records for efficient random access.

    Binary format per frame (4096 bytes):
    - Header (48 bytes): ts, src, price_min, price_max, step, n_buckets
    - Long intensity (1000 bytes): u8 array
    - Short intensity (1000 bytes): u8 array
    - Padding to 4096 bytes
    """

    def __init__(self, persistence_path: str, max_frames: int = HISTORY_MINUTES):
        self.persistence_path = persistence_path
        self.max_frames = max_frames
        self._lock = threading.RLock()
        self._frames: deque = deque(maxlen=max_frames)
        self._last_ts: float = 0
        self._frames_written_since_compact = 0
        self._compact_threshold = 100  # Compact every 100 frames

        # Load existing frames on init
        self._load_from_disk()

    def _load_from_disk(self):
        """Load frames from persistence file on startup."""
        if not os.path.exists(self.persistence_path):
            logger.info(f"[LIQ_BUFFER] No persistence file found at {self.persistence_path}")
            return

        file_size = os.path.getsize(self.persistence_path)
        total_frames = file_size // LIQ_FRAME_RECORD_SIZE

        if total_frames == 0:
            logger.info("[LIQ_BUFFER] Persistence file is empty")
            return

        # Calculate how many frames to load (last max_frames)
        frames_to_load = min(total_frames, self.max_frames)
        start_offset = (total_frames - frames_to_load) * LIQ_FRAME_RECORD_SIZE

        loaded = 0
        oldest_ts = None
        newest_ts = None

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
                            loaded += 1
                            if oldest_ts is None:
                                oldest_ts = frame.ts
                            newest_ts = frame.ts
                            self._last_ts = frame.ts
                    except Exception as e:
                        logger.warning(f"[LIQ_BUFFER] Failed to parse frame: {e}")
        except Exception as e:
            logger.error(f"[LIQ_BUFFER] Failed to load from disk: {e}")
            return

        logger.info(
            f"[LIQ_BUFFER] Loaded {loaded} frames from disk, "
            f"oldest={oldest_ts}, newest={newest_ts}"
        )

    def _frame_to_bytes(self, frame: 'HistoryFrame') -> bytes:
        """Serialize frame to fixed-size binary record."""
        # Compute step from prices if available
        if len(frame.prices) >= 2:
            step = frame.prices[1] - frame.prices[0]
        else:
            step = DEFAULT_STEP

        n_buckets = len(frame.long_intensity)

        header = struct.pack(
            LIQ_HEADER_FORMAT,
            frame.ts, frame.src, frame.price_min, frame.price_max,
            step, n_buckets
        )

        # Pad intensity arrays to LIQ_MAX_BUCKETS
        long_padded = frame.long_intensity.ljust(LIQ_MAX_BUCKETS, b'\x00')[:LIQ_MAX_BUCKETS]
        short_padded = frame.short_intensity.ljust(LIQ_MAX_BUCKETS, b'\x00')[:LIQ_MAX_BUCKETS]

        # Total: 48 + 1000 + 1000 = 2048, pad to 4096
        record = header + long_padded + short_padded
        record = record.ljust(LIQ_FRAME_RECORD_SIZE, b'\x00')
        return record

    def _frame_from_bytes(self, data: bytes) -> Optional['HistoryFrame']:
        """Deserialize frame from binary record."""
        if len(data) < LIQ_HEADER_SIZE:
            return None

        header = struct.unpack(LIQ_HEADER_FORMAT, data[:LIQ_HEADER_SIZE])
        ts, src, price_min, price_max, step, n_buckets = header

        if ts <= 0 or n_buckets == 0:
            return None

        # Extract intensity arrays
        long_start = LIQ_HEADER_SIZE
        short_start = LIQ_HEADER_SIZE + LIQ_MAX_BUCKETS
        long_intensity = data[long_start:long_start + n_buckets]
        short_intensity = data[short_start:short_start + n_buckets]

        # Reconstruct prices array from bounds and step
        prices = []
        p = price_min
        while p <= price_max + 0.01 and len(prices) < n_buckets:
            prices.append(p)
            p += step

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

            # Persist to disk
            self._persist_frame(frame)

            self._frames_written_since_compact += 1
            if self._frames_written_since_compact >= self._compact_threshold:
                self._maybe_compact()
                self._frames_written_since_compact = 0

            return True

    def _persist_frame(self, frame: 'HistoryFrame'):
        """Append frame to persistence file."""
        try:
            with open(self.persistence_path, 'ab') as f:
                f.write(self._frame_to_bytes(frame))
        except Exception as e:
            logger.error(f"[LIQ_BUFFER] Failed to persist frame: {e}")

    def _maybe_compact(self):
        """Compact file if it exceeds max_frames."""
        if not os.path.exists(self.persistence_path):
            return

        file_size = os.path.getsize(self.persistence_path)
        total_frames = file_size // LIQ_FRAME_RECORD_SIZE

        if total_frames <= self.max_frames:
            return

        # Rewrite with only last max_frames
        logger.info(f"[LIQ_BUFFER] Compacting file from {total_frames} to {self.max_frames} frames")

        try:
            start_offset = (total_frames - self.max_frames) * LIQ_FRAME_RECORD_SIZE
            with open(self.persistence_path, 'rb') as f:
                f.seek(start_offset)
                data = f.read()

            with open(self.persistence_path, 'wb') as f:
                f.write(data)

            logger.info(f"[LIQ_BUFFER] Compaction complete, file now {len(data)} bytes")
        except Exception as e:
            logger.error(f"[LIQ_BUFFER] Compaction failed: {e}")

    def get_frames(
        self,
        minutes: int = 60,
        stride: int = 1
    ) -> List['HistoryFrame']:
        """
        Get recent frames with optional downsampling.

        Args:
            minutes: Number of minutes to return
            stride: Return every Nth frame (1 = all frames)
        """
        with self._lock:
            n_frames = min(minutes, len(self._frames))
            if n_frames == 0:
                return []

            frames = list(self._frames)[-n_frames:]

            if stride > 1:
                frames = frames[::stride]

            return frames

    def frame_count(self) -> int:
        """Get current frame count."""
        with self._lock:
            return len(self._frames)

    def time_range(self) -> Tuple[float, float]:
        """Get (oldest_ts, newest_ts) or (0, 0) if empty."""
        with self._lock:
            if not self._frames:
                return (0, 0)
            return (self._frames[0].ts, self._frames[-1].ts)

    def get_time_range(self) -> Tuple[float, float]:
        """Alias for time_range() for compatibility with HistoryBuffer."""
        return self.time_range()

    def get_stats(self) -> dict:
        """Get buffer statistics."""
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
            "file_size_bytes": file_size,
            "persistence_path": self.persistence_path
        }


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
        history_minutes: int = HISTORY_MINUTES,
        persistence_path: Optional[str] = None
    ):
        self.snapshot_file = snapshot_file
        self.refresh_interval = refresh_interval
        self.persistence_path = persistence_path
        self._cache: Optional[Dict] = None
        self._cache_time: float = 0
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # History buffer - use persistent version if path provided
        if persistence_path:
            self.history = LiquidationHeatmapBuffer(
                persistence_path=persistence_path,
                max_frames=history_minutes
            )
        else:
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
_cache_v2: Optional[SnapshotCache] = None  # V2 cache
_ob_buffer: Optional['OrderbookHeatmapBuffer'] = None  # Orderbook heatmap buffer
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
        global _cache, _cache_v2, _ob_buffer, _start_time
        _start_time = time.time()

        # V1 cache with disk persistence
        _cache = SnapshotCache(
            SNAPSHOT_FILE,
            CACHE_REFRESH_INTERVAL,
            HISTORY_MINUTES,
            persistence_path=LIQ_HEATMAP_V1_FILE
        )
        _cache.start()
        v1_stats = _cache.history.get_stats() if hasattr(_cache.history, 'get_stats') else {}
        v1_loaded = v1_stats.get('frames_in_memory', 0)

        # V2 cache with disk persistence
        _cache_v2 = SnapshotCache(
            SNAPSHOT_V2_FILE,
            CACHE_REFRESH_INTERVAL,
            HISTORY_MINUTES,
            persistence_path=LIQ_HEATMAP_V2_FILE
        )
        _cache_v2.start()
        v2_stats = _cache_v2.history.get_stats() if hasattr(_cache_v2.history, 'get_stats') else {}
        v2_loaded = v2_stats.get('frames_in_memory', 0)

        # Initialize orderbook heatmap buffer (loads from disk)
        if HAS_OB_HEATMAP:
            try:
                _ob_buffer = OrderbookHeatmapBuffer(OB_HEATMAP_FILE)
                ob_stats = _ob_buffer.get_stats()
                print(f"Orderbook heatmap: {ob_stats['frames_in_memory']} frames loaded from {OB_HEATMAP_FILE}")
            except Exception as e:
                print(f"ERROR: Failed to initialize OrderbookHeatmapBuffer: {e}")
                _ob_buffer = None
        else:
            print(f"WARNING: Orderbook heatmap module not available: {OB_HEATMAP_IMPORT_ERROR}")

        print(f"API started, reading from: {SNAPSHOT_FILE}")
        print(f"V2 snapshot file: {SNAPSHOT_V2_FILE}")
        print(f"History buffer: {HISTORY_MINUTES} minutes ({HISTORY_MINUTES / 60:.1f} hours)")
        print(f"V1 liq heatmap: {v1_loaded} frames loaded from {LIQ_HEATMAP_V1_FILE}")
        print(f"V2 liq heatmap: {v2_loaded} frames loaded from {LIQ_HEATMAP_V2_FILE}")

    @app.on_event("shutdown")
    async def shutdown():
        global _cache, _cache_v2
        if _cache:
            _cache.stop()
        if _cache_v2:
            _cache_v2.stop()

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
        Intensities are u8 encoded (0-255) in flat row-major arrays.

        Response format:
        - t: array of timestamps (int milliseconds)
        - prices: unified price grid
        - long: flat row-major array [frames * prices] of u8 intensities
        - short: flat row-major array [frames * prices] of u8 intensities
        - step: price bucket size
        - scale: 255 (max intensity value)
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
                prices = [float(p) for p in range(int(price_min), int(price_max) + int(step), int(step))]

            print(f"[liq_heatmap_history] EMPTY: frames=0 prices_len={len(prices)}")

            return JSONResponse(content={
                "t": [],
                "prices": prices,
                "long": [],
                "short": [],
                "step": step,
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
            prices = [float(p) for p in range(int(price_min), int(price_max) + int(step), int(step))]

        prices_len = len(prices)

        # Build flat row-major arrays
        t_arr = []
        long_flat = []
        short_flat = []

        for frame in frames:
            # Timestamp in milliseconds (int)
            t_arr.append(int(frame.ts * 1000))

            # Map frame to unified grid and append to flat arrays
            long_row, short_row = map_frame_to_grid(frame, prices, step)
            long_flat.extend(long_row)
            short_flat.extend(short_row)

        # Debug stats
        num_frames = len(t_arr)
        long_len = len(long_flat)
        short_len = len(short_flat)
        expected_len = num_frames * prices_len

        max_long = max(long_flat) if long_flat else 0
        max_short = max(short_flat) if short_flat else 0
        nonzero_long = sum(1 for v in long_flat if v > 0)
        nonzero_short = sum(1 for v in short_flat if v > 0)

        first_ts_ms = t_arr[0] if t_arr else 0
        last_ts_ms = t_arr[-1] if t_arr else 0

        print(f"[liq_heatmap_history] frames={num_frames} prices_len={prices_len} long_len={long_len} short_len={short_len}")
        print(f"[liq_heatmap_history] max_long={max_long} max_short={max_short} nonzero_long={nonzero_long} nonzero_short={nonzero_short}")
        print(f"[liq_heatmap_history] first_ts_ms={first_ts_ms} last_ts_ms={last_ts_ms}")
        print(f"[liq_heatmap_history] len_check: expected={expected_len} actual_long={long_len} actual_short={short_len} OK={long_len == expected_len and short_len == expected_len}")

        response = {
            "t": t_arr,
            "prices": prices,
            "long": long_flat,
            "short": short_flat,
            "step": step,
            "scale": 255
        }

        return JSONResponse(content=response)

    @app.get("/v2/liq_heatmap")
    async def liq_heatmap_v2(
        symbol: str = Query(default="BTC", description="Symbol to query"),
        min_notional: float = Query(default=0, description="Minimum USD notional to include (0=all)")
    ):
        """
        Get V2 liquidation heatmap (tape + inference architecture).

        Returns clustered liquidity pools from:
        - Tape: Ground truth from forceOrder stream (historical)
        - Projection: OI + aggression based inference (forward-looking)

        Response format:
        {
            "symbol": "BTC",
            "ts": 1234567890.123,
            "price": 100000.0,
            "long_levels": [
                {
                    "price": 92000.0,        // Weighted centroid
                    "price_low": 91800.0,    // Cluster lower bound
                    "price_high": 92200.0,   // Cluster upper bound
                    "notional_usd": 250000.0,// Total USD in cluster
                    "intensity": 0.85,       // Normalized 0-1 strength
                    "bucket_count": 5        // Buckets merged
                }, ...
            ],
            "short_levels": [...],
            "meta": {
                "tape_weight": 0.35,
                "projection_weight": 0.65,
                "cluster_radius_pct": 0.005,
                "min_notional_usd": 10000.0,
                ...
            }
        }
        """
        v2_snapshot_path = SNAPSHOT_V2_FILE

        if not os.path.exists(v2_snapshot_path):
            raise HTTPException(
                status_code=404,
                detail="V2 heatmap not available. Ensure viewer is running with new engine."
            )

        try:
            with open(v2_snapshot_path, 'r') as f:
                data = json.load(f)

            if data.get('symbol') != symbol:
                raise HTTPException(
                    status_code=404,
                    detail=f"V2 snapshot is for {data.get('symbol')}, not {symbol}"
                )

            # Apply min_notional filter if specified
            if min_notional > 0:
                data['long_levels'] = [
                    lvl for lvl in data.get('long_levels', [])
                    if lvl.get('notional_usd', 0) >= min_notional
                ]
                data['short_levels'] = [
                    lvl for lvl in data.get('short_levels', [])
                    if lvl.get('notional_usd', 0) >= min_notional
                ]
                # Update meta
                if 'meta' in data:
                    data['meta']['filtered_min_notional'] = min_notional
                    data['meta']['long_pools_count'] = len(data['long_levels'])
                    data['meta']['short_pools_count'] = len(data['short_levels'])

            # Check staleness
            ts = data.get('ts', 0)
            age = time.time() - ts
            if age > 120:
                data["_warning"] = f"Data is {age:.0f}s old, viewer may be stopped"

            return JSONResponse(content=data)

        except json.JSONDecodeError:
            raise HTTPException(status_code=500, detail="V2 snapshot file corrupted")

    @app.get("/v2/liq_heatmap_history")
    async def liq_heatmap_history_v2(
        symbol: str = Query(default="BTC", description="Symbol to query"),
        minutes: int = Query(default=360, description="Minutes of history (clamped 5..720)"),
        stride: int = Query(default=1, description="Downsample stride (clamped 1..30)")
    ):
        """
        Get historical V2 liquidation heatmap data.

        Returns time-series of intensity values for backfilling charts.
        Intensities are u8 encoded (0-255) in flat row-major arrays.

        Response format:
        - t: array of timestamps (int milliseconds)
        - prices: unified price grid
        - long: flat row-major array [frames * prices] of u8 intensities
        - short: flat row-major array [frames * prices] of u8 intensities
        - step: price bucket size
        - scale: 255 (max intensity value)
        """
        # Clamp parameters
        minutes = max(5, min(720, minutes))
        stride = max(1, min(30, stride))

        if not _cache_v2:
            raise HTTPException(status_code=503, detail="V2 cache not initialized")

        # Get current snapshot for reference (needed for grid params)
        current = _cache_v2.get(symbol)

        # Get historical frames
        frames = _cache_v2.history.get_frames(minutes=minutes, stride=stride)

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
                prices = [float(p) for p in range(int(price_min), int(price_max) + int(step), int(step))]

            print(f"[v2/liq_heatmap_history] EMPTY: frames=0 prices_len={len(prices)}")

            return JSONResponse(content={
                "t": [],
                "prices": prices,
                "long": [],
                "short": [],
                "step": step,
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
            prices = [float(p) for p in range(int(price_min), int(price_max) + int(step), int(step))]

        prices_len = len(prices)

        # Build flat row-major arrays
        t_arr = []
        long_flat = []
        short_flat = []

        for frame in frames:
            # Timestamp in milliseconds (int)
            t_arr.append(int(frame.ts * 1000))

            # Map frame to unified grid and append to flat arrays
            long_row, short_row = map_frame_to_grid(frame, prices, step)
            long_flat.extend(long_row)
            short_flat.extend(short_row)

        # Debug stats
        num_frames = len(t_arr)
        long_len = len(long_flat)
        short_len = len(short_flat)
        expected_len = num_frames * prices_len

        max_long = max(long_flat) if long_flat else 0
        max_short = max(short_flat) if short_flat else 0
        nonzero_long = sum(1 for v in long_flat if v > 0)
        nonzero_short = sum(1 for v in short_flat if v > 0)

        first_ts_ms = t_arr[0] if t_arr else 0
        last_ts_ms = t_arr[-1] if t_arr else 0

        print(f"[v2/liq_heatmap_history] frames={num_frames} prices_len={prices_len} long_len={long_len} short_len={short_len}")
        print(f"[v2/liq_heatmap_history] max_long={max_long} max_short={max_short} nonzero_long={nonzero_long} nonzero_short={nonzero_short}")
        print(f"[v2/liq_heatmap_history] first_ts_ms={first_ts_ms} last_ts_ms={last_ts_ms}")
        print(f"[v2/liq_heatmap_history] len_check: expected={expected_len} actual_long={long_len} actual_short={short_len} OK={long_len == expected_len and short_len == expected_len}")

        response = {
            "t": t_arr,
            "prices": prices,
            "long": long_flat,
            "short": short_flat,
            "step": step,
            "scale": 255
        }

        return JSONResponse(content=response)

    @app.get("/v2/liq_stats")
    async def liq_stats_v2(
        symbol: str = Query(default="BTC", description="Symbol to query")
    ):
        """
        Get V2 heatmap statistics (tape + inference layers).

        Returns detailed stats for debugging and monitoring.
        """
        v2_snapshot_path = SNAPSHOT_V2_FILE

        if not os.path.exists(v2_snapshot_path):
            raise HTTPException(
                status_code=404,
                detail="V2 heatmap not available."
            )

        try:
            with open(v2_snapshot_path, 'r') as f:
                data = json.load(f)

            stats = data.get('stats', {})
            stats['snapshot_ts'] = data.get('ts', 0)
            stats['snapshot_age_s'] = round(time.time() - data.get('ts', 0), 1)

            return JSONResponse(content=stats)

        except json.JSONDecodeError:
            raise HTTPException(status_code=500, detail="V2 snapshot file corrupted")

    # =========================================================================
    # Orderbook Heatmap Endpoints (30s DoM screenshots)
    # =========================================================================

    @app.get("/v2/orderbook_heatmap_30s")
    async def orderbook_heatmap_30s(
        symbol: str = Query(default="BTC", description="Symbol to query"),
        range_pct: float = Query(default=0.10, description="Price range as decimal (0.10 = ±10%)"),
        step: float = Query(default=20.0, description="Price bucket size"),
        price_min: float = Query(default=None, description="Override minimum price"),
        price_max: float = Query(default=None, description="Override maximum price")
    ):
        """
        Get latest 30-second orderbook heatmap frame.

        Returns bid/ask intensity arrays for orderbook depth visualization.
        """
        if not HAS_OB_HEATMAP or not _ob_buffer:
            raise HTTPException(
                status_code=503,
                detail="Orderbook heatmap not available"
            )

        frame = _ob_buffer.get_latest()
        if not frame:
            raise HTTPException(
                status_code=404,
                detail="No orderbook frames available yet"
            )

        # Check staleness (warn if > 60s old)
        age = time.time() - frame.ts
        warning = None
        if age > 60:
            warning = f"Data is {age:.0f}s old, viewer may be stopped"

        # Use frame's grid or resample if overrides provided
        if price_min is not None or price_max is not None:
            # Build custom grid (using ob_heatmap version for OrderbookFrame)
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

        # Get notional arrays (USD per bucket)
        bid_notional_usd = frame.bid_notional if frame.bid_notional else []
        ask_notional_usd = frame.ask_notional if frame.ask_notional else []

        # Calculate BTC quantities: btc = usd / price
        bid_size_btc = []
        ask_size_btc = []
        for i, price in enumerate(prices):
            if price > 0:
                bid_btc = bid_notional_usd[i] / price if i < len(bid_notional_usd) else 0.0
                ask_btc = ask_notional_usd[i] / price if i < len(ask_notional_usd) else 0.0
            else:
                bid_btc = 0.0
                ask_btc = 0.0
            bid_size_btc.append(round(bid_btc, 6))
            ask_size_btc.append(round(ask_btc, 6))

        response = {
            "symbol": symbol,
            "ts": frame.ts,
            "src": frame.src,
            "step": frame.step,
            "price_min": p_min,
            "price_max": p_max,
            "prices": prices,
            "bid_u8": bid_u8,
            "ask_u8": ask_u8,
            "bid_notional_usd": [round(v, 2) for v in bid_notional_usd],
            "ask_notional_usd": [round(v, 2) for v in ask_notional_usd],
            "bid_size_btc": bid_size_btc,
            "ask_size_btc": ask_size_btc,
            "norm_p50": frame.norm_p50,
            "norm_p95": frame.norm_p95,
            "total_bid_notional": frame.total_bid_notional,
            "total_ask_notional": frame.total_ask_notional,
            "total_bid_btc": round(frame.total_bid_notional / frame.src, 6) if frame.src > 0 else 0.0,
            "total_ask_btc": round(frame.total_ask_notional / frame.src, 6) if frame.src > 0 else 0.0,
            "scale": 255
        }

        if warning:
            response["_warning"] = warning

        return JSONResponse(content=response)

    @app.get("/v2/orderbook_heatmap_30s_history")
    async def orderbook_heatmap_30s_history(
        symbol: str = Query(default="BTC", description="Symbol to query"),
        minutes: int = Query(default=360, description="Minutes of history (clamped 5..720)"),
        stride: int = Query(default=1, description="Downsample stride (clamped 1..60)"),
        range_pct: float = Query(default=0.10, description="Price range as decimal"),
        step: float = Query(default=20.0, description="Price bucket size"),
        price_min: float = Query(default=None, description="Override minimum price"),
        price_max: float = Query(default=None, description="Override maximum price")
    ):
        """
        Get historical 30-second orderbook heatmap frames.

        Returns time-series of bid/ask intensity arrays for heatmap backfill.
        Intensities are u8 encoded (0-255) in flat row-major arrays.

        Response format:
        - t: array of timestamps (int milliseconds)
        - prices: unified price grid
        - bid_u8: flat row-major array [frames * prices] of u8 intensities
        - ask_u8: flat row-major array [frames * prices] of u8 intensities
        - step: price bucket size
        - scale: 255 (max intensity value)
        """
        if not HAS_OB_HEATMAP or not _ob_buffer:
            raise HTTPException(
                status_code=503,
                detail="Orderbook heatmap not available"
            )

        # Clamp parameters
        minutes = max(5, min(720, minutes))
        stride = max(1, min(60, stride))

        # Get frames from buffer
        frames = _ob_buffer.get_frames(minutes=minutes, stride=stride)

        if not frames:
            # Return empty response
            return JSONResponse(content={
                "t": [],
                "prices": [],
                "bid_u8": [],
                "ask_u8": [],
                "bid_notional_usd": [],
                "ask_notional_usd": [],
                "bid_size_btc": [],
                "ask_size_btc": [],
                "step": step,
                "scale": 255,
                "norm_method": "p50_p95"
            })

        # Build unified grid (using ob_heatmap version for OrderbookFrame)
        prices, p_min, p_max = ob_build_unified_grid(
            frames,
            price_min=price_min,
            price_max=price_max,
            step=step
        )

        n_prices = len(prices)

        # Build flat row-major arrays
        t_arr = []
        bid_flat = []
        ask_flat = []
        bid_notional_flat = []
        ask_notional_flat = []
        bid_btc_flat = []
        ask_btc_flat = []

        for frame in frames:
            t_arr.append(int(frame.ts * 1000))  # ms
            bid_row, ask_row = resample_frame_to_grid(frame, prices, step)
            bid_flat.extend(bid_row)
            ask_flat.extend(ask_row)

            # Get notional arrays for this frame
            frame_bid_notional = frame.bid_notional if frame.bid_notional else []
            frame_ask_notional = frame.ask_notional if frame.ask_notional else []

            # Map notionals to unified grid (same indices as intensities)
            for i, price in enumerate(prices):
                # Find matching notional from frame
                frame_prices = frame.get_prices()
                bid_usd = 0.0
                ask_usd = 0.0
                if price in frame_prices:
                    idx = frame_prices.index(price)
                    if idx < len(frame_bid_notional):
                        bid_usd = frame_bid_notional[idx]
                    if idx < len(frame_ask_notional):
                        ask_usd = frame_ask_notional[idx]

                bid_notional_flat.append(round(bid_usd, 2))
                ask_notional_flat.append(round(ask_usd, 2))
                bid_btc_flat.append(round(bid_usd / price, 6) if price > 0 else 0.0)
                ask_btc_flat.append(round(ask_usd / price, 6) if price > 0 else 0.0)

        # Debug logging
        num_frames = len(t_arr)
        print(f"[ob_heatmap_30s_history] frames={num_frames} prices={n_prices} "
              f"bid_len={len(bid_flat)} ask_len={len(ask_flat)}")

        return JSONResponse(content={
            "t": t_arr,
            "prices": prices,
            "bid_u8": bid_flat,
            "ask_u8": ask_flat,
            "bid_notional_usd": bid_notional_flat,
            "ask_notional_usd": ask_notional_flat,
            "bid_size_btc": bid_btc_flat,
            "ask_size_btc": ask_btc_flat,
            "step": step,
            "price_min": p_min,
            "price_max": p_max,
            "scale": 255,
            "norm_method": "p50_p95"
        })

    @app.get("/v2/orderbook_heatmap_30s_stats")
    async def orderbook_heatmap_30s_stats():
        """
        Get orderbook heatmap buffer statistics.

        Useful for debugging and monitoring.
        """
        if not HAS_OB_HEATMAP or not _ob_buffer:
            raise HTTPException(
                status_code=503,
                detail="Orderbook heatmap not available"
            )

        stats = _ob_buffer.get_stats()
        stats["current_time"] = time.time()
        if stats["last_ts"] > 0:
            stats["last_frame_age_s"] = round(time.time() - stats["last_ts"], 1)
        else:
            stats["last_frame_age_s"] = None

        # Include reconstructor stats if available
        if os.path.exists(OB_RECON_STATS_FILE):
            try:
                with open(OB_RECON_STATS_FILE, 'r') as f:
                    recon_stats = json.load(f)
                stats["reconstructor"] = recon_stats
                # Calculate age of reconstructor stats
                if "written_at" in recon_stats:
                    stats["reconstructor"]["stats_age_s"] = round(
                        time.time() - recon_stats["written_at"], 1
                    )
            except Exception:
                stats["reconstructor"] = {"error": "failed to read stats"}
        else:
            stats["reconstructor"] = {"error": "stats file not found"}

        return JSONResponse(content=stats)

    @app.get("/v2/orderbook_heatmap_30s_debug")
    async def orderbook_heatmap_30s_debug():
        """
        Debug endpoint for orderbook heatmap system.

        Shows internal state without requiring working heatmap.
        """
        debug_info = {
            "has_ob_heatmap_module": HAS_OB_HEATMAP,
            "ob_heatmap_import_error": OB_HEATMAP_IMPORT_ERROR,
            "ob_buffer_initialized": _ob_buffer is not None,
            "ob_heatmap_file": OB_HEATMAP_FILE,
            "ob_heatmap_file_exists": os.path.exists(OB_HEATMAP_FILE),
            "ob_recon_stats_file": OB_RECON_STATS_FILE,
            "ob_recon_stats_exists": os.path.exists(OB_RECON_STATS_FILE),
        }

        # File sizes
        if os.path.exists(OB_HEATMAP_FILE):
            debug_info["ob_heatmap_file_size_bytes"] = os.path.getsize(OB_HEATMAP_FILE)

        # Buffer stats if available
        if _ob_buffer is not None:
            try:
                debug_info["ob_buffer_stats"] = _ob_buffer.get_stats()
            except Exception as e:
                debug_info["ob_buffer_stats_error"] = str(e)

        # Recon stats if available
        if os.path.exists(OB_RECON_STATS_FILE):
            try:
                with open(OB_RECON_STATS_FILE, 'r') as f:
                    debug_info["recon_stats"] = json.load(f)
            except Exception as e:
                debug_info["recon_stats_error"] = str(e)

        return JSONResponse(content=debug_info)

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
