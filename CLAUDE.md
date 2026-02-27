# CLAUDE.md — moonStreamProcess

Single source of truth for how this codebase works. Read this before making any changes.

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Engine Instantiation Parameters](#engine-instantiation-parameters)
3. [Calibrator Constants](#calibrator-constants--tuning-parameters)
4. [Leverage Configuration](#leverage-configuration)
5. [Zone Manager Configuration](#zone-manager-configuration)
6. [Entry Inference Tuning Parameters](#entry-inference-tuning-parameters)
7. [Data Flow — Critical Paths](#data-flow--critical-paths)
8. [Calibrator Integration Rules — CRITICAL](#calibrator-integration-rules--critical)
9. [API Endpoint Inventory](#api-endpoint-inventory)
10. [Response Caching](#response-caching)
11. [Snapshot Pipeline](#snapshot-pipeline)
12. [NEVER DO — Code Rules](#never-do--code-rules)
13. [Known Bugs and Past Issues](#known-bugs-and-past-issues)
14. [Symbol Conventions](#symbol-conventions)
15. [File and State Inventory](#file-and-state-inventory)
16. [Multi-Exchange Implementation Notes](#multi-exchange-implementation-notes)
17. [Testing Checklist](#testing-checklist-before-deployment)
18. [Verification Steps](#verification-steps-after-code-changes)

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
| `{sym}usdt@aggTrade` (×3) | Aggregated trades (buyer_is_maker) | Per-symbol `TakerAggressionAccumulator`, per-symbol volume tracking (`symbol_volumes`). BTC, ETH, SOL. |
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

**Independent normalization:** Tape and inference heatmaps are normalized to 0-1 independently BEFORE applying the 0.35/0.65 weights in `get_combined_heatmap()`. This is required because tape values (observed liquidation flow, ~$1-10K/min) and inference values (estimated position inventory, ~$1-5M/min after OI×price conversion) differ by 100-1000x in magnitude. Without independent normalization, inference completely dominates and tape becomes invisible. Each source uses p99-based scaling (`_p99_scale()` in `entry_inference.py`) to prevent single outlier spikes from crushing the rest. The `get_api_response()` path (clustered pools) keeps raw USD values for `notional_usd` since the API consumer needs real USD; visual intensity is handled by `_normalize_pools()` downstream.

**heatmap_balance debug metric:** Every 10 minutes, a `{"type": "heatmap_balance", ...}` entry is logged to `liq_cycles_{SYM}.jsonl` with `tape_max_raw`, `inf_max_raw`, `tape_scale`, `inf_scale`, and `combined_max`. This proves the normalization is working and makes magnitude regressions obvious.

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

### TakerAggressionAccumulator (per-symbol dict)
```python
self.taker_aggression = {sym: TakerAggressionAccumulator(history_minutes=10) for sym in SYMBOL_CONFIGS}
# Access: self.taker_aggression["BTC"], self.taker_aggression["ETH"], self.taker_aggression["SOL"]
```

### Per-symbol Volume Tracking
```python
self.symbol_volumes = {sym: {"buy_vol": 0.0, "sell_vol": 0.0, "buy_count": 0, "sell_count": 0} for sym in SYMBOL_CONFIGS}
# Accumulated from aggTrade stream in _process_trades(), reset every minute in _check_minute_rollover().
# BTC also continues to update self.state.perp_buyVol/perp_sellVol for backwards-compat dashboard.
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
| `DEFAULT_PERSIST_FILE` | `liq_active_zones.json` | Zone state persistence file (default only — runtime uses per-symbol paths: `liq_active_zones_BTC.json`, `liq_active_zones_ETH.json`, `liq_active_zones_SOL.json` via `ZONE_PERSIST_FILES` in viewer) |

Zone lifecycle: **CREATED** → **REINFORCED** (repeatable) → **SWEPT** (by price) or **EXPIRED** (by time/decay)

---

## Entry Inference Tuning Parameters

Defined as module-level constants in `entry_inference.py`. These control how projection buckets persist through price sweeps and OI declines.

### Sweep Behavior (Partial Reduction)

When price crosses a projected liquidation level, the bucket is **partially reduced** instead of hard-deleted. This prevents levels that accumulated over many minutes from vanishing on a single wick.

| Constant | Value | Purpose |
|----------|-------|---------|
| `SWEEP_REDUCTION_FACTOR` | 0.70 | Fraction of bucket value removed per sweep. **Starting default, expected to tune based on soak test results.** First sweep takes 70%, leaving 30%. Second sweep takes 70% of remainder, leaving 9%. Level fades naturally. |
| `SWEEP_MIN_BUCKET_USD` | 100.0 | Delete bucket entirely if reduced value falls below this threshold. Prevents ghost buckets with negligible value from persisting. |

Sweep logic per bucket:
1. `bucket.size_usd *= (1.0 - SWEEP_REDUCTION_FACTOR)` — reduce by configured fraction
2. If `bucket.size_usd < SWEEP_MIN_BUCKET_USD` → delete the bucket
3. Otherwise bucket survives at reduced intensity

**Applies to projection buckets only.** Tape (observed liquidation flow) continues to hard-delete on sweep because tape represents consumed liquidity — semantically different from projected inventory.

Each sweep reduction emits a debug log entry to `liq_debug_{SYM}.jsonl`:
```json
{"type": "sweep_reduced", "symbol": "BTC", "side": "long", "bucket": 67000.0, "before_usd": 50000.0, "after_usd": 15000.0, "deleted": false}
```

### OI-Based Reduction Cap

When OI drops (positions closing), projections are reduced proportionally. The reduction rate is capped to prevent rapid wipeout during brief OI declines.

| Constant | Value | Previous | Purpose |
|----------|-------|----------|---------|
| `MAX_OI_REDUCTION_PER_MINUTE` | 0.25 (25%) | 0.50 (50%) | Maximum fraction of projections removed per minute on OI drop. A 2-minute decline now removes ~44% instead of 75%. |

Reduction formula: `close_factor = min(abs(oi_delta) / total_oi, MAX_OI_REDUCTION_PER_MINUTE)` — scales proportionally to the magnitude of the OI drop relative to total OI, capped at 25% per minute. Falls back to `abs(oi_delta) * src_price / total_projected` when total OI is unavailable.

### Combined-Source Display Boost

Buckets where both tape and inference contribute are the highest-quality signal (56.5% sweep rate vs 0% for pure inference). A display-only intensity boost makes them visually prominent.

| Constant | Value | Purpose |
|----------|-------|---------|
| `COMBINED_ZONE_BOOST` | 1.5 | Multiplier applied to heatmap buckets where both tape and inference contribute above EPS threshold. Clamped to 1.0 max after application. |
| `COMBINED_SOURCE_EPS` | 1e-4 | Minimum normalized value (after independent p99 normalization) to count as "present" from a source. Prevents float noise from triggering false combined classifications. |

**Display-only boundary:** Two parallel methods exist on `EntryInference`:
- **`get_combined_heatmap()`** — raw combined map (tape_norm × tape_weight + inf_norm × projection_weight). Used by zone logic, calibrator input (`on_minute_snapshot` via `get_heatmap()`), diagnostic logging, and any non-display consumer. **NEVER modify this method to include the boost.**
- **`get_combined_heatmap_display()`** — display wrapper that calls `get_combined_heatmap()` internally, then applies `COMBINED_ZONE_BOOST` to overlap buckets. Used only for API/display snapshot rendering (via `LiquidationHeatmap.get_heatmap_display()`).

Similarly on `LiquidationHeatmap`:
- **`get_heatmap()`** — raw version, feeds calibrator `pred_longs`/`pred_shorts` via `on_minute_snapshot()`.
- **`get_heatmap_display()`** — display wrapper using `get_combined_heatmap_display()`. Used for V1/V2 snapshot JSON writes.

**If any future code needs combined heatmap data for logic (zones, clustering, persistence, calibrator), it MUST call `get_combined_heatmap()` / `get_heatmap()` — never the display wrappers.**

Boost debug metric logged every 10 minutes to `liq_debug_{SYM}.jsonl`:
```json
{"type": "combined_zone_boost", "symbol": "BTC", "total_buckets": 150, "boosted_buckets": 12, "boosted_pct": 8.0, "clamped_buckets": 2, "clamped_pct": 16.67, "avg_before_boost": 0.45, "avg_after_boost": 0.675, "boost_factor": 1.5}
```

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
  → for each symbol (BTC, ETH, SOL):
    → computes per-symbol OHLC4 from accumulated ticks / markPrice stream
    → eng.heatmap_v2.on_minute(src, high, low, oi, taker_buy, taker_sell)
  → for each symbol: computes V2 snapshot via get_api_response() + get_heatmap()
    → write_json_atomic(liq_api_snapshot_v2_{SYM}.json)
  → BTC zone tracker + display zones (BTC-only dashboard)
  → for each symbol: computes V1 snapshot via get_heatmap() + smoothing
    → write_json_atomic(liq_api_snapshot_{SYM}.json)
  → for each symbol: calibrator.on_minute_snapshot(...) with per-symbol OI delta
  → per-symbol volume + OHLC resets (AFTER all snapshot writes)
  → Embedded API cache refresh thread reads per-symbol JSON every 5 seconds
    → adds frame to per-symbol LiquidationHeatmapBuffer (deduplicates by minute)
    → ResponseCache serves cached responses (TTL-based per endpoint)
    → serves via /liq_heatmap, /liq_heatmap_v2, /liq_zones endpoints (all accept ?symbol=)
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
        → liq_calibrator_weights_{SYM}.json (per-symbol)
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

### Disabled Tier Weight Enforcement (`_enforce_disabled_tiers`)

The `_enforce_disabled_tiers()` method in `LiquidationCalibrator` zeros all `DISABLED_TIERS` weights and renormalizes enabled tiers to sum to 1.0. It is called in three mandatory locations:

1. **`_load_weights()`** — after loading weights from file (cleans stale disabled-tier weight from disk)
2. **`_save_weights()`** — before persisting (mutates `self.current_weights` in-memory first, so disk and memory never diverge)
3. **`_run_calibration()`** — inline after mid-mass floor enforcement, before entropy calculation (cleans the local `new_weights` list)

**Degenerate fallback:** If all enabled tiers have zero weight (sum ≤ 1e-9), the helper restores default weights from `get_leverage_config(symbol)` in `leverage_config.py`, normalized over enabled tiers only. Ultimate fallback: uniform distribution over enabled tiers.

### `calibration_enabled` Persistence Behavior

`calibration_enabled` is initialized to `True` in `__init__` and is **never persisted** to the weight file or any other file. A process restart always resets it to `True`, allowing calibration to resume even if a safety trip occurred in the previous session.

### Entropy Safety Check — Disabled Tier Exclusion

The Shannon entropy check in `_run_calibration()` computes entropy over **enabled tiers only** (excluding `DISABLED_TIERS`). Enabled weights are renormalized to sum to 1.0 before the entropy calculation. This prevents false positives where disabled tiers with structural zero-weight artificially cap the entropy below the safety threshold (`SAFETY_ENTROPY_MIN = 1.6`).

## API Endpoint Inventory

### Embedded Server (embedded_api.py, v3.0.0, port 8899)

**This is the only API server needed.** Runs as a daemon thread inside `full_metrics_viewer.py`. Shares `ob_buffer` by direct Python reference. Receives `EngineManager` for V3 zone access. All responses go through `ResponseCache` (see Response Caching section).

Clean paths are primary. Old `/vN/` paths are backwards-compatible aliases via stacked FastAPI `@app.get()` decorators.

| Method | Primary Path | Alias | Params | Data Source | Cache TTL |
|--------|-------------|-------|--------|-------------|-----------|
| GET | `/health` | `/v1/health` | — | In-memory state | none |
| GET | `/oi` | — | `symbol` (BTC/ETH/SOL) | `OIPollerThread` → `OISnapshot` (in-memory) | 5s |
| GET | `/liq_heatmap` | `/v1/liq_heatmap` | `symbol` | `liq_api_snapshot_{SYM}.json` (5s refresh, per-symbol) | 5s |
| GET | `/liq_heatmap_history` | `/v1/liq_heatmap_history` | `symbol`, `minutes` (5-720), `stride` (1-30) | Per-symbol `LiquidationHeatmapBuffer` ← `liq_heatmap_v1_{SYM}.bin` | 30s |
| GET | `/liq_heatmap_v2` | `/v2/liq_heatmap` | `symbol`, `min_notional` | `liq_api_snapshot_v2_{SYM}.json` (5s refresh, per-symbol) | 5s |
| GET | `/liq_heatmap_v2_history` | `/v2/liq_heatmap_history` | `symbol`, `minutes` (5-720), `stride` (1-30) | Per-symbol `LiquidationHeatmapBuffer` ← `liq_heatmap_v2_{SYM}.bin` | 30s |
| GET | `/liq_stats` | `/v2/liq_stats` | `symbol` | `liq_api_snapshot_v2_{SYM}.json` (per-symbol stats) | 5s |
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
  ├─ liq_api_snapshot_{SYM}.json   ← Per-symbol V1 heatmap (atomic write every minute)
  ├─ liq_api_snapshot_v2_{SYM}.json ← Per-symbol V2 heatmap (atomic write every minute)
  ├─ ob_heatmap_30s.bin           ← OB frames (appended every 30s, 4KB records)
  ├─ ob_recon_stats.json          ← OB reconstructor diagnostics
  └─ plot_feed.jsonl              ← OHLC + zone data for matplotlib plotter (all symbols)

Embedded API Server (reader — daemon thread in viewer)
  │
  ├─ Shares ob_buffer by direct Python reference (no disk I/O for OB)
  ├─ Reads per-symbol liq JSON snapshots every 5s (liq_api_snapshot_{SYM}.json, liq_api_snapshot_v2_{SYM}.json)
  ├─ Per-symbol history buffers write to liq_heatmap_v1_{SYM}.bin, liq_heatmap_v2_{SYM}.bin
  ├─ Receives EngineManager for V3 zone access (in-memory, no disk)
  └─ ResponseCache serves concurrent users from TTL-based cache

Frontend (consumer)
  │
  └─ Polls API endpoints, parses JSON responses
```

**Write format for snapshot JSON:** `write_json_atomic()` (tmp + flush + fsync + `os.replace`) in `full_metrics_viewer.py`. Both calibrator weights and snapshot files now use atomic writes.

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
15. **NEVER add code to the per-symbol minute snapshot loop without exception isolation — one symbol failure must not block others**
16. **NEVER build price grids with incremental `p += step` loops** — floating-point drift corrupts grids for non-integer steps (e.g. SOL step=0.1). Always use index-based construction: `round(price_min + i * step, ndigits)` with `ndigits = max(0, -Decimal(str(step)).as_tuple().exponent)`
17. **NEVER assume OI values from `oi_poller` are in USD** — they are in **base asset units** (BTC count, ETH count, SOL count). Multiply by `src_price` to convert to USD before threshold comparisons or USD-denominated computations
18. **NEVER combine tape and inference raw USD values without independent normalization** — tape (observed liquidation flow, ~$1-10K/min) and inference (estimated position inventory, ~$1-5M/min) differ by 100-1000x in magnitude. Always normalize each source to 0-1 independently via `_p99_scale()` before applying `tape_weight`/`projection_weight`. Raw combination makes the weight split useless and produces persistent horizontal lines.
19. **NEVER compute entropy for safety checks without excluding DISABLED_TIERS** — structural zero-weight tiers cap entropy below safety thresholds (e.g. 10 tiers with one disabled → max H ≈ 1.59, below the 1.6 threshold). Always filter to enabled tiers and renormalize before entropy calculation. See `_run_calibration()` entropy block.
20. **NEVER normalize weights without calling `_enforce_disabled_tiers()` afterward** — disabled tiers absorb weight through normalization drift (dividing total across all tiers including zeroed ones). After any weight normalization, zero disabled tiers and renormalize enabled tiers to sum to 1.0.

## Known Bugs and Past Issues

### BUG: 55 forceOrder events received, total_events stayed at 0
- **Root cause:** `on_minute_snapshot()` was not being called (or called too late), so `self.snapshots` dict was empty. `_get_snapshot_for_event()` returned `None`, and `_process_event()` returned at line 953 without incrementing `total_events`.
- **Misleading signal:** Events WERE appended to `self.events_window` (line 724), giving the false impression the calibrator was receiving and processing them.
- **Three possible triggers:** (a) viewer ran less than 60 seconds, (b) `self.state.perp_close` was 0 at every minute boundary preventing rollover, (c) exception in V2 engine path before `calibrator.on_minute_snapshot()` call.
- **Fix:** Ensure both `on_liquidation()` AND `on_minute_snapshot()` are wired. See "Calibrator Integration Rules" section.

### BUG: 10x tier disabled due to -$62K median miss corruption
- **Root cause:** The 10x leverage tier was producing systematically wrong implied liquidation prices, with a persistent -$62K median miss distance that corrupted offset learning and bias correction.
- **Fix:** Added `DISABLED_TIERS = {10}` in `leverage_config.py` (2026-02-17). Events from disabled tiers are still logged but tagged `tier_disabled: true` and excluded from all calibration updates.


### RESOLVED: Calibration stalled after first run
- **Root cause:** The per-symbol minute snapshot loop in `_check_minute_rollover()` was unguarded. After the first calibration, an exception from a symbol iteration (including weight update callback paths) could abort the entire loop, preventing all symbols from receiving future `on_minute_snapshot()` calls.
- **Fix:** Added per-symbol try/except isolation in `_check_minute_rollover()`, callback isolation in `_on_calibrator_weights_updated()` and `liq_calibrator._run_calibration()`, and per-symbol zone persistence files (`liq_active_zones_{SYMBOL}.json`).

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

### RESOLVED: Snapshot JSON writes are now atomic
- `write_json_atomic()` helper uses tmp + flush + fsync + `os.replace()`. All V1/V2 snapshot files and calibrator weights now use atomic writes. Temp file is in the same directory as target to avoid cross-filesystem rename failures.

### RESOLVED: SOL heatmap intensity arrays all zeros — floating-point grid precision loss
- **Root cause:** Grid construction used incremental `p += step` loops. For SOL with `step=0.1`, floating-point drift after ~57 iterations produced values like `75.19999999999968` instead of `75.2`. Heatmap dict keys (from `round(price/step)*step`) were exact (e.g. `75.2`), but grid values drifted, causing `dict.get(grid_price, 0.0)` to miss every key → all zeros in the intensity arrays.
- **Impact:** SOL liquidation heatmap returned all-zero intensity arrays on both V1 and V2 endpoints, rendering nothing on the frontend. BTC (step=20.0) and ETH (step=1.0) were unaffected because integer-representable steps don't accumulate drift.
- **Fix (2026-02-24):** Replaced all incremental grid construction (`while p += step`) with index-based grids: `round(price_min + i * step, ndigits)` where `ndigits = max(0, -Decimal(str(step)).as_tuple().exponent)`. Applied consistently across 8 files: `full_metrics_viewer.py`, `embedded_api.py`, `liq_tape.py`, `entry_inference.py`, `liq_calibrator.py`, `liq_heatmap.py`, `active_zone_manager.py`, `ob_heatmap.py`. All `_bucket_price()` functions also use the same `ndigits` rounding to ensure grid/dict-key consistency.

### RESOLVED: BTC entry inference completely dead — OI unit mismatch
- **Root cause:** `entry_inference.py` assumed OI was in USD (comment: "Assuming OI is in USD"), but `oi_poller.py` delivers OI in **base asset units** (e.g. BTC count). A typical BTC OI delta of ~118 BTC was treated as $118, which after buy_weight split became ~$77 — below the `$100` minimum threshold in `_project_side()`. Every inference was silently dropped.
- **Impact:** BTC produced 0 inferences despite 191 minutes of positive OI deltas. ETH and SOL were also affected but had larger base-unit OI counts that sometimes exceeded thresholds by accident.
- **Fix (2026-02-24):** In `entry_inference.py`, convert OI delta from base units to USD by multiplying by `src_price`: `oi_delta_usd = abs(oi_delta) * src_price`. Also converted: (a) `oi_threshold` to USD (`last_oi * src_price * MIN_OI_CHANGE_PCT`), (b) closing branch `close_factor` ratio (`abs(oi_delta) * src_price / total_projected`), (c) debug log field to actual USD value.

### RESOLVED: Persistent horizontal lines — inference overwhelms tape after OI unit fix
- **Root cause:** After Bug #2 fix (OI base→USD conversion), inference values became $1-5M/min per symbol while tape values remained $1-10K/min. The `get_combined_heatmap()` combined raw USD values with a 0.35/0.65 split, making inference 134-2626x larger than tape regardless of weighting. Max-value normalization in `get_heatmap()` then crushed tape to <1/255 intensity. Inference levels (projected at fixed leverage offsets from entry price) appeared as permanent horizontal lines, while actual liquidation tape events were invisible.
- **Impact:** BTC switched from dynamic (variable) to persistent heatmap at fix deployment (inference activated for the first time). ETH started persistent from run start (inference already dominant in old code due to large ETH OI deltas passing $100 threshold). SOL showed no data previously due to FP grid bug.
- **Fix (2026-02-25):** In `get_combined_heatmap()` (`entry_inference.py`), independently normalize tape and inference heatmaps to 0-1 using p99-based scaling before applying weights: `combined = tape_norm * 0.35 + inf_norm * 0.65`. Added `_p99_scale()` helper for outlier-resistant normalization. The `get_api_response()` path in `liq_heatmap.py` retains raw USD for `notional_usd` field (API consumer needs real values); visual intensity handled by `_normalize_pools()`. Added `heatmap_balance` debug metric logged every 10 minutes to `liq_cycles_{SYM}.jsonl`.

### RESOLVED: Entropy false positive disables calibration for all symbols
- **Root cause:** The safety entropy check (`H < SAFETY_ENTROPY_MIN` for 3 consecutive windows) did not account for disabled tiers. With 10x in `DISABLED_TIERS` (weight forced to 0.0), the maximum achievable Shannon entropy for the remaining tiers was ~1.59, structurally below the 1.6 threshold. Every run tripped after 3-4 calibration cycles, setting `calibration_enabled = False` for all symbols. Weight files froze at calibration #1 values while in-memory weights continued evolving for 85+ cycles.
- **Fix (2026-02-26):** Entropy calculation in `_run_calibration()` now filters to enabled tiers only (excluding `DISABLED_TIERS`) and renormalizes their weights to sum to 1.0 before computing Shannon entropy. This produces entropy values reflecting concentration among active tiers only.

### RESOLVED: Disabled 10x tier accumulates weight through normalization drift
- **Root cause:** After each calibration cycle, weights are normalized to sum to 1.0 across all tiers including disabled ones. Even though the 10x tier started at 0.0, the anchoring step (`w_new = 0.9*w_old + 0.1*w_update`) could push non-zero values into the disabled slot, and subsequent normalization redistributed weight to it. Over 85 cycles, the 10x tier absorbed 30-47% of total weight across symbols.
- **Fix (2026-02-26):** Added `_enforce_disabled_tiers()` helper in `liq_calibrator.py` that zeros all `DISABLED_TIERS` weights and renormalizes enabled tiers to sum to 1.0. Called in three locations: `_load_weights()` (after loading from file), `_save_weights()` (before persisting, mutates in-memory state), and inline in `_run_calibration()` (after mid-mass floor, before entropy check). Includes degenerate fallback: if all enabled tiers are zero, restores default weights from `leverage_config` normalized over enabled tiers only.

### NOTE: `calibration_enabled` resets to True on restart
- `calibration_enabled` is initialized to `True` in `__init__` and is NOT persisted in the weight file. A process restart always resets it, allowing calibration to resume even if a safety trip occurred in the previous run. This is correct behavior — especially after the entropy false positive fix, the trip condition that caused the original freeze will no longer trigger.

### NOTE: Weight file saving gated by calibration_enabled
- `_save_weights()` is only called inside `_run_calibration()` and is conditional on `self.calibration_enabled == True`. When a safety trip (e.g. entropy collapse) sets `calibration_enabled = False`, weight saving is intentionally suspended. The return value of `_save_weights()` is not checked at the call site (line 2418), but the method logs errors internally. This is intentional design — weights freeze when safety guardrails trip.

---

## Symbol Conventions

- **Calibrator uses short symbols:** `"BTC"`, `"ETH"`, `"SOL"`
- **Exchange APIs use full symbols:** `"BTCUSDT"`, `"ETHUSDT"`, `"SOLUSDT"`
- `SYMBOL_SHORT_MAP` (ws_connectors) and `FULL_TO_SHORT` (engine_manager) must be used for conversion
- The calibrator's `on_liquidation()` checks `event.symbol` against `self.symbol` — these MUST match or the event is silently dropped (line 720)
- The viewer creates per-symbol calibrators via `_create_engine_for_symbol()` with `symbol="BTC"`, `"ETH"`, `"SOL"` (short form)
- ForceOrder events pass short-form symbol (e.g. `'BTC'`) — this matches correctly

## File and State Inventory

| File | Created By | Read By | Missing at Startup |
|------|-----------|---------|-------------------|
| `poc/liq_api_snapshot_{SYM}.json` | `full_metrics_viewer.py` (atomic, every minute) | Embedded API (every 5s, per-symbol) | API returns 503 — normal on first run |
| `poc/liq_api_snapshot_v2_{SYM}.json` | `full_metrics_viewer.py` (atomic, every minute) | Embedded API (every 5s, per-symbol) | API returns 503 — normal on first run |
| `poc/ob_heatmap_30s.bin` | `OrderbookAccumulator` (every 30s) | Embedded API (shared buffer, no disk read) | API returns 404 for OB endpoints — normal on first run |
| `poc/ob_recon_stats.json` | `full_metrics_viewer.py` (periodically) | Embedded API `/orderbook_heatmap_stats` | Stats show "file not found" — non-fatal |
| `poc/liq_heatmap_v1_{SYM}.bin` | Embedded API `LiquidationHeatmapBuffer` (per-symbol) | Embedded API (loaded at startup for history) | No history available — empty response, non-fatal |
| `poc/liq_heatmap_v2_{SYM}.bin` | Embedded API `LiquidationHeatmapBuffer` (per-symbol) | Embedded API (loaded at startup for history) | No history available — empty response, non-fatal |
| `poc/liq_heatmap_v1.bin` | LEGACY (no longer written) | Not read by embedded API | Can be deleted manually |
| `poc/liq_heatmap_v2.bin` | LEGACY (no longer written) | Not read by embedded API | Can be deleted manually |
| `poc/liq_calibrator_weights_{SYM}.json` | `LiquidationCalibrator._save_weights()` (atomic write, per-symbol) | `LiquidationCalibrator._load_weights()` at startup | Uses default uniform weights — non-fatal |
| `poc/plot_feed.jsonl` | `full_metrics_viewer.py` (every minute) | `liq_plotter.py` (tail) | Plotter waits for data — non-fatal |
| `poc/liq_tape_{SYM}.jsonl` | `LiquidationTape` (per-symbol, via `LiquidationHeatmap`) | Manual inspection | No log — non-fatal |
| `poc/liq_inference_{SYM}.jsonl` | `EntryInference` (per-symbol, via `LiquidationHeatmap`) | Manual inspection | No log — non-fatal |
| `poc/liq_sweeps_{SYM}.jsonl` | `LiquidationTape`+`EntryInference` (per-symbol) | Manual inspection | No log — non-fatal |
| `poc/liq_debug_{SYM}.jsonl` | `EntryInference` (per-symbol debug) | Manual inspection | No log — non-fatal |
| `poc/liq_zones_{SYM}.jsonl` | `ActiveZoneManager` (per-symbol zone lifecycle) | Manual inspection | No log — non-fatal |
| `poc/liq_cycles_{SYM}.jsonl` | `LiquidationHeatmap` (per-symbol cycle summaries) | Manual inspection | No log — non-fatal |
| `poc/liq_active_zones_BTC.json` | `ActiveZoneManager` (every 60s, BTC engine) | `ActiveZoneManager` at startup | Starts with empty zone set — non-fatal |
| `poc/liq_active_zones_ETH.json` | `ActiveZoneManager` (every 60s, ETH engine) | `ActiveZoneManager` at startup | Starts with empty zone set — non-fatal |
| `poc/liq_active_zones_SOL.json` | `ActiveZoneManager` (every 60s, SOL engine) | `ActiveZoneManager` at startup | Starts with empty zone set — non-fatal |
| `poc/liq_calibrator_{SYM}.jsonl` | `LiquidationCalibrator` (event + calibration logs, per-symbol) | Manual inspection / audit scripts | No log — non-fatal |
| `poc/liq_engine_debug.log` | `LiquidationStressEngine` (V1 debug output) | Manual inspection | No log — non-fatal |

## Multi-Exchange Implementation Notes

### Completed (Phase 2)

- **Three exchanges active:** Binance, Bybit, OKX — all started by `MultiExchangeConnector.connect_all()` in `run_websocket_thread()`
- **Normalizer layer:** `liq_normalizer.py` converts all raw exchange messages into `CommonLiqEvent` before reaching engines. Handles Binance/Bybit order-side inversion (both use same convention: Buy=shorts liquidated, Sell=longs liquidated), OKX contract-to-base conversion, multi-symbol extraction
- **Per-symbol engines:** BTC, ETH, SOL each have independent `EngineInstance` (calibrator + heatmap). BTC additionally has orderbook engines. Created via `SYMBOL_CONFIGS` dict and `_create_engine_for_symbol()` factory
- **Per-symbol log/weight files:** `liq_calibrator_{SYM}.jsonl`, `liq_calibrator_weights_{SYM}.json`, `liq_tape_{SYM}.jsonl`, `liq_inference_{SYM}.jsonl`, `liq_sweeps_{SYM}.jsonl`, `liq_debug_{SYM}.jsonl`, `liq_zones_{SYM}.jsonl`, `liq_cycles_{SYM}.jsonl`
- **Dynamic liquidation routing:** `_process_liquidations()` uses `normalize_liquidation(exchange, data)` → iterates `CommonLiqEvent` list → routes each to correct engine via `engine_manager.on_force_order(event.symbol_short, ...)`
- ~~Current hardcoded BTC fallback prices (92000–108000) in API must become symbol-aware~~ **DONE** — `_get_fallback_price_range(symbol)` provides per-symbol fallback ranges
- ~~BTC-only liquidation filter~~ **DONE** — no exchange-specific parsing remains in the viewer

- **Multi-exchange OI aggregation:** `oi_poller.py` polls Binance + Bybit + OKX every 15s for BTC, ETH, SOL. Aggregated per-symbol OI delivered via callback. Exposed via `/oi` API endpoint with per-exchange breakdown
- ~~OI data from all exchanges gets aggregated per-symbol before feeding the engine~~ **DONE**

- ~~Per-symbol price feeds~~ **DONE** — BinanceConnector subscribes to `markPrice@1s` for BTC, ETH, SOL. `_process_markprice()` routes to `self.symbol_prices[sym]` for per-symbol mark price + OHLC tracking. `_process_liquidations()` reads `mark_price`/`last_price` from `symbol_prices` for all symbols (no more BTC-only conditional)
- ~~Per-symbol OHLC~~ **DONE** — `symbol_prices[sym]` tracks open/high/low/close from markPrice stream. Minute snapshots compute per-symbol OHLC4 = (O+H+L+C)/4 for all 3 symbols. OHLC reset on minute rollover. BTC continues using orderbook mid-price OHLC (perp_*) as primary, with markPrice as secondary via `symbol_prices`
- **Per-symbol price state:** `self.symbol_prices` dict in `FullMetricsProcessor` tracks `{mark, last, mid, open, high, low, close}` per symbol. `INSTRUMENT_TO_SHORT` maps lowercase instrument names (e.g. `"ethusdt"`) to short symbols (e.g. `"ETH"`) for markPrice routing
- **Per-symbol liquidation counters:** `self.liq_counts` is `defaultdict(lambda: defaultdict(int))` in `FullMetricsProcessor`, incremented in `_process_liquidations()` after successful `engine_manager.on_force_order(...)` routing. Tracks per-symbol per-exchange liquidation event counts for dashboard display.
- **Exchange heartbeat tracking:** `self.exchange_last_seen` is `defaultdict(float)` updated in `process_message()` as soon as the wrapped envelope is unpacked (`exchange`, `obj_type`, etc.). Used for LIVE/STALE/DOWN connection status in dashboard.
- **Rich dashboard multi-symbol sections (4 FPS):** terminal UI now includes (A) `MULTI-SYMBOL OVERVIEW` with BTC/ETH/SOL rows (price, liq events, calibrator stats, zone count, aggregated OI), (B) `EXCHANGE LIQ BREAKDOWN` (Binance/Bybit/OKX counts per symbol), and (C) `EXCHANGE CONNECTIONS` status line from `exchange_last_seen`. Existing BTC-specific panels remain intact and are explicitly labeled BTC.
- ~~Per-symbol aggTrade streams~~ **DONE** — BinanceConnector subscribes to `{sym}usdt@aggTrade` for BTC, ETH, SOL. `_process_trades()` routes by symbol using `INSTRUMENT_TO_SHORT` (same mapping as markPrice). Per-symbol volume accumulated in `self.symbol_volumes[sym]`, per-symbol taker aggression in `self.taker_aggression[sym]`. BTC continues updating `self.state.perp_buyVol`/`perp_sellVol` for backwards-compat dashboard. ETH/SOL minute snapshots now receive real `perp_buy_vol`/`perp_sell_vol` from `symbol_volumes` instead of hardcoded zeros.
- **Per-symbol volume state:** `self.symbol_volumes` dict in `FullMetricsProcessor` tracks `{buy_vol, sell_vol, buy_count, sell_count}` per symbol. Accumulated from aggTrade, reset every minute alongside OHLC reset. `self.taker_aggression` is a per-symbol dict of `TakerAggressionAccumulator` instances — NEVER shared (CLAUDE.md Rule #8).

### Remaining TODO

- ~~Snapshot files need per-symbol naming~~ **DONE** (Task 17) — writer produces per-symbol files
- ~~embedded_api.py needs per-symbol file routing~~ **DONE** (Task 18) — reads `liq_api_snapshot_{SYM}.json` and `liq_api_snapshot_v2_{SYM}.json` per symbol. No longer reads legacy BTC filenames.
- ~~Binary history files need per-symbol naming~~ **DONE** (Task 18) — `liq_heatmap_v1_{SYM}.bin`, `liq_heatmap_v2_{SYM}.bin` per symbol
- ~~Frame buffer snapshots must be per-symbol (not shared)~~ **DONE** (Task 18) — per-symbol `LiquidationHeatmapBuffer` instances
- ~~OB endpoint rejection for non-BTC~~ **DONE** (Task 19) — `_require_btc_orderbook()` returns 400 for non-BTC
- ~~V3 zone symbol normalization~~ **DONE** (Task 19) — `_resolve_symbol` replaced with `_validate_symbol`
- ~~Per-symbol heatmap log files~~ **DONE** (Task 19) — all 6 log files now include `_{SYM}` suffix
- ~~Legacy snapshot file writes removed~~ **DONE** (Task 19) — only per-symbol files written
- Hyperliquid excluded — no public liquidation WebSocket stream available
- `depth_band` inputs for ETH/SOL (needs per-symbol orderbook engines, deferred)

## Testing Checklist Before Deployment

- [ ] All WebSocket streams connecting — Binance (depth, aggTrade ×3, forceOrder, markPrice ×3), Bybit (allLiquidation ×3), OKX (liquidation-orders)
- [ ] Multi-exchange OI poller returning data for BTC, ETH, SOL (check `/oi?symbol=BTC`)
- [ ] REST pollers returning data (ratios)
- [x] Per-symbol V2 snapshot files written for all 3 symbols (verified Feb 23 — BTC 27KB, ETH 16KB, SOL 10KB)
- [x] ETH snapshot contains real heatmap data with non-zero intensity arrays (verified Feb 23 — 243 force orders, 51 inferences)
- [x] ~~Legacy BTC snapshot files still written for backwards compat (verified Feb 23)~~ Removed in Task 19 — only per-symbol files written now
- [x] Per-symbol V1 snapshot files written for all 3 symbols (verified Feb 23)
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

## Phase 2b Diagnostic Audit

This is a code-vs-spec parity audit across BTC/ETH/SOL for the full backend pipeline (WebSocket ingest → normalization → engine routing → minute snapshots/calibration → API output).

### Audit Scope and Method
- Read `CLAUDE.md` as intended architecture baseline.
- Audited implementation in:
  - `poc/ws_connectors.py`
  - `poc/full_metrics_viewer.py`
  - `poc/embedded_api.py`
  - `poc/engine_manager.py`
  - `poc/liq_normalizer.py`
  - `poc/liq_calibrator.py`
  - `poc/liq_heatmap.py`
  - `poc/active_zone_manager.py`
  - `poc/oi_poller.py`
- Followed codepaths through imports and call chains where needed.

### Complete Gap Summary (BTC vs ETH vs SOL)

| Component | BTC | ETH | SOL | File + Lines | What differs / why | What needs to change |
|---|---|---|---|---|---|---|
| Engine instantiation / registration | WORKING | WORKING | WORKING | `poc/full_metrics_viewer.py` L84-L112, L711-L717, L721-L804 | All 3 symbols get independent `EngineInstance`, per-symbol calibrator + heatmap + zone manager wiring. | No change for core registration. |
| Orderbook engine creation | WORKING | BROKEN | BROKEN | `poc/full_metrics_viewer.py` L82-L84, L101-L110, L772-L790 | ETH/SOL are explicitly `has_orderbook=False`; only BTC gets reconstructor/accumulator. This is intentional MVP but not parity with BTC reference pipeline. | Add ETH/SOL orderbook engines if parity target includes OB pipeline. |
| WS subscriptions: Binance | PARTIAL | PARTIAL | PARTIAL | `poc/ws_connectors.py` L107-L145 | Binance subscribes BTC depth, aggTrade for BTC/ETH/SOL, global forceOrder, and markPrice for BTC/ETH/SOL. ETH/SOL lack depth subscriptions only. | ~~Add ETH/SOL aggTrade streams~~ **DONE**. Add ETH/SOL depth streams if full parity required. |
| WS subscriptions: Bybit | PARTIAL | PARTIAL | PARTIAL | `poc/ws_connectors.py` L338-L345 | Bybit subscribes BTC orderbook/publicTrade/tickers; ETH/SOL only get liquidation streams (`allLiquidation.*`). | Add ETH/SOL Bybit orderbook/trade/ticker subs if parity required. |
| WS subscriptions: OKX | PARTIAL | PARTIAL | PARTIAL | `poc/ws_connectors.py` L222-L230 | OKX subscribes BTC books5/trades/funding/OI, plus global liquidation-orders for all swaps. ETH/SOL lack non-liquidation streams. | Add ETH/SOL OKX books/trades/funding/OI if parity required. |
| Depth processing model | WORKING | BROKEN | BROKEN | `poc/full_metrics_viewer.py` L971-L1009 | Depth path is BTC-centric (`self._btc().ob_reconstructor`); storage keyed by exchange only (`self.orderbooks[exchange]`), not symbol. Multi-symbol depth would collide/overwrite. | Key orderbooks/reconstructors per symbol, and route depth by instrument+symbol. |
| Trade/taker aggression ingestion | WORKING | WORKING | WORKING | `poc/full_metrics_viewer.py` L1148-L1205 | ~~`_process_trades` updates BTC-global state; no symbol parameter/route.~~ **FIXED**: `_process_trades(exchange, instrument, data)` routes by symbol via `INSTRUMENT_TO_SHORT`, updates per-symbol `symbol_volumes` and `taker_aggression[sym]`. BTC backwards-compat preserved. | ~~Route trades by symbol and maintain per-symbol vol/aggression state.~~ **DONE**. |
| Liquidation normalization | WORKING | WORKING | WORKING | `poc/liq_normalizer.py` L224-L229, L269-L356, L363-L390 | Symbol extraction and side conventions work for all three exchanges/symbols; Bybit inversion fix is present (`Buy→short`, `Sell→long`). | No change for core normalization. |
| Engine routing on liquidations | WORKING | WORKING | WORKING | `poc/full_metrics_viewer.py` L1174-L1248; `poc/engine_manager.py` L170-L220 | Normalized events route by `event.symbol_short` into per-symbol calibrator+heatmap; liq_counts increment per symbol/exchange. | No change for routing core. |
| Minute inference (`heatmap_v2.on_minute`) | WORKING | WORKING | WORKING | `poc/full_metrics_viewer.py` L1437-L1489 | ~~Only BTC heatmap received per-minute inference updates.~~ **FIXED**: Per-symbol loop calls `eng.heatmap_v2.on_minute()` for all 3 symbols with per-symbol OHLC, OI from `oi_by_symbol`, and taker aggression from per-symbol accumulator. | ~~Call `heatmap_v2.on_minute` per symbol.~~ **DONE**. |
| Snapshot feed to calibrator (`on_minute_snapshot`) | WORKING | WORKING | WORKING | `poc/full_metrics_viewer.py` L1720-L1803 | All symbols receive minute snapshots with real data. ~~ETH/SOL had `buy/sell vol=0` and `oi_change=0`~~ **FIXED**: ETH/SOL get real `perp_buy_vol`/`perp_sell_vol` from `symbol_volumes` and real `perp_oi_change` from `prev_oi_by_symbol` delta tracking. Still missing: `depth_band` (needs per-symbol OB). | ~~Build real per-symbol volumes and OI delta.~~ **DONE**. Still need `depth_band`. |
| Calibration stall fix | WORKING | WORKING | WORKING | `poc/liq_calibrator.py` L952-L954, L2796-L2807 | `_get_snapshot_for_event` returns exact minute or closest prior snapshot; avoids hard drop when exact minute missing. | No change (fix present). |
| Callback isolation (weights update) | WORKING | WORKING | WORKING | `poc/full_metrics_viewer.py` L752-L754, L889-L901 | Lambda captures `_sym=sym_short`, so callback updates correct symbol heatmap. | No change (fix present). |
| Per-symbol zone persistence files | WORKING | WORKING | WORKING | `poc/full_metrics_viewer.py` L120-L124, L758-L770 | `ZONE_PERSIST_FILES` wired into each heatmap's `zone_persist_file`. | No change (fix present). |
| API snapshots (V1/V2 JSON) | WORKING | WORKING | WORKING | `poc/full_metrics_viewer.py` L1512-L1595, L1675-L1711, L1850-L1990; `poc/embedded_api.py` | ~~Writer produced single BTC snapshot files.~~ **FIXED** (Task 17+18): Writer produces per-symbol files (verified Feb 23: BTC V2 27KB, ETH V2 16KB, SOL V2 10KB). `embedded_api.py` now reads per-symbol files directly. Legacy filenames no longer read. | ~~Write per-symbol snapshot files~~ **DONE**. ~~Read per-symbol files~~ **DONE** (Task 18). |
| API history buffers (V1/V2 bin) | WORKING | WORKING | WORKING | `poc/embedded_api.py` | ~~One shared V1/V2 history buffer/file.~~ **FIXED** (Task 18): Per-symbol `LiquidationHeatmapBuffer` instances with per-symbol binary files (`liq_heatmap_v1_{SYM}.bin`, `liq_heatmap_v2_{SYM}.bin`). Old shared files no longer used. | ~~Use per-symbol binary history files/buffers~~ **DONE** (Task 18). |
| API `/liq_stats` symbol handling | WORKING | WORKING | WORKING | `poc/embedded_api.py` | ~~Endpoint always returned single cached BTC stats.~~ **FIXED** (Task 18): `_validate_symbol()` validates input, loads per-symbol V2 snapshot, returns symbol-specific stats. Invalid symbols return 400. | ~~Validate symbol and load per-symbol stats~~ **DONE** (Task 18). |
| API orderbook endpoints | WORKING | N/A | N/A | `poc/embedded_api.py` | ~~Endpoint accepted `symbol` but always served BTC buffer.~~ **FIXED** (Task 19): `_require_btc_orderbook()` rejects non-BTC with 400. ETH/SOL orderbook data not available. | ~~Reject non-BTC explicitly~~ **DONE** (Task 19). |
| OI ingestion + `/oi` endpoint | WORKING | WORKING | WORKING | `poc/oi_poller.py` L164-L205; `poc/full_metrics_viewer.py` L667-L674, L903-L922; `poc/embedded_api.py` L546-L580 | OI is truly per-symbol aggregated across Binance+Bybit+OKX and exposed correctly via symbol query. | No change for core OI path. |
| Dashboard multi-symbol rendering | WORKING | WORKING | WORKING | `poc/full_metrics_viewer.py` L1876-L1957 | Multi-symbol overview, exchange liquidation breakdown, and exchange connection health all render per symbol. | No change (fix present). |
| `liq_counts` tracking | WORKING | WORKING | WORKING | `poc/full_metrics_viewer.py` L676-L677, L1247, L1927-L1935 | Per-symbol, per-exchange liquidation counters are updated and displayed. | No change (fix present). |
| `exchange_last_seen` tracking | WORKING | WORKING | WORKING | `poc/full_metrics_viewer.py` L679-L680, L946, L1943-L1953 | Last-seen heartbeat updated on every message and surfaced in dashboard status. | No change (fix present). |
| Symbol normalization in V3 zone API | WORKING | WORKING | WORKING | `poc/embedded_api.py` | ~~`_resolve_symbol` lacked `upper()`/USDT-strip.~~ **FIXED** (Task 19): `_resolve_symbol` removed. All V3 zone endpoints (`/liq_zones`, `/liq_zones_summary`, `/liq_zones_heatmap`) now use `_validate_symbol()` for consistent normalization + 400 on invalid symbol. | ~~Normalize through `_validate_symbol()`~~ **DONE** (Task 19). |
| Shared log resources in heatmap stack | WORKING | WORKING | WORKING | `poc/liq_heatmap.py` L84-L90 | ~~Tape/inference/sweep/debug/zones/cycles logs shared across symbols.~~ **FIXED** (Task 19): All 6 log files now include `_{SYM}` suffix (`liq_tape_{SYM}.jsonl`, `liq_inference_{SYM}.jsonl`, `liq_sweeps_{SYM}.jsonl`, `liq_debug_{SYM}.jsonl`, `liq_zones_{SYM}.jsonl`, `liq_cycles_{SYM}.jsonl`). | ~~Per-symbol log filenames~~ **DONE** (Task 19). |

### Verification of Previously Claimed Fixes

| Claimed Fix | Status | Evidence |
|---|---|---|
| Calibration stall fix | PRESENT | Snapshot fallback lookup in calibrator `_get_snapshot_for_event` (`exact or closest prior`). |
| Callback isolation | PRESENT | Captured symbol in lambda default arg (`_sym=sym_short`) and symbol-routed callback update. |
| Per-symbol zone files | PRESENT | `ZONE_PERSIST_FILES` and `zone_persist_file` per symbol. |
| Bybit side convention fix | PRESENT | Bybit normalizer maps `Buy→short`, `Sell→long`. |
| markPrice streams (BTC/ETH/SOL) | PRESENT | Binance connector subscribes all 3 markPrice streams. |
| Dashboard multi-symbol rendering | PRESENT | Overview + exchange breakdown + connections iterate all symbols. |
| `liq_counts` tracking | PRESENT | Per-symbol/exchange counter dict increment on routed events. |
| `exchange_last_seen` tracking | PRESENT | Updated in `process_message` and used in connection panel. |
| Per-symbol snapshot writes (Task 17) | VERIFIED IN PRODUCTION | Feb 23: BTC V2 27KB, ETH V2 16KB (243 force orders, 51 inferences), SOL V2 10KB. V1 + legacy files also written. |
| Per-symbol API serving (Task 18) | PRESENT | `embedded_api.py` reads per-symbol snapshot files, per-symbol `LiquidationHeatmapBuffer` instances, per-symbol binary history files. `VALID_SYMBOLS` validation on all liq endpoints. `/liq_stats` returns per-symbol stats. |
| API hardening cleanup (Task 19) | PRESENT | OB endpoints reject non-BTC (400), `_resolve_symbol` replaced with `_validate_symbol` in V3 zones, per-symbol heatmap log files (`liq_tape_{SYM}.jsonl` etc.), legacy snapshot writes removed. |

### CLAUDE.md vs Code Mismatches

1. ~~CLAUDE says per-symbol engines must have independent frame buffer/state; code used shared V1/V2 snapshot files and shared history buffers, so ETH/SOL API paths were not independent.~~ **FIXED** (Task 18): `embedded_api.py` now reads per-symbol snapshot files and uses per-symbol `LiquidationHeatmapBuffer` instances with per-symbol binary files. All liq endpoints are fully symbol-partitioned.
2. ~~CLAUDE notes remaining TODO for per-symbol history buffers and embedded_api per-symbol routing.~~ **FIXED** (Task 18): Per-symbol history buffers implemented, per-symbol file routing implemented, `/liq_stats` validates and routes by symbol.
3. ~~CLAUDE describes broad multi-symbol flow, but actual minute inference is BTC-only (`heatmap_v2.on_minute` only called for BTC), leaving ETH/SOL prediction updates incomplete.~~ **FIXED**: `heatmap_v2.on_minute()` now called for all 3 symbols with per-symbol OHLC, OI, and taker aggression.
4. ~~CLAUDE describes all three symbols being tracked with per-symbol price feeds/OHLC; code does that, but ETH/SOL calibrator inputs still miss key market fields (OI delta, aggression, depth).~~ **MOSTLY FIXED**: ETH/SOL now receive real trade volume, taker aggression, and OI delta via `prev_oi_by_symbol` tracking. Still missing: `depth_band` (needs per-symbol OB).

### Net Assessment

- **BTC** remains the fully wired end-to-end reference pipeline (WS depth/trades/liquidations + minute inference + snapshots/history + API consistency).
- **ETH/SOL** are now **fully served through the API** with per-symbol snapshot reading, per-symbol history buffers, and per-symbol binary files. All liquidation heatmap endpoints (`/liq_heatmap`, `/liq_heatmap_v2`, `/liq_heatmap_history`, `/liq_heatmap_v2_history`, `/liq_stats`) route correctly by symbol with proper validation (400 for invalid, 503 for warming up).
- **All Phase 2b audit gaps resolved** (Task 19): OB endpoints reject non-BTC with 400, V3 zone endpoints use `_validate_symbol()`, per-symbol log files, legacy snapshot writes removed.
- **Remaining deferred items:**
  - `depth_band` inputs for ETH/SOL (needs per-symbol orderbook engines)
  - Hyperliquid excluded (no public liquidation WebSocket stream)

