"""
BTC Risk Score — daily composite 0-1 score from Binance public OHLCV data.

Components (all normalized 0-1 via expanding historical percentile rank,
so the score self-calibrates over time without hardcoded thresholds):

  1. Log-regression band position   (35%) — price vs. long-term log-log growth curve
  2. 200-day MA multiple            (25%) — price stretch vs. long-term trend
  3. RSI-14 (daily)                 (20%) — short-term overbought/oversold
  4. Volatility-adjusted momentum   (20%) — 30d return / 30d realized vol

  A 3-day EMA is applied to the composite score before zone lookup, to reduce
  day-to-day whipsaw from sharp single-day price moves.

0 = cheap / accumulate harder.  1 = expensive / reduce or take profit.

Usage:
    python btc_risk_score.py            # fetch full history, recompute, write data/btc_risk_history.json
    python btc_risk_score.py --update   # fetch only recent candles and append (fast daily run)
"""

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

BINANCE_KLINES_URL = "https://data-api.binance.vision/api/v3/klines"
BINANCE_KLINES_URL_FALLBACK = "https://api.binance.com/api/v3/klines"
SYMBOL = "BTCUSDT"
INTERVAL = "1d"
DATA_DIR = Path(__file__).parent / "data"
HISTORY_FILE = DATA_DIR / "btc_risk_history.json"

WEIGHTS = {
    "log_regression": 0.35,
    "ma200_multiple": 0.25,
    "rsi14": 0.20,
    "vol_adj_momentum": 0.20,
}

# EMA smoothing span applied to the composite score to reduce day-to-day
# whipsaw. Weight reallocation alone doesn't fix this — even a component with
# low weight can still swing the composite several points on a sharp price
# day, since normalization is relative to full history. Smoothing directly
# targets the actual symptom (fast day-to-day jumps) without diluting what
# each component measures.
SMOOTH_SPAN_DAYS = 3

BINANCE_LISTING_DATE = pd.Timestamp("2017-08-17")  # BTCUSDT earliest daily candle on Binance


def _get_with_fallback(params):
    """Try the geo-block-resistant mirror first, fall back to the main API domain."""
    try:
        resp = requests.get(BINANCE_KLINES_URL, params=params, timeout=30)
        resp.raise_for_status()
        return resp
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        print(f"  Primary endpoint failed (status={status}), retrying via fallback domain...")
        resp = requests.get(BINANCE_KLINES_URL_FALLBACK, params=params, timeout=30)
        resp.raise_for_status()
        return resp


def fetch_klines(start_time_ms=None, limit=1000):
    """Fetch daily klines from Binance, paginating until caught up to now."""
    all_rows = []
    cursor = start_time_ms
    while True:
        params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": limit}
        if cursor is not None:
            params["startTime"] = cursor
        resp = _get_with_fallback(params)
        rows = resp.json()
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < limit:
            break
        # next page starts right after the last candle's open time
        cursor = rows[-1][0] + 1
        time.sleep(0.2)  # be polite to the public endpoint
    return all_rows


def klines_to_df(rows):
    cols = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
    ]
    df = pd.DataFrame(rows, columns=cols)
    df["date"] = pd.to_datetime(df["open_time"], unit="ms").dt.normalize()
    df["close"] = df["close"].astype(float)
    df = df[["date", "close"]].drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)
    return df


def percentile_rank_expanding(series: pd.Series, min_periods=60) -> pd.Series:
    """
    For each point, rank it against all prior history (inclusive), scaled 0-1.
    This is what makes each component self-calibrating: no hardcoded bounds,
    the definition of 'cheap' vs 'expensive' adapts as more history accumulates.
    """
    def rank_last(window):
        if len(window) < min_periods:
            return np.nan
        return (window <= window[-1]).sum() / len(window)

    return series.expanding(min_periods=min_periods).apply(rank_last, raw=True)


