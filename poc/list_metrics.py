#!/usr/bin/env python3
"""
List all BTC perpetual metrics available in the moonStreamProcess library.
This is a reference guide - no websocket connection required.
"""

# All metrics as defined in synthHub.py and synthesis.py
METRICS_REFERENCE = """
================================================================================
              BTC PERPETUAL METRICS - COMPLETE REFERENCE
================================================================================

These are ALL the metrics computed by the moonStreamProcess library for BTC
perpetuals, as defined in StreamEngine/synthHub.py:ratrive_data() and the
synthesis module classes.

================================================================================
PRICE METRICS
================================================================================
  timestamp          : Current timestamp (YYYY-MM-DD HH:MM)
  perp_open          : Opening price of the 1-minute window
  perp_close         : Closing price of the 1-minute window
  perp_high          : Highest price in the window
  perp_low           : Lowest price in the window
  perp_Vola          : Price volatility (std dev) over the window

================================================================================
ORDERBOOK METRICS (perp_books)
================================================================================
  perp_books         : dict{price_level: btc_amount}
                       Aggregated orderbook depth heatmap across all exchanges
                       Price levels are bucketed by level_size (default $20)

  Exchange coverage:
    - binance_btcusdt, binance_btcusd
    - okx_btcusdt, okx_btcusd
    - bybit_btcusdt, bybit_btcusd
    - bingx_btcusdt
    - mexc_btcusdt
    - kucoin_btcusdt
    - deribit_btcusd
    - htx_btcusdt
    - gateio_btcusdt
    - bitget_btcusdt

================================================================================
TRADE METRICS
================================================================================
  perp_buyVol             : Total buy volume in BTC (1m)
  perp_sellVol            : Total sell volume in BTC (1m)
  perp_VolProfile         : dict{price_level: btc_volume}
                            Total trade volume heatmap
  perp_buyVolProfile      : dict{price_level: btc_volume}
                            Buy-side volume heatmap
  perp_sellVolProfile     : dict{price_level: btc_volume}
                            Sell-side volume heatmap
  perp_numberBuyTrades    : Count of buy trades (1m)
  perp_numberSellTrades   : Count of sell trades (1m)
  perp_orderedBuyTrades   : list[btc_amount]
                            Chronological buy trade sizes
  perp_orderedSellTrades  : list[btc_amount]
                            Chronological sell trade sizes

================================================================================
ADJUSTMENT METRICS (Voided/Reinforced Orders)
================================================================================
These track limit orders that were added then removed (voided) or added
(reinforced) in the orderbook, computed by comparing consecutive snapshots.

  perp_voids              : dict{price_level: btc_amount}
                            Voided (canceled) order volume heatmap
  perp_reinforces         : dict{price_level: btc_amount}
                            Reinforced (added) order volume heatmap
  perp_totalVoids         : Total voided volume in BTC
  perp_totalReinforces    : Total reinforced volume in BTC
  perp_totalVoidsVola     : Volatility of total voids
  perp_voidsDuration      : dict{price_level: seconds}
                            Median duration between voids at each level
  perp_voidsDurationVola  : dict{price_level: seconds}
                            Volatility of void durations
  perp_reinforcesDuration : dict{price_level: seconds}
                            Median duration between reinforcements
  perp_reinforcesDurationVola : dict{price_level: seconds}
                            Volatility of reinforcement durations

================================================================================
FUNDING & OPEN INTEREST METRICS
================================================================================
  perp_weighted_funding   : OI-weighted average funding rate across exchanges
  perp_total_oi           : Total open interest across all exchanges
  perp_oi_change          : Net OI change over the window
  perp_oi_Vola            : OI volatility
  perp_oi_increases       : dict{price_level: btc_amount}
                            OI increase heatmap
  perp_oi_increases_Vola  : dict{price_level: btc_amount}
                            OI increase volatility per level
  perp_oi_decreases       : dict{price_level: btc_amount}
                            OI decrease heatmap
  perp_oi_decreases_Vola  : dict{price_level: btc_amount}
                            OI decrease volatility per level
  perp_oi_turnover        : dict{price_level: btc_amount}
                            Absolute OI change heatmap
  perp_oi_turnover_Vola   : dict{price_level: btc_amount}
                            OI turnover volatility per level
  perp_oi_total           : dict{price_level: btc_amount}
                            Net OI change per level
  perp_oi_total_Vola      : dict{price_level: btc_amount}
                            Net OI change volatility per level
  perp_orderedOIChanges   : list[btc_amount]
                            Chronological OI changes
  perp_OIs_per_instrument : dict{exchange_instrument: btc_oi}
                            OI per exchange/instrument
  perp_fundings_per_instrument : dict{exchange_instrument: rate}
                            Funding rate per exchange/instrument

================================================================================
LIQUIDATION METRICS
================================================================================
  perp_liquidations_longsTotal  : Total long liquidation volume in BTC (1m)
  perp_liquidations_longs       : dict{price_level: btc_amount}
                                  Long liquidation heatmap
  perp_liquidations_shortsTotal : Total short liquidation volume in BTC (1m)
  perp_liquidations_shorts      : dict{price_level: btc_amount}
                                  Short liquidation heatmap
  perp_liquidations_orderedLongs  : list[btc_amount]
                                    Chronological long liquidations
  perp_liquidations_orderedShorts : list[btc_amount]
                                    Chronological short liquidations

  Exchange coverage for liquidations:
    - binance_btcusdt, binance_btcusd
    - okx_btc (all BTC contracts)
    - bybit_btcusdt, bybit_btcusd
    - gateio_btcusdt

================================================================================
POSITION RATIO METRICS
================================================================================
These are long/short ratios from exchanges that provide them.

  perp_TTA_ratio          : Top Traders Account ratio (Binance only)
                            = longAccount / shortAccount
  perp_TTP_ratio          : Top Traders Position ratio (Binance only)
                            = longPosition / shortPosition
  perp_GTA_ratio          : Global Traders Account ratio
                            OI-weighted average across exchanges
                            Sources: Binance (GTA), OKX (GTA), Bybit (buy/sell ratio)

================================================================================
DATA STRUCTURES
================================================================================
Output from btcSynth.merge() is a flat dict with all above keys.
Heatmap dicts have string keys (price levels) and float values (BTC amounts).

Example output shape:
{
    "timestamp": "2024-01-30 14:30",
    "perp_books": {"94000.0": 12.5, "94020.0": 8.2, ...},
    "perp_VolProfile": {"94000.0": 2.1, "94020.0": 1.5, ...},
    "perp_buyVol": 45.6,
    "perp_sellVol": 52.3,
    "perp_weighted_funding": 0.0001,
    ...
}

================================================================================
AGGREGATION PARAMETERS
================================================================================
  level_size        : Price bucket size for heatmaps (default: $20)
  book_ceil_thresh  : Max % from price for book levels (default: 5%)
  pranges           : Options OI percentage ranges from ATM
  expiry_windows    : Options expiry grouping windows

================================================================================
TIME WINDOWS
================================================================================
All metrics are computed over 60-second windows aligned to wall-clock seconds.
Window rollover occurs when current_second < previous_second.
Timestamps use exchange timestamps (E/ts fields) when available.

================================================================================
"""

