# TradeNet Terminal — MVP Roadmap
## Updated: Feb 27, 2026 (Session 2)

---

## GOAL
260 people waiting. $13/mo. $36/mo infrastructure. Ship a professional terminal with predictive liquidation heatmaps nobody else offers.

---

## COMPLETED — 38 tasks + 6 bug fixes

### Phase 1: Backend Foundation ✅ (9 tasks)
Core engine architecture, EngineManager, per-symbol calibrator + heatmap + zone manager pipeline, embedded API server, snapshot pipeline, atomic writes.

### Phase 2: Multi-Exchange Aggregation ✅ (8 tasks)
Binance, Bybit, OKX WebSocket connectors. Liquidation normalizer (`liq_normalizer.py`). Multi-exchange OI poller. Per-symbol routing. Dynamic liquidation routing.

### Phase 2b: ETH/SOL Engine Parity ✅ (5 tasks)
Per-symbol engines for ETH/SOL. Per-symbol snapshot files, history buffers, binary files, log files. API serving per-symbol with validation. OB endpoints reject non-BTC.

### Phase 3 (partial): Frontend Aggregation ✅ (4 tasks + 1 bug fix)
AGGREGATED tickers in screener. Binance klines as reference for aggregated pairs. aggr-server WebSocket for trade data.

### Bug Fixes ✅ (6 resolved)
SOL FP grid (floating-point drift), BTC OI unit mismatch (base→USD conversion), tape/inference normalization (independent p99 scaling), calibrator entropy false positive (disabled tier exclusion), disabled tier weight drift (enforce after normalization), frontend live update stream fix.

### Heatmap Tuning ✅ (3 tasks)

| # | Task | Status | Constants |
|---|------|--------|-----------|
| A1 | Partial sweep reduction | ✅ | `SWEEP_REDUCTION_FACTOR=0.70`, `SWEEP_MIN_BUCKET_USD=100` |
| A2 | OI reduction cap | ✅ | `MAX_OI_REDUCTION_PER_MINUTE=0.25` |
| A3a | Combined-source display boost (bucket-level) | ✅ deployed, ⚠️ not firing | `COMBINED_ZONE_BOOST=1.5`, `COMBINED_SOURCE_EPS=1e-4` |
| A3a-diag | Near-overlap diagnostic logging | ✅ | Confirmed: $430 avg distance between tape/inference buckets (21.5 steps) |

**A3a finding:** Bucket-level boost never fires because tape and inference operate in structurally different price regions (tape records at liquidation price, inference projects from entry price at leverage offsets). The original 56.5% sweep rate diagnostic was measured at the CLUSTER level. A3a-v2 moves the boost to cluster-level matching.

### Current Engine State (verified Feb 27, 2026):
- Backend: real per-symbol liq heatmaps for BTC/ETH/SOL from 3 exchanges (Binance/Bybit/OKX)
- Frontend: AGGREGATED tickers with live heatmaps and aggregated OI
- Calibrator: healthy (entropy fix, disabled tier fix, weights saving, events counting)
- OI: 3-exchange aggregation working (Binance 79k + Bybit 45k + OKX 28k BTC)
- Heatmap: tape producing data (9 buckets, 77K raw max), inference producing data (110 buckets, 1.2M raw max), independent normalization working (combined_max=0.65)
- Zone Manager: zones creating, reinforcing, sweeping, expiring normally (73 created, 15 reinforced, 2 swept, 15 expired, 23 merged in 45-min window)
- Known: multi-pane heatmap bug (only one pane can display at a time)
- Known: Bybit geo-blocked without VPN to non-US country (Germany works for both Binance + Bybit)

---

## WHAT'S LEFT — Ordered by Launch Impact

### Rule: The liquidation heatmap IS the product. Everything else supports it.

---

## PHASE A: Heatmap Quality ← ACTIVE NOW

| # | Task | Status | Notes |
|---|------|--------|-------|
| A1 | Partial sweep reduction | ✅ | `SWEEP_REDUCTION_FACTOR=0.70` |
| A2 | Cap OI-based reduction | ✅ | `MAX_OI_REDUCTION_PER_MINUTE=0.25` |
| A3a | Combined-source bucket boost | ✅ deployed | Not firing — $430 structural gap between tape/inference buckets |
| A3a-diag | Near-overlap diagnostics | ✅ | Confirmed bucket-level approach is wrong abstraction |
| A3a-v2 | Combined-source CLUSTER-level boost | ⬜ NEXT | Move boost to clustered pool level in display path. Check if cluster [p_low, p_high] range contains both tape and inference buckets. Side-specific. Display-only wrapper (get_api_response_display). Prompt drafted, pending Claude Code. |
| A5 | Verification soak test | ⬜ | After A3a-v2. Hard acceptance criteria below. |
| A4 | TAU_PCT_BASE tuning | ⬜ CONDITIONAL | Only if A1-A3 + soak still insufficient |

