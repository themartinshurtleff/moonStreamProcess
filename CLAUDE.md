# CLAUDE.md — moonStreamProcess

Single source of truth for how this codebase works. Read this before making any changes.

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Engine Instantiation Parameters](#engine-instantiation-parameters)
3. [Calibrator Constants](#calibrator-constants--tuning-parameters)
4. [Leverage Configuration](#leverage-configuration)
5. [Zone Manager Configuration](#zone-manager-configuration)
6. [Data Flow — Critical Paths](#data-flow--critical-paths)
7. [Calibrator Integration Rules — CRITICAL](#calibrator-integration-rules--critical)
8. [API Endpoint Inventory](#api-endpoint-inventory)
9. [Response Caching](#response-caching)
10. [Snapshot Pipeline](#snapshot-pipeline)
11. [NEVER DO — Code Rules](#never-do--code-rules)
12. [Known Bugs and Past Issues](#known-bugs-and-past-issues)
13. [Symbol Conventions](#symbol-conventions)
14. [File and State Inventory](#file-and-state-inventory)
15. [Multi-Exchange Implementation Notes](#multi-exchange-implementation-notes)
16. [Testing Checklist](#testing-checklist-before-deployment)
17. [Verification Steps](#verification-steps-after-code-changes)

---

## Architecture Overview

All core code lives in `poc/`. The system collects market data from **Binance, Bybit, and OKX** via WebSocket (and Binance REST), computes liquidation heatmaps and orderbook snapshots, and serves them via a FastAPI HTTP API. Liquidation events from all three exchanges are normalized via `liq_normalizer.py` before reaching the per-symbol engines.

| File | Role |
|------|------|
| `full_metrics_viewer.py` | **Main entry point.** Launches WebSocket/REST connections, instantiates all sub-engines, runs Rich terminal dashboard at 4 FPS, writes snapshot files, starts embedded API server. |
| `ws_connectors.py` | `BinanceConnector`, `BybitConnector`, `OKXConnector`, and `MultiExchangeConnector` — WebSocket streams for depth, aggTrade, forceOrder/liquidations, markPrice. All three exchanges active. |
| `liq_normalizer.py` | **Liquidation normalizer.** Converts raw exchange-specific liquidation messages into `CommonLiqEvent` before reaching `EngineManager`. Handles Binance/Bybit order-side → position-side inversion (both use same convention), OKX contract-to-base conversion. |
| `oi_poller.py` | `MultiExchangeOIPoller` + `OIPollerThread` — polls OI from Binance, Bybit, OKX every 15s per symbol (BTC, ETH, SOL). Aggregates across exchanges, delivers per-symbol updates via callback. All OI in base asset. |
| `rest_pollers.py` | `BinanceRESTPollerThread` — polls trader ratios (TTA, TTP, GTA) from Binance REST. OI polling moved to `oi_poller.py`. |
| `liq_engine.py` | **ORPHANED (Phase 1).** V1 `LiquidationStressEngine` — volume-based liquidation zone prediction. Superseded by V2 heatmap. Still importable but no longer instantiated by the viewer. |
| `liq_heatmap.py` | V2 `LiquidationHeatmap` — unified engine combining `LiquidationTape` + `EntryInference` for forward-looking liquidation pools. |
| `liq_tape.py` | Ground-truth liquidation accumulator from forceOrder events. Tracks actual liquidation volumes by price bucket. |
| `entry_inference.py` | Infers position entries from OI changes + taker aggression, projects liquidation prices across leverage tiers. |
| `liq_calibrator.py` | Self-calibrating leverage weight system. Uses forceOrder events as feedback with soft attribution in percent-space. ~2800 lines. |
| `leverage_config.py` | Per-symbol leverage ladder definitions and tier weight constants (5x through 250x). |
| `active_zone_manager.py` | V3 zone lifecycle manager. Persistent zones: CREATED → REINFORCED → SWEPT/EXPIRED. Disk persistence via JSON. |
| `ob_heatmap.py` | Orderbook heatmap with `OrderbookReconstructor` (snapshot+diff) and `OrderbookAccumulator` (30-second frames). Binary ring buffer persistence. |
| `liq_api.py` | **ORPHANED (Phase 1).** Standalone FastAPI HTTP server (v3.0.0). Reads from snapshot files on disk. Superseded by `embedded_api.py` which now has all endpoints including V3 zones. |
| `engine_manager.py` | `EngineManager` + `EngineInstance` — per-symbol container holding calibrator, heatmap_v2, zone_manager, ob_reconstructor, ob_accumulator. Single owner of all engine objects after init. |
| `embedded_api.py` | **Embedded** FastAPI server (v3.0.0). Runs as daemon thread inside viewer. Shares `ob_buffer` by direct Python reference. Receives `EngineManager` for V3 zone endpoints. Includes `ResponseCache` with eviction for concurrent-user scaling. Cache refresh loops log errors instead of swallowing exceptions. All endpoints (V1–V3) available. **This is now the only API server needed.** |
| `liq_plotter.py` | Matplotlib live chart tailing `plot_feed.jsonl`. |
| `metrics_viewer.py` | Earlier/simpler viewer (predecessor to `full_metrics_viewer.py`). |
| `list_metrics.py` | Reference script listing all ~40 BTC perpetual metrics from Binance. |
| `audit_v2_comprehensive.py` | Code+log verified audit script producing markdown report. |
| `audit_v2_engine.py` | Runtime audit computing hit-rate metrics from log files. |

### WebSocket Streams (via `MultiExchangeConnector`)

All three exchanges are started by `MultiExchangeConnector.connect_all()` in `run_websocket_thread()`. Each sub-connector runs as an independent asyncio task.

#### Binance (`BinanceConnector`)
Base URL: `wss://fstream.binance.com/ws`

| Stream | Data | Used By |
|--------|------|---------|
| `btcusdt@depth@100ms` | Orderbook diffs (100ms throttle) | `OrderbookReconstructor` → `OrderbookAccumulator` → 30s OB heatmap frames |
| `btcusdt@aggTrade` | Aggregated trades (buyer_is_maker) | `TakerAggressionAccumulator`, price tracking, volume accumulation |
| `!forceOrder@arr` | Liquidation events (ALL symbols) | `liq_normalizer` → `EngineManager` → calibrator + heatmap |
| `{sym}usdt@markPrice@1s` (×3) | Mark price, funding rate | Per-symbol mark price for calibrator OHLC + event-time src, funding display. BTC, ETH, SOL. |

#### Bybit (`BybitConnector`)
Base URL: `wss://stream.bybit.com/v5/public/linear`

| Stream | Data | Used By |
|--------|------|---------|
| `orderbook.50.BTCUSDT` | Orderbook snapshots | BTC depth (future use) |
| `publicTrade.BTCUSDT` | Trades | BTC trade tracking (future use) |
| `tickers.BTCUSDT` | OI + funding | OI/funding display (future use) |
| `allLiquidation.{SYMBOL}` (×3) | Liquidations (500ms batches) | `liq_normalizer` → `EngineManager` → calibrator + heatmap |

Subscribes to `allLiquidation` for BTCUSDT, ETHUSDT, SOLUSDT.

#### OKX (`OKXConnector`)
Base URL: `wss://ws.okx.com:8443/ws/v5/public`

| Stream | Data | Used By |
|--------|------|---------|
| `books5` (BTC-USDT-SWAP) | Top-5 orderbook | BTC depth (future use) |
| `trades` (BTC-USDT-SWAP) | Trades | BTC trade tracking (future use) |
| `funding-rate` (BTC-USDT-SWAP) | Funding rate | Funding display (future use) |
| `open-interest` (BTC-USDT-SWAP) | Open interest | OI display (future use) |
| `liquidation-orders` (instType=SWAP) | Liquidations (ALL SWAP symbols) | `liq_normalizer` → `EngineManager` → calibrator + heatmap |

**Note:** Hyperliquid has no public all-liquidations WebSocket stream and is excluded from Phase 2.

### Liquidation Side Conventions (CRITICAL)

Each exchange uses different side semantics. The normalizer (`liq_normalizer.py`) converts all of them to `"long"` or `"short"` (the **position** that was liquidated):

| Exchange | Raw Field | Value | Meaning | Normalized `side` |
|----------|-----------|-------|---------|-------------------|
| Binance | `o.S` | `"BUY"` | Shorts bought back | `"short"` |
| Binance | `o.S` | `"SELL"` | Longs sold | `"long"` |
| Bybit | `S` | `"Buy"` | Shorts bought back | `"short"` |
| Bybit | `S` | `"Sell"` | Longs sold | `"long"` |
| OKX | `posSide` | `"long"` | Direct | `"long"` |
| OKX | `posSide` | `"short"` | Direct | `"short"` |

**OKX quantities are in contracts**, not base asset. `liq_normalizer.py` converts using `OKX_CONTRACT_SIZES`: BTC=0.01, ETH=0.1, SOL=1.0 per contract.

### REST Polling

#### Open Interest (via `MultiExchangeOIPoller` in `oi_poller.py`)

Polls every 15 seconds for BTC, ETH, SOL. 3 symbols × 3 exchanges = 9 requests per cycle (36 req/min).

| Exchange | Endpoint | Response Field | Unit |
|----------|----------|---------------|------|
| Binance | `GET /fapi/v1/openInterest?symbol={SYM}USDT` | `openInterest` | Base asset |
| Bybit | `GET /v5/market/open-interest?category=linear&symbol={SYM}USDT&intervalTime=5min&limit=1` | `result.list[0].openInterest` | Base asset |
| OKX | `GET /api/v5/public/open-interest?instId={SYM}-USDT-SWAP` | `data[0].oiCcy` | Base asset |

**CRITICAL:** OKX has both `oi` (contracts) and `oiCcy` (base currency). Always use `oiCcy`.

Per-exchange values are summed to produce `aggregated_oi`. Individual exchange failures are logged and skipped — never crash the loop. Results delivered via `on_oi_update(symbol_short, aggregated_oi, per_exchange_dict)` callback.

#### Trader Ratios (via `BinanceRESTPollerThread` in `rest_pollers.py`)

| Endpoint | Interval | Data |
|----------|----------|------|
| `/futures/data/topLongShortAccountRatio` | 60s | Top trader account long/short ratio |
| `/futures/data/topLongShortPositionRatio` | 60s | Top trader position long/short ratio |
| `/futures/data/globalLongShortAccountRatio` | 60s | Global account long/short ratio |

## Engine Instantiation Parameters

Engine instances are created by `_create_engine_for_symbol()` for each entry in `SYMBOL_CONFIGS`:

### SYMBOL_CONFIGS (module-level dict)
```python
"BTC": steps=20.0, ob_step=20.0, ob_range_pct=0.10, ob_gap_tolerance=1000, has_orderbook=True
"ETH": steps=1.0,  ob_step=1.0,  ob_range_pct=0.10, ob_gap_tolerance=50,   has_orderbook=False
"SOL": steps=0.10, ob_step=0.10, ob_range_pct=0.10, ob_gap_tolerance=5,    has_orderbook=False
```

### LiquidationCalibrator (per-symbol)
```python
symbol={SYM}, steps={from SYMBOL_CONFIGS}, window_minutes=15, hit_bucket_tolerance=5,
learning_rate=0.10, closer_level_gamma=0.35,
enable_buffer_tuning=True, enable_tolerance_tuning=True,
log_file="poc/liq_calibrator_{SYM}.jsonl",
weights_file="poc/liq_calibrator_weights_{SYM}.json",
on_weights_updated=lambda → self._on_calibrator_weights_updated({SYM}, w, b)
```
Note: `window_minutes=15` in the viewer override (calibrator default is 30). Each symbol's callback routes to that symbol's heatmap.

### LiquidationHeatmap V2 (per-symbol)
```python
HeatmapConfig(symbol={SYM}, steps={from SYMBOL_CONFIGS}, decay=0.995, buffer=0.002,
              tape_weight=0.35, projection_weight=0.65)
# Also: cluster_radius_pct=0.005, min_notional_usd=10000.0, max_pools_per_side=20
```

### MultiExchangeOIPoller (via `OIPollerThread`)
```python
symbols=["BTC", "ETH", "SOL"], poll_interval=15.0
# Polls Binance + Bybit + OKX concurrently per symbol
```

### BinanceRESTPollerThread (ratios only)
```python
symbols=["BTCUSDT"], oi_interval=999999, ratio_interval=60.0
# OI polling disabled here — handled by MultiExchangeOIPoller
```

### OrderbookReconstructor — line 691
```python
symbol="BTCUSDT", gap_tolerance=1000
```
Uses `pu` (previous update ID) for Futures reconciliation. Gap tolerance of 1000 allows slightly stale books for heatmap visualization rather than constant resyncs.

### OrderbookAccumulator — line 677
```python
step=20.0, range_pct=0.10  # ±10% around mid price, $20 buckets
```
Emits frames every 30 seconds. Frames are 4KB binary records.

### ZoneTracker (UI stability) — line 643
```python
steps=20.0, max_zones=5, enter_margin=0.07, exit_margin=0.05,
ttl_minutes=10, peak_drop_threshold=0.70, min_strength_threshold=0.01
```

### TakerAggressionAccumulator — line 672
```python
history_minutes=10
```

---

## Calibrator Constants & Tuning Parameters

All defined at module level in `liq_calibrator.py`:

### Soft Attribution (percent-space)
| Constant | Value | Purpose |
|----------|-------|---------|
| `TAU_PCT_BASE` | 0.0012 (12 bps) | Responsibility temperature — how broadly events attribute across tiers |
| `DELTA_PCT_BASE` | 0.0008 (8 bps) | Hit contribution decay scale |
| `HIT_BUCKET_TOLERANCE` | 4 | Max bucket distance for a "hit" (overrides loaded value) |

### Volatility Scaling
| Constant | Value | Purpose |
|----------|-------|---------|
| `VOL_BASELINE` | 0.0015 (15 bps) | Neutral vol for scaling. `scale = vol_ewma / VOL_BASELINE` |
| `VOL_SCALE_MIN` | 0.7 | Floor on vol scaling factor |
| `VOL_SCALE_MAX` | 1.6 | Cap on vol scaling factor |
| `VOL_EWMA_ALPHA` | 0.1 | EWMA smoothing (10% weight to new observation) |

### Weight Update Stabilization
| Constant | Value | Purpose |
|----------|-------|---------|
| `ANCHOR_LAMBDA` | 0.10 | Weight anchoring: `w_new = 0.9*w_old + 0.1*w_update` |
| Per-update ratio clamp | [0.85, 1.20] | `new_w/old_w` must stay in this range |
| Max weight cap | 0.35 | No single tier can exceed 35% weight |

### Bias Correction (Phase 3, percent-based)
| Constant | Value | Purpose |
|----------|-------|---------|
| `BIAS_ETA` | 0.10 | EMA learning rate for bias updates |
| `BIAS_CAP` | 0.008 (0.8%) | Max absolute bias in either direction |
| `MIN_BIAS_SAMPLES` | 10 | Minimum events before updating bias |

### Offset Limits (legacy USD-based, deprecated but active)
| Constant | Value | Purpose |
|----------|-------|---------|
| `MAX_OFFSET_PCT` | 0.005 (0.5%) | Max offset as fraction of price |
| `MAX_OFFSET_USD` | $2,000 | Absolute max offset safety net |

### Approach & Zones
| Constant | Value | Purpose |
|----------|-------|---------|
| `APPROACH_PCT` | 0.0075 (0.75%) | Zone is "approachable" if price came within this distance |
| `MINUTE_CACHE_SIZE` | 60 | Rolling cache of minute data |

### Log Rotation
| Constant | Value | Purpose |
|----------|-------|---------|
| `LOG_ROTATION_MAX_MB` | 200 | Rotate log if file exceeds 200 MB |
| `LOG_ROTATION_MAX_AGE_HOURS` | 24 | Rotate log if older than 24 hours |

---

## Leverage Configuration

Defined in `leverage_config.py`.

### BTC Leverage Ladder
```
Tiers:   [5, 10, 20, 25, 50, 75, 100, 125, 150, 200, 250]
Weights: [1.0, 1.0, 0.9, 0.7, 0.35, 0.14, 0.08, 0.05, 0.03, 0.015, 0.008]
```

### DISABLED TIERS
**10x is disabled** (added 2026-02-17) due to persistent -$62K median miss corruption. Disabled tiers are excluded from: offset learning, bias correction, zone production, hit rate calculation, sweep matching. Events from disabled tiers are still logged but tagged `tier_disabled: true`.

```python
DISABLED_TIERS: Set[int] = {10}
```

### V3 Tier Weights (zone intensity)
```python
{5: 0.2, 10: 0.0, 20: 0.4, 25: 0.5, 50: 0.8, 75: 0.9,
 100: 1.0, 125: 1.0, 150: 0.9, 200: 0.8, 250: 0.7}
```

### Other Symbol Ladders
- **ETH:** `[5, 10, 20, 25, 50, 75, 100, 125]` (max 125x)
- **SOL:** `[5, 10, 20, 25, 50, 75]` (max 75x)
- **Default:** `[5, 10, 20, 50, 75, 100]`

`LeverageConfigRegistry` provides per-symbol lookup with defaults. Prepared for per-exchange overrides (not yet implemented).

---

## Zone Manager Configuration

Defined in `active_zone_manager.py`. Governs the V3 persistent zone lifecycle.

| Constant | Value | Purpose |
|----------|-------|---------|
| `MAX_ZONE_AGE_MINUTES` | 240 (4 hours) | Zones expire after this without reinforcement |
| `ZONE_DECAY_PER_MINUTE` | 0.995 | Weight multiplier per minute (~2.3 hour half-life) |
| `MIN_ZONE_WEIGHT` | 0.05 | Remove zones below this weight |
| `MAX_ZONE_WEIGHT` | 5.0 | Soft cap to prevent runaway brightness |
| `REINFORCEMENT_FACTOR` | 0.3 | `weight += new_weight * 0.3` on reinforcement |
| `CLUSTER_DISTANCE_PCT` | 0.0005 (0.05%) | Merge zones within this price distance |
| `EXPIRATION_PRICE_DIST_PCT` | 0.02 (2%) | Only expire if price has moved 2% away |
| `PERSIST_INTERVAL_SECONDS` | 60 | Write zones to disk every minute |
| `DEFAULT_PERSIST_FILE` | `liq_active_zones.json` | Zone state persistence file |

Zone lifecycle: **CREATED** → **REINFORCED** (repeatable) → **SWEPT** (by price) or **EXPIRED** (by time/decay)

---

## Data Flow — Critical Paths

### Path A: Liquidation Event (any exchange) → Normalizer → EngineManager → Calibrator

```
Exchange WS (Binance !forceOrder@arr | Bybit allLiquidation.* | OKX liquidation-orders)
  → sub-connector _wrap_message(data, exchange, instrument, "perpetual", "liquidations")
    → MultiExchangeConnector callback → process_message()
      → obj_type == "liquidations" → _process_liquidations(exchange, data)
        → normalize_liquidation(exchange, data)            (liq_normalizer.py)
          → returns List[CommonLiqEvent]                   (exchange-agnostic)
        → for each CommonLiqEvent:
          → engine_manager.get_engine(event.symbol_short)  (skip if no engine)
          → engine_manager.on_force_order(symbol, ...)
            → calibrator.on_liquidation()                  (calibration feedback)
            → heatmap_v2.on_force_order()                  (V2 ground truth)
              → _process_event() → soft attribution → stats.total_events += 1
```

### Path B: OI Data → Engine

```
MultiExchangeOIPoller (oi_poller.py) polls every 15s:
  → Binance + Bybit + OKX fetched concurrently per symbol (BTC, ETH, SOL)
  → aggregated_oi = sum of non-None exchange values
  → on_oi_update(symbol_short, aggregated_oi, per_exchange_dict)
    → full_metrics_viewer._on_oi_update()
      → self.oi_by_symbol[sym] updated (for /oi API endpoint)
      → BTC: self.state.perp_total_oi + perp_oi_change updated (backwards compat)
      → EntryInference.on_oi_update() (via heatmap_v2, on next minute rollover)
        → detects OI delta → infers new position entries
        → projects liquidation prices per leverage tier
```

### Path C: Minute Rollover → Snapshot → API → Frontend

```
full_metrics_viewer detects new minute boundary
  → computes OHLC4 from accumulated ticks
  → calls self._btc().calibrator.on_minute_snapshot(...)  (via EngineManager)
  → calls self._btc().heatmap_v2.compute_snapshot()       (via EngineManager)
  → _write_api_snapshot(snapshot)       → poc/liq_api_snapshot.json
  → _write_api_snapshot_v2(snapshot_v2) → poc/liq_api_snapshot_v2.json
  → Embedded API cache refresh thread reads JSON every 5 seconds
    → adds frame to LiquidationHeatmapBuffer (deduplicates by minute)
    → ResponseCache serves cached responses (TTL-based per endpoint)
    → serves via /liq_heatmap, /liq_heatmap_v2, /liq_zones endpoints
```

### Path D: Calibrator Learning Loop

```
on_liquidation() → appends event to events_window
on_minute_snapshot() → stores MinuteSnapshot in self.snapshots
  → processes any pending approach events against new snapshot
  → increments minutes_since_calibration
  → IF minutes_since_calibration >= window_minutes (default 30):
    → _run_calibration()
      → computes per-tier hit rates from soft attribution stats
      → multiplicative weight update with stabilization clamps:
        - Per-update ratio clamp: new_w/old_w in [0.85, 1.20]
        - Max weight cap: 0.35 per leverage tier
        - Normalized after clamping
      → adaptive percent-based bias correction
      → _save_weights() → atomic write (tmp + flush + fsync + rename)
        → old_logs/old_log/liq_calibrator_weights.json
      → on_weights_updated callback → engine re-reads weights
      → resets stats for next window
```

## Calibrator Integration Rules — CRITICAL

### Calibrator MUST receive TWO calls to function:

1. **`calibrator.on_liquidation()`** — called for every forceOrder event
2. **`calibrator.on_minute_snapshot()`** — called every 60 seconds with OHLC4, predictions, ladder, weights, buffer, volume, OI data

If `on_minute_snapshot()` is never called, `self.snapshots` stays empty. If `self.snapshots` is empty, `_process_event()` returns at line 953 without counting the event. This means `on_liquidation()` will silently drop EVERY event.

**NEVER wire `on_liquidation()` without also wiring `on_minute_snapshot()`. Both calls are required. Missing either one makes the calibrator appear to work while doing nothing.**

### Early return conditions in on_liquidation() (line 672):

1. **Line 720:** `if event.price <= 0 or event.symbol != self.symbol: return` — price/symbol filter
2. **Line 732:** `except Exception as e:` — exception handler swallows errors
3. **Line 953 (inside _process_event):** `if not snapshot: return` — no snapshot available because `on_minute_snapshot()` was never called

### How to verify the calibrator is working:

- Check `calibrator.stats.total_events` — must increment after forceOrder events arrive
- Check `len(calibrator.snapshots)` — must be > 0 (proves `on_minute_snapshot()` is being called)
- Check `calibrator.minutes_since_calibration` — must increment each minute

## API Endpoint Inventory

### Embedded Server (embedded_api.py, v3.0.0, port 8899)

**This is the only API server needed.** Runs as a daemon thread inside `full_metrics_viewer.py`. Shares `ob_buffer` by direct Python reference. Receives `EngineManager` for V3 zone access. All responses go through `ResponseCache` (see Response Caching section).

Clean paths are primary. Old `/vN/` paths are backwards-compatible aliases via stacked FastAPI `@app.get()` decorators.

| Method | Primary Path | Alias | Params | Data Source | Cache TTL |
|--------|-------------|-------|--------|-------------|-----------|
| GET | `/health` | `/v1/health` | — | In-memory state | none |
| GET | `/oi` | — | `symbol` (BTC/ETH/SOL) | `OIPollerThread` → `OISnapshot` (in-memory) | 5s |
| GET | `/liq_heatmap` | `/v1/liq_heatmap` | `symbol` | `liq_api_snapshot.json` (5s refresh) | 5s |
| GET | `/liq_heatmap_history` | `/v1/liq_heatmap_history` | `symbol`, `minutes` (5-720), `stride` (1-30) | `LiquidationHeatmapBuffer` ← `liq_heatmap_v1.bin` | 30s |
| GET | `/liq_heatmap_v2` | `/v2/liq_heatmap` | `symbol`, `min_notional` | `liq_api_snapshot_v2.json` (5s refresh) | 5s |
| GET | `/liq_heatmap_v2_history` | `/v2/liq_heatmap_history` | `symbol`, `minutes` (5-720), `stride` (1-30) | `LiquidationHeatmapBuffer` ← `liq_heatmap_v2.bin` | 30s |
| GET | `/liq_stats` | `/v2/liq_stats` | `symbol` | `liq_api_snapshot_v2.json` | 5s |
| GET | `/liq_zones` | `/v3/liq_zones` | `symbol`, `side`, `min_leverage`, `max_leverage`, `min_weight` | `EngineManager` → `ActiveZoneManager` (in-memory) | 5s |
| GET | `/liq_zones_summary` | `/v3/liq_zones_summary` | `symbol` | `EngineManager` → `ActiveZoneManager` (in-memory) | 5s |
| GET | `/liq_zones_heatmap` | `/v3/liq_heatmap` | `symbol`, `min_notional`, `min_leverage`, `max_leverage`, `min_weight` | `EngineManager` → `ActiveZoneManager` (in-memory) | 5s |
| GET | `/orderbook_heatmap` | `/v2/orderbook_heatmap_30s` | `symbol`, `range_pct`, `step`, `price_min`, `price_max`, `format` | Shared `ob_buffer` (direct Python reference) | 5s (json only) |
| GET | `/orderbook_heatmap_history` | `/v2/orderbook_heatmap_30s_history` | `symbol`, `minutes`, `stride`, `range_pct`, `step`, `price_min`, `price_max`, `format` | Shared `ob_buffer` | 30s (json only) |
| GET | `/orderbook_heatmap_stats` | `/v2/orderbook_heatmap_30s_stats` | — | Buffer stats + `ob_recon_stats.json` | 10s |
| GET | `/orderbook_heatmap_debug` | `/v2/orderbook_heatmap_30s_debug` | — | Module checks + buffer stats | 10s |

**Note:** Binary format responses (`format=bin`) for orderbook endpoints bypass the `ResponseCache` and are always generated fresh.

### Standalone Server (liq_api.py — ORPHANED)

`liq_api.py` is no longer needed. All its endpoints (including V3 zones) are now served by `embedded_api.py`. The standalone server remains in the codebase for reference but should not be run alongside the viewer.

## Response Caching

Defined in `embedded_api.py` via the `ResponseCache` class. Thread-safe `Dict[str, Tuple[JSONResponse, float]]` protected by `threading.Lock`.

| Endpoint Group | TTL | Rationale |
|----------------|-----|-----------|
| Live data (`/oi`, `/liq_heatmap`, `/liq_heatmap_v2`, `/liq_stats`, `/liq_zones`, `/liq_zones_summary`, `/liq_zones_heatmap`, `/orderbook_heatmap`) | **5 seconds** | Snapshot data updates every 60s (liq), 30s (OB), or 15s (OI); 5s is the minimum allowed by CLAUDE.md rules |
| History (`/liq_heatmap_history`, `/liq_heatmap_v2_history`, `/orderbook_heatmap_history`) | **30 seconds** | Historical data changes slowly; 30s avoids redundant recomputation |
| Stats/debug (`/orderbook_heatmap_stats`, `/orderbook_heatmap_debug`) | **10 seconds** | Diagnostic data, moderate refresh |
| Health (`/health`) | **none** | Always computed fresh for monitoring accuracy |
| Binary responses (`format=bin`) | **none** | Binary `Response` objects bypass the cache (only `JSONResponse` is cached) |

Cache keys include all query parameters that affect the response (e.g., `liq_zones?symbol=BTC&side=long&min_leverage=20`). This prevents cross-contamination between different filter combinations.

`ResponseCache` evicts expired entries (>60s) every 100 cache sets to prevent unbounded memory growth. Error responses and binary format responses bypass the cache.

## Snapshot Pipeline

```
full_metrics_viewer.py (writer)
  │
  ├─ liq_api_snapshot.json      ← V1 heatmap (overwritten every minute)
  ├─ liq_api_snapshot_v2.json   ← V2 heatmap (overwritten every minute)
  ├─ ob_heatmap_30s.bin         ← OB frames (appended every 30s, 4KB records)
  ├─ ob_recon_stats.json        ← OB reconstructor diagnostics
  ├─ liq_heatmap_v1.bin         ← V1 history ring buffer (appended by embedded API)
  ├─ liq_heatmap_v2.bin         ← V2 history ring buffer (appended by embedded API)
  └─ plot_feed.jsonl            ← OHLC + zone data for matplotlib plotter

Embedded API Server (reader — daemon thread in viewer)
  │
  ├─ Shares ob_buffer by direct Python reference (no disk I/O for OB)
  ├─ Reads liq JSON snapshots every 5s (EmbeddedAPIState._cache_ttl)
  ├─ Receives EngineManager for V3 zone access (in-memory, no disk)
  └─ ResponseCache serves concurrent users from TTL-based cache

Frontend (consumer)
  │
  └─ Polls API endpoints, parses JSON responses
```

**Write format for snapshot JSON:** plain `json.dump()` to file (NOT atomic — no tmp+rename for snapshots, only calibrator weights use atomic writes).

**Write format for binary files:** fixed 4KB records. Header (48 bytes): `ts(d) + src(d) + price_min(d) + price_max(d) + step(d) + n_buckets(I)`. Followed by 1000 bytes long intensity (u8), 1000 bytes short intensity (u8), padded to 4096.

## NEVER DO — Code Rules

1. **NEVER create standalone engine or zone manager instances in API endpoints** — always route through `EngineManager`'s live `EngineInstance` objects
2. **NEVER assume the calibrator is working just because `on_liquidation()` is being called** — verify `total_events` is incrementing by also wiring `on_minute_snapshot()`
3. **NEVER change API response formats without documenting the exact JSON shape** — the frontend Rust parser must match exactly
4. **NEVER set `EmbeddedAPIState._cache_ttl` below 5.0 seconds** — this controls disk I/O refresh for snapshot files
5. **NEVER hardcode "127.0.0.1" as the API bind address** — use "0.0.0.0" for production configs
6. **NEVER wire `on_liquidation()` without also wiring `on_minute_snapshot()`** — both are required or the calibrator silently drops all events
7. **NEVER modify the calibrator's weight file format without updating `_load_weights()` migration logic**
8. **NEVER share state between per-symbol engines** — each symbol gets independent calibrator, weights, zone manager, and frame buffer
9. **NEVER create background threads with refresh intervals under 5 seconds for disk I/O or HTTP requests**
10. **NEVER swallow exceptions silently in data pipelines** — always log the full traceback
11. **NEVER access engine objects (calibrator, heatmap_v2, ob_reconstructor, ob_accumulator) as direct attributes of `FullMetricsProcessor`** — always go through `self._btc()` (or `self.engine_manager.get_engine(symbol)` for multi-symbol). Direct attributes are `del`'d after `EngineInstance` registration.
12. **NEVER instantiate standalone `ActiveZoneManager` instances in API endpoints** — always resolve zones through `EngineManager → EngineInstance → heatmap_v2.zone_manager`. Creating standalone instances produces zones with no data.
13. **NEVER pass `min_weight` to `ActiveZoneManager.get_active_zones()`** — it doesn't accept it (signature: `side`, `min_leverage`, `max_leverage` only). Filter `min_weight` in Python after the call returns.
14. **NEVER parse raw exchange liquidation messages in `_process_liquidations()`** — always use `normalize_liquidation(exchange, data)` from `liq_normalizer.py`. All exchange-specific format handling, side convention mapping, and OKX contract conversion belongs in the normalizer, not the viewer.

## Known Bugs and Past Issues

### BUG: 55 forceOrder events received, total_events stayed at 0
- **Root cause:** `on_minute_snapshot()` was not being called (or called too late), so `self.snapshots` dict was empty. `_get_snapshot_for_event()` returned `None`, and `_process_event()` returned at line 953 without incrementing `total_events`.
- **Misleading signal:** Events WERE appended to `self.events_window` (line 724), giving the false impression the calibrator was receiving and processing them.
- **Three possible triggers:** (a) viewer ran less than 60 seconds, (b) `self.state.perp_close` was 0 at every minute boundary preventing rollover, (c) exception in V2 engine path before `calibrator.on_minute_snapshot()` call.
- **Fix:** Ensure both `on_liquidation()` AND `on_minute_snapshot()` are wired. See "Calibrator Integration Rules" section.

### BUG: 10x tier disabled due to -$62K median miss corruption
- **Root cause:** The 10x leverage tier was producing systematically wrong implied liquidation prices, with a persistent -$62K median miss distance that corrupted offset learning and bias correction.
- **Fix:** Added `DISABLED_TIERS = {10}` in `leverage_config.py` (2026-02-17). Events from disabled tiers are still logged but tagged `tier_disabled: true` and excluded from all calibration updates.

### RESOLVED: BTC-only liquidation filter silently dropped non-BTC events
- **Root cause:** `_process_liquidations()` in `full_metrics_viewer.py` had a hardcoded `"BTC" not in order.get("s", "")` check that silently dropped all non-BTC forceOrder events. Any new symbol registered in `EngineManager` would never receive liquidation data.
- **Fix (Phase 1):** Replaced with dynamic lookup that iterates `engine_manager.engines`.
- **Fix (Phase 2):** Entire method replaced to use `liq_normalizer.normalize_liquidation()`. No exchange-specific parsing remains in the viewer — all exchange format handling, side inversion, and symbol extraction is delegated to the normalizer. Events from Binance, Bybit, and OKX all flow through the same path.

### RESOLVED: Hardcoded BTC fallback prices in embedded_api.py
- **Root cause:** Four locations in `embedded_api.py` used `range(92000, 108000)` as fallback price ranges when no data exists. These BTC-specific values would produce nonsensical axes for ETH (~$3000) or SOL (~$150).
- **Fix:** Added `_get_fallback_price_range(symbol)` helper returning per-symbol ranges: BTC (85000, 115000), ETH (1500, 5000), SOL (50, 400), with a generic default of (100, 10000).

### RESOLVED: Bybit side convention was inverted in normalizer
- **Root cause:** `normalize_bybit()` in `liq_normalizer.py` mapped `"Buy"` → `"long"` and `"Sell"` → `"short"`. This was incorrect. Bybit's `allLiquidation` `S` field is the **order side** (same convention as Binance), not the position side: `"Buy"` = exchange buys to close a short position (shorts liquidated), `"Sell"` = exchange sells to close a long position (longs liquidated).
- **Impact:** All Bybit liquidation events had longs/shorts swapped, corrupting calibrator learning, heatmap directionality, and zone predictions for Bybit data. The inline tests also encoded the wrong expected values, masking the bug.
- **Fix (Phase 2):** Corrected mapping to `"Buy"` → `"short"`, `"Sell"` → `"long"` — matching Binance convention exactly. Updated all inline test assertions. All 36 tests pass.

### DESIGN NOTE: Snapshot JSON writes are NOT atomic
- `_write_api_snapshot()` and `_write_api_snapshot_v2()` use plain `json.dump()` — no tmp+rename. The API reader could see partial JSON on slow I/O. Only the calibrator weight file uses atomic writes (tmp + flush + fsync + rename).

---

## Symbol Conventions

- **Calibrator uses short symbols:** `"BTC"`, `"ETH"`, `"SOL"`
- **Exchange APIs use full symbols:** `"BTCUSDT"`, `"ETHUSDT"`, `"SOLUSDT"`
- `SYMBOL_SHORT_MAP` (ws_connectors) and `FULL_TO_SHORT` (engine_manager) must be used for conversion
- The calibrator's `on_liquidation()` checks `event.symbol` against `self.symbol` — these MUST match or the event is silently dropped (line 720)
- The viewer creates the calibrator with `symbol="BTC"` (line 609 of `full_metrics_viewer.py`)
- ForceOrder events pass `'symbol': 'BTC'` (short form) — this matches correctly

## File and State Inventory

| File | Created By | Read By | Missing at Startup |
|------|-----------|---------|-------------------|
| `poc/liq_api_snapshot.json` | `full_metrics_viewer.py` (every minute) | Embedded API (every 5s) | API returns 404 — normal on first run |
| `poc/liq_api_snapshot_v2.json` | `full_metrics_viewer.py` (every minute) | Embedded API (every 5s) | API returns 404 — normal on first run |
| `poc/ob_heatmap_30s.bin` | `OrderbookAccumulator` (every 30s) | Embedded API (shared buffer, no disk read) | API returns 404 for OB endpoints — normal on first run |
| `poc/ob_recon_stats.json` | `full_metrics_viewer.py` (periodically) | Embedded API `/orderbook_heatmap_stats` | Stats show "file not found" — non-fatal |
| `poc/liq_heatmap_v1.bin` | Embedded API `LiquidationHeatmapBuffer` | Embedded API (loaded at startup for history) | No history available — empty response, non-fatal |
| `poc/liq_heatmap_v2.bin` | Embedded API `LiquidationHeatmapBuffer` | Embedded API (loaded at startup for history) | No history available — empty response, non-fatal |
| `poc/old_logs/old_log/liq_calibrator_weights.json` | `LiquidationCalibrator._save_weights()` (atomic write) | `LiquidationCalibrator._load_weights()` at startup | Uses default uniform weights — non-fatal |
| `poc/liq_calibrator_weights.json` | Calibrator (may also write here) | Calibrator | Falls back to default weights |
| `poc/plot_feed.jsonl` | `full_metrics_viewer.py` (every minute) | `liq_plotter.py` (tail) | Plotter waits for data — non-fatal |
| `poc/liq_debug.jsonl` | `full_metrics_viewer.py` (debug logging) | Manual inspection | No debug log — non-fatal |
| `poc/liq_active_zones.json` | `ActiveZoneManager` (every 60s) | `ActiveZoneManager` at startup | Starts with empty zone set — non-fatal |
| `poc/liq_calibrator.jsonl` | `LiquidationCalibrator` (event + calibration logs) | Manual inspection / audit scripts | No log — non-fatal |
| `poc/liq_engine_debug.log` | `LiquidationStressEngine` (V1 debug output) | Manual inspection | No log — non-fatal |
| `poc/liq_sweeps.jsonl` | `LiquidationStressEngine` (sweep events) | Manual inspection / audit scripts | No log — non-fatal |

## Multi-Exchange Implementation Notes

### Completed (Phase 2)

- **Three exchanges active:** Binance, Bybit, OKX — all started by `MultiExchangeConnector.connect_all()` in `run_websocket_thread()`
- **Normalizer layer:** `liq_normalizer.py` converts all raw exchange messages into `CommonLiqEvent` before reaching engines. Handles Binance/Bybit order-side inversion (both use same convention: Buy=shorts liquidated, Sell=longs liquidated), OKX contract-to-base conversion, multi-symbol extraction
- **Per-symbol engines:** BTC, ETH, SOL each have independent `EngineInstance` (calibrator + heatmap). BTC additionally has orderbook engines. Created via `SYMBOL_CONFIGS` dict and `_create_engine_for_symbol()` factory
- **Per-symbol log/weight files:** `liq_calibrator_{SYM}.jsonl`, `liq_calibrator_weights_{SYM}.json`
- **Dynamic liquidation routing:** `_process_liquidations()` uses `normalize_liquidation(exchange, data)` → iterates `CommonLiqEvent` list → routes each to correct engine via `engine_manager.on_force_order(event.symbol_short, ...)`
- ~~Current hardcoded BTC fallback prices (92000–108000) in API must become symbol-aware~~ **DONE** — `_get_fallback_price_range(symbol)` provides per-symbol fallback ranges
- ~~BTC-only liquidation filter~~ **DONE** — no exchange-specific parsing remains in the viewer

- **Multi-exchange OI aggregation:** `oi_poller.py` polls Binance + Bybit + OKX every 15s for BTC, ETH, SOL. Aggregated per-symbol OI delivered via callback. Exposed via `/oi` API endpoint with per-exchange breakdown
- ~~OI data from all exchanges gets aggregated per-symbol before feeding the engine~~ **DONE**

- ~~Per-symbol price feeds~~ **DONE** — BinanceConnector subscribes to `markPrice@1s` for BTC, ETH, SOL. `_process_markprice()` routes to `self.symbol_prices[sym]` for per-symbol mark price + OHLC tracking. `_process_liquidations()` reads `mark_price`/`last_price` from `symbol_prices` for all symbols (no more BTC-only conditional)
- ~~Per-symbol OHLC~~ **DONE** — `symbol_prices[sym]` tracks open/high/low/close from markPrice stream. Minute snapshots compute per-symbol OHLC4 = (O+H+L+C)/4 for all 3 symbols. OHLC reset on minute rollover. BTC continues using orderbook mid-price OHLC (perp_*) as primary, with markPrice as secondary via `symbol_prices`
- **Per-symbol price state:** `self.symbol_prices` dict in `FullMetricsProcessor` tracks `{mark, last, mid, open, high, low, close}` per symbol. `INSTRUMENT_TO_SHORT` maps lowercase instrument names (e.g. `"ethusdt"`) to short symbols (e.g. `"ETH"`) for markPrice routing

### Remaining TODO

- Snapshot files need per-symbol naming: `liq_api_snapshot_{symbol}.json`
- Binary history files need per-symbol naming: `liq_heatmap_v1_{symbol}.bin`
- Frame buffer snapshots must be per-symbol (not shared)
- Hyperliquid excluded — no public liquidation WebSocket stream available

## Testing Checklist Before Deployment

- [ ] All WebSocket streams connecting — Binance (depth, aggTrade, forceOrder, markPrice ×3), Bybit (allLiquidation ×3), OKX (liquidation-orders)
- [ ] Multi-exchange OI poller returning data for BTC, ETH, SOL (check `/oi?symbol=BTC`)
- [ ] REST pollers returning data (ratios)
- [ ] V2 heatmap snapshots being written to disk (`liq_api_snapshot_v2.json`)
- [ ] EngineManager has registered engine (`engine_manager.get_engine("BTC")` is not None)
- [ ] Calibrator `total_events` incrementing after forceOrder events arrive (via `self._btc().calibrator`)
- [ ] Calibrator `self.snapshots` dict is non-empty (proves `on_minute_snapshot()` is wired)
- [ ] All API endpoints return populated data (not empty arrays), including V3 zone endpoints
- [ ] OB heatmap frames accumulating in `ob_heatmap_30s.bin`
- [ ] No silent exceptions in logs
- [ ] Weight file being written/updated after calibration window (30 minutes)

## Verification Steps After Code Changes

1. **Confirm `engine_manager.get_engine("BTC")` returns a valid `EngineInstance`** with non-None calibrator and heatmap_v2
2. **Confirm calibrator `total_events` increments** after forceOrder events arrive (via `self._btc().calibrator`)
3. **Confirm all API endpoints return populated data** (not empty arrays), including V3 zone endpoints
4. **Confirm API response JSON format matches** what the frontend Rust structs expect
5. **Confirm no silent exceptions in logs**
6. **If multi-symbol:** confirm each symbol's calibrator is independently receiving and counting events
