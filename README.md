# BTC Risk Score

A daily composite 0–1 risk score for BTC, built entirely from Binance public
OHLCV data (no API key, no paid on-chain data). Static site + GitHub Actions,
same pattern as the asset-tracking dashboard.

**Live idea:** `0` = cheap, accumulate harder. `1` = expensive, reduce buys /
start distributing once holdings clear your $500 sell-tier threshold.

## How the score is built

Four components, each normalized to 0–1 via **expanding historical percentile
rank** (today's raw value ranked against every prior day back to the start of
data) — this means there are no hardcoded "cheap" / "expensive" thresholds to
maintain; the scale self-calibrates as more history accumulates.

| Component | Weight | What it captures |
|---|---|---|
| Log-regression band position | 35% | Price vs. long-term log-log growth curve, refit each run |
| 200-day MA multiple | 25% | Price stretch vs. long-term trend (price ÷ 200d MA) |
| RSI-14 (daily) | 20% | Short-term overbought/oversold |
| Volatility-adjusted momentum | 20% | 30d return ÷ 30d realized volatility |

Composite = weighted sum of the four, then smoothed with a **3-day EMA**
before being used for zone lookup. The raw (unsmoothed) score is still saved
in the output as `composite_score_raw` in case it's useful to compare.

**Why smoothing, not just reweighting:** early on this was "fixed" by shifting
weight toward the slower log-regression component, but that didn't actually
solve it — even a component with reduced weight can still swing the composite
several points on a sharp single-day price move, since each component is
normalized relative to full history rather than to a fixed scale. Smoothing
the final score directly targets the actual symptom (fast day-to-day jumps)
without changing what each component measures.

Composite = weighted sum of the four, clipped to [0, 1].

**Caveat on the log-regression component:** Binance's BTCUSDT pair only has
daily candles back to **2017-08-17**. A "real" BTC log-regression band is
usually fit on price history since 2013 or earlier. This version fits on
Binance's shorter window, so the regression — and therefore the composite
score — will be less stable/meaningful for roughly the first year of computed
history (2017–2018) since the curve hasn't seen a full cycle yet. It's stable
from the 2018 bear market onward. If this ever matters enough to fix, the
regression coefficients could be hardcoded from a longer external dataset
instead of fit live — not done here to keep this Binance-only.

## Repo structure

```
btc_risk_score.py              # fetch + compute + write data/btc_risk_history.json
data/btc_risk_history.json     # generated — one row per day
index.html                     # static site, reads data/ directly, Chart.js
.github/workflows/daily-update.yml   # cron job, runs btc_risk_score.py --update daily
```

## Setup

1. Push this repo to GitHub, enable **GitHub Pages** (Settings → Pages →
   Deploy from branch → `main` / root).
2. Run the full backfill once, locally or via Actions "Run workflow" with
   the `--update` flag removed (see below), so `data/btc_risk_history.json`
   exists before the site goes live.
3. The daily workflow (`daily-update.yml`) runs automatically at 00:15 UTC,
   pulls the last 400 days from Binance, recomputes rolling metrics, and
   commits the updated JSON. GitHub Pages redeploys automatically on push.

### Local run

```bash
pip install pandas numpy requests
python btc_risk_score.py            # full history backfill (first run)
python btc_risk_score.py --update   # fast daily run (last 400 days only)
python -m http.server 8000          # then open localhost:8000
```

## Notes

- No API key required — Binance's `/api/v3/klines` endpoint is public.
- The score is descriptive, not a signal to auto-trade on. Same discipline as
  the existing DCA multiplier table: it scales buy size, doesn't override the
  plan.
- Not financial advice.
