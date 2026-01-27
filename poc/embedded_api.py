#!/usr/bin/env python3
"""
Embedded API Server for Full Metrics Viewer

This module provides FastAPI endpoints that share buffers directly with the
collector (full_metrics_viewer.py), eliminating the disk sync issue where
the standalone API would load frames once at startup and never refresh.

Usage:
    # In full_metrics_viewer.py:
    from embedded_api import create_embedded_app, start_api_thread

    app = create_embedded_app(
        ob_buffer=processor.ob_heatmap_buffer,
        snapshot_file=LIQ_API_SNAPSHOT,
        snapshot_v2_file=LIQ_API_SNAPSHOT_V2
    )
    api_thread = start_api_thread(app, host="127.0.0.1", port=8899)
"""

import json
import os
import time
import threading
from typing import Dict, List, Optional, Any, Tuple
import logging

logger = logging.getLogger(__name__)

# Try to import FastAPI
HAS_FASTAPI = False
try:
    from fastapi import FastAPI, Query, HTTPException
    from fastapi.responses import JSONResponse
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
        DEFAULT_STEP, DEFAULT_RANGE_PCT
    )
    HAS_OB_HEATMAP = True
except ImportError:
    pass

# API version
API_VERSION = "2.1.0"  # Bumped for embedded mode

# Default grid parameters
DEFAULT_STEP = 20.0
DEFAULT_BAND_PCT = 0.08


class EmbeddedAPIState:
    """
    Holds shared references to buffers and files.

    This allows the API to read directly from in-memory buffers
    instead of loading from disk.
    """

    def __init__(
        self,
        ob_buffer: Optional['OrderbookHeatmapBuffer'] = None,
        snapshot_file: Optional[str] = None,
        snapshot_v2_file: Optional[str] = None,
        liq_heatmap_v1_file: Optional[str] = None,
        liq_heatmap_v2_file: Optional[str] = None
    ):
        self.ob_buffer = ob_buffer  # Shared OrderbookHeatmapBuffer
        self.snapshot_file = snapshot_file  # V1 JSON snapshot path
        self.snapshot_v2_file = snapshot_v2_file  # V2 JSON snapshot path
        self.liq_heatmap_v1_file = liq_heatmap_v1_file
        self.liq_heatmap_v2_file = liq_heatmap_v2_file
        self.start_time = time.time()

        # Cached snapshot data (refreshed from files periodically)
        self._v1_cache: Optional[Dict] = None
        self._v1_cache_time: float = 0
        self._v2_cache: Optional[Dict] = None
        self._v2_cache_time: float = 0
        self._cache_ttl: float = 1.0  # Refresh from file every 1s

    def get_v1_snapshot(self) -> Optional[Dict]:
        """Get V1 snapshot, refreshing from file if stale."""
        now = time.time()
        if now - self._v1_cache_time > self._cache_ttl:
            self._refresh_v1_cache()
        return self._v1_cache

    def get_v2_snapshot(self) -> Optional[Dict]:
        """Get V2 snapshot, refreshing from file if stale."""
        now = time.time()
        if now - self._v2_cache_time > self._cache_ttl:
            self._refresh_v2_cache()
        return self._v2_cache

    def _refresh_v1_cache(self):
        """Refresh V1 cache from file."""
        if not self.snapshot_file or not os.path.exists(self.snapshot_file):
            return
        try:
            with open(self.snapshot_file, 'r') as f:
                self._v1_cache = json.load(f)
            self._v1_cache_time = time.time()
        except Exception:
            pass

    def _refresh_v2_cache(self):
        """Refresh V2 cache from file."""
        if not self.snapshot_v2_file or not os.path.exists(self.snapshot_v2_file):
            return
        try:
            with open(self.snapshot_v2_file, 'r') as f:
                self._v2_cache = json.load(f)
            self._v2_cache_time = time.time()
        except Exception:
            pass