**A5 acceptance criteria (no endless tuning):**
- Median combined-zone lifespan > 20 minutes
- Combined-zone swept rate > 40%
- < 30% of strong zones (>$50k notional) disappearing before first price touch
- Visible strong zones survive minor wicks
- Subjectively: "variable but sticky," not "bands chasing price"

**Display-only boundary (architectural invariant):**
- `get_combined_heatmap()` / `get_heatmap()` / `get_api_response()` = raw, used by calibrator + zone logic
- `get_combined_heatmap_display()` / `get_heatmap_display()` / `get_api_response_display()` = display wrappers with boost, used by V1/V2 snapshot writes only
- Calibrator at line ~1741 of `full_metrics_viewer.py` MUST always use raw `get_heatmap()`

---

## PHASE B: Visual Polish — Ship Quality

**Launch blockers:**

| # | Task | Impact | Repo |
|---|------|--------|------|
| B1 | Colored cell backgrounds on footprint | Single biggest visual upgrade — makes the footprint chart look professional | Frontend |
| B4 | Fix CVD + OI CVD accumulation bugs | Data correctness — CVD may drift, OI CVD midnight reset needs verification | Frontend |
| B5 | Fix bar stats timeframe mismatch | Data correctness — switching timeframes may show stale/wrong data | Frontend |
| B6 | Connect liq data to bar stats Short Liq / Long Liq rows | Currently shows zeros — backend has data via `/liq_zones`, frontend needs to consume it | Frontend + Backend coordination |
| B7 | Fix multi-pane heatmap bug | Users expect to see heatmaps on multiple panes simultaneously — currently only one pane displays | Frontend |

**Nice-to-have (cut if time is tight):**

| # | Task | Impact | Repo |
|---|------|--------|------|
| B2 | POC row highlighting | Professional standard — highlights point of control on footprint | Frontend |
| B3 | K/M number formatting | Readability — format large numbers as 1.2K, 3.4M instead of raw integers | Frontend |

---

## PHASE C: Deployment

| # | Task | Notes |
|---|------|-------|
| C1 | Configurable API base URL (frontend) | Currently hardcoded `http://127.0.0.1:8899/v2` in `liq_heatmap.rs:15` and `orderbook_heatmap.rs:22`. Must support `api.tradenet.org:8899` for production. Also switch from `/v2/` aliases to clean paths. |
| C2 | Deploy backend to DigitalOcean Droplet 2 | `poc/full_metrics_viewer.py --api`. Droplet has no geo-blocking — all 3 exchanges connect clean. |
| C3 | nginx + SSL on backend droplet | Reverse proxy, TLS termination, rate limiting. |
| C3.5 | Route Bybit REST traffic through proxy server | Bybit is geo-blocked in US. Frontend Bybit klines/tickers need proxy.tradenet.org route (same pattern as Binance). Backend on droplet is unaffected. |
| C4 | Frontend: build release binary (.exe) | `cargo build --release`. Windows target. |
| C5 | Health checks + basic monitoring | Backend `/health` endpoint exists. Need external monitoring (uptime check, alert on crash). |
| C6 | End-to-end test: frontend binary → remote backend | Verify all heatmap overlays, OI endpoint, zone endpoints work over network. |
| C7 | Private beta: 3-5 trusted users on remote backend | Real users, real feedback, real load testing. |

---

## PHASE D: Brand & First Impression (parallel with B/C)

| # | Task | Notes |
|---|------|-------|
| D1 | TradeNet branding (logo, about) | Logo in terminal header, about screen. |
| D2 | Login screen polish | Currently functional but basic. Animated login exists. |
| D3 | Color scheme / dark theme refinement | Dark theme exists but may need polish for professional look. |
| D4 | Sidebar cleanup | Remove dev artifacts, clean navigation. |
| D5 | Toolbar/header cleanup | Remove debug elements, clean layout. |

---

## LAUNCH 🚀
After A-D: professional terminal, predictive liq heatmaps, 3 symbols, 3 exchanges, deployed.

---

## TASK COUNT SUMMARY

