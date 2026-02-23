"""
Liquidation event normalizer — converts raw exchange messages into a common format.

Each exchange sends liquidation/forceOrder events in a completely different
structure with different field names, side conventions, and quantity units.
This module normalizes all of them into CommonLiqEvent before they reach
the EngineManager.

Supported exchanges:
  - Binance: !forceOrder@arr stream (data["o"] nesting)
  - Bybit:   allLiquidation.* topic (data["data"] list, batched at 500ms)
  - OKX:     liquidation-orders channel (data["data"][].details[] nesting)

Side conventions (CRITICAL — each exchange is different):
  - Binance: "BUY" = shorts liquidated, "SELL" = longs liquidated
  - Bybit:   "Buy" = longs liquidated,  "Sell" = shorts liquidated
  - OKX:     "posSide" gives the position side directly ("long"/"short")
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OKX contract values — how many base asset units per 1 contract.
# Source: https://www.okx.com/api/v5/public/instruments?instType=SWAP
#
# These should ideally be fetched from OKX REST API at startup, but
# hardcoded values are fine for MVP with BTC/ETH/SOL.  Add new symbols
# here as needed.
# ---------------------------------------------------------------------------
OKX_CONTRACT_SIZES: Dict[str, float] = {
    "BTC": 0.01,       # 1 contract = 0.01 BTC
    "ETH": 0.1,        # 1 contract = 0.1 ETH
    "SOL": 1.0,        # 1 contract = 1 SOL
    "DOGE": 100.0,     # 1 contract = 100 DOGE
    "XRP": 100.0,      # 1 contract = 100 XRP
}

# Known USDT-pair suffixes to strip when extracting the short symbol.
_USDT_SUFFIXES = ("USDT", "USD", "BUSD")


@dataclass
class CommonLiqEvent:
    """
    Exchange-agnostic liquidation event.

    This is the single format that all exchange-specific messages get
    normalized into before reaching the EngineManager.

    Fields:
        exchange:      Source exchange ("binance", "bybit", "okx")
        symbol_short:  Internal short symbol ("BTC", "ETH", "SOL")
        symbol_full:   Binance-style full symbol ("BTCUSDT")
        side:          Position that was liquidated ("long" or "short")
        price:         Liquidation / bankruptcy price
        qty:           Size in BASE ASSET (e.g. BTC, not contracts)
        notional:      price * qty (USD value)
        timestamp:     Unix timestamp in seconds (float)
        raw:           Original exchange message for debugging
    """
    exchange: str
    symbol_short: str
    symbol_full: str
    side: str
    price: float
    qty: float
    notional: float
    timestamp: float
    raw: dict = field(default_factory=dict, repr=False)


# ---------------------------------------------------------------------------
# Symbol extraction helper
# ---------------------------------------------------------------------------

def _extract_symbol_short(raw_symbol: str, exchange: str) -> Optional[str]:
    """
    Extract the short symbol (e.g. "BTC") from an exchange-specific symbol.

    Handles:
      - Binance / Bybit: "BTCUSDT" → strip "USDT" → "BTC"
      - OKX:             "BTC-USDT-SWAP" → split("-")[0] → "BTC"

    Returns None if the symbol can't be parsed.
    """
    if not raw_symbol:
        return None

    raw_symbol = raw_symbol.strip().upper()

    if exchange == "okx":
        # OKX format: "BTC-USDT-SWAP" or "ETH-USDT-SWAP"
        parts = raw_symbol.split("-")
        if parts:
            return parts[0]
        return None

    # Binance / Bybit format: "BTCUSDT", "ETHUSDT", "SOLUSDT"
    for suffix in _USDT_SUFFIXES:
        if raw_symbol.endswith(suffix):
            short = raw_symbol[: -len(suffix)]
            if short:
                return short

    # Fallback: return as-is if no suffix matched (unusual)
    logger.warning(
        "Could not strip suffix from symbol %r (exchange=%s)", raw_symbol, exchange
    )
    return raw_symbol if raw_symbol else None


def _to_binance_full(symbol_short: str) -> str:
    """Convert short symbol to Binance-style full symbol."""
    return f"{symbol_short}USDT"


# ---------------------------------------------------------------------------
# Binance normalizer
# ---------------------------------------------------------------------------

def normalize_binance(data: dict) -> Optional[CommonLiqEvent]:
    """
    Normalize a Binance forceOrder message into CommonLiqEvent.

    Binance sends:
        data["o"]["s"]  → symbol ("BTCUSDT")
        data["o"]["S"]  → side: "BUY" = shorts liquidated, "SELL" = longs liquidated
        data["o"]["p"]  → liquidation price (string)
        data["o"]["q"]  → quantity in base asset (string)
        data["o"]["T"]  → trade time in ms (optional)

    Returns None if parsing fails.
    """
    try:
        order = data.get("o")
        if not order:
            logger.warning("normalize_binance: missing 'o' key in data")
            return None

        raw_symbol = order.get("s", "")
        symbol_short = _extract_symbol_short(raw_symbol, "binance")
        if symbol_short is None:
            logger.warning("normalize_binance: could not parse symbol from %r", raw_symbol)
            return None

        raw_side = order.get("S", "").upper()
        if raw_side == "BUY":
            side = "short"  # shorts got liquidated (forced buy-back)
        elif raw_side == "SELL":
            side = "long"   # longs got liquidated (forced sell)
        else:
            logger.warning("normalize_binance: unknown side %r", raw_side)
            return None

        price = float(order.get("p", 0))
        qty = float(order.get("q", 0))
        if price <= 0 or qty <= 0:
            logger.warning(
                "normalize_binance: invalid price=%s qty=%s", price, qty
            )
            return None

        # Timestamp: prefer trade time from event, fall back to now
        ts_ms = order.get("T")
        timestamp = ts_ms / 1000.0 if ts_ms else time.time()

        return CommonLiqEvent(
            exchange="binance",
            symbol_short=symbol_short,
            symbol_full=_to_binance_full(symbol_short),
            side=side,
            price=price,
            qty=qty,
            notional=price * qty,
            timestamp=timestamp,
            raw=data,
        )
    except (ValueError, TypeError, KeyError) as exc:
        logger.warning("normalize_binance: parse error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Bybit normalizer
# ---------------------------------------------------------------------------

def normalize_bybit(data: dict) -> List[CommonLiqEvent]:
    """
    Normalize a Bybit allLiquidation message into a list of CommonLiqEvents.

    Bybit sends batches at 500ms intervals:
        data["data"]  → list of liquidation items
        item["s"]     → symbol ("BTCUSDT")
        item["S"]     → side: "Buy" = longs liquidated, "Sell" = shorts liquidated
        item["p"]     → bankruptcy price (string)
        item["v"]     → quantity in base asset (string)
        item["T"]     → timestamp in ms

    Returns a list (may be empty if parsing fails).
    """
    events: List[CommonLiqEvent] = []

    try:
        items = data.get("data")
        if not items or not isinstance(items, list):
            logger.warning("normalize_bybit: missing or invalid 'data' list")
            return events

        for item in items:
            try:
                raw_symbol = item.get("s", "")
                symbol_short = _extract_symbol_short(raw_symbol, "bybit")
                if symbol_short is None:
                    logger.warning(
                        "normalize_bybit: could not parse symbol from %r", raw_symbol
                    )
                    continue

                raw_side = item.get("S", "")
                if raw_side == "Buy":
                    side = "long"   # longs liquidated (forced sell)
                elif raw_side == "Sell":
                    side = "short"  # shorts liquidated (forced buy)
                else:
                    logger.warning("normalize_bybit: unknown side %r", raw_side)
                    continue

                price = float(item.get("p", 0))
                qty = float(item.get("v", 0))
                if price <= 0 or qty <= 0:
                    logger.warning(
                        "normalize_bybit: invalid price=%s qty=%s", price, qty
                    )
                    continue

                ts_ms = item.get("T")
                timestamp = ts_ms / 1000.0 if ts_ms else time.time()

                events.append(CommonLiqEvent(
                    exchange="bybit",
                    symbol_short=symbol_short,
                    symbol_full=_to_binance_full(symbol_short),
                    side=side,
                    price=price,
                    qty=qty,
                    notional=price * qty,
                    timestamp=timestamp,
                    raw=item,
                ))
            except (ValueError, TypeError, KeyError) as exc:
                logger.warning("normalize_bybit: item parse error: %s", exc)
                continue

    except Exception as exc:
        logger.warning("normalize_bybit: parse error: %s", exc)

    return events


# ---------------------------------------------------------------------------
# OKX normalizer
# ---------------------------------------------------------------------------

def normalize_okx(data: dict) -> List[CommonLiqEvent]:
    """
    Normalize an OKX liquidation-orders message into a list of CommonLiqEvents.

    OKX sends batches with nested details:
        data["data"]  → list of instrument items
        item["instId"]          → "BTC-USDT-SWAP"
        item["details"]         → list of liquidation details
        detail["posSide"]       → "long" or "short" (position side, no inversion)
        detail["bkPx"]          → bankruptcy price (string)
        detail["sz"]            → size in CONTRACTS (must convert to base asset)
        detail["ts"]            → timestamp in ms (string)

    Returns a list (may be empty if parsing fails).
    """
    events: List[CommonLiqEvent] = []

    try:
        items = data.get("data")
        if not items or not isinstance(items, list):
            logger.warning("normalize_okx: missing or invalid 'data' list")
            return events

        for item in items:
            try:
                inst_id = item.get("instId", "")
                symbol_short = _extract_symbol_short(inst_id, "okx")
                if symbol_short is None:
                    logger.warning(
                        "normalize_okx: could not parse symbol from instId %r", inst_id
                    )
                    continue

                details = item.get("details")
                if not details or not isinstance(details, list):
                    continue

                for detail in details:
                    try:
                        pos_side = detail.get("posSide", "").lower()
                        if pos_side not in ("long", "short"):
                            logger.warning(
                                "normalize_okx: unknown posSide %r", pos_side
                            )
                            continue
                        side = pos_side  # OKX gives position side directly

                        price = float(detail.get("bkPx", 0))
                        sz_contracts = float(detail.get("sz", 0))
                        if price <= 0 or sz_contracts <= 0:
                            logger.warning(
                                "normalize_okx: invalid bkPx=%s sz=%s",
                                price, sz_contracts,
                            )
                            continue

                        # Convert contracts to base asset units
                        contract_size = OKX_CONTRACT_SIZES.get(symbol_short, 1.0)
                        qty = sz_contracts * contract_size

                        ts_str = detail.get("ts", "")
                        timestamp = int(ts_str) / 1000.0 if ts_str else time.time()

                        events.append(CommonLiqEvent(
                            exchange="okx",
                            symbol_short=symbol_short,
                            symbol_full=_to_binance_full(symbol_short),
                            side=side,
                            price=price,
                            qty=qty,
                            notional=price * qty,
                            timestamp=timestamp,
                            raw=detail,
                        ))
                    except (ValueError, TypeError, KeyError) as exc:
                        logger.warning(
                            "normalize_okx: detail parse error: %s", exc
                        )
                        continue

            except (ValueError, TypeError, KeyError) as exc:
                logger.warning("normalize_okx: item parse error: %s", exc)
                continue

    except Exception as exc:
        logger.warning("normalize_okx: parse error: %s", exc)

    return events


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def normalize_liquidation(exchange: str, data: dict) -> List[CommonLiqEvent]:
    """
    Main entry point. Takes raw exchange message data, returns list of
    CommonLiqEvents.

    Args:
        exchange: Exchange name ("binance", "bybit", "okx")
        data:     The raw message payload (the "data" dict from the
                  wrapper envelope, NOT the outer wrapper itself)

    Returns:
        List of CommonLiqEvent. Empty list if exchange is unknown or
        parsing fails entirely.
    """
    exchange = exchange.lower()

    if exchange == "binance":
        event = normalize_binance(data)
        return [event] if event is not None else []

    if exchange == "bybit":
        return normalize_bybit(data)

    if exchange == "okx":
        return normalize_okx(data)

    logger.warning("normalize_liquidation: unknown exchange %r", exchange)
    return []


# ---------------------------------------------------------------------------
# Inline test cases
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")
    _counts = {"passed": 0, "failed": 0}

    def check(name: str, condition: bool, detail: str = ""):
        if condition:
            _counts["passed"] += 1
            print(f"  PASS: {name}")
        else:
            _counts["failed"] += 1
            print(f"  FAIL: {name} — {detail}")

    # -------------------------------------------------------------------
    # Test 1: Binance forceOrder
    # -------------------------------------------------------------------
    print("\n--- Test 1: Binance forceOrder ---")
    binance_msg = {
        "e": "forceOrder",
        "E": 1708700000123,
        "o": {
            "s": "BTCUSDT",
            "S": "BUY",         # shorts liquidated
            "o": "LIMIT",
            "f": "IOC",
            "q": "0.500",
            "p": "97500.00",
            "ap": "97450.00",
            "X": "FILLED",
            "l": "0.500",
            "z": "0.500",
            "T": 1708700000100,
        },
    }
    result = normalize_liquidation("binance", binance_msg)
    check("returns 1 event", len(result) == 1, f"got {len(result)}")
    if result:
        ev = result[0]
        check("exchange", ev.exchange == "binance", ev.exchange)
        check("symbol_short", ev.symbol_short == "BTC", ev.symbol_short)
        check("symbol_full", ev.symbol_full == "BTCUSDT", ev.symbol_full)
        check("side=short for BUY", ev.side == "short", ev.side)
        check("price", ev.price == 97500.0, str(ev.price))
        check("qty", ev.qty == 0.5, str(ev.qty))
        check("notional", ev.notional == 97500.0 * 0.5, str(ev.notional))
        check("timestamp from T", abs(ev.timestamp - 1708700000.1) < 0.001, str(ev.timestamp))

    # Also test SELL side
    binance_sell = {
        "o": {"s": "ETHUSDT", "S": "SELL", "p": "3500.00", "q": "2.0", "T": 1708700001000}
    }
    result_sell = normalize_liquidation("binance", binance_sell)
    check("SELL → side=long", len(result_sell) == 1 and result_sell[0].side == "long",
          f"len={len(result_sell)}, side={result_sell[0].side if result_sell else 'N/A'}")
    if result_sell:
        check("ETH symbol", result_sell[0].symbol_short == "ETH", result_sell[0].symbol_short)

    # -------------------------------------------------------------------
    # Test 2: Bybit allLiquidation (batch of 2)
    # -------------------------------------------------------------------
    print("\n--- Test 2: Bybit allLiquidation (batch of 2) ---")
    bybit_msg = {
        "topic": "allLiquidation.BTCUSDT",
        "type": "snapshot",
        "ts": 1739502303204,
        "data": [
            {
                "T": 1739502302929,
                "s": "BTCUSDT",
                "S": "Sell",       # shorts liquidated
                "v": "0.003",
                "p": "43511.70",
            },
            {
                "T": 1739502303100,
                "s": "BTCUSDT",
                "S": "Buy",        # longs liquidated
                "v": "0.010",
                "p": "43600.50",
            },
        ],
    }
    result = normalize_liquidation("bybit", bybit_msg)
    check("returns 2 events", len(result) == 2, f"got {len(result)}")
    if len(result) == 2:
        ev0, ev1 = result[0], result[1]
        check("event 0: exchange=bybit", ev0.exchange == "bybit", ev0.exchange)
        check("event 0: symbol=BTC", ev0.symbol_short == "BTC", ev0.symbol_short)
        check("event 0: Sell → side=short", ev0.side == "short", ev0.side)
        check("event 0: price", ev0.price == 43511.70, str(ev0.price))
        check("event 0: qty", ev0.qty == 0.003, str(ev0.qty))
        check("event 1: Buy → side=long", ev1.side == "long", ev1.side)
        check("event 1: qty", ev1.qty == 0.010, str(ev1.qty))

    # -------------------------------------------------------------------
    # Test 3: OKX liquidation-orders (contract conversion)
    # -------------------------------------------------------------------
    print("\n--- Test 3: OKX liquidation-orders (contract conversion) ---")
    okx_msg = {
        "arg": {"channel": "liquidation-orders", "instType": "SWAP"},
        "data": [
            {
                "instId": "BTC-USDT-SWAP",
                "instFamily": "BTC-USDT",
                "instType": "SWAP",
                "uly": "BTC-USDT",
                "details": [
                    {
                        "bkLoss": "0",
                        "bkPx": "97500.00",
                        "ccy": "",
                        "posSide": "long",
                        "side": "sell",
                        "sz": "100",          # 100 contracts * 0.01 = 1.0 BTC
                        "ts": "1723892524781",
                    }
                ],
            },
            {
                "instId": "ETH-USDT-SWAP",
                "instFamily": "ETH-USDT",
                "instType": "SWAP",
                "uly": "ETH-USDT",
                "details": [
                    {
                        "bkLoss": "0",
                        "bkPx": "3400.50",
                        "ccy": "",
                        "posSide": "short",
                        "side": "buy",
                        "sz": "50",           # 50 contracts * 0.1 = 5.0 ETH
                        "ts": "1723892525000",
                    }
                ],
            },
        ],
    }
    result = normalize_liquidation("okx", okx_msg)
    check("returns 2 events", len(result) == 2, f"got {len(result)}")
    if len(result) >= 1:
        ev0 = result[0]
        check("BTC: exchange=okx", ev0.exchange == "okx", ev0.exchange)
        check("BTC: symbol_short=BTC", ev0.symbol_short == "BTC", ev0.symbol_short)
        check("BTC: symbol_full=BTCUSDT", ev0.symbol_full == "BTCUSDT", ev0.symbol_full)
        check("BTC: posSide=long → side=long", ev0.side == "long", ev0.side)
        check("BTC: price", ev0.price == 97500.0, str(ev0.price))
        check(
            "BTC: contract conversion (100 * 0.01 = 1.0)",
            abs(ev0.qty - 1.0) < 1e-9,
            str(ev0.qty),
        )
        check(
            "BTC: notional",
            abs(ev0.notional - 97500.0) < 0.01,
            str(ev0.notional),
        )
        check(
            "BTC: timestamp",
            abs(ev0.timestamp - 1723892524.781) < 0.001,
            str(ev0.timestamp),
        )
    if len(result) >= 2:
        ev1 = result[1]
        check("ETH: symbol_short=ETH", ev1.symbol_short == "ETH", ev1.symbol_short)
        check("ETH: posSide=short → side=short", ev1.side == "short", ev1.side)
        check(
            "ETH: contract conversion (50 * 0.1 = 5.0)",
            abs(ev1.qty - 5.0) < 1e-9,
            str(ev1.qty),
        )

    # -------------------------------------------------------------------
    # Test 4: Edge cases
    # -------------------------------------------------------------------
    print("\n--- Test 4: Edge cases ---")
    check("unknown exchange → empty", normalize_liquidation("kraken", {}) == [], "")
    check("binance missing 'o' → empty", normalize_liquidation("binance", {}) == [], "")
    check("bybit missing 'data' → empty", normalize_liquidation("bybit", {}) == [], "")
    check("okx missing 'data' → empty", normalize_liquidation("okx", {}) == [], "")
    check(
        "binance zero price → empty",
        normalize_liquidation("binance", {"o": {"s": "BTCUSDT", "S": "BUY", "p": "0", "q": "1"}}) == [],
        "",
    )

    # -------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------
    print(f"\n{'='*50}")
    print(f"Results: {_counts['passed']} passed, {_counts['failed']} failed")
    if _counts["failed"]:
        print("SOME TESTS FAILED")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")
        sys.exit(0)
