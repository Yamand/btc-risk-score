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

**The real fix for score whipsaw was a bug fix, not reweighting.** `--update`
used to fetch only the last 400 days and recompute everything — including the
log-regression fit and every component's percentile-rank basis — using just
that short window as "history." That meant the regression coefficients (and
therefore every component) shifted for reasons that had nothing to do with
actual price movement, purely because the historical window used for the math
changed day to day. Fixed by caching the **full** raw close-price series in
`data/btc_prices_raw.json` and always merging + recomputing over complete
history, so `--update` and a full backfill produce identical results — it's
only faster because it skips redundant Binance requests, not because it uses
less data for the math. The 3-day EMA smoothing is a secondary, optional
layer on top of that fix, for the genuine day-to-day noise that's left over.

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
btc_risk_score.py                    # fetch + compute + write data/
data/btc_risk_history.json           # generated — one scored row per day
data/btc_prices_raw.json             # generated — full raw close-price cache (see above)
index.html                           # static site, reads data/ directly, Chart.js
.github/workflows/daily-update.yml   # cron job, runs btc_risk_score.py --update daily
```

## Setup

1. Push this repo to GitHub, enable **GitHub Pages** (Settings → Pages →
   Deploy from branch → `main` / root).
2. Run a full backfill once, locally, so both `data/btc_risk_history.json`
   and `data/btc_prices_raw.json` exist before the site goes live — see
   "Local run" below.
3. Commit **both** files in `data/` — the workflow commits both on every run
   too, since `btc_prices_raw.json` has to persist across runs for `--update`
   to work correctly (a fresh checkout with no cached prices falls back to a
   full fetch automatically, but that defeats the point of `--update`).
4. The daily workflow (`daily-update.yml`) runs automatically at 00:15 UTC,
   fetches the last 400 days, merges with the cached full history, recomputes,
   and commits both updated JSON files. GitHub Pages redeploys automatically
   on push.

### Local run

```bash
pip install pandas numpy requests
python btc_risk_score.py            # full history backfill (first run)
python btc_risk_score.py --update   # fast daily run — same math, fewer requests
python -m http.server 8000          # then open localhost:8000
```

## Notes

- No API key required — Binance's `/api/v3/klines` endpoint is public.
- The score is descriptive, not a signal to auto-trade on. Same discipline as
  the existing DCA multiplier table: it scales buy size, doesn't override the
  plan.
- Not financial advice.
