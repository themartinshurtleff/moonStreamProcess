# CLAUDE.md — moonStreamProcess

Single source of truth for how this codebase works. Read this before making any changes.

## Architecture Overview

All core code lives in `poc/`. The system collects Binance Futures market data via WebSocket and REST, computes liquidation heatmaps and orderbook snapshots, and serves them via a FastAPI HTTP API.

| File | Role |
|------|------|
| `full_metrics_viewer.py` | **Main entry point.** Launches WebSocket/REST connections, instantiates all sub-engines, runs Rich terminal dashboard at 4 FPS, writes snapshot files, starts embedded API server. |
| `ws_connectors.py` | `BinanceConnector` and `MultiExchangeConnector` — WebSocket streams for depth, aggTrade, forceOrder, markPrice. |
| `rest_pollers.py` | Async REST pollers for open interest, top trader account/position ratios, global account ratios. |
| `liq_engine.py` | V1 `LiquidationStressEngine` — volume-based liquidation zone prediction (superseded by V2). |
| `liq_heatmap.py` | V2 `LiquidationHeatmap` — unified engine combining `LiquidationTape` + `EntryInference` for forward-looking liquidation pools. |
| `liq_tape.py` | Ground-truth liquidation accumulator from forceOrder events. Tracks actual liquidation volumes by price bucket. |
| `entry_inference.py` | Infers position entries from OI changes + taker aggression, projects liquidation prices across leverage tiers. |
| `liq_calibrator.py` | Self-calibrating leverage weight system. Uses forceOrder events as feedback with soft attribution in percent-space. ~2800 lines. |
| `leverage_config.py` | Per-symbol leverage ladder definitions and tier weight constants (5x through 250x). |
| `active_zone_manager.py` | V3 zone lifecycle manager. Persistent zones: CREATED → REINFORCED → SWEPT/EXPIRED. Disk persistence via JSON. |
| `ob_heatmap.py` | Orderbook heatmap with `OrderbookReconstructor` (snapshot+diff) and `OrderbookAccumulator` (30-second frames). Binary ring buffer persistence. |
| `liq_api.py` | **Standalone** FastAPI HTTP server (v3.0.0). Reads from snapshot files on disk. Includes V3 zone endpoints. Run separately from viewer. |
| `embedded_api.py` | **Embedded** FastAPI server (v2.1.0). Runs as daemon thread inside viewer. Shares `ob_buffer` by direct Python reference. No V3 endpoints. |
| `liq_plotter.py` | Matplotlib live chart tailing `plot_feed.jsonl`. |
| `metrics_viewer.py` | Earlier/simpler viewer (predecessor to `full_metrics_viewer.py`). |
| `list_metrics.py` | Reference script listing all ~40 BTC perpetual metrics from Binance. |
| `audit_v2_comprehensive.py` | Code+log verified audit script producing markdown report. |
| `audit_v2_engine.py` | Runtime audit computing hit-rate metrics from log files. |

## Data Flow — Critical Paths

### Path A: ForceOrder Event → Calibrator → Heatmap Rendering

```
Binance WS forceOrder
  → MultiExchangeConnector callback
    → full_metrics_viewer on_force_order()
      → LiquidationTape.on_liquidation()       (V2 ground truth)
      → LiquidationCalibrator.on_liquidation()  (calibration feedback)
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
      → EntryInference.on_oi_update()
        → detects OI delta → infers new position entries
        → projects liquidation prices per leverage tier
      → LiquidationStressEngine also consumes OI for V1 zones
```

### Path C: Minute Rollover → Snapshot → API → Frontend

