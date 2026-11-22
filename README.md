# Cross-Market Sentiment Dashboard — US, China/HK and Crypto

A static, single-page dashboard comparing market sentiment and technical conditions in
US equities, China / Hong Kong equities and the crypto market. It is rebuilt once a day from public
sources and makes no data requests of its own when opened.

**What it shows**

- **Fear & Greed gauges** — CNN Business Fear & Greed Index for the US (with its seven
  components), a derived China/HK score (method below), and the Alternative.me Crypto
  Fear & Greed Index.
- **Volatility and positioning** — CBOE VIX and equity put/call ratio; HSI Volatility
  Index (VHSI); Southbound Stock Connect net buying.
- **14-day RSI monitor** — SPY, QQQ, IWM, DIA, FXI, KWEB, 2800.HK, 3033.HK, Bitcoin and
  Ether, computed with Wilder's method from daily closes (UTC daily closes for crypto).
- **Trend charts, headlines, key drivers and upcoming catalysts.** Headlines, drivers and
  catalysts are curated by hand in `index.html` (`DATA_CONFIG`).

Every figure on the page carries its source link and as-of date.

## How it works

```
GitHub Actions (daily, 00:17 UTC)
   └─ update_snapshot.py ──> snapshot-data.js ──> committed to the repo
GitHub Pages
   └─ index.html (+ snapshot-data.js) ──> your browser
```

`update_snapshot.py` fetches each item through a chain of sources, with per-server pacing,
retries, a rate-limit cool-down, and a check that rejects stale or incomplete data. If every
source for an item fails, the previous value is kept with its original date, and the
dashboard lists the failure under the header. If `snapshot-data.js` is missing, the page
falls back to the values built into `index.html`.

| Data | Sources, in order |
|---|---|
| CNN Fear & Greed, put/call, VIX | `fear-greed` library → CNN endpoint directly |
| US-listed ETFs | Sina → Eastmoney → Yahoo |
| HK ETFs (2800, 3033) | Sina → Eastmoney → Yahoo |
| Hang Seng Index | Sina → Eastmoney → Eastmoney Stock Connect table → Yahoo |
| HS TECH, VHSI | Sina → Eastmoney |
| Southbound net buying | Eastmoney 南向资金 → Eastmoney 港股通沪 + 港股通深 |
| Crypto Fear & Greed | Alternative.me |
| Bitcoin, Ether | Kraken → CoinGecko → Yahoo |

Sina and Eastmoney are accessed through [AKShare](https://github.com/akfamily/akshare);
Yahoo through [yfinance](https://github.com/ranaroussi/yfinance).

### China/HK Fear & Greed method

No official China/HK Fear & Greed index exists. The score here is derived, following
the structure of CNN's price-based components, using Hong Kong data. Each component is
scored as its percentile rank over the trailing year (252 trading days), and the three
are averaged with equal weights:

| Component | Measure | Higher score means |
|---|---|---|
| Momentum | HSI relative to its 125-day moving average | greed |
| Price strength | HSI relative to its 52-week high | greed |
| Volatility | VHSI relative to its 50-day moving average (inverted) | greed (calm) |

Southbound flow is displayed but not scored. Mainland buying often rises during sell-offs,
so its direction as a fear/greed signal is ambiguous. The dashboard shows each component's
score beneath the gauge.

## Setup

1. Create a new **public** repository on GitHub (it can start empty).
2. Upload the files to the `main` branch, including the `.github/workflows` folder
   (web: **Add file → Upload files**, drag in the folder contents; or use Git / GitHub Desktop).
3. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
4. **Actions → "Update snapshot and publish" → Run workflow** to create the first snapshot and publish.
   The run triggered by the upload itself may fail at the deploy step if Pages was not yet
   enabled; that is expected and harmless.
5. The site is published at `https://<username>.github.io/<repository>/`.

The workflow then runs automatically every day at 00:17 UTC (08:17 Singapore time), after
the US close and after the crypto UTC day closes. On weekends only the crypto figures change. GitHub may start scheduled runs some minutes late. Its daily commit
also keeps the schedule active; GitHub disables scheduled workflows in public repositories
after 60 days without activity.

### Run locally

```
pip install -r requirements.txt
python update_snapshot.py          # writes snapshot-data.js next to the script
```

Then open `index.html` in a browser. Tailwind CSS and Chart.js load from public CDNs.

## Disclaimer

This project is provided for **informational and educational purposes only**. It is not
investment, financial, legal or tax advice, and nothing on the dashboard is a
recommendation to buy, sell or hold any security. Consult a licensed professional before
making investment decisions.

- **No guarantee of accuracy or timeliness.** Data comes from third-party public sources,
  including unofficial interfaces that can change, fail or return incorrect values without
  notice. Figures may be delayed, incomplete or wrong. Always verify against the original
  source before relying on any number.
- **Derived measures are the author's construction.** The China/HK Fear & Greed score and
  the headline sentiment scores are simple, transparent heuristics. They are not
  published indices and have not been validated as predictive of returns.
- **Crypto assets are highly volatile and lightly regulated.** Prices differ between
  exchanges; the figures shown are from a single venue or aggregator at the UTC daily close.
- **Past patterns do not predict future results.** RSI thresholds, sentiment extremes and
  similar signals describe conditions; they do not forecast prices.
- **Third-party data and trademarks.** All market data, index names and trademarks belong
  to their respective owners, including CNN, Cboe, Hang Seng Indexes Company, HKEX, Sina,
  Eastmoney, Yahoo, Alternative.me, Kraken and CoinGecko. This project is not affiliated with, endorsed by or sponsored by
  any of them. The MIT License covers this repository's code only, not the data it
  retrieves. Users are responsible for complying with each data provider's terms of use.
- **Use at your own risk.** The software is provided "as is", without warranty of any
  kind, as set out in the [LICENSE](LICENSE). The author accepts no liability for any loss
  or damage arising from its use.

## License

Code released under the [MIT License](LICENSE).
