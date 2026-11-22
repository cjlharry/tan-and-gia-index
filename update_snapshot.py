#!/usr/bin/env python3
r"""
Daily snapshot generator for the dashboard (index.html). MIT License, see LICENSE.

Runs ONCE per day (Windows Task Scheduler), fetches the data and writes
`snapshot-data.js` next to the dashboard. The dashboard makes no data calls
itself; it reads this file when opened.

Every data item has a chain of sources, tried in order:
  CNN Fear & Greed (+ 7 components, put/call, VIX)
        fear-greed library -> direct call to the same CNN endpoint
  US-listed ETFs (SPY QQQ IWM DIA FXI KWEB)
        Sina -> Eastmoney -> Yahoo (only if yfinance is installed)
  HK ETFs (2800.HK 3033.HK)
        Sina -> Eastmoney -> Yahoo (only if yfinance is installed)
  Hang Seng Index
        Sina -> Eastmoney quotes -> Eastmoney Stock Connect table -> Yahoo (optional)
  HS TECH, VHSI
        Sina -> Eastmoney quotes
  Southbound Stock Connect net buy
        Eastmoney "南向资金" -> Eastmoney "港股通沪" + "港股通深"
  Crypto Fear & Greed Index
        Alternative.me (published index; previous value kept if unavailable)
  Bitcoin, Ether (UTC daily closes; today's unfinished day is excluded)
        Kraken -> CoinGecko -> Yahoo (only if yfinance is installed)

Rate-limit protection:
  - minimum gap between calls to the same server (HOST_GAP_S)
  - transient network errors retried with back-off (RETRIES, BACKOFF_S)
  - a server that rate-limits us is not called again in that pass; the next
    source in the chain is used instead
  - items that failed because of a rate limit get one more try after a
    cool-down pause (COOLDOWN_S; set 0 to disable)
  - a server that keeps failing (DOWN_AFTER items in a row) is skipped for the
    rest of the run instead of being retried for every item
  - identical requests are made only once per run (cached)

China/HK Fear & Greed (derived; no official index exists) mirrors CNN's
price-based components with Hong Kong data, equal weights, each scored as its
percentile rank over the trailing 1 year (252 trading days):
    Momentum        HSI vs its 125-day moving average        (high = greed)
    Price strength  HSI vs its 52-week high                  (high = greed)
    Volatility      VHSI vs its 50-day moving average        (high = fear, inverted)
Missing inputs -> remaining weights renormalised. Southbound flow is reported
but not scored: mainland buying rises in sell-offs (dip-buying), so its sign
is ambiguous as a fear/greed signal.

Install:  py -m pip install akshare fear-greed        (optional extra: yfinance)
Run:      run_snapshot.bat   (or: py update_snapshot.py [--out path\to\snapshot-data.js])

If every source for an item fails, its previous value in snapshot-data.js is
kept (with its original as-of date) and the failure is listed in `errors`.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# ----------------------------------------------------------------- settings
RSI_N = 14
PCT_WINDOW = 252                     # 1-year percentile window for the HK Fear & Greed components
PCT_MIN_PERIODS = 126
HISTORY_DAYS = 130                   # points kept per series (covers the 120-day period, incl. daily crypto data)
PERIODS_D = (7, 30, 60, 90, 120)     # calendar-day periods for % change (1-day change is "chg")
MAX_AGE_DAYS = 12                    # a source whose latest data is older than this is treated as stale -> next source

HOST_GAP_S = {"sina": 1.5, "em-quote": 4.0, "em-data": 2.0, "cnn": 1.0, "yahoo": 1.5,
              "altme": 1.0, "kraken": 1.5, "coingecko": 3.0}
RETRIES = 2                          # extra attempts after a transient network error
BACKOFF_S = (5, 20)                  # wait before retry 1, retry 2
COOLDOWN_S = 360                     # pause before re-trying rate-limited items (0 = no second pass)
DOWN_AFTER = 2                       # skip a server for the rest of the run after this many items fail on it in a row

EM_US = {"SPY": "107.SPY", "QQQ": "105.QQQ", "IWM": "107.IWM", "DIA": "107.DIA", "FXI": "107.FXI", "KWEB": "107.KWEB"}
ETFS = {"SPY": "US", "QQQ": "US", "IWM": "US", "DIA": "US", "FXI": "US", "KWEB": "US", "2800.HK": "HK", "3033.HK": "HK"}

CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
CNN_SRC = "https://www.cnn.com/markets/fear-and-greed"
ALTME_URL = "https://api.alternative.me/fng/?limit=150"
ALTME_SRC = "https://alternative.me/crypto/fear-and-greed-index/"
CRYPTO = {  # symbol: (Kraken pair, CoinGecko id, Yahoo symbol)
    "BTC": ("XBTUSD", "bitcoin", "BTC-USD"),
    "ETH": ("ETHUSD", "ethereum", "ETH-USD"),
}
HSGT_SRC = "https://data.eastmoney.com/hsgt/index.html"

# AKShare renamed these in 1.20.x (needs Python >= 3.11.5); older installs keep the old names.
AK_OLD_NAMES = {"stock_us_hist_sina": "stock_us_daily", "stock_hk_hist_sina": "stock_hk_daily",
                "index_hk_daily_em": "stock_hk_index_daily_em", "stock_us_hist_em": "stock_us_hist",
                "index_hk_daily_sina": "stock_hk_index_daily_sina", "stock_hk_hist_em": "stock_hk_hist"}
HAS_YF = importlib.util.find_spec("yfinance") is not None


# ----------------------------------------------------------------- polite networking
class RateLimited(Exception):
    pass


_last_call: dict[str, float] = {}
_blocked: set[str] = set()          # rate-limited this pass (cleared after the cool-down)
_down: set[str] = set()             # unreachable this run (not retried)
_consec_fail: dict[str, int] = {}
_cache: dict[tuple, object] = {}


def looks_rate_limited(e: Exception) -> bool:
    t = f"{type(e).__name__} {e}".lower()
    return any(k in t for k in ("ratelimit", "rate limit", "限流", "too many requests")) or bool(re.search(r"\b(429|418|456)\b", t))


def is_transient(e: Exception) -> bool:
    status = getattr(getattr(e, "response", None), "status_code", None)
    if isinstance(status, int) and 400 <= status < 500:      # 4xx (other than rate limits) will not fix itself
        return False
    mod = type(e).__module__ or ""
    return isinstance(e, (ConnectionError, TimeoutError, OSError)) or mod.startswith(("requests", "urllib3", "http"))


def call(host: str, fn, *args, **kwargs):
    """One paced, cached, retried request to `host`. Raises RateLimited if the host is (or becomes) blocked."""
    key = (host, getattr(fn, "__name__", str(fn)), args, tuple(sorted(kwargs.items())))
    if key in _cache:
        return _cache[key]
    if host in _blocked:
        raise RateLimited(f"{host} rate-limited earlier in this pass; not called")
    if host in _down:
        raise ConnectionError(f"{host} unreachable earlier in this run; not called")
    for attempt in range(RETRIES + 1):
        wait = HOST_GAP_S.get(host, 1.0) - (time.monotonic() - _last_call.get(host, -1e9))
        if wait > 0:
            time.sleep(wait)
        try:
            result = fn(*args, **kwargs)
            _cache[key] = result
            _consec_fail[host] = 0
            return result
        except Exception as e:
            if looks_rate_limited(e):
                _blocked.add(host)
                raise RateLimited(f"{host} rate-limited us ({type(e).__name__})") from None
            if not is_transient(e):
                raise
            if attempt == RETRIES:
                _consec_fail[host] = _consec_fail.get(host, 0) + 1
                if _consec_fail[host] >= DOWN_AFTER:
                    _down.add(host)
                raise
            time.sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
        finally:
            _last_call[host] = time.monotonic()


class ChainFailed(Exception):
    def __init__(self, item: str, errors: list[str], rate_limited: bool):
        super().__init__("; ".join(errors))
        self.item, self.rate_limited = item, rate_limited


def first_ok(item: str, providers: list[tuple]):
    """providers: [(label, thunk)], thunk() -> result. Returns (label, result) from the first that works."""
    errors, rate = [], False
    for label, thunk in providers:
        try:
            return label, thunk()
        except RateLimited as e:
            rate = True
            errors.append(f"{label}: {e}")
        except Exception as e:
            errors.append(f"{label}: {type(e).__name__}: {str(e)[:120]}")
    raise ChainFailed(item, errors, rate)


# ----------------------------------------------------------------- helpers
def akfn(name: str):
    import akshare as ak
    try:
        return getattr(ak, name)
    except AttributeError:
        old = AK_OLD_NAMES.get(name)
        if old and hasattr(ak, old):
            return getattr(ak, old)
        raise AttributeError(f"akshare {getattr(ak, '__version__', '?')} has neither {name} nor {old}")


def wilder_rsi(closes: pd.Series, n: int = RSI_N) -> float | None:
    c = pd.to_numeric(closes, errors="coerce").dropna().to_numpy()
    if len(c) < n + 1:
        return None
    d = c[1:] - c[:-1]
    gain, loss = d.clip(min=0), (-d).clip(min=0)
    g, l = gain[:n].mean(), loss[:n].mean()
    for i in range(n, len(d)):
        g = (g * (n - 1) + gain[i]) / n
        l = (l * (n - 1) + loss[i]) / n
    return 100.0 if l == 0 else float(100 - 100 / (1 + g / l))


def pick_col(df: pd.DataFrame, candidates: tuple[str, ...], what: str) -> str:
    for c in candidates:
        if c in df.columns:
            return c
    raise KeyError(f"{what}: none of {candidates} in columns {list(df.columns)}")


def checked(s: pd.Series, what: str, min_rows: int = 2) -> pd.Series:
    """Reject too-short or stale data so the next source in the chain is tried."""
    if len(s) < min_rows:
        raise ValueError(f"{what}: only {len(s)} rows (need {min_rows})")
    age = (pd.Timestamp(datetime.now().date()) - s.index[-1]).days
    if age > MAX_AGE_DAYS:
        raise ValueError(f"{what}: stale, latest {iso(s.index[-1])} ({age} days old)")
    return s


def daily_series(df: pd.DataFrame, value_cols: tuple[str, ...], what: str, min_rows: int = 2) -> pd.Series:
    """Date-indexed float series, oldest→newest, NaNs dropped, validated by checked()."""
    dcol = pick_col(df, ("date", "日期"), what)
    vcol = pick_col(df, value_cols, what)
    s = pd.Series(pd.to_numeric(df[vcol], errors="coerce").to_numpy(),
                  index=pd.to_datetime(df[dcol]).dt.normalize(), name=what)
    s = s.dropna().sort_index()
    s = s[~s.index.duplicated(keep="last")]
    return checked(s, what, min_rows)


def trailing_pct(s: pd.Series, at: pd.Timestamp) -> float | None:
    """Percentile rank (0–100) of the value at `at` within its trailing PCT_WINDOW observations."""
    w = s.loc[:at].tail(PCT_WINDOW)
    if len(w) < PCT_MIN_PERIODS:
        return None
    return float(w.rank(pct=True).iloc[-1] * 100)


def iso(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


def utc_day(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def yf_close(symbol: str) -> pd.Series:
    import yfinance as yf
    df = yf.Ticker(symbol).history(period="2y", auto_adjust=False)
    return pd.Series(df["Close"].to_numpy(), index=pd.to_datetime([d.date() for d in df.index])).dropna()


# ----------------------------------------------------------------- source chains
def cnn_direct() -> dict:
    import requests
    r = requests.get(CNN_URL, timeout=15, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
        "Referer": "https://edition.cnn.com/", "Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def cnn_providers():
    def lib():
        import fear_greed
        return call("cnn", fear_greed.fetch)
    return [("fear-greed library", lib), ("CNN direct", lambda: call("cnn", cnn_direct))]


def etf_providers(t: str):
    code = t.replace(".HK", "").zfill(5)
    if ETFS[t] == "US":
        p = [("Sina", lambda: (daily_series(call("sina", akfn("stock_us_hist_sina"), symbol=t), ("close",), f"Sina {t}", RSI_N + 1),
                               "https://finance.sina.com.cn/stock/usstock/sector.shtml", "akshare.stock_us_hist_sina")),
             ("Eastmoney", lambda: (daily_series(call("em-quote", akfn("stock_us_hist_em"), symbol=EM_US[t]), ("收盘", "close"), f"EM {t}", RSI_N + 1),
                                    f"https://quote.eastmoney.com/us/{t}.html", "akshare.stock_us_hist_em"))]
    else:
        p = [("Sina", lambda: (daily_series(call("sina", akfn("stock_hk_hist_sina"), symbol=code), ("close",), f"Sina {t}", RSI_N + 1),
                               f"https://stock.finance.sina.com.cn/hkstock/quotes/{code}.html", "akshare.stock_hk_hist_sina")),
             ("Eastmoney", lambda: (daily_series(call("em-quote", akfn("stock_hk_hist_em"), symbol=code), ("收盘", "close"), f"EM {t}", RSI_N + 1),
                                    f"https://quote.eastmoney.com/hk/{code}.html", "akshare.stock_hk_hist_em"))]
    if HAS_YF:
        p.append(("Yahoo", lambda: (checked(call("yahoo", yf_close, t), f"Yahoo {t}", RSI_N + 1),
                                    f"https://finance.yahoo.com/quote/{t}", "yfinance")))
    return p


def hk_index_providers(sym: str):
    p = [("Sina", lambda: (daily_series(call("sina", akfn("index_hk_daily_sina"), symbol=sym), ("close",), f"Sina {sym}"),
                           f"https://stock.finance.sina.com.cn/hkstock/quotes/{sym}.html")),
         ("Eastmoney", lambda: (daily_series(call("em-quote", akfn("index_hk_daily_em"), symbol=sym), ("latest", "close", "收盘"), f"EM {sym}"),
                                f"https://quote.eastmoney.com/gb/zs{sym}.html"))]
    if sym == "HSI":
        p.append(("Eastmoney Stock Connect table",
                  lambda: (daily_series(call("em-data", akfn("stock_hsgt_hist_em"), symbol="港股通沪"), ("恒生指数",), "EM hsgt HSI"), HSGT_SRC)))
        if HAS_YF:
            p.append(("Yahoo", lambda: (checked(call("yahoo", yf_close, "^HSI"), "Yahoo ^HSI"), "https://finance.yahoo.com/quote/%5EHSI")))
    return p


def http_json(url: str) -> dict:
    import requests
    r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (snapshot script)", "Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def utc_today() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(timezone.utc).date())


def completed_days(s: pd.Series) -> pd.Series:
    """Crypto trades 24/7: keep only fully closed UTC days (drop today's candle in progress)."""
    return s[s.index < utc_today()]


def kraken_close(pair: str) -> pd.Series:
    j = http_json(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=1440")
    if j.get("error"):
        raise ValueError(f"Kraken {pair}: {j['error']}")
    rows = next(v for k, v in j["result"].items() if k != "last")          # [time, open, high, low, close, ...]
    s = pd.Series([float(r[4]) for r in rows], index=pd.to_datetime([int(r[0]) for r in rows], unit="s").normalize())
    return completed_days(s)


def coingecko_close(cid: str) -> pd.Series:
    j = http_json(f"https://api.coingecko.com/api/v3/coins/{cid}/market_chart?vs_currency=usd&days=365&interval=daily")
    # Daily points sit at (about) 00:00 UTC = the close of the previous UTC day; the final point is the live price.
    pts = [(pd.to_datetime(ms, unit="ms"), float(p)) for ms, p in j["prices"]]
    pts = [(t.normalize() - pd.Timedelta(days=1), p) for t, p in pts if t - t.normalize() < pd.Timedelta(hours=1)]
    s = pd.Series(dict(pts), dtype=float).sort_index()
    return completed_days(s[~s.index.duplicated(keep="last")])


def crypto_providers(sym: str):
    pair, cid, ysym = CRYPTO[sym]
    p = [("Kraken", lambda: (checked(call("kraken", kraken_close, pair), f"Kraken {pair}", RSI_N + 1),
                             f"https://pro.kraken.com/app/trade/{sym.lower()}-usd", "public OHLC API (UTC daily close)")),
         ("CoinGecko", lambda: (checked(call("coingecko", coingecko_close, cid), f"CoinGecko {cid}", RSI_N + 1),
                                f"https://www.coingecko.com/en/coins/{cid}", "market_chart API (UTC daily close)"))]
    if HAS_YF:
        p.append(("Yahoo", lambda: (checked(completed_days(call("yahoo", yf_close, ysym)), f"Yahoo {ysym}", RSI_N + 1),
                                    f"https://finance.yahoo.com/quote/{ysym}", "yfinance")))
    return p


def crypto_fg_providers():
    return [("Alternative.me", lambda: call("altme", http_json, ALTME_URL))]


def southbound_providers():
    def combined():
        sh = daily_series(call("em-data", akfn("stock_hsgt_hist_em"), symbol="港股通沪"), ("当日成交净买额",), "EM 港股通沪")
        sz = daily_series(call("em-data", akfn("stock_hsgt_hist_em"), symbol="港股通深"), ("当日成交净买额",), "EM 港股通深")
        return checked((sh + sz).dropna(), "EM 港股通沪+深")
    return [("Eastmoney 南向资金", lambda: daily_series(call("em-data", akfn("stock_hsgt_hist_em"), symbol="南向资金"),
                                                    ("当日成交净买额",), "EM 南向资金")),
            ("Eastmoney 港股通沪+深", combined)]


# ----------------------------------------------------------------- item handlers
def apply_cnn(snap, label, raw, got):
    fg = raw["fear_and_greed"]
    hist = {utc_day(p["x"]): {"d": utc_day(p["x"]), "v": round(p["y"], 1)} for p in raw["fear_and_greed_historical"]["data"]}
    keys = ["market_momentum_sp500", "stock_price_strength", "stock_price_breadth", "put_call_options",
            "market_volatility_vix", "junk_bond_demand", "safe_haven_demand"]
    us = snap["us"]
    us["fearGreed"] = {"value": round(fg["score"], 1), "rating": fg["rating"], "asOf": fg["timestamp"][:10], "src": CNN_SRC,
                       "via": label, "history": list(hist.values())[-HISTORY_DAYS:],
                       "components": {k: {"score": round(raw[k]["score"], 1), "rating": raw[k]["rating"]} for k in keys if k in raw}}

    def last_two(key):      # CNN appends an intraday point with the same date as the last close: one point per UTC day
        pts = list({utc_day(p["x"]): p for p in raw.get(key, {}).get("data") or []}.values())
        return pts[-2:] if len(pts) >= 2 else None

    if pc := last_two("put_call_options"):
        us["putCall"] = {"value": round(pc[-1]["y"], 3), "asOf": utc_day(pc[-1]["x"]), "src": CNN_SRC,
                         "note": "CBOE equity put/call, 5-day average (as used by CNN)"}
    if vx := last_two("market_volatility_vix"):
        us["vix"] = {"value": round(vx[-1]["y"], 2), "chgPct": round((vx[-1]["y"] / vx[-2]["y"] - 1) * 100, 2),
                     "asOf": utc_day(vx[-1]["x"]), "src": CNN_SRC}


def pct_back(s: pd.Series, days: int) -> float | None:
    """% change from the last close on or before (latest date − days calendar days) to the latest close."""
    ref = s.loc[: s.index[-1] - pd.Timedelta(days=days)]
    return round(float(s.iloc[-1] / ref.iloc[-1] - 1) * 100, 2) if len(ref) else None


def apply_etf(t):
    def f(snap, label, res, got):
        s, src, fn = res
        s = s.tail(300)
        snap["etfs"][t] = {"price": round(float(s.iloc[-1]), 3), "chg": round(float(s.iloc[-1] / s.iloc[-2] - 1) * 100, 2),
                           **{f"chg{n}": pct_back(s, n) for n in PERIODS_D},
                           "rsi": round(wilder_rsi(s), 2), "asOf": iso(s.index[-1]), "src": src,
                           "srcName": f"{label} via {fn} · RSI(14, Wilder) from {len(s)} closes"}
    return f


def apply_index(key):
    def f(snap, label, res, got):
        s, src = res
        got[key] = s
        cn = snap["cn"]
        if key == "hsi":
            last252 = s.tail(252)
            cn["hsi52wHigh"] = {"value": round(float(last252.max()), 2), "date": iso(last252.idxmax()), "src": src}
            cn["hsiCloses"] = [{"d": iso(d), "v": round(float(v), 2), "src": src} for d, v in s.tail(HISTORY_DAYS).items()]
        else:
            cn[key] = {"value": round(float(s.iloc[-1]), 2), "chgPct": round(float(s.iloc[-1] / s.iloc[-2] - 1) * 100, 2),
                       "asOf": iso(s.index[-1]), "src": src, "via": label}
    return f


def apply_south(snap, label, daily, got):
    five = daily.rolling(5).sum().dropna()
    snap["cn"]["southbound"] = {"daily100m": round(float(daily.iloc[-1]), 2), "fiveDay100m": round(float(five.iloc[-1]), 2),
                                "asOf": iso(daily.index[-1]), "src": HSGT_SRC, "via": label,
                                "note": "Eastmoney net buy, unit 亿 (100 million)"}


def apply_crypto_fg(snap, label, j, got):
    data = sorted(j.get("data") or [], key=lambda r: int(r["timestamp"]))     # timestamp = UTC day of the reading
    if not data:
        raise ValueError(f"Alternative.me: no data ({(j.get('metadata') or {}).get('error')})")
    day = lambda r: datetime.fromtimestamp(int(r["timestamp"]), tz=timezone.utc).strftime("%Y-%m-%d")
    hist = list({day(r): {"d": day(r), "v": float(r["value"])} for r in data}.values())
    latest = data[-1]
    snap["crypto"]["fearGreed"] = {"value": float(latest["value"]), "rating": latest["value_classification"],
                                   "asOf": day(latest), "src": ALTME_SRC, "via": label, "history": hist[-HISTORY_DAYS:]}


ITEMS = [("CNN Fear & Greed", cnn_providers, apply_cnn)] + \
        [(t, (lambda t=t: etf_providers(t)), apply_etf(t)) for t in ETFS] + \
        [(f"HK index {s}", (lambda s=s: hk_index_providers(s)), apply_index(k)) for k, s in (("hsi", "HSI"), ("hstech", "HSTECH"), ("vhsi", "VHSI"))] + \
        [("Southbound flow", southbound_providers, apply_south)] + \
        [("Crypto Fear & Greed", crypto_fg_providers, apply_crypto_fg)] + \
        [(s, (lambda s=s: crypto_providers(s)), apply_etf(s)) for s in CRYPTO]


def hk_fear_greed(hsi: pd.Series | None, vhsi: pd.Series | None, dates) -> list[dict]:
    """CNN-style HK Fear & Greed: equal-weight mean of 1-year percentile ranks of
    momentum (HSI/MA125), strength (HSI/52w high) and inverted volatility (VHSI/MA50)."""
    comps = []
    if hsi is not None and len(hsi) >= 252:
        comps.append(("Momentum (HSI vs 125d MA)", (hsi / hsi.rolling(125).mean() - 1).dropna(), False))
        comps.append(("Strength (HSI vs 52w high)", (hsi / hsi.rolling(252).max() - 1).dropna(), False))
    if vhsi is not None and len(vhsi) >= 60:
        comps.append(("Volatility (VHSI vs 50d MA, inverted)", (vhsi / vhsi.rolling(50).mean() - 1).dropna(), True))
    rows = []
    for d in dates:
        parts = []
        for name, s, invert in comps:
            upto = s.loc[:d]
            if upto.empty or (d - upto.index[-1]).days > 5:
                continue
            p = trailing_pct(s, upto.index[-1])
            if p is not None:
                parts.append((name, 100 - p if invert else p))
        if parts:
            rows.append({"d": iso(d), "v": round(sum(v for _, v in parts) / len(parts), 1),
                         "parts": [{"name": n, "weight": round(1 / len(parts), 2), "score": round(v, 1)} for n, v in parts]})
    return rows


# ----------------------------------------------------------------- main
def load_previous(path: Path) -> dict:
    if not path.exists():
        return {}
    m = re.search(r"window\.SNAPSHOT\s*=\s*(\{.*\});?\s*$", path.read_text(encoding="utf-8"), re.S)
    try:
        return json.loads(m.group(1)) if m else {}
    except json.JSONDecodeError:
        return {}


def run_items(items, snap, got) -> list[ChainFailed]:
    failed = []
    for name, providers, apply in items:
        try:
            label, res = first_ok(name, providers())
            apply(snap, label, res, got)
            print(f"  ok  {name:<18} <- {label}")
        except ChainFailed as e:
            failed.append(e)
            print(f"  --  {name:<18} failed: {e}")
        except Exception as e:                       # bad data from a source that answered
            failed.append(ChainFailed(name, [f"{type(e).__name__}: {e}"], False))
            print(f"  --  {name:<18} failed: {type(e).__name__}: {e}")
    return failed


def main() -> int:
    for stream in (sys.stdout, sys.stderr):      # Windows consoles default to cp1252; source names are Chinese
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).with_name("snapshot-data.js")))
    args = ap.parse_args()
    out_path = Path(args.out)
    prev = load_previous(out_path)
    snap = {"generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "us": dict(prev.get("us", {})), "cn": dict(prev.get("cn", {})), "etfs": dict(prev.get("etfs", {})),
            "crypto": dict(prev.get("crypto", {})), "errors": []}
    got: dict[str, pd.Series] = {}

    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] snapshot run · yfinance {'available' if HAS_YF else 'not installed'}")
    failed = run_items(ITEMS, snap, got)

    retry = [f for f in failed if f.rate_limited]
    if retry and COOLDOWN_S > 0:
        print(f"  .. {len(retry)} item(s) hit a rate limit; pausing {COOLDOWN_S}s before one more try")
        time.sleep(COOLDOWN_S)
        _blocked.clear()
        names = {f.item for f in retry}
        again = run_items([it for it in ITEMS if it[0] in names], snap, got)
        failed = [f for f in failed if f.item not in names] + again

    snap["errors"] = [f"{f.item}: {f}" for f in failed]

    # HK Fear & Greed (CNN-style, derived) from whatever inputs we have this run
    vhsi, hsi = got.get("vhsi"), got.get("hsi")
    if hsi is not None or vhsi is not None:
        base = hsi if hsi is not None else vhsi
        hist = hk_fear_greed(hsi, vhsi, list(base.tail(HISTORY_DAYS).index))
        if hist:
            snap["cn"]["sentiment"] = {**hist[-1], "history": [{"d": r["d"], "v": r["v"]} for r in hist],
                                       "method": "CNN-style HK Fear & Greed: momentum, price strength and volatility (inverted), equal weights, 1-year percentiles",
                                       "src": "https://www.cnn.com/markets/fear-and-greed"}

    dates = [v.get("asOf") for v in snap["etfs"].values()] + [snap["us"].get("fearGreed", {}).get("asOf")]
    snap["asOf"] = max((d for d in dates if d), default=None)

    out_path.write_text("/* Generated by update_snapshot.py - do not edit by hand */\nwindow.SNAPSHOT = "
                        + json.dumps(snap, ensure_ascii=True, indent=1) + ";\n", encoding="utf-8")
    print(f"wrote {out_path} · as of {snap['asOf']} · {len(snap['errors'])} error(s)")
    return 0 if not snap["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