def main():
    print(METRICS_REFERENCE)

    # Also print as structured data
    print("\n" + "=" * 80)
    print("METRICS AS PYTHON DICT (for programmatic use)")
    print("=" * 80)

    metrics_dict = {
        "price": [
            "timestamp", "perp_open", "perp_close", "perp_high", "perp_low", "perp_Vola"
        ],
        "orderbook": [
            "perp_books"
        ],
        "trades": [
            "perp_buyVol", "perp_sellVol", "perp_VolProfile", "perp_buyVolProfile",
            "perp_sellVolProfile", "perp_numberBuyTrades", "perp_numberSellTrades",
            "perp_orderedBuyTrades", "perp_orderedSellTrades"
        ],
        "adjustments": [
            "perp_voids", "perp_reinforces", "perp_totalVoids", "perp_totalReinforces",
            "perp_totalVoidsVola", "perp_voidsDuration", "perp_voidsDurationVola",
            "perp_reinforcesDuration", "perp_reinforcesDurationVola"
        ],
        "funding_oi": [
            "perp_weighted_funding", "perp_total_oi", "perp_oi_change", "perp_oi_Vola",
            "perp_oi_increases", "perp_oi_increases_Vola", "perp_oi_decreases",
            "perp_oi_decreases_Vola", "perp_oi_turnover", "perp_oi_turnover_Vola",
            "perp_oi_total", "perp_oi_total_Vola", "perp_orderedOIChanges",
            "perp_OIs_per_instrument", "perp_fundings_per_instrument"
        ],
        "liquidations": [
            "perp_liquidations_longsTotal", "perp_liquidations_longs",
            "perp_liquidations_shortsTotal", "perp_liquidations_shorts",
            "perp_liquidations_orderedLongs", "perp_liquidations_orderedShorts"
        ],
        "positions": [
            "perp_TTA_ratio", "perp_TTP_ratio", "perp_GTA_ratio"
        ]
    }

    import json
    print(json.dumps(metrics_dict, indent=2))

    total = sum(len(v) for v in metrics_dict.values())
    print(f"\nTotal unique metrics: {total}")


if __name__ == "__main__":
    main()