```
full_metrics_viewer detects new minute boundary
  → computes OHLC4 from accumulated ticks
  → calls calibrator.on_minute_snapshot(ohlc4, predictions, ladder, weights, ...)
  → calls liq_engine.compute_snapshot() (V1)
  → calls liq_heatmap.compute_snapshot() (V2)
  → _write_api_snapshot(snapshot)       → poc/liq_api_snapshot.json
  → _write_api_snapshot_v2(snapshot_v2) → poc/liq_api_snapshot_v2.json
  → API cache refresh thread reads JSON every 1 second
    → adds frame to LiquidationHeatmapBuffer (deduplicates by minute)
    → serves via /v1/liq_heatmap or /v2/liq_heatmap endpoints
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

### Standalone Server (liq_api.py, v3.0.0, port 8899)

| Method | Path | Params | Data Source |
|--------|------|--------|-------------|
| GET | `/v1/health` | — | In-memory cache + disk file check |
| GET | `/v1/liq_heatmap` | `symbol`, `window_minutes` | `SnapshotCache` ← `liq_api_snapshot.json` (1s refresh) |
| GET | `/v1/liq_heatmap_history` | `symbol`, `minutes` (5-720), `stride` (1-30) | `LiquidationHeatmapBuffer` ← `liq_heatmap_v1.bin` |
| GET | `/v2/liq_heatmap` | `symbol`, `min_notional` | Direct disk read of `liq_api_snapshot_v2.json` |
| GET | `/v2/liq_heatmap_history` | `symbol`, `minutes` (5-720), `stride` (1-30) | `LiquidationHeatmapBuffer` ← `liq_heatmap_v2.bin` |
| GET | `/v2/liq_stats` | `symbol` | Direct disk read of `liq_api_snapshot_v2.json` |
| GET | `/v3/liq_zones` | `symbol`, `side`, `min_leverage`, `max_leverage`, `min_weight` | `ActiveZoneManager` (in-memory) |
| GET | `/v3/liq_zones_summary` | `symbol` | `ActiveZoneManager` (in-memory) |
| GET | `/v3/liq_heatmap` | `symbol`, `min_notional`, `min_leverage`, `max_leverage`, `min_weight` | `ActiveZoneManager` (in-memory) |
| GET | `/v2/orderbook_heatmap_30s` | `symbol`, `range_pct`, `step`, `price_min`, `price_max`, `format` | `OrderbookHeatmapBuffer` ← `ob_heatmap_30s.bin` (5s tail) |
| GET | `/v2/orderbook_heatmap_30s_history` | `symbol`, `minutes`, `stride`, `range_pct`, `step`, `price_min`, `price_max`, `format` | Same buffer |
| GET | `/v2/orderbook_heatmap_30s_stats` | — | Buffer stats + `ob_recon_stats.json` |
| GET | `/v2/orderbook_heatmap_30s_debug` | — | Module checks + buffer stats |

### Embedded Server (embedded_api.py, v2.1.0, port 8899)

Same endpoints as standalone EXCEPT: no `/v3/*` endpoints. Orderbook data comes from shared Python object reference (no disk I/O). Liq snapshots still read from disk JSON files.

## Snapshot Pipeline

```
full_metrics_viewer.py (writer)
  │
  ├─ liq_api_snapshot.json      ← V1 heatmap (overwritten every minute)
  ├─ liq_api_snapshot_v2.json   ← V2 heatmap (overwritten every minute)
  ├─ ob_heatmap_30s.bin         ← OB frames (appended every 30s, 4KB records)
  ├─ ob_recon_stats.json        ← OB reconstructor diagnostics
  ├─ liq_heatmap_v1.bin         ← V1 history ring buffer (appended by standalone API)
  ├─ liq_heatmap_v2.bin         ← V2 history ring buffer (appended by standalone API)
  └─ plot_feed.jsonl            ← OHLC + zone data for matplotlib plotter

API Server (reader)
  │
  ├─ Standalone: reads JSON every 1s, tails .bin every 5s
  └─ Embedded: shares ob_buffer by reference, reads JSON every 1s

Frontend (consumer)
  │
  └─ Polls API endpoints, parses JSON responses
```

**Write format for snapshot JSON:** plain `json.dump()` to file (NOT atomic — no tmp+rename for snapshots, only calibrator weights use atomic writes).

**Write format for binary files:** fixed 4KB records. Header (48 bytes): `ts(d) + src(d) + price_min(d) + price_max(d) + step(d) + n_buckets(I)`. Followed by 1000 bytes long intensity (u8), 1000 bytes short intensity (u8), padded to 4096.

## NEVER DO — Code Rules

1. **NEVER create standalone zone managers in API endpoints** — always route through the engine manager's live instances
2. **NEVER assume the calibrator is working just because `on_liquidation()` is being called** — verify `total_events` is incrementing by also wiring `on_minute_snapshot()`
3. **NEVER change API response formats without documenting the exact JSON shape** — the frontend Rust parser must match exactly
4. **NEVER set liq_api.py `refresh_interval` below 5.0 seconds**
5. **NEVER hardcode "127.0.0.1" as the API bind address** — use "0.0.0.0" for production configs
6. **NEVER wire `on_liquidation()` without also wiring `on_minute_snapshot()`** — both are required or the calibrator silently drops all events
7. **NEVER modify the calibrator's weight file format without updating `_load_weights()` migration logic**
8. **NEVER share state between per-symbol engines** — each symbol gets independent calibrator, weights, zone manager, and frame buffer
9. **NEVER create background threads with refresh intervals under 5 seconds for disk I/O or HTTP requests**
10. **NEVER swallow exceptions silently in data pipelines** — always log the full traceback

## Symbol Conventions

- **Calibrator uses short symbols:** `"BTC"`, `"ETH"`, `"SOL"`
- **Exchange APIs use full symbols:** `"BTCUSDT"`, `"ETHUSDT"`, `"SOLUSDT"`
- `SYMBOL_SHORT_MAP` must be used for conversion
- The calibrator's `on_liquidation()` checks `event.symbol` against `self.symbol` — these MUST match or the event is silently dropped (line 720)
- The viewer creates the calibrator with `symbol="BTC"` (line 609 of `full_metrics_viewer.py`)
- ForceOrder events pass `'symbol': 'BTC'` (short form) — this matches correctly

## File and State Inventory

| File | Created By | Read By | Missing at Startup |
|------|-----------|---------|-------------------|
| `poc/liq_api_snapshot.json` | `full_metrics_viewer.py` (every minute) | API server (every 1s) | API returns 404 — normal on first run |
| `poc/liq_api_snapshot_v2.json` | `full_metrics_viewer.py` (every minute) | API server (every 1s) | API returns 404 — normal on first run |
| `poc/ob_heatmap_30s.bin` | `OrderbookAccumulator` (every 30s) | API server (loaded at startup, tailed every 5s) | API returns 404 for OB endpoints — normal on first run |
| `poc/ob_recon_stats.json` | `full_metrics_viewer.py` (periodically) | API `/v2/orderbook_heatmap_30s_stats` | Stats show "file not found" — non-fatal |
| `poc/liq_heatmap_v1.bin` | Standalone API `LiquidationHeatmapBuffer` | API (loaded at startup for history) | No history available — empty response, non-fatal |
| `poc/liq_heatmap_v2.bin` | Standalone API `LiquidationHeatmapBuffer` | API (loaded at startup for history) | No history available — empty response, non-fatal |
| `poc/old_logs/old_log/liq_calibrator_weights.json` | `LiquidationCalibrator._save_weights()` (atomic write) | `LiquidationCalibrator._load_weights()` at startup | Uses default uniform weights — non-fatal |
| `poc/liq_calibrator_weights.json` | Calibrator (may also write here) | Calibrator | Falls back to default weights |
| `poc/plot_feed.jsonl` | `full_metrics_viewer.py` (every minute) | `liq_plotter.py` (tail) | Plotter waits for data — non-fatal |
| `poc/liq_debug.jsonl` | `full_metrics_viewer.py` (debug logging) | Manual inspection | No debug log — non-fatal |
| `poc/liq_active_zones.json` | `ActiveZoneManager` (every 60s) | `ActiveZoneManager` at startup | Starts with empty zone set — non-fatal |

## Multi-Exchange Implementation Notes

When implementing multi-exchange support:

- Each symbol (BTC, ETH, SOL) needs its own independent engine instance
- Each engine needs its own calibrator with its own weight file
- ForceOrder events from all exchanges feed into the same symbol's calibrator
- OI data from all exchanges gets aggregated per-symbol before feeding the engine
- The API must route requests by `symbol` parameter to the correct engine instance
- Frame buffer snapshots must be per-symbol (not shared)
- The frontend requests data with a `symbol` parameter — the API must route accordingly
- Current hardcoded BTC fallback prices (92000–108000) in API must become symbol-aware
- Snapshot files need per-symbol naming: `liq_api_snapshot_{symbol}.json`
- Binary history files need per-symbol naming: `liq_heatmap_v1_{symbol}.bin`

## Testing Checklist Before Deployment

- [ ] All WebSocket streams connecting (depth, aggTrade, forceOrder, markPrice)
- [ ] REST pollers returning data (OI, ratios)
- [ ] V1 and V2 heatmap snapshots being written to disk
- [ ] Calibrator `total_events` incrementing after forceOrder events arrive
- [ ] Calibrator `self.snapshots` dict is non-empty (proves `on_minute_snapshot()` is wired)
- [ ] API endpoints return populated data (not empty arrays)
- [ ] OB heatmap frames accumulating in `ob_heatmap_30s.bin`
- [ ] No silent exceptions in logs
- [ ] Weight file being written/updated after calibration window (30 minutes)

## Verification Steps After Code Changes

1. **Confirm calibrator `total_events` increments** after forceOrder events arrive
2. **Confirm all API endpoints return populated data** (not empty arrays)
3. **Confirm API response JSON format matches** what the frontend Rust structs expect
4. **Confirm no silent exceptions in logs**
5. **If multi-symbol:** confirm each symbol's calibrator is independently receiving and counting events
