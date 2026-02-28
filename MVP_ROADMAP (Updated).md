# TradeNet Quantum Terminal — MVP Roadmap
## Updated: Feb 27, 2026 (End of Session)

---

## GOAL
260 people waiting. $13/mo. $36/mo infrastructure. Ship a professional terminal with predictive liquidation heatmaps nobody else offers.

---

## COMPLETED — 46 tasks + 6 bug fixes

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

### Phase A: Heatmap Quality ✅ (6 tasks)

| # | Task | Status | Notes |
|---|------|--------|-------|
| A1 | Partial sweep reduction | ✅ | `SWEEP_REDUCTION_FACTOR=0.70`, `SWEEP_MIN_BUCKET_USD=100` |
| A2 | OI reduction cap | ✅ | `MAX_OI_REDUCTION_PER_MINUTE=0.25` |
| A3a | Combined-source bucket-level boost | ✅ vestigial | Deployed, never fires — $430 structural gap between tape/inference buckets. Dead code kept for now. |
| A3a-diag | Near-overlap diagnostic logging | ✅ | Confirmed bucket-level wrong abstraction. $430 avg, 21.5 steps apart. |
| A3a-v2 | Combined-source CLUSTER-level boost | ✅ deployed, confirmed firing | Boost at clustered pool level. First combined cluster detected: short $66,140–$67,340. `cluster_combined: 1/6 (16.67%)`. Soaking for more data. |
| A3a-v2 diag | Cluster-level diagnostic fields | ✅ | cluster_total, cluster_combined, cluster_combined_pct, example_boosted_clusters logged every 10 min. |

### Codebase Audit — Sprint 1: Production Safety ✅ (8 fixes)

| # | Fix | Status |
|---|-----|--------|
| C1 | BTC depth fallback (markPrice when OB stale >60s) | ✅ |
| C2 | `write_json_atomic()` error logging (no more bare except:pass) | ✅ |
| C3 | Shutdown cleanup (`heatmap_v2.close()` for all engines) | ✅ |
| C5 | OB endpoint `step` validation (400 on step≤0) | ✅ |
| H1 | History deserialization logging (corrupt frames counted) | ✅ |
| H3 | OI poller thread lock (`_snapshot_lock`) | ✅ |
| H5 | `/liq_stats` cache mutation fix (`deepcopy`) | ✅ |
| H2-lite | WebSocket error logging (9 except blocks, all 3 connectors) | ✅ |

### Codebase Audit — Sprint 2: Operational Visibility ✅ (9 fixes)

