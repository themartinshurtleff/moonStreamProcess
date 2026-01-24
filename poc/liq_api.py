#!/usr/bin/env python3
"""
Liquidation Heatmap API Server

Lightweight HTTP API serving liquidation zone data for terminal integration.
Reads from a snapshot file written by full_metrics_viewer.py.

Usage:
    python -m poc.liq_api --host 127.0.0.1 --port 8899
"""

import argparse
import json
import os
import sys
import time
import threading
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict

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
API_VERSION = "1.0.0"

# Cache refresh interval
CACHE_REFRESH_INTERVAL = 1.0  # seconds


@dataclass
class ZoneInfo:
    """A liquidation zone with metadata."""
    price: float
    strength: float
    age_min: int


@dataclass
class HeatmapSnapshot:
    """Complete heatmap snapshot for API response."""
    schema_version: int
    symbol: str
    ts: float
    src: float
    step: float
    price_min: float
    price_max: float
    prices: List[float]
    long_intensity: List[float]
    short_intensity: List[float]
    top_long_zones: List[Dict[str, Any]]
    top_short_zones: List[Dict[str, Any]]
    norm: Dict[str, Any]


class SnapshotCache:
    """Thread-safe cache for heatmap snapshots."""

    def __init__(self, snapshot_file: str, refresh_interval: float = 1.0):
        self.snapshot_file = snapshot_file
        self.refresh_interval = refresh_interval
        self._cache: Optional[Dict] = None
        self._cache_time: float = 0
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

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
        """Load snapshot from file."""
        if not os.path.exists(self.snapshot_file):
            return

        try:
            with open(self.snapshot_file, 'r') as f:
                data = json.load(f)

            with self._lock:
                self._cache = data
                self._cache_time = time.time()

        except Exception as e:
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
        description="API for liquidation zone visualization",
        version=API_VERSION
    )

    @app.on_event("startup")
    async def startup():
        global _cache, _start_time
        _start_time = time.time()
        _cache = SnapshotCache(SNAPSHOT_FILE, CACHE_REFRESH_INTERVAL)
        _cache.start()
        print(f"API started, reading from: {SNAPSHOT_FILE}")

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

        return {
            "ok": snapshot_exists and cache_age < 120,
            "version": API_VERSION,
            "uptime_s": round(time.time() - _start_time, 1),
            "snapshot_file": SNAPSHOT_FILE,
            "snapshot_exists": snapshot_exists,
            "cache_age_s": round(cache_age, 1) if cache_age < float('inf') else None
        }

    @app.get("/v1/liq_heatmap")
    async def liq_heatmap(
        symbol: str = Query(default="BTC", description="Symbol to query"),
        window_minutes: int = Query(default=60, description="Window for normalization (unused, for future)")
    ):
        """
        Get liquidation heatmap snapshot.

        Returns intensity values for long/short liquidation zones across a price range.
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
        if cache_age > 120:  # More than 2 minutes old
            snapshot["_warning"] = f"Data is {cache_age:.0f}s old, viewer may be stopped"

        return JSONResponse(content=snapshot)

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
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload for development")

    args = parser.parse_args()

    print("=" * 60)
    print("Liquidation Heatmap API Server")
    print("=" * 60)
    print(f"Host: {args.host}")
    print(f"Port: {args.port}")
    print(f"Snapshot file: {SNAPSHOT_FILE}")
    print()
    print("Endpoints:")
    print(f"  GET http://{args.host}:{args.port}/v1/health")
    print(f"  GET http://{args.host}:{args.port}/v1/liq_heatmap?symbol=BTC")
    print()
    print("Make sure full_metrics_viewer.py is running to generate snapshots.")
    print("=" * 60)

    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
