# INFRASTRUCTURE.md — TradeNet Server Architecture

Reference document for how the production infrastructure works. Read alongside CLAUDE.md.

---

## Overview

Two DigitalOcean droplets in Frankfurt, Germany (EU IP bypasses Binance US geo-block).

| Droplet | Domain | Role | Cost |
|---------|--------|------|------|
| Droplet 1 | proxy.tradenet.org | Nginx reverse proxy + aggr-server | $12/mo |
| Droplet 2 | api.tradenet.org:8899 | Python backend (liq engine, calibrator, API) | $24/mo |

---

## Droplet 1: Proxy + Aggr-Server

### Nginx Proxy

**Purpose:** Bypass Binance geo-restrictions and cache REST responses for multi-user rate limit protection.

**What it proxies:**

| Type | Frontend Connects To | Proxied To |
|------|---------------------|------------|
| Binance Spot REST | `https://proxy.tradenet.org/spot/api/v3/*` | `https://api.binance.com/api/v3/*` |
| Binance Futures REST | `https://proxy.tradenet.org/futures/fapi/v1/*` | `https://fapi.binance.com/fapi/v1/*` |
| Binance Inverse REST | `https://proxy.tradenet.org/inverse/dapi/v1/*` | `https://dapi.binance.com/dapi/v1/*` |
| Binance Spot WS | `wss://proxy.tradenet.org/ws` | `wss://stream.binance.com/ws` |
| Binance Futures WS | `wss://proxy.tradenet.org/ws-futures` | `wss://fstream.binance.com/ws` |
| Binance Inverse WS | `wss://proxy.tradenet.org/ws-inverse` | `wss://dstream.binance.com/ws` |

**Caching config:**
```nginx
proxy_cache binance_cache;
proxy_cache_valid 200 5s;
```

100 users requesting the same endpoint within 5 seconds = 1 request to Binance, 99 served from cache.

**Rate limit headroom:** Binance allows 300 WebSocket connections per 5 minutes per IP. Won't hit that until 300+ simultaneous users. At that point, add a second proxy IP.

### Aggr-Server

**What it is:** Node.js application based on [Tucsky/aggr-server](https://github.com/Tucsky/aggr-server). Runs as systemd service (`aggr-server`).

**What it does:** Connects to trade WebSocket streams on multiple exchanges simultaneously (Binance, Bybit, OKX, Hyperliquid), aggregates all trades into a unified stream, stores in InfluxDB, and resamples into time-bucketed bars.

**WebSocket endpoint:** `wss://proxy.tradenet.org/aggr/` (proxied through nginx to aggr-server's local port)

**Output format:** Each trade message includes: exchange source, price, quantity, side (buy/sell), timestamp.

**Usage:** ONLY used when user selects an "AGGREGATED" ticker. Single-exchange tickers use the direct proxy-to-Binance path.

**Data retention:** InfluxDB stores all aggregated trade data. No historical backfill — data only exists from when the server started collecting.

**Health check:**
```bash
sudo systemctl status aggr-server
sudo journalctl -u aggr-server --no-pager -n 20
# Should see "resampling X markets" repeating every minute
```

---

## Droplet 2: Python Backend API

**Domain:** api.tradenet.org (port 8899)

**Purpose:** Runs liquidation prediction engine, calibrator, zone manager, and serves all analytical data via FastAPI.

**NOT involved in trade aggregation.** The Python backend has its OWN WebSocket connections to exchanges for forceOrder events and its OWN REST polling for OI data. Completely independent from the aggr-server.

**Entry point:** `poc/full_metrics_viewer.py` with `--api` flag to enable embedded API server.

**systemd config requirements:**
- `WorkingDirectory=/opt/tradenet-backend/poc` (relative file paths)
- API bind address must be `0.0.0.0` not `127.0.0.1`
- OB heatmap background thread must be evaluated for CPU usage on 2-core server

---

## Data Routing — Complete Reference

| Data Type | Single Exchange (e.g. BINANCE:BTCUSDT) | Aggregated (AGGREGATED:BTCUSDT) |
|-----------|----------------------------------------|--------------------------------|
| **Klines / OHLC** | proxy.tradenet.org → Binance REST/WS | proxy.tradenet.org → Binance (price reference — prices identical across exchanges due to arbitrage) |
| **Trade stream** (footprint, volume, delta) | proxy.tradenet.org → Binance aggTrade WS | wss://proxy.tradenet.org/aggr/ → aggr-server (trades from ALL exchanges) |
| **OI data** | proxy.tradenet.org → Binance OI REST | api.tradenet.org:8899 → Python backend (OI summed from 4 exchanges) |
| **Liq heatmap / zones** | api.tradenet.org:8899 → Python backend | api.tradenet.org:8899 → Python backend (forceOrders from all exchanges) |
| **OB heatmap** | api.tradenet.org:8899 → Python backend (Binance orderbook) | api.tradenet.org:8899 → Python backend (aggregated OB — future feature) |

**Key insight:** Klines always come from Binance because prices are virtually identical across exchanges (arbitrage keeps them within a few dollars). Aggregation only matters for VOLUME and TRADE FLOW — that's where you see the full market picture.

---

## Scaling Notes

- **300 users:** Single proxy IP handles it. Nginx caching prevents Binance rate limits.
- **300+ users:** Add a second proxy droplet/IP. Load balance with DNS round-robin or upstream block.
- **Backend scaling:** Response caching at API level (serve same cached response to all users hitting same endpoint within TTL). Current 1s cache refresh is a start.
- **Aggr-server scaling:** Can run in cluster mode — multiple collector instances + one API node. Not needed until data volume exceeds single-node capacity.
- **Cost at MVP:** $36/mo total. One subscriber at $13/mo covers it in 3 months.