| # | Fix | Status |
|---|-----|--------|
| C4 | Calibrator event drop warning (rate-limited) | ✅ |
| H2-full | WebSocket exponential backoff (1s→30s, jitter, reset on success) | ✅ |
| H4 | `/oi` warmup returns 503 (not 404) | ✅ |
| H6 | Thread join on shutdown (OI poller + REST poller) | ✅ |
| H7 | OB heatmap grid fix (Rule #16, index-based) | ✅ |
| H9 | API refresh thread joined on shutdown | ✅ |
| M1 | EntryInference projection lock (copy-on-read) | ✅ |
| M2 | OI poller skip-on-total-failure (no stale 0.0) | ✅ |
| M8 | API bind address configurable (`API_HOST` env var) | ✅ |

---

## CURRENT ENGINE STATE (verified Feb 27, 2026)

- **Backend:** 3 exchanges (Binance/Bybit/OKX), 3 symbols (BTC/ETH/SOL), real per-symbol liq heatmaps
- **OI:** 3-exchange aggregation (Binance 79k + Bybit 45k + OKX 30k BTC = 154k total)
- **Calibrator:** healthy, event drop warning added, 5 force orders in first 20s of latest run
- **Heatmap:** tape + inference producing data, independent normalization working
- **A3a-v2:** cluster-level boost confirmed firing (1/6 clusters combined at 16.67%)
- **Zone Manager:** zones creating, reinforcing, sweeping, expiring normally
- **Audit status:** 17/29 findings fixed (Sprint 1 + Sprint 2). Sprint 3 (code quality) pending. Second-pass audit running.
- **Display/logic boundary:** verified clean by 4 independent auditors

---

## WHAT'S LEFT — Ordered by Launch Impact

---

## PHASE A (remaining): Heatmap Quality

| # | Task | Status | Notes |
|---|------|--------|-------|
| A5 | Verification soak test | ⬜ NEXT | After overnight soak with A3a-v2. Hard acceptance criteria below. |
| A4 | TAU_PCT_BASE tuning | ⬜ CONDITIONAL | Only if A1-A3 + soak still insufficient |

**A5 acceptance criteria (hard gate — no endless tuning):**
- Median combined-zone lifespan > 20 minutes
- Combined-zone swept rate > 40%
- < 30% of strong zones (>$50k notional) disappearing before first price touch
- Visible strong zones survive minor wicks
- Subjectively: "variable but sticky," not "bands chasing price"

---

## AUDIT: Sprint 3 — Code Quality (before beta)

| # | ID | Finding | Fix |
|---|-----|---------|-----|
| 1 | M4 | Factory default ETH step mismatch (2.0 vs 1.0) | Align constant |
| 2 | M5 | Magic `weight * 100000` in V3 zone heatmap | Extract to named constant |
| 3 | M6 | API cache keys use raw symbol before normalization | Normalize before cache key |
| 4 | M7 | Calibrator tolerance docs say 5, code enforces 4 | Align CLAUDE.md |
| 5 | M9 | `active_zone_manager.__del__` bare except | Add logging |
| 6 | M10 | `ob_heatmap.py` unconditional `print()` | Replace with logger |
| 7 | M11 | Silent exception swallowing in 4+ locations | Add logging |
| 8 | M12 | `DEFAULT_STEP` constant shadowing | Rename or remove import |
| 9 | M13 | Binary header comments say 48 bytes, struct is 44 | Fix comments |
| 10 | D1-D6 | CLAUDE.md updates (6 items) | Batch update |
| 11 | L1-L4 | Dead code, unused imports, orphaned files | Batch cleanup |

---

## PHASE B: Visual Polish — Ship Quality

**Launch blockers:**

| # | Task | Impact | Repo |
|---|------|--------|------|
| B1 | Colored cell backgrounds on footprint | Single biggest visual upgrade — professional footprint chart | Frontend |
| B4 | Fix CVD + OI CVD accumulation bugs | Data correctness — CVD drift, OI CVD midnight reset | Frontend |
| B5 | Fix bar stats timeframe mismatch | Data correctness — switching timeframes shows stale data | Frontend |
| B6 | Connect liq data to bar stats Short Liq / Long Liq rows | Currently zeros — backend has data, frontend needs to consume | Frontend + Backend |
| B7 | Fix multi-pane heatmap bug | Only one pane displays heatmap at a time | Frontend |

**Nice-to-have (cut if time tight):**

| # | Task | Impact | Repo |
|---|------|--------|------|
| B2 | POC row highlighting | Professional standard | Frontend |
| B3 | K/M number formatting | Readability | Frontend |

---

## PHASE C: Deployment

| # | Task | Notes |
|---|------|-------|
| C1 | Configurable API base URL (frontend) | Hardcoded `127.0.0.1:8899` in `liq_heatmap.rs:15` and `orderbook_heatmap.rs:22` |
| C2 | Deploy backend to DigitalOcean Droplet 2 | No geo-blocking on droplet. `API_HOST=0.0.0.0` (M8 done). |
| C3 | nginx + SSL on backend droplet | Reverse proxy, TLS, rate limiting |
| C3.5 | Route Bybit REST through proxy | Bybit geo-blocked in US. Frontend needs proxy route. Backend on droplet unaffected. |
| C4 | Frontend: build release binary (.exe) | `cargo build --release` |
| C5 | Health checks + monitoring | `/health` exists. Need external uptime check + crash alert. |
| C6 | End-to-end test: frontend → remote backend | All overlays, OI, zones over network |
| C7 | Private beta: 3-5 trusted users | Real users, real feedback, real load |

---

## PHASE D: Brand & First Impression (parallel with B/C)

| # | Task | Notes |
|---|------|-------|
| D1 | TradeNet branding (logo, about) | Logo in header, about screen |
| D2 | Login screen polish | Functional but basic |
| D3 | Color scheme refinement | Dark theme needs professional polish |
| D4 | Sidebar cleanup | Remove dev artifacts |
| D5 | Toolbar/header cleanup | Remove debug elements |

---

## LAUNCH 🚀

---

## TASK COUNT

| Phase | Total | Done | Remaining |
|-------|-------|------|-----------|
| Phase 1: Backend Foundation | 9 | 9 | 0 |
| Phase 2: Multi-Exchange | 8 | 8 | 0 |
| Phase 2b: ETH/SOL Parity | 5 | 5 | 0 |
| Phase 3 (partial): Frontend Aggr | 5 | 5 | 0 |
| Bug Fixes | 6 | 6 | 0 |
| Phase A: Heatmap Quality | 8 | 6 | 2 (A5 soak + A4 conditional) |
| Audit Sprint 1 | 8 | 8 | 0 |
| Audit Sprint 2 | 9 | 9 | 0 |
| Audit Sprint 3 | 11 | 0 | 11 |
| Phase B: Visual Polish | 7 | 0 | 7 (5 blockers + 2 nice-to-have) |
| Phase C: Deployment | 8 | 0 | 8 |
| Phase D: Brand | 5 | 0 | 5 |
| **TOTAL** | **89** | **56** | **33** (28 without conditional + nice-to-haves) |

**Progress: 63% complete. ~28 tasks to launch.**

---

## DISPLAY vs LOGIC BOUNDARY (architectural invariant)

Verified clean by 4 independent auditors (1 Claude Code + 3 ChatGPT Codex).

**Raw (logic/calibrator/zones):**
- `get_combined_heatmap()` → zone logic, calibrator input, diagnostic logging
- `get_heatmap()` → calibrator `on_minute_snapshot` (line ~1741), `EngineManager.get_snapshot()`
- `get_api_response()` → called only by display wrapper internally

**Display (snapshots/UI only):**
- `get_combined_heatmap_display()` → bucket-level boost (vestigial, never fires)
- `get_heatmap_display()` → V1/V2 snapshot intensity arrays
- `get_api_response_display()` → V2 snapshot + cluster-level boost (A3a-v2)

**Rule: future logic consumers MUST call raw versions. NEVER the display wrappers.**

---

## KNOWN ISSUES (tracked, not blocking launch)

| Issue | Severity | Notes |
|-------|----------|-------|
| Bybit geo-blocked without VPN | DEV ONLY | Droplet has no geo-restrictions. Germany VPN for local dev. |
| Proxy latency (~1.2s/kline) | LOW | Frontend kline loading slow through proxy. Direct Binance 399ms. Phase C. |
| depth_band missing for ETH/SOL | DEFERRED | Needs per-symbol OB engines. Not MVP. |
| Hyperliquid excluded | DEFERRED | No public liquidation WebSocket stream. |
| 10x leverage tier disabled | KNOWN | -$62K median miss. Excluded from calibration. Monitor post-launch. |
| Agent signals disabled | DEFERRED | Re-enable post-launch. |
| A3a bucket-level boost vestigial | COSMETIC | Never fires. Dead code cleanup in Sprint 3. |
| Second-pass audit running | IN PROGRESS | Results pending. |

---

## POST-LAUNCH

**Phase E:** Feature depth — shared data fetching, data service layer, OB noise filter, OHLC bar, candle spacing, volume profile labels, bar stats colors, bid/ask flip, DoM/T&S fixes.

**Phase F:** Aggregated OB heatmap for ETH/SOL — per-symbol orderbook engines.

**Phase G:** ML/AI integration — regime classification → cascade probability → smart alerts.

**Phase H:** Competitive features — stacked imbalances, OI candles, liq bubbles, absorption, synced cursors, TPO, VWAP, alerts, multi-layout.

---

## ARCHITECTURE QUICK REFERENCE

**Backend** (`moonstreamprocess`): Python, FastAPI. Entry: `poc/full_metrics_viewer.py --api`. Port 8899.
- EngineManager → per-symbol EngineInstance (BTC, ETH, SOL)
- Each: LiquidationCalibrator + LiquidationHeatmap (LiquidationTape + EntryInference) + ActiveZoneManager
- BTC additionally: OrderbookReconstructor + OrderbookAccumulator
- 3 exchanges: Binance, Bybit, OKX. Normalized via `liq_normalizer.py`
- OI polled every 15s from 3 exchanges, aggregated per symbol
- API_HOST env var for bind address (default 127.0.0.1, set 0.0.0.0 for production)

**Frontend** (`tradenet-terminal`): Rust, Iced 0.13+. Cargo workspace.
- Canvas-based rendering, GPU-accelerated via wgpu
- Liq heatmap: 1s live + 10s history poll to backend API
- OB heatmap: 30s live + 60s history poll
- Klines/trades: Binance via proxy.tradenet.org
- Aggregated tickers: AGGREGATED:BTCUSDT, AGGREGATED:ETHUSDT, AGGREGATED:SOLUSDT

**Infrastructure:** $36/mo. 3 subscribers covers it. 260 waiting.

**Commands:**
- Start backend: `cd poc && python full_metrics_viewer.py --api`
- Start frontend: `cargo run` (dev) or `cargo build --release` (prod)
- Check health: `curl http://127.0.0.1:8899/health`
- Check boost: `Select-String -Path "poc\liq_debug_BTC.jsonl" -Pattern "combined_zone_boost" | Select-Object -Last 1`