def create_embedded_app(
    ob_buffer: Optional['OrderbookHeatmapBuffer'] = None,
    snapshot_file: Optional[str] = None,
    snapshot_v2_file: Optional[str] = None,
    liq_heatmap_v1_file: Optional[str] = None,
    liq_heatmap_v2_file: Optional[str] = None
) -> 'FastAPI':
    """
    Create FastAPI application with shared buffer references.

    Args:
        ob_buffer: Shared OrderbookHeatmapBuffer from collector
        snapshot_file: Path to V1 liquidation snapshot JSON
        snapshot_v2_file: Path to V2 liquidation snapshot JSON
        liq_heatmap_v1_file: Path to V1 liquidation heatmap binary
        liq_heatmap_v2_file: Path to V2 liquidation heatmap binary

    Returns:
        FastAPI application instance
    """
    if not HAS_FASTAPI:
        raise ImportError("FastAPI not installed. Install with: pip install fastapi uvicorn")

    # Create shared state
    state = EmbeddedAPIState(
        ob_buffer=ob_buffer,
        snapshot_file=snapshot_file,
        snapshot_v2_file=snapshot_v2_file,
        liq_heatmap_v1_file=liq_heatmap_v1_file,
        liq_heatmap_v2_file=liq_heatmap_v2_file
    )

    app = FastAPI(
        title="Liquidation Heatmap API (Embedded)",
        description="API for liquidation zone visualization - embedded mode with shared buffers",
        version=API_VERSION
    )

    @app.on_event("startup")
    async def startup():
        ob_frames = 0
        if state.ob_buffer:
            ob_stats = state.ob_buffer.get_stats()
            ob_frames = ob_stats.get('frames_in_memory', 0)

        print(f"[EMBEDDED_API] Started with shared buffer: {ob_frames} OB frames")
        print(f"[EMBEDDED_API] V1 snapshot: {state.snapshot_file}")
        print(f"[EMBEDDED_API] V2 snapshot: {state.snapshot_v2_file}")

    @app.get("/v1/health")
    async def health():
        """Health check endpoint."""
        ob_stats = {}
        if state.ob_buffer:
            ob_stats = state.ob_buffer.get_stats()

        v1_snapshot = state.get_v1_snapshot()
        v2_snapshot = state.get_v2_snapshot()

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
            "v1_snapshot": {
                "available": v1_snapshot is not None,
                "ts": v1_snapshot.get('ts', 0) if v1_snapshot else 0
            },
            "v2_snapshot": {
                "available": v2_snapshot is not None,
                "ts": v2_snapshot.get('ts', 0) if v2_snapshot else 0
            }
        }

    @app.get("/v1/liq_heatmap")
    async def liq_heatmap(
        symbol: str = Query(default="BTC", description="Symbol to query")
    ):
        """Get current V1 liquidation heatmap snapshot."""
        snapshot = state.get_v1_snapshot()
        if not snapshot:
            raise HTTPException(
                status_code=404,
                detail=f"No V1 snapshot available for {symbol}. Is the viewer running?"
            )

        if snapshot.get('symbol') != symbol:
            raise HTTPException(
                status_code=404,
                detail=f"Snapshot is for {snapshot.get('symbol')}, not {symbol}"
            )

        # Check staleness
        ts = snapshot.get('ts', 0)
        age = time.time() - ts
        if age > 120:
            snapshot["_warning"] = f"Data is {age:.0f}s old, viewer may be stopped"

        return JSONResponse(content=snapshot)

    @app.get("/v2/liq_heatmap")
    async def liq_heatmap_v2(
        symbol: str = Query(default="BTC", description="Symbol to query"),
        min_notional: float = Query(default=0, description="Minimum USD notional to include")
    ):
        """Get current V2 liquidation heatmap (tape + inference)."""
        snapshot = state.get_v2_snapshot()
        if not snapshot:
            raise HTTPException(
                status_code=404,
                detail=f"No V2 snapshot available for {symbol}. Is the viewer running?"
            )

        if snapshot.get('symbol') != symbol:
            raise HTTPException(
                status_code=404,
                detail=f"V2 snapshot is for {snapshot.get('symbol')}, not {symbol}"
            )

        # Apply min_notional filter if specified
        if min_notional > 0:
            snapshot = snapshot.copy()  # Don't modify cached version
            snapshot['long_levels'] = [
                lvl for lvl in snapshot.get('long_levels', [])
                if lvl.get('notional_usd', 0) >= min_notional
            ]
            snapshot['short_levels'] = [
                lvl for lvl in snapshot.get('short_levels', [])
                if lvl.get('notional_usd', 0) >= min_notional
            ]

        # Check staleness
        ts = snapshot.get('ts', 0)
        age = time.time() - ts
        if age > 120:
            snapshot["_warning"] = f"Data is {age:.0f}s old, viewer may be stopped"

        return JSONResponse(content=snapshot)

    # =========================================================================
    # Orderbook Heatmap Endpoints (30s DoM - uses shared buffer directly)
    # =========================================================================

    @app.get("/v2/orderbook_heatmap_30s")
    async def orderbook_heatmap_30s(
        symbol: str = Query(default="BTC", description="Symbol to query"),
        range_pct: float = Query(default=0.10, description="Price range as decimal"),
        step: float = Query(default=20.0, description="Price bucket size"),
        price_min: float = Query(default=None, description="Override minimum price"),
        price_max: float = Query(default=None, description="Override maximum price")
    ):
        """
        Get latest 30-second orderbook heatmap frame.

        Uses shared buffer directly - no disk sync needed!
        """
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

        # Check staleness
        age = time.time() - frame.ts
        warning = None
        if age > 60:
            warning = f"Data is {age:.0f}s old"

        # Use frame's grid or resample if overrides provided
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
            "norm_p50": frame.norm_p50,
            "norm_p95": frame.norm_p95,
            "total_bid_notional": frame.total_bid_notional,
            "total_ask_notional": frame.total_ask_notional,
            "scale": 255,
            "_mode": "shared_buffer"  # Indicates we're using shared memory
        }

        if warning:
            response["_warning"] = warning

        return JSONResponse(content=response)

    @app.get("/v2/orderbook_heatmap_30s_history")
    async def orderbook_heatmap_30s_history(
        symbol: str = Query(default="BTC", description="Symbol to query"),
        minutes: int = Query(default=360, description="Minutes of history (clamped 5..720)"),
        stride: int = Query(default=1, description="Downsample stride (clamped 1..60)"),
        step: float = Query(default=20.0, description="Price bucket size"),
        price_min: float = Query(default=None, description="Override minimum price"),
        price_max: float = Query(default=None, description="Override maximum price")
    ):
        """
        Get historical 30-second orderbook heatmap frames.

        Uses shared buffer directly - always up to date!
        """
        if not HAS_OB_HEATMAP or not state.ob_buffer:
            raise HTTPException(
                status_code=503,
                detail="Orderbook heatmap not available (no shared buffer)"
            )

        # Clamp parameters
        minutes = max(5, min(720, minutes))
        stride = max(1, min(60, stride))

        # Get frames from shared buffer
        frames = state.ob_buffer.get_frames(minutes=minutes, stride=stride)

        if not frames:
            return JSONResponse(content={
                "t": [],
                "prices": [],
                "bid_u8": [],
                "ask_u8": [],
                "step": step,
                "scale": 255,
                "norm_method": "p50_p95",
                "_mode": "shared_buffer"
            })

        # Build unified grid
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

        for frame in frames:
            t_arr.append(int(frame.ts * 1000))  # ms
            bid_row, ask_row = resample_frame_to_grid(frame, prices, step)
            bid_flat.extend(bid_row)
            ask_flat.extend(ask_row)

        num_frames = len(t_arr)
        print(f"[EMBEDDED_API] ob_heatmap_30s_history: frames={num_frames} prices={n_prices}")

        return JSONResponse(content={
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

    @app.get("/v2/orderbook_heatmap_30s_stats")
    async def orderbook_heatmap_30s_stats():
        """Get orderbook heatmap buffer statistics."""
        if not state.ob_buffer:
            raise HTTPException(
                status_code=503,
                detail="Orderbook heatmap not available (no shared buffer)"
            )

        stats = state.ob_buffer.get_stats()
        stats["current_time"] = time.time()
        stats["mode"] = "shared_buffer"

        if stats["last_ts"] > 0:
            stats["last_frame_age_s"] = round(time.time() - stats["last_ts"], 1)
        else:
            stats["last_frame_age_s"] = None

        return JSONResponse(content=stats)

    @app.get("/v2/orderbook_heatmap_30s_debug")
    async def orderbook_heatmap_30s_debug():
        """Debug endpoint for orderbook heatmap system."""
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

        return JSONResponse(content=debug_info)

    return app


def start_api_thread(
    app: 'FastAPI',
    host: str = "127.0.0.1",
    port: int = 8899,
    log_level: str = "warning"
) -> threading.Thread:
    """
    Start the API server in a background daemon thread.

    Args:
        app: FastAPI application instance
        host: Host to bind to
        port: Port to bind to
        log_level: Uvicorn log level

    Returns:
        Thread running the API server
    """
    if not HAS_FASTAPI:
        raise ImportError("FastAPI not installed")

    def run_server():
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level=log_level,
            access_log=False  # Reduce noise
        )
        server = uvicorn.Server(config)
        server.run()

    thread = threading.Thread(target=run_server, daemon=True, name="EmbeddedAPI")
    thread.start()

    print(f"[EMBEDDED_API] Server started on http://{host}:{port}")
    print(f"[EMBEDDED_API] Endpoints:")
    print(f"  GET http://{host}:{port}/v1/health")
    print(f"  GET http://{host}:{port}/v1/liq_heatmap")
    print(f"  GET http://{host}:{port}/v2/liq_heatmap")
    print(f"  GET http://{host}:{port}/v2/orderbook_heatmap_30s")
    print(f"  GET http://{host}:{port}/v2/orderbook_heatmap_30s_history")
    print(f"  GET http://{host}:{port}/v2/orderbook_heatmap_30s_stats")

    return thread
