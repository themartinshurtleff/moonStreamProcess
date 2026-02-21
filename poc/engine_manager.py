"""
Engine Manager — per-symbol engine orchestration for multi-symbol support.

Bundles all engines for a single symbol into an EngineInstance dataclass and
provides EngineManager for routing events to the correct symbol's engines.

Design rules (from CLAUDE.md):
  - Each symbol MUST have independent engines. NEVER share state between
    per-symbol engines — each symbol gets its own calibrator, weights,
    zone manager, and frame buffer.
  - on_force_order routes BOTH to calibrator.on_liquidation() AND
    heatmap_v2.on_force_order().  The calibrator requires BOTH
    on_liquidation() AND on_minute_snapshot() calls to function —
    missing either one makes it silently drop all events.
  - Symbol convention: short symbols ("BTC", "ETH", "SOL") for internal
    routing; full symbols ("BTCUSDT", "ETHUSDT", "SOLUSDT") for exchange
    API calls.

Current engine constructor parameters (from full_metrics_viewer.py):
  ┌─────────────────────────────────────────────────────────────────────┐
  │ LiquidationCalibrator (line 595):                                  │
  │   symbol="BTC", steps=20.0, window_minutes=15,                    │
  │   hit_bucket_tolerance=5, learning_rate=0.10,                     │
  │   closer_level_gamma=0.35, enable_buffer_tuning=True,             │
  │   enable_tolerance_tuning=True,                                   │
  │   log_file="poc/liq_calibrator.jsonl",                            │
  │   weights_file="poc/liq_calibrator_weights.json",                 │
  │   on_weights_updated=callback                                     │
  │                                                                   │
  │ LiquidationHeatmap (line 644):                                    │
  │   config=HeatmapConfig(symbol="BTC", steps=20.0, decay=0.995,    │
  │     buffer=0.002, tape_weight=0.35, projection_weight=0.65),      │
  │   log_dir="poc/"                                                  │
  │   Internally creates: LiquidationTape, EntryInference,            │
  │     ActiveZoneManager                                             │
  │                                                                   │
  │ OrderbookReconstructor (line 677):                                 │
  │   symbol="BTCUSDT", on_book_update=callback, gap_tolerance=1000   │
  │                                                                   │
  │ OrderbookAccumulator (line 663):                                   │
  │   step=20.0, range_pct=0.10, on_frame_callback=callback           │
  └─────────────────────────────────────────────────────────────────────┘
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

from liq_calibrator import LiquidationCalibrator
from liq_heatmap import LiquidationHeatmap
from active_zone_manager import ActiveZoneManager
from ob_heatmap import OrderbookReconstructor, OrderbookAccumulator

logger = logging.getLogger(__name__)

# Full-symbol to short-symbol mapping.
# Used by get_engine_by_full() to resolve exchange symbols like "BTCUSDT"
# to internal short symbols like "BTC".
FULL_TO_SHORT: Dict[str, str] = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
    "SOLUSDT": "SOL",
}


@dataclass
class EngineInstance:
    """
    All engines for ONE symbol, bundled together.

    Each symbol MUST have its own independent EngineInstance. NEVER share
    any of these objects between symbols — the calibrator, heatmap, zone
    manager, and orderbook components all maintain internal state that is
    specific to a single symbol's price action.

    Required engines:
        calibrator   — LiquidationCalibrator for weight learning
        heatmap_v2   — LiquidationHeatmap (V2) combining tape + inference

    Optional engines (may not be available for all symbols initially):
        zone_manager      — ActiveZoneManager for V3 persistent zones
                            (created internally by LiquidationHeatmap, but
                            exposed here for direct access by the API)
        ob_reconstructor  — OrderbookReconstructor (needs depth WS stream)
        ob_accumulator    — OrderbookAccumulator (needs reconstructor)
    """

    symbol_short: str                                    # e.g. "BTC"
    symbol_full: str                                     # e.g. "BTCUSDT"
    calibrator: LiquidationCalibrator
    heatmap_v2: LiquidationHeatmap
    zone_manager: Optional[ActiveZoneManager] = None
    ob_reconstructor: Optional[OrderbookReconstructor] = None
    ob_accumulator: Optional[OrderbookAccumulator] = None


class EngineManager:
    """
    Routes events to the correct per-symbol engine instance.

    Central registry for all symbol engines. Enforces the rule that each
    symbol has fully independent state — no cross-symbol sharing of
    calibrators, heatmaps, zone managers, or orderbook components.

    Usage:
        manager = EngineManager()
        manager.register_engine("BTC", "BTCUSDT", btc_engine)
        manager.register_engine("ETH", "ETHUSDT", eth_engine)

        # Route a forceOrder event to BTC's calibrator AND heatmap
        manager.on_force_order("BTC", event_data={...},
                               timestamp=t, side="long", price=p,
                               qty=q, notional=n)

        # Route a minute snapshot to BTC's calibrator
        manager.on_minute_snapshot("BTC", symbol="BTC", timestamp=t,
                                   src=price, ...)
    """

    def __init__(self) -> None:
        self.engines: Dict[str, EngineInstance] = {}

    def register_engine(
        self,
        symbol_short: str,
        symbol_full: str,
        engine_instance: EngineInstance,
    ) -> None:
        """
        Register an engine instance for a symbol.

        Args:
            symbol_short: Internal short symbol, e.g. "BTC"
            symbol_full:  Exchange full symbol, e.g. "BTCUSDT"
            engine_instance: Fully constructed EngineInstance
        """
        if symbol_short in self.engines:
            logger.warning(
                "Overwriting existing engine for %s", symbol_short
            )
        engine_instance.symbol_short = symbol_short
        engine_instance.symbol_full = symbol_full
        self.engines[symbol_short] = engine_instance
        logger.info(
            "Registered engine for %s (%s)", symbol_short, symbol_full
        )

    def get_engine(self, symbol_short: str) -> Optional[EngineInstance]:
        """Retrieve engine by short symbol (e.g. "BTC")."""
        return self.engines.get(symbol_short)

    def get_engine_by_full(
        self, symbol_full: str
    ) -> Optional[EngineInstance]:
        """
        Retrieve engine by full exchange symbol (e.g. "BTCUSDT").

        Uses FULL_TO_SHORT mapping to resolve the short symbol, then
        looks up the engine. Returns None if the symbol is not mapped
        or no engine is registered.
        """
        short = FULL_TO_SHORT.get(symbol_full)
        if short is None:
            logger.warning(
                "No short-symbol mapping for %s", symbol_full
            )
            return None
        return self.engines.get(short)

    def on_force_order(
        self,
        symbol_short: str,
        event_data: dict,
        timestamp: float,
        side: str,
        price: float,
        qty: float,
        notional: float,
    ) -> None:
        """
        Route a forceOrder event to the correct symbol's engines.

        CRITICAL: This routes to BOTH:
          1. calibrator.on_liquidation(event_data)  — for weight learning
          2. heatmap_v2.on_force_order(...)          — for tape ground truth

        Both calls are required for the calibrator to function. The
        calibrator also needs on_minute_snapshot() calls every 60 seconds
        (via on_minute_snapshot below), otherwise on_liquidation() silently
        drops every event because self.snapshots stays empty.

        Args:
            symbol_short: Internal short symbol, e.g. "BTC"
            event_data:   Dict for calibrator with keys: timestamp, symbol,
                          side, price, qty, and optional mark_price/mid_price
            timestamp:    Unix timestamp for heatmap
            side:         "long" or "short"
            price:        Liquidation price
            qty:          Size in base asset
            notional:     USD notional value
        """
        engine = self.engines.get(symbol_short)
        if engine is None:
            logger.warning(
                "on_force_order: no engine for symbol %s", symbol_short
            )
            return

        # 1. Calibrator — weight learning feedback
        engine.calibrator.on_liquidation(event_data)

        # 2. Heatmap V2 — tape ground truth
        engine.heatmap_v2.on_force_order(
            timestamp=timestamp,
            side=side,
            price=price,
            qty=qty,
            notional=notional,
        )

    def on_minute_snapshot(
        self, symbol_short: str, **snapshot_kwargs: Any
    ) -> None:
        """
        Route a minute snapshot to the correct symbol's calibrator.

        CRITICAL: This MUST be called every 60 seconds for each symbol.
        Without it, calibrator.on_liquidation() silently drops every
        event because self.snapshots stays empty (see CLAUDE.md
        "Calibrator Integration Rules").

        Args:
            symbol_short:     Internal short symbol, e.g. "BTC"
            **snapshot_kwargs: All keyword args forwarded to
                              calibrator.on_minute_snapshot(). Expected
                              keys: symbol, timestamp, src, pred_longs,
                              pred_shorts, ladder, weights, buffer,
                              steps, high, low, close, perp_buy_vol,
                              perp_sell_vol, perp_oi_change, depth_band
        """
        engine = self.engines.get(symbol_short)
        if engine is None:
            logger.warning(
                "on_minute_snapshot: no engine for symbol %s", symbol_short
            )
            return

        engine.calibrator.on_minute_snapshot(**snapshot_kwargs)

    def get_all_symbols(self) -> List[str]:
        """Return list of all registered short symbols."""
        return list(self.engines.keys())

    def get_snapshot(self, symbol_short: str) -> Optional[Dict]:
        """
        Get the V2 heatmap snapshot for a symbol.

        Returns the result of heatmap_v2.get_heatmap(), or None if no
        engine is registered for the symbol.
        """
        engine = self.engines.get(symbol_short)
        if engine is None:
            return None
        return engine.heatmap_v2.get_heatmap()

    def get_zones(
        self,
        symbol_short: str,
        side: str = None,
        min_leverage: int = None,
        max_leverage: int = None,
        min_weight: float = None,
    ) -> Optional[List[Dict]]:
        """
        Get active V3 zones for a symbol.

        Returns zone data from the symbol's ActiveZoneManager, or None
        if no engine or no zone manager is available.

        Args:
            symbol_short:  Internal short symbol, e.g. "BTC"
            side:          Optional filter: "long" or "short"
            min_leverage:  Optional minimum leverage tier
            max_leverage:  Optional maximum leverage tier
            min_weight:    Optional minimum zone weight
        """
        engine = self.engines.get(symbol_short)
        if engine is None:
            return None
        if engine.zone_manager is None:
            return None
        return engine.zone_manager.get_active_zones(
            side=side,
            min_leverage=min_leverage,
            max_leverage=max_leverage,
            min_weight=min_weight,
        )