| Phase | Total | Done | Remaining |
|-------|-------|------|-----------|
| Phase 1: Backend Foundation | 9 | 9 | 0 |
| Phase 2: Multi-Exchange | 8 | 8 | 0 |
| Phase 2b: ETH/SOL Parity | 5 | 5 | 0 |
| Phase 3 (partial): Frontend Aggr | 5 | 5 | 0 |
| Bug Fixes | 6 | 6 | 0 |
| Phase A: Heatmap Quality | 7 | 4 | 3 (A3a-v2, A5, A4 conditional) |
| Phase B: Visual Polish | 7 | 0 | 7 (5 blockers + 2 nice-to-have) |
| Phase C: Deployment | 8 | 0 | 8 |
| Phase D: Brand | 5 | 0 | 5 |
| **TOTAL** | **60** | **37** | **23** (20 without conditional + nice-to-haves) |

**At 7 tasks/session pace: 3 sessions to launch.**

---

## KNOWN ISSUES (not blocking launch but tracked)

| Issue | Severity | Notes |
|-------|----------|-------|
| Bybit geo-blocked without VPN | DEV ONLY | Resolves at deployment — DigitalOcean droplet has no geo-restrictions. VPN to Germany works for local dev. |
| Proxy server latency (~1.2s per kline fetch) | LOW | Frontend kline loading slow through proxy.tradenet.org. Direct Binance is 399ms. Investigate proxy hosting/config in Phase C. |
| depth_band missing for ETH/SOL | DEFERRED | Needs per-symbol orderbook engines. Not required for MVP — BTC has full OB pipeline, ETH/SOL work without it. |
| Hyperliquid excluded | DEFERRED | No public liquidation WebSocket stream available. |
| 10x leverage tier disabled | KNOWN | -$62K median miss corruption. Excluded from calibration but events still logged. Monitor for potential re-enable post-launch. |
| Agent signals feature disabled | DEFERRED | Polling commented out. UI code exists. Re-enable post-launch. |
| Tick-based charts panic on visible_timerange() | LOW | `unimplemented!()` for `Basis::Tick`. Avoid tick-based charts or fix in Phase E. |
| TEST_LIQUIDATION_GRADIENT flag in production | COSMETIC | Should be `#[cfg(debug_assertions)]`. Non-functional when false. |

---

## POST-LAUNCH

**Phase E:** Feature depth — aggr-server trades, shared data fetching, data service layer, OB noise filter, OHLC bar, candle spacing, volume profile labels, bar stats colors, bid/ask flip, DoM/T&S fixes.

**Phase F:** Aggregated OB heatmap for ETH/SOL (7 tasks) — requires per-symbol orderbook engines (BinanceConnector depth subs for ETH/SOL, OrderbookReconstructor + OrderbookAccumulator per symbol).

**Phase G:** ML/AI integration — regime classification → cascade probability → smart alerts → premium features.

**Phase H:** Competitive features — stacked imbalances, OI candles, liq bubbles, large order bubbles, absorption, synced cursors, TPO, VWAP, alerts, multi-layout, etc.

---

## ARCHITECTURE QUICK REFERENCE

**Backend** (`moonstreamprocess`): Python, FastAPI. Entry: `poc/full_metrics_viewer.py --api`. Port 8899.
- EngineManager → per-symbol EngineInstance (BTC, ETH, SOL)
- Each: LiquidationCalibrator + LiquidationHeatmap (LiquidationTape + EntryInference) + ActiveZoneManager
- BTC additionally: OrderbookReconstructor + OrderbookAccumulator
- 3 exchanges: Binance, Bybit, OKX. Normalized via `liq_normalizer.py`
- OI polled every 15s from 3 exchanges, aggregated per symbol

**Frontend** (`tradenet-terminal`): Rust, Iced 0.13+. Cargo workspace (root + data/ + exchange/).
- Canvas-based rendering, GPU-accelerated via wgpu
- Liq heatmap: 1s live poll + 10s history poll to backend API
- OB heatmap: 30s live + 60s history poll to backend API
- Klines/trades: Binance via proxy.tradenet.org, aggr-server for aggregated
- Aggregated tickers: AGGREGATED:BTCUSDT, AGGREGATED:ETHUSDT, AGGREGATED:SOLUSDT

**Infrastructure:** $36/mo — 3 subscribers covers it. 260 waiting.

**Start backend:** `cd poc && python full_metrics_viewer.py --api`
**Start frontend:** `cargo run` (dev) or `cargo build --release` (production)

**Rule #1:** Not tested locally → not going to production.
**Rule #2:** The liquidation heatmap is the product.
