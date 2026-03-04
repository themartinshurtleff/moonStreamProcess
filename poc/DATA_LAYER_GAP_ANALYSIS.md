# Data Layer Gap Analysis: Backend API vs Frontend Needs

> Generated: 2026-03-04
> Scope: All API endpoints in `embedded_api.py` vs all frontend indicators across BTC/ETH/SOL
> Method: Code-level investigation of embedded_api.py, full_metrics_viewer.py, engine_manager.py, liq_normalizer.py, liq_calibrator.py, liq_tape.py, liq_heatmap.py, entry_inference.py, active_zone_manager.py, ob_heatmap.py, oi_poller.py, rest_pollers.py, ws_connectors.py. JSONL log file sampling for schema verification.

---

## Table of Contents

1. [Currently Served (Working)](#1-currently-served-working)
2. [Gaps: New Endpoints Needed](#2-gaps-new-endpoints-needed)
3. [Gaps: Existing Endpoints Missing Data](#3-gaps-existing-endpoints-missing-data)
4. [Recommended Implementation Order](#4-recommended-implementation-order)

---

## 1. Currently Served (Working)

### 1.1 Liquidation Heatmap V1 — Live

| Field | Status |
|-------|--------|
| **Endpoint** | `GET /liq_heatmap` (alias `/v1/liq_heatmap`) |
| **Symbols** | BTC, ETH, SOL |
| **Cache TTL** | 5s |
| **Data Source** | `liq_api_snapshot_{SYM}.json` (atomic write every 60s, 5s refresh in API) |
| **Response Fields** | `ts`, `src`, `step`, `price_min`, `price_max`, `prices[]`, `long_intensity[]` (0-255 u8), `short_intensity[]` (0-255 u8), `long_notional_usd[]`, `short_notional_usd[]`, `long_size_btc[]`, `short_size_btc[]`, `top_long_zones[]`, `top_short_zones[]`, `norm{}`, `stats{}`, `scale=255` |
| **Verdict** | **WORKING** — complete grid-based intensity data |

### 1.2 Liquidation Heatmap V2 — Live

| Field | Status |
|-------|--------|
| **Endpoint** | `GET /liq_heatmap_v2` (alias `/v2/liq_heatmap`) |
| **Params** | `symbol` (default BTC), `min_notional` (default 0) |
| **Symbols** | BTC, ETH, SOL |
| **Cache TTL** | 5s |
| **Data Source** | `liq_api_snapshot_v2_{SYM}.json` (atomic write every 60s, 5s refresh) |
| **Response Fields** | `ts`, `src`, `step`, `symbol`, `long_levels[]` (clustered pools: `price`, `price_low`, `price_high`, `notional_usd`, `intensity`, `bucket_count`), `short_levels[]`, `stats{}` (tape/inference/zone_manager sub-objects), `meta{}`, grid arrays (`prices[]`, `long_intensity[]`, `short_intensity[]`, etc.) |
| **Verdict** | **WORKING** — both clustered pools and grid intensities |

### 1.3 Liquidation Heatmap V2 — History

| Field | Status |
|-------|--------|
| **Endpoint** | `GET /liq_heatmap_v2_history` (alias `/v2/liq_heatmap_history`) |
| **Params** | `symbol` (default BTC), `minutes` (5-720), `stride` (1-30) |
| **Symbols** | BTC, ETH, SOL |
| **Cache TTL** | 30s |
| **Data Source** | Per-symbol `LiquidationHeatmapBuffer` backed by `liq_heatmap_v2_{SYM}.bin` (4096-byte records, persisted, survives restart) |
| **Response Fields** | `t[]` (ms timestamps), `prices[]`, `long[]` (flattened 0-255 grid), `short[]`, `step`, `scale=255` |
| **Verdict** | **WORKING** — up to 12 hours of history with downsample |

### 1.4 Liquidation Heatmap V1 — History

| Field | Status |
|-------|--------|
| **Endpoint** | `GET /liq_heatmap_history` (alias `/v1/liq_heatmap_history`) |
| **Verdict** | **DEAD** — V1 history binary files are no longer written (removed in v1_histories cleanup). Endpoint exists but always returns 503. |

### 1.5 Liquidation Stats

| Field | Status |
|-------|--------|
| **Endpoint** | `GET /liq_stats` (alias `/v2/liq_stats`) |
| **Params** | `symbol` (default BTC) |
| **Symbols** | BTC, ETH, SOL |
| **Cache TTL** | 5s |
| **Response Fields** | V2 snapshot `stats{}` object with `symbol`, `snapshot_ts`, `snapshot_age_s`, `history_frames`, tape stats (`total_events`, `total_long_notional`, `total_short_notional`, `long_buckets`, `short_buckets`), inference stats (`total_inferred_long_usd`, `total_inferred_short_usd`, `leverage_weights{}`), zone_manager stats (`active_zones_long/short`, `zones_created/swept/expired_total`) |
| **Verdict** | **WORKING** — comprehensive summary stats |

### 1.6 Open Interest

| Field | Status |
|-------|--------|
| **Endpoint** | `GET /oi` |
| **Params** | `symbol` (default BTC) |
| **Symbols** | BTC, ETH, SOL |
| **Cache TTL** | 5s |
| **Data Source** | `MultiExchangeOIPoller` — polls Binance+Bybit+OKX every 15s |
| **Response Fields** | `symbol`, `aggregated_oi` (base asset), `per_exchange{}` (binance/bybit/okx), `ts` |
| **Verdict** | **WORKING** — live aggregated OI with per-exchange breakdown. No delta or history. |

### 1.7 Orderbook Heatmap — Live

| Field | Status |
|-------|--------|
| **Endpoint** | `GET /orderbook_heatmap` (alias `/v2/orderbook_heatmap_30s`) |
| **Params** | `symbol`, `range_pct` (0-1.0), `step`, `price_min`, `price_max`, `format` (json/bin) |
| **Symbols** | BTC, ETH, SOL |
| **Cache TTL** | 5s (JSON only; binary bypasses cache) |
| **Data Source** | Per-symbol `ob_buffers` (shared Python reference from `OrderbookHeatmapBuffer`) |
| **Response Fields** | `symbol`, frame timing fields, `prices[]`, `bid_u8[]`, `ask_u8[]` (0-255), normalization stats (`norm_p50`, `norm_p95`, etc.), `total_bid_notional`, `total_ask_notional` |
| **Verdict** | **WORKING** — 30s-aggregated depth intensity grid |

### 1.8 Orderbook Heatmap — History

| Field | Status |
|-------|--------|
| **Endpoint** | `GET /orderbook_heatmap_history` (alias `/v2/orderbook_heatmap_30s_history`) |
| **Params** | `symbol`, `minutes` (5-720), `stride` (1-60), `step`, `price_min`, `price_max`, `format` |
| **Symbols** | BTC, ETH, SOL |
| **Cache TTL** | 30s (JSON only) |
| **Response Fields** | `t[]` (ms timestamps), `prices[]`, `bid_u8[]` (flattened grid), `ask_u8[]`, grid metadata |
| **Verdict** | **WORKING** — up to 12 hours of OB depth history |

### 1.9 V3 Liquidation Zones

| Field | Status |
|-------|--------|
| **Endpoint** | `GET /liq_zones` (alias `/v3/liq_zones`) |
| **Params** | `symbol`, `side`, `min_leverage`, `max_leverage`, `min_weight` |
| **Symbols** | BTC, ETH, SOL |
| **Cache TTL** | 5s |
| **Data Source** | `EngineManager → ActiveZoneManager` (in-memory) |
| **Response Fields** | `zones[]` (price, side, weight, tier_contributions, reinforcement_count, source, created_at, last_reinforced_at, status), `zones_count`, `summary{}`, `filters{}` |
| **Verdict** | **WORKING** — full zone lifecycle data |

### 1.10 V3 Zone Summary + Heatmap

| Endpoints | Status |
|-----------|--------|
| `GET /liq_zones_summary` | **WORKING** — zone counts and weight totals |
| `GET /liq_zones_heatmap` | **WORKING** — filtered zone-based heatmap with tier breakdown |

### 1.11 OB Stats and Debug

| Endpoints | Status |
|-----------|--------|
| `GET /orderbook_heatmap_stats` | **WORKING** — frame counts, staleness, reconstructor stats |
| `GET /orderbook_heatmap_debug` | **WORKING** — module checks, buffer stats |

### 1.12 Health

| Field | Status |
|-------|--------|
| `GET /health` | **WORKING** — version, uptime, per-symbol OB/liq buffer availability |

---

## 2. Gaps: New Endpoints Needed

### GAP-1: Individual Liquidation Events (CRITICAL)

**What the frontend needs:** Real-time feed of individual force order events for:
- (a) Liquidation bubbles overlay on heatmap (timestamp, price, size bubble, side color, exchange icon)
- (b) Per-candle ShortLiq/LongLiq on footprint bar stats
- (c) Liquidation tape / trade & liquidation list

**Current state:** Individual events flow through the pipeline but are NEVER exposed via any API endpoint. All endpoints return aggregated/clustered data only.

**Where events exist internally:**

| Location | Type | Retention | Fields |
|----------|------|-----------|--------|
| `calibrator.events_window` | RAM deque | ~30 min | `timestamp`, `symbol`, `side`, `price`, `qty`, `notional`, `minute_key`, `event_src_price`, `src_source` |
| `tape.events` | RAM deque (maxlen=7200) | ~12 hours | `timestamp`, `symbol`, `side`, `price`, `qty`, `notional`, `entry_estimate` |
| `liq_tape_{SYM}.jsonl` | Disk JSONL | Until rotation (200MB) | `type`, `ts`, `symbol`, `side`, `price`, `qty`, `notional`, `bucket`, `entry_est` |

**Event rates (measured from logs):**
- BTC: ~3.0 events/min (13,364 events over 4,426 min)
- ETH: ~2.6 events/min (11,548 events over 4,428 min)
- SOL: ~1.6 events/min (7,006 events over 4,388 min)
- **Total all symbols: ~7.2 events/min normal, spikes to ~50-100/min during cascades**

**Proposed endpoint:**
```
GET /liq_events?symbol=BTC&limit=100&since_ts=1741234567.0
```

**Proposed response schema:**
```json
{
  "symbol": "BTC",
  "events": [
    {
      "ts": 1772244490.564,
      "side": "short",
      "price": 66143.2,
      "qty": 0.006,
      "notional_usd": 396.86,
      "exchange": "binance"
    }
  ],
  "count": 100,
  "oldest_ts": 1772242551.082,
  "newest_ts": 1772244490.564
}
```

**Expected data volume:** ~7 events/min × 60 min = ~420 events per hour, each ~120 bytes JSON = ~50 KB/hour. Trivial.

**Historical backfill:** Live-only acceptable (tape.events deque already holds ~12h). No persistent history beyond JSONL logs.

**Implementation approach:** New bounded deque in `FullMetricsProcessor` per symbol, populated in `_process_liquidations()` alongside engine routing. New endpoint reads deque under lock, returns newest N events. Exchange field must be added (currently dropped after normalization — `CommonLiqEvent.exchange` exists but isn't passed to engines).

**Difficulty:** **LOW** — deque + endpoint, ~50 lines. The critical data (`CommonLiqEvent.exchange`, `.price`, `.qty`, `.notional`, `.side`, `.timestamp`) is already available in `_process_liquidations()`.

---

### GAP-2: Footprint Bar Stats (9 Metrics)

**What the frontend needs:** Per-candle (1-minute) metrics for footprint chart:

| # | Metric | Data Exists? | Where | Exposed? |
|---|--------|-------------|-------|----------|
| 1 | CandleVolume | YES | `symbol_volumes[sym]["buy_vol"] + ["sell_vol"]` | NO |
| 2 | BuyVolume | YES | `symbol_volumes[sym]["buy_vol"]` | NO |
| 3 | SellVolume | YES | `symbol_volumes[sym]["sell_vol"]` | NO |
| 4 | CandleDelta | IMPLICIT | `buy_vol - sell_vol` (must compute) | NO |
| 5 | Cvd24h | **NO** | Does not exist — needs 24h accumulator | NO |
| 6 | OiDeltaChange | YES | `oi_by_symbol[sym] - prev_oi_by_symbol[sym]` (computed at minute boundary) | NO |
| 7 | OiCumulativeDelta | **NO** | Does not exist — needs accumulator | NO |
| 8 | ShortLiqVolume | PARTIAL | BTC only in `state.perp_liquidations_shortsTotal`; all symbols in `LiquidationTape` | NO |
| 9 | LongLiqVolume | PARTIAL | BTC only in `state.perp_liquidations_longsTotal`; all symbols in `LiquidationTape` | NO |

**Notes on units:**
- `symbol_volumes` is in **base asset** (BTC count, ETH count, SOL count). Must multiply by price for USD.
- `symbol_volumes` resets every minute in `_check_minute_rollover()`. Current minute is live; previous minutes are discarded.
- OI from `oi_poller` is in **base asset**. Delta computed per minute.
- Liq volumes: BTC tracked in `self.state` (qty); all symbols tracked in `LiquidationTape` (USD notional in buckets).

**Proposed endpoint:**
```
GET /candle_metrics?symbol=BTC
```

**Proposed response schema (current minute, live):**
```json
{
  "symbol": "BTC",
  "minute_key": 29541805,
  "ts": 1772508300.0,
  "price": 68281.15,
  "candle_volume": 45.67,
  "candle_volume_usd": 3119123.45,
  "buy_volume": 28.34,
  "buy_volume_usd": 1935389.40,
  "sell_volume": 17.33,
  "sell_volume_usd": 1183734.05,
  "candle_delta": 11.01,
  "candle_delta_usd": 751655.35,
  "oi_delta": 54.32,
  "oi_delta_usd": 3709856.80,
  "long_liq_volume": 0.045,
  "long_liq_notional_usd": 3073.65,
  "long_liq_count": 2,
  "short_liq_volume": 0.012,
  "short_liq_notional_usd": 819.37,
  "short_liq_count": 1,
  "trade_count": 342
}
```

**For CVD and OI cumulative delta**, two options:
- **Option A:** Frontend computes from per-candle deltas (fetches history and sums client-side)
- **Option B:** Backend accumulates and serves directly

**Historical backfill:** For footprint bars, the frontend needs historical per-candle data matching the visible chart window. Two sub-gaps:

### GAP-2a: Current-Minute Candle Metrics (Live)
- Just expose `symbol_volumes` and `oi_by_symbol` delta
- **Difficulty:** **LOW** — read existing in-memory state, format JSON

### GAP-2b: Historical Candle Metrics (Per-Minute Buffer)
- Need per-symbol ring buffer of completed candle metrics (last N minutes)
- Populate at minute boundary when `symbol_volumes` resets (capture before reset)
- **Difficulty:** **MEDIUM** — new deque per symbol, populate in `_check_minute_rollover()`, new history endpoint

### GAP-2c: CVD 24h Accumulator
- Running sum of `buy_vol - sell_vol` over sliding 24h window
- Needs 1440-entry ring buffer per symbol
- **Difficulty:** **MEDIUM** — new accumulator + endpoint

### GAP-2d: OI Cumulative Delta Accumulator
- Running sum of OI deltas from session start (or 24h window)
- Needs per-symbol accumulator
- **Difficulty:** **MEDIUM** — new accumulator + endpoint

---

### GAP-3: Mark Price / Funding Rate

**What the frontend needs:** Current mark price and funding rate per symbol for chart header display and indicator overlays.

**Current state:**
- Mark price: Received via Binance `markPrice@1s` stream for BTC/ETH/SOL. Stored in `self.symbol_prices[sym]["mark"]`. **No API endpoint.**
- Funding rate: Extracted in `_process_funding()` from markPrice stream. Stored in `self.state.perp_funding` (BTC only). **No API endpoint.**

**Proposed endpoint:**
```
GET /market_data?symbol=BTC
```

**Proposed response schema:**
```json
{
  "symbol": "BTC",
  "mark_price": 68281.15,
  "last_price": 68279.50,
  "funding_rate": 0.000125,
  "funding_interval_hours": 8,
  "next_funding_ts": 1772524800,
  "open_interest": 987654.32,
  "oi_usd": 67456789012.00,
  "ts": 1772508300.0
}
```

**Difficulty:** **LOW** — data already in memory, just needs endpoint. Funding rate for ETH/SOL may need extension (currently BTC-only in `self.state`).

---

### GAP-4: Trader Ratios (Long/Short Ratios)

**What the frontend needs:** Top trader account ratio, top trader position ratio, global account ratio — standard sentiment indicators.

**Current state:**
- Polled from Binance REST every 60s by `BinanceRESTPollerThread` in `rest_pollers.py`
- Stored in `self.state.tta_ratio`, `self.state.ttp_ratio`, `self.state.gta_ratio` (BTC only)
- **No API endpoint whatsoever.**

**Proposed endpoint:**
```
GET /trader_ratios?symbol=BTC
```

**Proposed response schema:**
```json
{
  "symbol": "BTCUSDT",
  "ts": 1772508300.0,
  "top_trader_accounts": {
    "long_ratio": 0.5234,
    "short_ratio": 0.4766,
    "long_short_ratio": 1.098
  },
  "top_trader_positions": {
    "long_ratio": 0.5891,
    "short_ratio": 0.4109,
    "long_short_ratio": 1.433
  },
  "global_accounts": {
    "long_ratio": 0.4912,
    "short_ratio": 0.5088,
    "long_short_ratio": 0.965
  }
}
```

**Note:** Currently BTC-only from Binance. ETH/SOL would need additional REST polling.

**Difficulty:** **LOW** for BTC (data exists in `self.state`). **MEDIUM** for ETH/SOL (need to extend `BinanceRESTPollerThread`).

---

### GAP-5: OHLC Candle Data

**What the frontend needs:** OHLC candle data for the main chart. Currently frontend likely fetches from exchanges directly, but having backend-served candles aligned with the heatmap timeline would be ideal.

**Current state:**
- BTC: Primary OHLC from orderbook depth mid-price, secondary from markPrice stream. Stored in `self.state.perp_open/high/low/close` (minute-level).
- ETH/SOL: OHLC from markPrice stream. Stored in `self.symbol_prices[sym]["open/high/low/close"]`.
- Written per-minute to `plot_feed.jsonl` (all symbols: `{symbol, ts, minute, o, h, l, c, long_zones, short_zones}`).
- **No API endpoint.**

**Proposed endpoint:**
```
GET /candles?symbol=BTC&minutes=720
```

**Proposed response schema:**
```json
{
  "symbol": "BTC",
  "interval": "1m",
  "candles": [
    {"ts": 1772508300000, "o": 68250.0, "h": 68300.0, "l": 68200.0, "c": 68281.15}
  ]
}
```

**Historical backfill:** Would need per-symbol ring buffer (same infrastructure as GAP-2b).

**Difficulty:** **MEDIUM** — need to capture OHLC before minute reset and store in ring buffer. Current code discards OHLC data at each minute boundary.

---

### GAP-6: Calibrator Diagnostics

**What the frontend needs (advanced/diagnostic panel):** Per-symbol calibrator health for monitoring.

**Current state:**
- Rich calibrator stats exist in `CalibrationStats` dataclass but only partially exposed via `/liq_stats` stats.
- Key unexposed fields: `leverage_hits{}`, `leverage_totals{}`, `band_counts{}`, `implied_band_counts{}`, `bias_pct_long/short`, `minutes_since_calibration`, `calibration_enabled`, `current_weights[]`.

**Proposed endpoint:**
```
GET /calibrator_status?symbol=BTC
```

**Proposed response schema:**
```json
{
  "symbol": "BTC",
  "calibration_enabled": true,
  "minutes_since_calibration": 12,
  "window_minutes": 15,
  "total_events": 456,
  "events_in_window": 45,
  "snapshots_count": 12,
  "current_weights": {"5": 0.10, "20": 0.18, "25": 0.27, "50": 0.30, "75": 0.15, "100": 0.08, "125": 0.02},
  "bias_pct_long": 0.001,
  "bias_pct_short": -0.0005,
  "band_counts": {"NEAR": 123, "MID": 234, "FAR": 99},
  "hit_rate": 0.12,
  "last_calibration_ts": 1772507400.0
}
```

**Difficulty:** **LOW** — all data in memory, just needs endpoint and `calibrator.get_detailed_stats()`.

---

## 3. Gaps: Existing Endpoints Missing Data

### GAP-E1: `/oi` Missing Delta and Historical Data

**Current response:** `aggregated_oi`, `per_exchange{}`, `ts`

**Missing fields:**
- `oi_delta` — change since previous reading (15s polling interval)
- `oi_delta_pct` — percentage change
- `previous_oi` — previous value for client-side delta computation
- `oi_usd` — OI in USD (currently base asset only; frontend must multiply by price)

**Fix:** Extend `/oi` response with delta fields. Requires storing previous OI in `OIPollerThread` (or read `prev_oi_by_symbol` from viewer).

**Difficulty:** **LOW**

---

### GAP-E2: `/liq_heatmap_v2` Pool Objects Missing Source Info

**Current pool schema:**
```json
{"price": 66780.0, "price_low": 66780.0, "price_high": 66780.0,
 "notional_usd": 103212.54, "intensity": 1.0, "bucket_count": 1}
```

**Missing fields:**
- `source` — "tape", "inference", or "combined" (indicates signal quality; combined zones have 56.5% sweep rate vs 0% for pure inference)
- `side` — "long" or "short" (currently only inferred from which array the pool is in)

The V3 zone endpoint (`/liq_zones`) already serves `source` and `side` per zone. The V2 clustered pools lack this.

**Fix:** Add `source` field to `LiquidityPool` in `liq_heatmap.py` `get_api_response_display()`.

**Difficulty:** **LOW** — source attribution already computed at cluster level for the boost logic.

---

### GAP-E3: `/liq_heatmap_v2` Missing Exchange Breakdown

For the frontend's per-exchange liquidation coloring, the heatmap pools need per-exchange contribution. Currently all exchanges are merged at the normalizer level.

**Missing fields per pool:**
- `exchange_breakdown` — e.g. `{"binance": 0.6, "bybit": 0.3, "okx": 0.1}` (fraction of pool notional from each exchange)

**Fix:** Requires propagating `exchange` field through the tape bucket accumulation path. Currently `LiquidationTape.on_force_order()` does not record which exchange each event came from.

**Difficulty:** **MEDIUM** — needs schema change in `LiqBucket` + propagation through clustering.

---

### GAP-E4: `/liq_stats` Missing Per-Minute Volume Data

The `stats` object in V2 snapshots has aggregate totals but no per-minute breakdown. Frontend footprint bars need per-candle data.

**Missing:** Per-minute trade volume, delta, liquidation volume breakdown. This is covered by GAP-2 (new endpoint).

---

### GAP-E5: `/health` Missing Exchange Connection Quality

**Current:** Per-symbol buffer/file availability only.

**Missing:**
- Per-exchange connection status (connected/disconnected/backoff)
- Last message timestamp per exchange per stream type
- Reconnect count / error count
- Backoff state

**Fix:** Expose `exchange_last_seen` dict (already computed in viewer) and WS connector status.

**Difficulty:** **LOW** — data exists in `processor.exchange_last_seen`, just needs routing to health endpoint.

---

## 4. Recommended Implementation Order

Ordered by frontend priority (what blocks the most user-visible features) and dependency chain.

### Tier 1: Critical Path (Blocks Core Visualizations)

| Priority | Gap | Endpoint | Difficulty | Why First |
|----------|-----|----------|-----------|-----------|
| **P0** | GAP-1 | `GET /liq_events` | LOW | Blocks liquidation bubbles, tape display, and per-candle liq volumes. ~50 lines. No schema changes to existing code. |
| **P1** | GAP-2a | `GET /candle_metrics` | LOW | Blocks all 9 footprint bar stats. Live current-minute data only. ~40 lines. |
| **P2** | GAP-E1 | Extend `GET /oi` | LOW | Blocks OI delta display. Add 3 fields to existing response. ~15 lines. |
| **P3** | GAP-3 | `GET /market_data` | LOW | Blocks chart header price/funding display. Data already in memory. ~30 lines. |

### Tier 2: Enables Full Feature Set

| Priority | Gap | Endpoint | Difficulty | Why Next |
|----------|-----|----------|-----------|----------|
| **P4** | GAP-2b | `GET /candle_metrics_history` | MEDIUM | Enables historical footprint bars. Needs per-symbol ring buffer filled at minute rollover. ~100 lines. |
| **P5** | GAP-5 | `GET /candles` | MEDIUM | Enables backend-served OHLC. Same ring buffer infrastructure as P4. ~80 lines. Can share buffer. |
| **P6** | GAP-E2 | Extend `/liq_heatmap_v2` pools | LOW | Adds source field to pools for signal quality indication. ~20 lines. |
| **P7** | GAP-4 | `GET /trader_ratios` | LOW | Enables long/short ratio display. BTC data already polled. ~25 lines. |

### Tier 3: Completeness & Advanced Features

| Priority | Gap | Endpoint | Difficulty | Why Later |
|----------|-----|----------|-----------|-----------|
| **P8** | GAP-2c | CVD 24h accumulator | MEDIUM | Enables CVD indicator. Needs 1440-entry ring buffer. ~80 lines. |
| **P9** | GAP-2d | OI cumulative delta | MEDIUM | Enables cumulative OI delta indicator. Same buffer pattern as P8. ~60 lines. |
| **P10** | GAP-E3 | Exchange breakdown in pools | MEDIUM | Enables per-exchange liq coloring. Needs LiqBucket schema change. ~100 lines. |
| **P11** | GAP-6 | `GET /calibrator_status` | LOW | Diagnostic panel only. ~40 lines. |
| **P12** | GAP-E5 | Extend `/health` with exchange quality | LOW | Monitoring only. ~30 lines. |

### Implementation Notes

**Shared infrastructure for P4/P5/P8/P9:** All four require per-symbol ring buffers of per-minute data. Build a single `MinuteMetricsBuffer` class:
```python
class MinuteMetricsBuffer:
    def __init__(self, maxlen=1440):  # 24 hours
        self._buffer: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add_minute(self, data: dict): ...
    def get_recent(self, minutes: int) -> List[dict]: ...
```
Populate once per minute in `_check_minute_rollover()` BEFORE resetting `symbol_volumes`. This gives P4, P5, P8, and P9 their data source.

**GAP-1 (liq_events) implementation detail:** The `exchange` field from `CommonLiqEvent` is available in `_process_liquidations()` but is NOT passed through `engine_manager.on_force_order()`. For the events buffer, capture directly in `_process_liquidations()` before the engine routing call — append `{ts, symbol, side, price, qty, notional_usd, exchange}` to a per-symbol deque.

**Thread safety:** All new endpoint data must follow existing patterns:
- Write under lock in main thread
- Copy-on-read under lock in API thread
- Same as `_display_lock` pattern for `symbol_prices`

---

## Appendix A: JSONL Log Files (Data Already Captured on Disk)

These files capture rich per-event data that could theoretically be served via API, but are currently only for manual inspection/debugging.

| File | Schema (sampled) | Events |
|------|-----------------|--------|
| `liq_tape_{SYM}.jsonl` | `{type: "forceOrder", ts, symbol, side, price, qty, notional, bucket, entry_est}` | BTC: 13,364; ETH: 11,548; SOL: 7,006 |
| `liq_calibrator_{SYM}.jsonl` | `{type: "event", timestamp, symbol, side, event_price, event_qty, event_notional, ...40+ calibrator fields}` | Rich per-event attribution data |
| `liq_calibrator_{SYM}.jsonl` | `{type: "minute_inputs", minute_key, src, high, low, close, buy_vol, sell_vol, oi_change}` | Per-minute OHLCV + OI |
| `liq_inference_{SYM}.jsonl` | `{type: "inference", ts, minute_key, src_price, oi_delta, buy_pct, inferences: [{side, size_usd, projected_liq, confidence}]}` | Per-inference event with projections |
| `liq_sweeps_{SYM}.jsonl` | `{type: "sweep", ts, minute_key, side, bucket, total_notional_usd, trade_count, peak_notional_usd, layer, reason, trigger_high}` | Sweep events |
| `liq_zones_{SYM}.jsonl` | `{type: "zone_created/swept/expired", ts, side, price, weight, source, final_weight, lifespan_min, reinforcement_count}` | Zone lifecycle |
| `liq_cycles_{SYM}.jsonl` | `{type: "cycle_summary", ts, minute_key, total_force_orders, total_inferences, active_zones_long/short, ...}` | Per-minute cycle stats |
| `liq_debug_{SYM}.jsonl` | `{type: "minute_inference/combined_zone_boost", ...normalization metrics...}` | Debug/diagnostic |
| `plot_feed.jsonl` | `{symbol, ts, minute, o, h, l, c, long_zones, short_zones}` | Per-symbol OHLC for plotter |

---

## Appendix B: Internal Data Not Exposed (Complete Inventory)

| Category | Data | Location | Type | Potential Use |
|----------|------|----------|------|---------------|
| Calibrator | `events_window` (LiquidationEvent deque) | `liq_calibrator.py` | RAM, ~30 min | Individual event access |
| Calibrator | `stats.leverage_hits/totals/notional` | `liq_calibrator.py` | RAM, per-window | Tier performance monitoring |
| Calibrator | `bias_pct_long/short` | `liq_calibrator.py` | Persisted in weights file | Directional bias display |
| Calibrator | `calibration_enabled` flag | `liq_calibrator.py` | RAM | Health monitoring |
| Tape | `events` deque (maxlen=7200) | `liq_tape.py` | RAM, ~12h | Individual event access |
| Tape | `offset_samples` per-leverage | `liq_tape.py` | RAM | Offset learning display |
| Inference | `projected_long/short_liqs` dicts | `entry_inference.py` | RAM (lock-protected) | Raw projection display |
| Inference | `leverage_weights` per-tier | `entry_inference.py` | RAM | Weight visualization |
| Inference | `_normalization_debug` | `entry_inference.py` | RAM, ephemeral | Heatmap balance metrics |
| Zones | `tier_contributions` per-zone | `active_zone_manager.py` | Persistent (JSON) | Already in `/liq_zones` |
| Viewer | `symbol_volumes` | `full_metrics_viewer.py` | RAM, per-minute reset | Volume metrics |
| Viewer | `symbol_prices` (OHLC) | `full_metrics_viewer.py` | RAM, per-minute reset | Candle data |
| Viewer | `taker_aggression` history | `full_metrics_viewer.py` | RAM, 10-min deque | Aggression indicator |
| Viewer | `liq_counts` per-exchange | `full_metrics_viewer.py` | RAM | Exchange liq breakdown |
| Viewer | `exchange_last_seen` | `full_metrics_viewer.py` | RAM | Connection health |
| OI Poller | `snapshots` per-symbol | `oi_poller.py` | RAM | Current OI (partially exposed) |
| REST Poller | `tta/ttp/gta_ratio` | `rest_pollers.py` → `state` | RAM | Trader sentiment |
| OB Recon | Full reconstructed orderbook | `ob_heatmap.py` | RAM | Raw depth access |
| WS | Connection state, backoff counters | `ws_connectors.py` | RAM | Connection monitoring |