def compute_components(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["log_price"] = np.log(df["close"])
    df["days_since_genesis"] = (df["date"] - pd.Timestamp("2009-01-03")).dt.days
    df["log_days"] = np.log(df["days_since_genesis"])

    # --- 1. Log-regression band position ---
    # Fit log(price) ~ a * log(days) + b using all available history (refit each run).
    # NOTE: Binance BTCUSDT only goes back to 2017-08-17, so this regression is fit on a
    # shorter window than the full 2013+ cycle history — it will be less stable in the
    # first year or two of computed scores. Caveat noted in README.
    coeffs = np.polyfit(df["log_days"], df["log_price"], 1)
    df["log_price_fit"] = np.polyval(coeffs, df["log_days"])
    df["regression_residual"] = df["log_price"] - df["log_price_fit"]
    df["log_regression"] = percentile_rank_expanding(df["regression_residual"])

    # --- 2. 200-day MA multiple ---
    df["ma200"] = df["close"].rolling(200, min_periods=200).mean()
    df["ma200_ratio"] = df["close"] / df["ma200"]
    df["ma200_multiple"] = percentile_rank_expanding(df["ma200_ratio"])

    # --- 3. RSI-14 ---
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    df["rsi14"] = (rsi / 100).clip(0, 1)

    # --- 4. Volatility-adjusted momentum ---
    df["ret"] = df["close"].pct_change()
    df["roc_30d"] = df["close"].pct_change(30)
    df["vol_30d"] = df["ret"].rolling(30, min_periods=30).std()
    df["vol_adj_mom_raw"] = df["roc_30d"] / df["vol_30d"].replace(0, np.nan)
    df["vol_adj_momentum"] = percentile_rank_expanding(df["vol_adj_mom_raw"])

    return df


def compute_composite(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["composite_score_raw"] = (
        df["log_regression"] * WEIGHTS["log_regression"]
        + df["ma200_multiple"] * WEIGHTS["ma200_multiple"]
        + df["rsi14"] * WEIGHTS["rsi14"]
        + df["vol_adj_momentum"] * WEIGHTS["vol_adj_momentum"]
    )
    # Smoothed score is what drives the zone/action lookup. Raw score is kept
    # in the output too, in case it's useful to see how much smoothing is
    # pulling a given day's reading.
    df["composite_score"] = df["composite_score_raw"].ewm(
        span=SMOOTH_SPAN_DAYS, min_periods=1, adjust=False
    ).mean()
    return df


# Base weekly DCA size. Zone sizes below are BASE_WEEKLY_USD * multiplier.
BASE_WEEKLY_USD = 10

# Actual DCA rule table (BTC/USDT, base $10/week).
# Sell tiers only fire in practice once holdings >= $500 per asset — that's a
# portfolio-level gate this script can't see (it only knows price/score), so
# sell zones are always computed here and the $500 gate is applied by you
# (or by the dashboard, which does know your holdings) before acting on them.
ZONES = [
    # (upper_bound_exclusive, zone, tier, multiplier, action)
    (0.10, "Extreme Buy",   "buy",   3.0, "Max accumulate"),
    (0.20, "Strong Buy",    "buy",   1.5, "Accumulate"),
    (0.25, "Buy",           "buy",   1.0, "Normal DCA"),
    (0.35, "Reduced Buy",   "buy",   0.5, "Slow down"),
    (0.60, "Stop — Hold",   "hold",  0.0, "Accumulation done"),
    (0.70, "Sell Tier 1",   "sell1", None, "Exit 5% of holdings"),
    (0.80, "Sell Tier 2",   "sell2", None, "Exit 10% of holdings"),
    (1.01, "Sell Tier 3 / Exit", "sell3", None, "Exit 20% or full position"),
]


def zone_for_score(score):
    if pd.isna(score):
        return {"zone": "Insufficient history", "tier": "none", "multiplier": None,
                "size_usd": None, "action": "—"}
    for upper, zone, tier, mult, action in ZONES:
        if score < upper:
            size = round(BASE_WEEKLY_USD * mult, 2) if mult is not None else None
            return {"zone": zone, "tier": tier, "multiplier": mult, "size_usd": size, "action": action}
    # score == 1.0 edge case, falls into last zone above via < 1.01
    upper, zone, tier, mult, action = ZONES[-1]
    return {"zone": zone, "tier": tier, "multiplier": mult, "size_usd": None, "action": action}


def build_output(df: pd.DataFrame) -> list:
    out = []
    for _, row in df.iterrows():
        if pd.isna(row["composite_score"]):
            continue
        z = zone_for_score(row["composite_score"])
        out.append({
            "date": row["date"].strftime("%Y-%m-%d"),
            "close": round(row["close"], 2),
            "composite_score": round(row["composite_score"], 4),
            "composite_score_raw": round(row["composite_score_raw"], 4) if not pd.isna(row["composite_score_raw"]) else None,
            "zone": z["zone"],
            "tier": z["tier"],
            "multiplier": z["multiplier"],
            "size_usd": z["size_usd"],
            "action": z["action"],
            "components": {
                "log_regression": round(row["log_regression"], 4) if not pd.isna(row["log_regression"]) else None,
                "ma200_multiple": round(row["ma200_multiple"], 4) if not pd.isna(row["ma200_multiple"]) else None,
                "rsi14": round(row["rsi14"], 4) if not pd.isna(row["rsi14"]) else None,
                "vol_adj_momentum": round(row["vol_adj_momentum"], 4) if not pd.isna(row["vol_adj_momentum"]) else None,
            },
        })
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--update", action="store_true",
                         help="Only fetch recent candles (last 400 days) instead of full history. "
                              "Faster for daily cron runs; still recomputes rolling metrics correctly "
                              "because 400d > 200d MA window.")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if args.update:
        start_ms = int((pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=400)).timestamp() * 1000)
    else:
        start_ms = int(BINANCE_LISTING_DATE.timestamp() * 1000)

    print(f"Fetching BTCUSDT daily klines from Binance (start={pd.to_datetime(start_ms, unit='ms')})...")
    rows = fetch_klines(start_time_ms=start_ms)
    df = klines_to_df(rows)
    print(f"Fetched {len(df)} daily candles, {df['date'].min().date()} to {df['date'].max().date()}")

    df = compute_components(df)
    df = compute_composite(df)
    output = build_output(df)

    if args.update and HISTORY_FILE.exists():
        # merge: keep old history, overwrite/append recomputed recent tail
        existing = json.loads(HISTORY_FILE.read_text())
        existing_by_date = {r["date"]: r for r in existing}
        for r in output:
            existing_by_date[r["date"]] = r
        merged = sorted(existing_by_date.values(), key=lambda r: r["date"])
        HISTORY_FILE.write_text(json.dumps(merged, indent=2))
        print(f"Updated {HISTORY_FILE}, {len(merged)} total rows")
    else:
        HISTORY_FILE.write_text(json.dumps(output, indent=2))
        print(f"Wrote {HISTORY_FILE}, {len(output)} rows")

    if output:
        latest = output[-1]
        size = f"${latest['size_usd']}" if latest['size_usd'] is not None else "—"
        print(f"\nLatest ({latest['date']}): score={latest['composite_score']} "
              f"[{latest['zone']}] size={size}/wk — {latest['action']}")


if __name__ == "__main__":
    main()
