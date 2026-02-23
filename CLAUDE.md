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

All core code lives in `poc/`. The system collects Binance Futures market data via WebSocket and REST, computes liquidation heatmaps and orderbook snapshots, and serves them via a FastAPI HTTP API.

| File | Role |
|------|------|
| `full_metrics_viewer.py` | **Main entry point.** Launches WebSocket/REST connections, instantiates all sub-engines, runs Rich terminal dashboard at 4 FPS, writes snapshot files, starts embedded API server. |
| `ws_connectors.py` | `BinanceConnector` and `MultiExchangeConnector` — WebSocket streams for depth, aggTrade, forceOrder, markPrice. |
| `rest_pollers.py` | Async REST pollers for open interest, top trader account/position ratios, global account ratios. |
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

### WebSocket Streams (via `BinanceConnector`)

Base URL: `wss://fstream.binance.com/ws`

| Stream | Data | Used By |
|--------|------|---------|
| `btcusdt@depth@100ms` | Orderbook diffs (100ms throttle) | `OrderbookReconstructor` → `OrderbookAccumulator` → 30s OB heatmap frames |
| `btcusdt@aggTrade` | Aggregated trades (buyer_is_maker) | `TakerAggressionAccumulator`, price tracking, volume accumulation |
| `btcusdt@forceOrder` | Liquidation events | `LiquidationTape`, `LiquidationCalibrator.on_liquidation()` |
| `btcusdt@markPrice@1s` | Mark price, funding rate, OI | Mark price for event-time src, funding display |

### REST Polling (via `BinanceRESTPollerThread`)

| Endpoint | Interval | Data |
|----------|----------|------|
| `/fapi/v1/openInterest` | 10s | Open interest for OI delta tracking |
| `/futures/data/topLongShortAccountRatio` | 60s | Top trader account long/short ratio |
| `/futures/data/topLongShortPositionRatio` | 60s | Top trader position long/short ratio |
| `/futures/data/globalLongShortAccountRatio` | 60s | Global account long/short ratio |

## Engine Instantiation Parameters

These are the actual values used in `full_metrics_viewer.py` `FullMetricsProcessor.__init__()`:

### LiquidationCalibrator — line 609
```python
symbol="BTC", steps=20.0, window_minutes=15, hit_bucket_tolerance=5,
learning_rate=0.10, closer_level_gamma=0.35,
enable_buffer_tuning=True, enable_tolerance_tuning=True,
weights_file="poc/liq_calibrator_weights.json",
on_weights_updated=self._on_calibrator_weights_updated
```
Note: `window_minutes=15` in the viewer override (calibrator default is 30).

### LiquidationHeatmap (V2) — line 658
```python
HeatmapConfig(symbol="BTC", steps=20.0, decay=0.995, buffer=0.002,
              tape_weight=0.35, projection_weight=0.65)
# Also: cluster_radius_pct=0.005, min_notional_usd=10000.0, max_pools_per_side=20
```

### BinanceRESTPollerThread — line 628
```python
symbols=["BTCUSDT"], oi_interval=10.0, ratio_interval=60.0
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

### Path A: ForceOrder Event → EngineManager → Calibrator → Heatmap Rendering

```
Binance WS forceOrder
  → MultiExchangeConnector callback
    → full_metrics_viewer on_force_order()
      → self._btc().heatmap_v2.tape.on_liquidation()       (V2 ground truth, via EngineManager)
      → self._btc().calibrator.on_liquidation()             (calibration feedback, via EngineManager)
        → event appended to events_window (line 724)
        → _process_event() called
          → _get_snapshot_for_event() looks up self.snapshots
          → IF self.snapshots is empty → RETURNS WITHOUT COUNTING (line 953)
          → IF snapshot found → soft attribution → stats.total_events += 1 (line 1041)
```

### Path B: OI Data → Engine

```
BinanceRESTPollerThread polls /fapi/v1/openInterest every ~5s
  → PollerState.oi updated
    → full_metrics_viewer reads poller_state.oi each cycle
      → EntryInference.on_oi_update()  (via self._btc().heatmap_v2)
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
| Live data (`/liq_heatmap`, `/liq_heatmap_v2`, `/liq_stats`, `/liq_zones`, `/liq_zones_summary`, `/liq_zones_heatmap`, `/orderbook_heatmap`) | **5 seconds** | Snapshot data updates every 60s (liq) or 30s (OB); 5s is the minimum allowed by CLAUDE.md rules |
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
- **Fix:** Replaced with dynamic lookup that iterates `engine_manager.engines` and matches `engine.symbol_full` against the incoming symbol. Adding a new symbol now only requires registering it in the `EngineManager`.

### RESOLVED: Hardcoded BTC fallback prices in embedded_api.py
- **Root cause:** Four locations in `embedded_api.py` used `range(92000, 108000)` as fallback price ranges when no data exists. These BTC-specific values would produce nonsensical axes for ETH (~$3000) or SOL (~$150).
- **Fix:** Added `_get_fallback_price_range(symbol)` helper returning per-symbol ranges: BTC (85000, 115000), ETH (1500, 5000), SOL (50, 400), with a generic default of (100, 10000).

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

When implementing multi-exchange support:

- Each symbol (BTC, ETH, SOL) needs its own independent engine instance
- Each engine needs its own calibrator with its own weight file
- ForceOrder events from all exchanges feed into the same symbol's calibrator
- OI data from all exchanges gets aggregated per-symbol before feeding the engine
- The API must route requests by `symbol` parameter to the correct engine instance
- Frame buffer snapshots must be per-symbol (not shared)
- The frontend requests data with a `symbol` parameter — the API must route accordingly
- ~~Current hardcoded BTC fallback prices (92000–108000) in API must become symbol-aware~~ **DONE** — `_get_fallback_price_range(symbol)` provides per-symbol fallback ranges
- The liquidation handler (`_process_liquidations`) now dynamically matches incoming forceOrder symbols against all registered engines. No hardcoded symbol filters remain — adding a new symbol only requires registering it in the `EngineManager`
- Snapshot files need per-symbol naming: `liq_api_snapshot_{symbol}.json`
- Binary history files need per-symbol naming: `liq_heatmap_v1_{symbol}.bin`

## Testing Checklist Before Deployment

- [ ] All WebSocket streams connecting (depth, aggTrade, forceOrder, markPrice)
- [ ] REST pollers returning data (OI, ratios)
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
