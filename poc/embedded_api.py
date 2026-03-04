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
  liq_heatmap_v2_{SYM}.bin  (V2 history, per symbol)

Usage:
    # In full_metrics_viewer.py:
    from embedded_api import create_embedded_app, start_api_thread

    app = create_embedded_app(
        ob_buffer=processor.ob_heatmap_buffers,
        snapshot_dir=POC_DIR,
        engine_manager=processor.engine_manager
    )
    api_thread = start_api_thread(app, host="127.0.0.1", port=8899)
"""

import copy
import hashlib
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

# Maximum grid buckets — prevents DoS via tiny step or huge range
MAX_GRID_BUCKETS = 10000


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

# File size limit for persistence rotation (50 MB)
MAX_PERSISTENCE_FILE_SIZE = 50 * 1024 * 1024


MAX_CACHE_ENTRIES = 500  # LRU cap to prevent unbounded cache growth

# Maximum cache key length before hashing (prevents key storage bloat)
_MAX_KEY_LENGTH = 200


class ResponseCache:
    """
    Thread-safe in-memory response cache for API endpoints.

    Allows hundreds of concurrent users to receive the same cached response
    instead of each triggering separate data reads. Cache entries expire
    after a configurable TTL. LRU eviction caps entries at MAX_CACHE_ENTRIES.

    Cache keys should be built via ``normalize_cache_key()`` to prevent
    cardinality explosion from raw user-controlled query params.

    Usage:
        cache = ResponseCache()
        cached = cache.get("liq_heatmap?symbol=BTC", ttl=5.0)
        if cached is not None:
            return cached
        # ... generate response ...
        cache.set("liq_heatmap?symbol=BTC", response)
        return response
    """

    def __init__(self, max_entries: int = MAX_CACHE_ENTRIES):
        # Cache entry tuple: (response, created_ts, access_ts)
        self._cache: Dict[str, Tuple[Any, float, float]] = {}
        self._lock = threading.Lock()
        self._sets_count: int = 0
        self._max_entries = max_entries

    @staticmethod
    def normalize_cache_key(**params) -> str:
        """Build a normalized cache key from endpoint params.

        - Symbols: stripped, uppercased, truncated to 10 chars
        - Numeric values: rounded to 2 decimal places
        - Side: must be 'long', 'short', or None (omitted from key)
        - Keys exceeding 200 chars are hashed via MD5 for uniqueness
        """
        parts: List[str] = []
        for k, v in sorted(params.items()):
            if v is None:
                continue
            if k == "symbol":
                v = str(v).strip().upper()[:10]
            elif k == "side":
                v = str(v).strip().lower()
            elif isinstance(v, float):
                v = round(v, 2)
            parts.append(f"{k}={v}")
        key = "&".join(parts)
        if len(key) > _MAX_KEY_LENGTH:
            key = hashlib.md5(key.encode()).hexdigest()
        return key

    def get(self, key: str, ttl: float) -> Optional[JSONResponse]:
        """Return cached JSONResponse if within TTL, else None."""
        with self._lock:
            entry = self._cache.get(key)
            if not entry:
                return None

            now = time.time()
            # Backward compatibility for legacy 2-tuple entries: (response, ts)
            if len(entry) == 2:
                response, ts = entry
                created_ts = ts
                access_ts = ts
            else:
                response, created_ts, access_ts = entry

            # TTL is based on age since set/creation, not last access
            if (now - created_ts) >= ttl:
                del self._cache[key]
                return None

            # Update access timestamp only for LRU tracking
            self._cache[key] = (response, created_ts, now)
            return response
        return None

    def set(self, key: str, response: JSONResponse) -> None:
        """Store a JSONResponse in the cache."""
        with self._lock:
            now = time.time()
            self._cache[key] = (response, now, now)
            self._sets_count += 1
            if self._sets_count % 100 == 0:
                self._evict_expired()
            # LRU eviction if over max entries
            if len(self._cache) > self._max_entries:
                self._evict_lru()

    def _evict_expired(self) -> None:
        """Remove entries older than max_ttl to prevent unbounded growth. Caller must hold _lock."""
        now = time.time()
        max_ttl = 60.0  # nothing should be cached longer than this
        expired = []
        for key, entry in self._cache.items():
            # Legacy format support: (response, ts)
            created_ts = entry[1]
            if (now - created_ts) > max_ttl:
                expired.append(key)
        for k in expired:
            del self._cache[k]

    def _evict_lru(self) -> None:
        """Evict oldest-accessed entries until under max_entries. Caller must hold _lock."""
        excess = len(self._cache) - self._max_entries
        if excess <= 0:
            return
        # Sort by access time (oldest first) and remove excess
        oldest = sorted(
            self._cache.items(),
            # Legacy format support: (response, ts) uses ts as access_ts
            key=lambda item: item[1][2] if len(item[1]) == 3 else item[1][1],
        )
        for k, _ in oldest[:excess]:
            del self._cache[k]

    # Dev sanity check (manual):
    #   cache = ResponseCache()
    #   cache.set("k", JSONResponse({"ok": True}))
    #   _ = cache.get("k", ttl=0.2)
    #   time.sleep(0.25)
    #   assert cache.get("k", ttl=0.2) is None
    # Repeated reads before TTL should NOT pin entries forever without set().


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

        # Cap n_buckets to prevent corrupt frames from allocating huge arrays
        if n_buckets > LIQ_MAX_BUCKETS:
            logger.warning("Corrupt frame: n_buckets=%d exceeds LIQ_MAX_BUCKETS=%d, skipping",
                           n_buckets, LIQ_MAX_BUCKETS)
            return None

        # Validate that the record has enough data for the claimed buckets.
        # Each side needs n_buckets bytes of intensity data starting after the header.
        # Layout: header + LIQ_MAX_BUCKETS (long) + LIQ_MAX_BUCKETS (short) + padding
        min_required = LIQ_HEADER_SIZE + 2 * LIQ_MAX_BUCKETS
        if len(data) < min_required:
            logger.warning("Truncated frame: %d bytes < %d required (header + 2*%d), skipping",
                           len(data), min_required, LIQ_MAX_BUCKETS)
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

    @staticmethod
    def _frame_to_bytes(frame: HistoryFrame, step: float) -> bytes:
        """Serialize a HistoryFrame to a fixed-size binary record.

        Layout (LIQ_FRAME_RECORD_SIZE = 4096 bytes):
          [0..48)   header: ts(d), src(d), price_min(d), price_max(d), step(d), n_buckets(I)
          [48..1048) long intensity:  n_buckets bytes + zero-padding to LIQ_MAX_BUCKETS
          [1048..2048) short intensity: n_buckets bytes + zero-padding to LIQ_MAX_BUCKETS
          [2048..4096) reserved padding (zeros)
        """
        n_buckets = min(len(frame.long_intensity), LIQ_MAX_BUCKETS)
        header = struct.pack(
            LIQ_HEADER_FORMAT,
            frame.ts, frame.src, frame.price_min, frame.price_max,
            step, n_buckets,
        )

        # Pad intensity arrays to exactly LIQ_MAX_BUCKETS each
        long_padded = frame.long_intensity[:n_buckets].ljust(LIQ_MAX_BUCKETS, b'\x00')
        short_padded = frame.short_intensity[:n_buckets].ljust(LIQ_MAX_BUCKETS, b'\x00')

        record = header + long_padded + short_padded
        # Pad to exactly LIQ_FRAME_RECORD_SIZE
        record = record.ljust(LIQ_FRAME_RECORD_SIZE, b'\x00')
        return record

    def _write_frame_to_disk(self, frame: HistoryFrame, step: float) -> None:
        """Append a single frame to the persistence file.

        Opens file in append-binary mode, writes a single contiguous record,
        flushes, and closes. Thread safety: caller must hold self._lock.
        Errors are logged but never propagated — disk failure must not break
        the live history endpoint.
        """
        if not self.persistence_path:
            return

        try:
            # Rotate if file exceeds size limit
            self._maybe_rotate_file()

            record = self._frame_to_bytes(frame, step)
            with open(self.persistence_path, 'ab') as f:
                f.write(record)
                f.flush()
        except Exception as e:
            logger.error("Failed to write frame to %s: %s",
                         self.persistence_path, e, exc_info=True)

    def _maybe_rotate_file(self) -> None:
        """Rotate persistence file if it exceeds MAX_PERSISTENCE_FILE_SIZE.

        Single-slot rotation: current → {path}.old (overwrites any existing .old).
        """
        try:
            if not os.path.exists(self.persistence_path):
                return
            file_size = os.path.getsize(self.persistence_path)
            if file_size <= MAX_PERSISTENCE_FILE_SIZE:
                return

            old_path = self.persistence_path + '.old'
            # Remove existing .old first (single-slot rotation)
            if os.path.exists(old_path):
                os.unlink(old_path)
            os.rename(self.persistence_path, old_path)
            logger.info("Rotated persistence file %s (%.1f MB) → %s",
                        self.persistence_path, file_size / (1024 * 1024), old_path)
        except Exception as e:
            logger.warning("Failed to rotate persistence file %s: %s",
                           self.persistence_path, e)

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

            # Persist to disk — step comes from the snapshot dict
            step = snapshot.get('step', 0)
            if step <= 0 and len(frame.prices) >= 2:
                step = frame.prices[1] - frame.prices[0]
            self._write_frame_to_disk(frame, step)

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


def _normalize_price(p: float, step: float, ndigits: int) -> float:
    """Snap a price to the nearest grid point and round to ndigits.

    Prevents float misalignment when building lookup dicts — e.g. a frame
    key of 95019.999999 will snap to 95020.0 with step=20.
    """
    return round(round(p / step) * step, ndigits)


def build_unified_grid(
    frames: List[HistoryFrame],
    current_src: float,
    step: float = DEFAULT_STEP,
    band_pct: float = DEFAULT_BAND_PCT
) -> Tuple[List[float], float, float]:
    """Build a unified price grid that covers all frames.

    Uses the union of all frames' price ranges so that old frames whose
    prices have drifted from the current price are still mapped correctly.
    Falls back to current_src ± band_pct when no frames are available.
    If the union grid exceeds MAX_GRID_BUCKETS, it is trimmed symmetrically
    around current_src.
    """
    if current_src <= 0:
        if frames:
            current_src = frames[-1].src
        else:
            return ([], 0, 0)

    ndigits = _step_ndigits(step)

    if frames:
        # Union of all frames' actual price ranges
        union_min = min(f.price_min for f in frames)
        union_max = max(f.price_max for f in frames)
        # Also include the current-price band so the latest minute is covered
        band_min = current_src * (1 - band_pct)
        band_max = current_src * (1 + band_pct)
        raw_min = min(union_min, band_min)
        raw_max = max(union_max, band_max)
    else:
        raw_min = current_src * (1 - band_pct)
        raw_max = current_src * (1 + band_pct)

    price_min = round(round(raw_min / step) * step, ndigits)
    price_max = round(round(raw_max / step) * step, ndigits)

    # Safety: cap grid size to MAX_GRID_BUCKETS, trimming around current_src
    est_buckets = int(round((price_max - price_min) / step)) + 1
    if est_buckets > MAX_GRID_BUCKETS:
        half_range = (MAX_GRID_BUCKETS // 2) * step
        price_min = round(round((current_src - half_range) / step) * step, ndigits)
        price_max = round(round((current_src + half_range) / step) * step, ndigits)

    prices = _build_price_grid(price_min, price_max, step)

    return (prices, price_min, price_max)


def map_frame_to_grid(
    frame: HistoryFrame,
    target_prices: List[float],
    step: float
) -> Tuple[List[int], List[int]]:
    """Map a frame's intensities to a target grid.

    Both frame keys and target prices are snapped via ``_normalize_price``
    to prevent float misalignment causing silent zero-fills.
    """
    ndigits = _step_ndigits(step)

    frame_lookup_long = {}
    frame_lookup_short = {}

    for i, price in enumerate(frame.prices):
        norm_p = _normalize_price(price, step, ndigits)
        if i < len(frame.long_intensity):
            frame_lookup_long[norm_p] = frame.long_intensity[i]
        if i < len(frame.short_intensity):
            frame_lookup_short[norm_p] = frame.short_intensity[i]

    long_out = []
    short_out = []

    for target_price in target_prices:
        norm_t = _normalize_price(target_price, step, ndigits)
        best_long = frame_lookup_long.get(norm_t, 0)
        best_short = frame_lookup_short.get(norm_t, 0)
        long_out.append(best_long)
        short_out.append(best_short)

    return (long_out, short_out)


def _log_grid_overlap(sym: str, version: str, frames: List[HistoryFrame],
                      prices: List[float], step: float):
    """Log DEBUG overlap ratio between unified grid and individual frames.

    For each frame, counts how many of its non-zero intensity prices
    fall within the unified grid.  Low overlap indicates grid drift.
    """
    if not frames or not prices:
        return
    ndigits = _step_ndigits(step)
    grid_set = set(_normalize_price(p, step, ndigits) for p in prices)

    total_keys = 0
    matched_keys = 0
    for frame in frames:
        for i, price in enumerate(frame.prices):
            has_data = False
            if i < len(frame.long_intensity) and frame.long_intensity[i] > 0:
                has_data = True
            if i < len(frame.short_intensity) and frame.short_intensity[i] > 0:
                has_data = True
            if has_data:
                total_keys += 1
                norm_p = _normalize_price(price, step, ndigits)
                if norm_p in grid_set:
                    matched_keys += 1

    overlap_pct = (matched_keys / total_keys * 100) if total_keys > 0 else 0.0
    logger.debug(
        "%s history grid overlap (%s): %d/%d keys mapped (%.1f%%), "
        "grid=[%.2f..%.2f] %d buckets, frames=%d",
        version, sym, matched_keys, total_keys, overlap_pct,
        prices[0] if prices else 0, prices[-1] if prices else 0,
        len(prices), len(frames)
    )


class EmbeddedAPIState:
    """
    Holds shared references to buffers and files.

    Per-symbol snapshot caches and history buffers (CLAUDE.md Rule #8:
    per-symbol buffers must be independent, never shared).
    """

    def __init__(
        self,
        ob_buffer=None,
        snapshot_dir: Optional[str] = None,
        engine_manager: Optional['EngineManager'] = None,
        oi_poller: Optional[Any] = None,
        liq_events: Optional[Dict[str, Any]] = None,
        liq_events_lock: Optional[Any] = None,
        # Legacy params — accepted but ignored (kept for caller compat during transition)
        snapshot_file: Optional[str] = None,
        snapshot_v2_file: Optional[str] = None,
        liq_heatmap_v2_file: Optional[str] = None,
    ):
        # ob_buffer: Dict[str, OrderbookHeatmapBuffer] keyed by symbol, or single buffer (legacy)
        if isinstance(ob_buffer, dict):
            self.ob_buffers: Dict[str, Any] = ob_buffer
        elif ob_buffer is not None:
            # Legacy single-buffer: assign to BTC for backwards compat
            self.ob_buffers = {"BTC": ob_buffer}
        else:
            self.ob_buffers = {}
        # Legacy alias for code that still references state.ob_buffer
        self.ob_buffer = self.ob_buffers.get("BTC")
        self.engine_manager = engine_manager
        self.oi_poller = oi_poller  # MultiExchangeOIPoller or OIPollerThread
        # Per-symbol individual liq event deques + lock (shared from FullMetricsProcessor)
        self.liq_events: Dict[str, Any] = liq_events or {}
        self.liq_events_lock = liq_events_lock
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

        # Per-symbol V2 history buffers with per-symbol binary files
        self.v2_histories: Dict[str, LiquidationHeatmapBuffer] = {}
        for sym in VALID_SYMBOLS:
            v2_bin = os.path.join(self._snapshot_dir, f"liq_heatmap_v2_{sym}.bin")
            self.v2_histories[sym] = LiquidationHeatmapBuffer(v2_bin)

        # One-time cleanup: remove stale v1 binary history files (no longer written)
        for sym in VALID_SYMBOLS:
            v1_stale = os.path.join(self._snapshot_dir, f"liq_heatmap_v1_{sym}.bin")
            if os.path.exists(v1_stale):
                try:
                    os.unlink(v1_stale)
                    logger.info("Removed stale V1 history file: %s", v1_stale)
                except OSError as e:
                    logger.warning("Failed to remove stale V1 history file %s: %s", v1_stale, e)

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
            else:
                self._v2_caches[symbol] = data
                self.v2_histories[symbol].add_frame(data)
        except Exception as e:
            # Keep last good cached snapshot — never crash the refresh thread
            logger.warning("Cache refresh error (%s/%s): %s", version, symbol, e)

    def stop(self):
        self._running = False
        if self._refresh_thread.is_alive():
            self._refresh_thread.join(timeout=5)
            if self._refresh_thread.is_alive():
                logger.warning("API refresh thread did not terminate within 5s")

    def get_v1_snapshot(self, symbol: str = "BTC") -> Optional[Dict]:
        return self._v1_caches.get(symbol)

    def get_v2_snapshot(self, symbol: str = "BTC") -> Optional[Dict]:
        return self._v2_caches.get(symbol)

    def get_v1_history(self, symbol: str = "BTC") -> Optional[LiquidationHeatmapBuffer]:
        return None  # V1 history removed — endpoint returns 503

    def get_v2_history(self, symbol: str = "BTC") -> Optional[LiquidationHeatmapBuffer]:
        return self.v2_histories.get(symbol)


def create_embedded_app(
    ob_buffer=None,
    snapshot_dir: Optional[str] = None,
    engine_manager: Optional['EngineManager'] = None,
    oi_poller: Optional[Any] = None,
    liq_events: Optional[Dict[str, Any]] = None,
    liq_events_lock: Optional[Any] = None,
    # Legacy params — accepted for caller compat during transition, passed through
    snapshot_file: Optional[str] = None,
    snapshot_v2_file: Optional[str] = None,
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
        liq_events=liq_events,
        liq_events_lock=liq_events_lock,
        snapshot_file=snapshot_file,
        snapshot_v2_file=snapshot_v2_file,
        liq_heatmap_v2_file=liq_heatmap_v2_file,
    )

    # Response cache — shared across all concurrent requests
    cache = ResponseCache()

    app = FastAPI(
        title="Liquidation Heatmap API (Embedded)",
        description="API for liquidation zone visualization - embedded mode with shared buffers",
        version=API_VERSION
    )

    # Attach state to app so callers can access it for shutdown (H9)
    app._api_state = state

    def _validate_symbol(symbol: str) -> str:
        """Validate and normalize symbol. Returns uppercase short symbol or raises 400."""
        sym = symbol.strip().upper().replace("USDT", "")
        if not sym or len(sym) > 10:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid symbol '{symbol}'. Valid symbols: {', '.join(sorted(VALID_SYMBOLS))}"
            )
        if sym not in VALID_SYMBOLS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid symbol '{symbol}'. Valid symbols: {', '.join(sorted(VALID_SYMBOLS))}"
            )
        return sym

    def _validate_side(side: Optional[str]) -> Optional[str]:
        """Validate side parameter. Returns 'long', 'short', or None. Raises 400 on invalid."""
        if side is None:
            return None
        s = side.strip().lower()
        if s not in ("long", "short"):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid side '{side}'. Must be 'long' or 'short'."
            )
        return s

    @app.on_event("startup")
    async def startup():
        for sym in sorted(VALID_SYMBOLS):
            ob_buf = state.ob_buffers.get(sym)
            if ob_buf:
                ob_stats = ob_buf.get_stats()
                print(f"[EMBEDDED_API] {sym} OB buffer: {ob_stats.get('frames_in_memory', 0)} frames")
            else:
                print(f"[EMBEDDED_API] {sym} OB buffer: not configured")
        print(f"[EMBEDDED_API] Snapshot dir: {state._snapshot_dir}")
        for sym in sorted(VALID_SYMBOLS):
            v2_frames = state.v2_histories[sym].frame_count()
            print(f"[EMBEDDED_API] {sym}: V2 history={v2_frames} frames")

    @app.on_event("shutdown")
    async def shutdown():
        state.stop()

    @app.get("/health")
    @app.get("/v1/health")  # backwards compat alias
    async def health():
        """Health check endpoint."""
        symbols_status = {}
        for sym in sorted(VALID_SYMBOLS):
            v1_snap = state.get_v1_snapshot(sym)
            v2_snap = state.get_v2_snapshot(sym)
            ob_buf = state.ob_buffers.get(sym)
            ob_buf_stats = ob_buf.get_stats() if ob_buf else {}
            symbols_status[sym] = {
                "v1_available": v1_snap is not None,
                "v1_ts": v1_snap.get('ts', 0) if v1_snap else 0,
                "v2_available": v2_snap is not None,
                "v2_ts": v2_snap.get('ts', 0) if v2_snap else 0,
                "v2_history_frames": state.v2_histories[sym].frame_count(),
                "ob_frames": ob_buf_stats.get('frames_in_memory', 0),
            }

        return {
            "ok": True,
            "version": API_VERSION,
            "mode": "embedded",
            "uptime_s": round(time.time() - state.start_time, 1),
            "ob_buffers": {sym: bool(state.ob_buffers.get(sym)) for sym in sorted(VALID_SYMBOLS)},
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
        cache_key = cache.normalize_cache_key(endpoint="oi", symbol=symbol_short)
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
                status_code=503,
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

        cache_key = cache.normalize_cache_key(endpoint="liq_heatmap", symbol=sym)
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

        cache_key = cache.normalize_cache_key(endpoint="liq_heatmap_history", symbol=sym, minutes=minutes, stride=stride)
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
        _log_grid_overlap(sym, "V1", frames, prices, step)

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

        cache_key = cache.normalize_cache_key(endpoint="liq_heatmap_v2", symbol=sym, min_notional=min_notional)
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

        cache_key = cache.normalize_cache_key(endpoint="liq_heatmap_v2_history", symbol=sym, minutes=minutes, stride=stride)
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
        _log_grid_overlap(sym, "V2", frames, prices, step)

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

        cache_key = cache.normalize_cache_key(endpoint="liq_stats", symbol=sym)
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
    # Individual Liquidation Events (for liq bubbles, tape, footprint liq rows)
    # =========================================================================

    @app.get("/liq_events")
    @app.get("/v2/liq_events")
    async def liq_events(
        symbol: str = Query(default="BTC", description="Symbol to query (BTC, ETH, SOL)"),
        limit: int = Query(default=500, description="Max events to return (capped at 5000)"),
        since_ts: int = Query(default=0, description="Only return events with ts > this value (milliseconds)"),
    ):
        """
        Get individual liquidation (force order) events.

        Returns raw events with exchange attribution for liq bubbles,
        footprint bar liq rows, and liquidation tape.
        Events are returned in chronological order (oldest first).
        If more events match than `limit`, the most recent `limit` events are returned.
        """
        sym = _validate_symbol(symbol)
        limit = max(1, min(5000, limit))

        if not state.liq_events or not state.liq_events_lock:
            raise HTTPException(
                status_code=503,
                detail="Liquidation events buffer not available"
            )

        sym_deque = state.liq_events.get(sym)
        if sym_deque is None:
            raise HTTPException(
                status_code=400,
                detail=f"No liquidation event buffer for symbol {sym}"
            )

        # Copy deque under lock, filter/slice outside
        with state.liq_events_lock:
            all_events = list(sym_deque)

        # Filter by since_ts
        if since_ts > 0:
            all_events = [e for e in all_events if e["ts"] > since_ts]

        # Take last `limit` events (most recent), return in chronological order
        if len(all_events) > limit:
            all_events = all_events[-limit:]

        oldest_available_ts = all_events[0]["ts"] if all_events else 0
        newest_ts = all_events[-1]["ts"] if all_events else 0

        return JSONResponse(content={
            "symbol": sym,
            "events": all_events,
            "count": len(all_events),
            "oldest_available_ts": oldest_available_ts,
            "newest_ts": newest_ts,
            "ts_unit": "ms",
        })

    # =========================================================================
    # Orderbook Heatmap Endpoints (30s DoM — uses per-symbol shared buffer)
    # =========================================================================

    def _get_ob_buffer(symbol: str):
        """Validate symbol and return the per-symbol orderbook buffer. Raises 400/503."""
        sym = _validate_symbol(symbol)
        ob_buf = state.ob_buffers.get(sym)
        if not ob_buf:
            raise HTTPException(
                status_code=503,
                detail=f"Orderbook buffer not available for {sym}"
            )
        return sym, ob_buf

    @app.get("/orderbook_heatmap")
    @app.get("/v2/orderbook_heatmap_30s")  # backwards compat alias
    async def orderbook_heatmap_30s(
        symbol: str = Query(default="BTC", description="Symbol to query (BTC, ETH, SOL)"),
        range_pct: float = Query(default=0.10, description="Price range as decimal"),
        step: float = Query(default=None, description="Price bucket size (default: per-symbol native step)"),
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
        sym, ob_buf = _get_ob_buffer(symbol)

        if not HAS_OB_HEATMAP:
            raise HTTPException(
                status_code=503,
                detail="Orderbook heatmap module not available"
            )

        frame = ob_buf.get_latest()
        if not frame:
            raise HTTPException(
                status_code=404,
                detail="No orderbook frames available yet"
            )

        # Resolve step: use frame's native step if not explicitly provided
        if step is None:
            step = frame.step

        # Validate user-provided grid parameters
        if step <= 0:
            raise HTTPException(status_code=400, detail="step must be positive")
        if range_pct <= 0 or range_pct > 1.0:
            raise HTTPException(status_code=400, detail="range_pct must be between 0 and 1.0")
        if price_min is not None and price_max is not None and price_max <= price_min:
            raise HTTPException(status_code=400, detail="price_max must be greater than price_min")
        # Estimate grid size to prevent DoS from tiny step with wide range
        if price_min is not None and price_max is not None:
            est_buckets = int((price_max - price_min) / step) + 1
            if est_buckets > MAX_GRID_BUCKETS or est_buckets <= 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"grid too large: {est_buckets} buckets exceeds limit of {MAX_GRID_BUCKETS}"
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
        cache_key = cache.normalize_cache_key(endpoint="orderbook_heatmap", symbol=symbol, range_pct=range_pct, step=step, price_min=price_min, price_max=price_max)
        cached = cache.get(cache_key, ttl=5.0)
        if cached is not None:
            return cached

        server_recv_ts_ms = int(time.time() * 1000)

        # JSON format response
        if price_min is not None or price_max is not None:
            p_min = price_min if price_min is not None else frame.price_min
            p_max = price_max if price_max is not None else frame.price_max
            prices, p_min, p_max = ob_build_unified_grid([frame], p_min, p_max, step)
            if len(prices) > MAX_GRID_BUCKETS:
                raise HTTPException(
                    status_code=400,
                    detail=f"grid too large: {len(prices)} buckets exceeds limit of {MAX_GRID_BUCKETS}"
                )
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
        symbol: str = Query(default="BTC", description="Symbol to query (BTC, ETH, SOL)"),
        minutes: int = Query(default=720, description="Minutes of history (clamped 5..720)"),  # was 360 (6h), changed to 720 (12h) 2026-02-15
        stride: int = Query(default=1, description="Downsample stride (clamped 1..60)"),
        step: float = Query(default=None, description="Price bucket size (default: per-symbol native step)"),
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
        sym, ob_buf = _get_ob_buffer(symbol)

        minutes = max(5, min(720, minutes))
        stride = max(1, min(60, stride))

        frames = ob_buf.get_frames(minutes=minutes, stride=stride)

        # Resolve step: use first frame's native step if not explicitly provided
        if step is None:
            if frames:
                step = frames[0].step
            else:
                # Fallback for empty response — look up from SYMBOL_CONFIGS
                _symbol_ob_steps = {"BTC": 20.0, "ETH": 1.0, "SOL": 0.10}
                step = _symbol_ob_steps.get(sym, 20.0)

        # Validate user-provided grid parameters
        if step <= 0:
            raise HTTPException(status_code=400, detail="step must be positive")
        if price_min is not None and price_max is not None and price_max <= price_min:
            raise HTTPException(status_code=400, detail="price_max must be greater than price_min")
        if price_min is not None and price_max is not None:
            est_buckets = int((price_max - price_min) / step) + 1
            if est_buckets > MAX_GRID_BUCKETS or est_buckets <= 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"grid too large: {est_buckets} buckets exceeds limit of {MAX_GRID_BUCKETS}"
                )

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
        cache_key = cache.normalize_cache_key(endpoint="orderbook_heatmap_history", symbol=sym, minutes=minutes, stride=stride, step=step, price_min=price_min, price_max=price_max)
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
    async def orderbook_heatmap_30s_stats(
        symbol: str = Query(default="BTC", description="Symbol to query (BTC, ETH, SOL)")
    ):
        """
        Get orderbook heatmap buffer and reconstructor statistics.

        Includes:
        - Buffer stats: frames in memory/disk, write timing
        - Reconstructor stats: state, resyncs, gaps, update IDs
        - Timing: last frame age, staleness indicator
        """
        sym, ob_buf = _get_ob_buffer(symbol)

        cache_key = cache.normalize_cache_key(endpoint="orderbook_heatmap_stats", symbol=sym)
        cached = cache.get(cache_key, ttl=10.0)
        if cached is not None:
            return cached

        stats = ob_buf.get_stats()
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
    async def orderbook_heatmap_30s_debug(
        symbol: str = Query(default="BTC", description="Symbol to query (BTC, ETH, SOL)")
    ):
        """Debug endpoint for orderbook heatmap system."""
        sym = _validate_symbol(symbol)

        cache_key = cache.normalize_cache_key(endpoint="orderbook_heatmap_debug", symbol=sym)
        cached = cache.get(cache_key, ttl=10.0)
        if cached is not None:
            return cached

        ob_buf = state.ob_buffers.get(sym)
        debug_info = {
            "symbol": sym,
            "has_ob_heatmap_module": HAS_OB_HEATMAP,
            "ob_buffer_available": ob_buf is not None,
            "mode": "embedded"
        }

        if ob_buf is not None:
            try:
                debug_info["ob_buffer_stats"] = ob_buf.get_stats()
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
        symbol_short = _validate_symbol(symbol)
        side = _validate_side(side)

        cache_key = cache.normalize_cache_key(endpoint="liq_zones", symbol=symbol_short, side=side, min_leverage=min_leverage, max_leverage=max_leverage, min_weight=min_weight)
        cached = cache.get(cache_key, ttl=5.0)
        if cached is not None:
            return cached

        if not state.engine_manager:
            raise HTTPException(
                status_code=503,
                detail="Engine manager not available"
            )

        zone_mgr = _get_zone_manager(symbol_short)
        if not zone_mgr:
            raise HTTPException(
                status_code=404,
                detail=f"No zone manager available for {symbol_short}. Is the engine registered?"
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
        symbol_short = _validate_symbol(symbol)

        cache_key = cache.normalize_cache_key(endpoint="liq_zones_summary", symbol=symbol_short)
        cached = cache.get(cache_key, ttl=5.0)
        if cached is not None:
            return cached

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
        symbol_short = _validate_symbol(symbol)

        cache_key = cache.normalize_cache_key(endpoint="liq_zones_heatmap", symbol=symbol_short, min_notional=min_notional, min_leverage=min_leverage, max_leverage=max_leverage, min_weight=min_weight)
        cached = cache.get(cache_key, ttl=5.0)
        if cached is not None:
            return cached

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
    host: str = None,
    port: int = 8899,
    log_level: str = "warning"
) -> threading.Thread:
    """Start the API server in a background daemon thread."""
    if not HAS_FASTAPI:
        raise ImportError("FastAPI not installed")

    # Resolve host: explicit param > API_HOST env var > default 127.0.0.1
    if host is None:
        host = os.environ.get("API_HOST", "127.0.0.1")

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
    print(f"  GET http://{host}:{port}/liq_events")
    print(f"  GET http://{host}:{port}/orderbook_heatmap")
    print(f"  GET http://{host}:{port}/orderbook_heatmap_history")
    print(f"  GET http://{host}:{port}/orderbook_heatmap_stats")
    print(f"  GET http://{host}:{port}/orderbook_heatmap_debug")
    print(f"  GET http://{host}:{port}/liq_zones")
    print(f"  GET http://{host}:{port}/liq_zones_summary")
    print(f"  GET http://{host}:{port}/liq_zones_heatmap")

    return thread
