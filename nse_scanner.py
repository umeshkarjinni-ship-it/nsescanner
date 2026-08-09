"""
NSE/BSE Weekly & Monthly Trend-Change Scanner
==============================================
Scans a universe of large/mid/small cap stocks and flags BUY / SELL
trend-change signals using the same logic as the companion TradingView
Pine Script (Volatility Stop + ATR + EMA trend filter + RSI + Volume).

This is a research / decision-support tool, NOT financial advice.
Signals are based purely on price/volume technicals with no fundamental
or macro context. Validate thoroughly before acting on any signal.

SETUP
-----
1. pip install -r requirements.txt
2. Edit stocks_universe.csv to include the stocks you want to track
   (a starter list is provided — replace/expand it with the full NSE
   list from https://www.nseindia.com/market-data/securities-available-for-trading
   or an index constituent list such as Nifty 500).
3. Run once manually to test:  python nse_scanner.py
4. Schedule it to run daily (see "SCHEDULING" section at the bottom of
   this file, or the README notes below).

OUTPUT
------
- Prints a summary table to the console.
- Writes a timestamped CSV to the "signals/" folder with every stock's
  latest Weekly and Monthly trend + signal.
- Optionally emails / sends a Telegram message when new BUY or SELL
  signals are found (disabled by default — see NOTIFY_* settings).
"""

import os
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from datetime import datetime
from io import BytesIO

import numpy as np
import pandas as pd
import yfinance as yf
import requests
import matplotlib
matplotlib.use("Agg")  # non-interactive backend, no display needed on a server/scheduled task
import matplotlib.pyplot as plt

# =========================================================================
# CONFIG — tweak these to match the Pine Script inputs
# =========================================================================
UNIVERSE_CSV = "stocks_universe.csv"     # Symbol, Name, Category columns
# For the full ~1900-symbol NSE universe, run build_universe.py first, then
# point this at its output instead:
#   UNIVERSE_CSV = "stocks_universe_full.csv"

INCLUDE_OTHER_CATEGORY = False           # "Other" = micro-caps/recent listings/
                                          # thin liquidity from build_universe.py.
                                          # Set True to scan them too (slower,
                                          # noisier signals due to low volume).

OUTPUT_DIR = "signals"

# Volatility Stop / ATR
ATR_LEN = 14
ATR_MULT = 3.0

# Trend Filter — KAMA (Kaufman's Adaptive MA) replaces fixed EMA. KAMA
# automatically slows down in choppy conditions and speeds up when trending,
# instead of using one fixed smoothing speed for every market regime.
KAMA_FAST_LEN = 20                # replaces old EMA_FAST
KAMA_MID_LEN = 50                 # replaces old EMA_MID
KAMA_SLOW_LEN = 100                # replaces old EMA_SLOW
KAMA_FASTEST_SC = 2                # KAMA's internal fastest smoothing period
KAMA_SLOWEST_SC = 30               # KAMA's internal slowest smoothing period
REQUIRE_MA_STACK = True           # KAMA_fast > KAMA_mid > KAMA_slow

# RSI momentum filter
RSI_LEN = 14
RSI_BUY_LEVEL = 50
RSI_OVERBOUGHT = 78

# Volume confirmation
VOL_MA_LEN = 20
VOL_MULT_REQ = 1.2

# On-Balance Volume (OBV) trend confirmation — checks that volume is
# actually flowing IN alongside the price rise (not just spiking on isolated
# days), catching cases where volume is quietly leaking out on down days.
USE_OBV_FILTER = True
OBV_MA_LEN = 10

# Relative Strength vs NIFTY 50 — requires the stock to be outperforming the
# index, not just drifting up with the broader market. Catches genuine
# leaders rather than passive followers.
USE_RELATIVE_STRENGTH_FILTER = True
RS_MA_LEN = 10

# Trend-strength filter (ADX) — avoids taking signals in choppy/sideways
# conditions, which is where most false VStop flips happen.
ADX_LEN = 14
ADX_THRESHOLD = 20                # require ADX above this to accept a BUY

# Market regime filter — only take a BUY on a stock if the NIFTY 50 index
# itself is in an uptrend on the same timeframe. Cuts down on buying
# individual stocks against the broader market current.
USE_MARKET_REGIME_FILTER = True
REGIME_INDEX_TICKER = "^NSEI"     # NIFTY 50 on Yahoo Finance

# Multi-timeframe confluence — a Weekly BUY only counts as high-quality if
# the Monthly trend (the "bigger picture") is also UP. Monthly signals are
# already the highest timeframe here, so they don't need a further filter.
REQUIRE_MONTHLY_CONFLUENCE_FOR_WEEKLY = True

# Anti-whipsaw buffer — requires price to close beyond the Volatility Stop
# by this percentage before a trend flip is registered, instead of any
# close beyond it. Cuts down on flip-then-immediately-flip-back whipsaws.
VSTOP_WHIPSAW_BUFFER_PCT = 0.5    # 0.5 = require a 0.5% buffer past the stop

# Earnings-week avoidance — many "false" technical breakouts are really
# pre-earnings volatility the chart can't distinguish from a real breakout.
# OFF by default: this adds one extra network call PER STOCK (via yfinance's
# earnings calendar), which meaningfully slows down a 1500-stock scan and is
# itself an unofficial/sometimes-missing data source. Turn on for smaller
# watchlists where the extra time is acceptable.
USE_EARNINGS_AVOIDANCE = False
EARNINGS_AVOID_DAYS = 5           # skip BUY if earnings due within N days

# Data
HISTORY_PERIOD = "10y"            # yfinance period to download (need enough
                                   # daily bars to build stable monthly EMA100)
BATCH_SIZE = 40                   # tickers per yfinance batch download
BATCH_SLEEP_SEC = 3               # pause between batches (avoid rate limits)
EXCHANGE_SUFFIX = ".NS"           # ".NS" = NSE, ".BO" = BSE
CHECKPOINT_EVERY_N_BATCHES = 5    # autosave partial results periodically —
                                   # matters at 1500+ stocks since a full run
                                   # can take well over an hour and you don't
                                   # want to lose everything to one crash/
                                   # network drop

# Notifications
NOTIFY_EMAIL = True               # set to False to disable
NOTIFY_TELEGRAM = False

EMAIL_FROM = os.environ.get("SCANNER_EMAIL_FROM", "")
EMAIL_TO = os.environ.get("SCANNER_EMAIL_TO", "umesh.karjinni@gmail.com")
EMAIL_APP_PASSWORD = os.environ.get("SCANNER_EMAIL_APP_PASSWORD", "")  # Gmail app password
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587

TELEGRAM_BOT_TOKEN = os.environ.get("SCANNER_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("SCANNER_TELEGRAM_CHAT_ID", "")

# Email chart images — small inline PNG per BUY signal, kept deliberately
# tiny to keep the total email size small.
EMAIL_INCLUDE_CHARTS = True
EMAIL_MAX_CHARTS = 10              # cap embedded charts regardless of how
                                    # many BUY signals fire, so the email
                                    # can't balloon on a big signal day
EMAIL_CHART_WIDTH_IN = 3.6
EMAIL_CHART_HEIGHT_IN = 2.0
EMAIL_CHART_DPI = 80               # ~15-30 KB per chart at this size/DPI
EMAIL_CHART_LOOKBACK_WEEKLY = 60   # bars of history to show
EMAIL_CHART_LOOKBACK_MONTHLY = 36

# Limit the email/console report to the top N signals by trend strength
# (ADX) — the full, unfiltered results still get saved to the CSV, this
# only trims what's reported/emailed daily.
TOP_N_BUY = 20
TOP_N_SELL = 20


# =========================================================================
# INDICATOR LOGIC (mirrors the Pine Script f_signal() function)
# =========================================================================
def wilder_smooth(series: pd.Series, length: int) -> pd.Series:
    """Wilder's RMA smoothing — matches Pine Script's ta.atr / ta.rsi internals."""
    return series.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def compute_atr(df: pd.DataFrame, length: int) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return wilder_smooth(tr, length)


def compute_rsi(close: pd.Series, length: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = wilder_smooth(gain, length)
    avg_loss = wilder_smooth(loss, length)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)


def compute_kama(series: pd.Series, er_length: int, fastest: int = 2, slowest: int = 30) -> pd.Series:
    """
    Kaufman's Adaptive Moving Average. Unlike EMA (fixed smoothing speed),
    KAMA speeds up when the market is trending efficiently and slows down
    in choppy/sideways conditions, based on the Efficiency Ratio (ER).
    """
    change = series.diff(er_length).abs()
    volatility = series.diff().abs().rolling(er_length).sum()
    er = (change / volatility.replace(0, np.nan)).fillna(0)

    fast_sc = 2 / (fastest + 1)
    slow_sc = 2 / (slowest + 1)
    sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2

    kama = pd.Series(np.nan, index=series.index)
    values = series.values
    sc_values = sc.values
    n = len(series)

    first_valid = er_length
    if first_valid >= n:
        return kama

    kama_vals = np.full(n, np.nan)
    kama_vals[first_valid] = values[first_valid]
    for i in range(first_valid + 1, n):
        prev = kama_vals[i - 1]
        if np.isnan(prev):
            kama_vals[i] = values[i]
        else:
            s = sc_values[i] if not np.isnan(sc_values[i]) else 0
            kama_vals[i] = prev + s * (values[i] - prev)

    return pd.Series(kama_vals, index=series.index)


def compute_obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume — cumulative volume signed by the direction of price change."""
    direction = np.sign(df["Close"].diff()).fillna(0)
    return (direction * df["Volume"]).cumsum()

def compute_adx(df: pd.DataFrame, length: int) -> pd.Series:
    """
    Wilder's ADX — measures trend STRENGTH regardless of direction.
    Used as a filter: only accept a trend-following signal when ADX shows
    the stock is actually trending, not chopping sideways (VStop's most
    common source of false flips).
    """
    high, low, close = df["High"], df["Low"], df["Close"]
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_dm = pd.Series(plus_dm, index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    atr = wilder_smooth(tr, length)
    plus_di = 100 * wilder_smooth(plus_dm, length) / atr.replace(0, np.nan)
    minus_di = 100 * wilder_smooth(minus_dm, length) / atr.replace(0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = wilder_smooth(dx.fillna(0), length)
    return adx.fillna(0)


def compute_vstop(df: pd.DataFrame, atr_len: int, atr_mult: float, whipsaw_buffer_pct: float = 0.0) -> pd.DataFrame:
    """
    Recursive Volatility Stop — identical logic to the Pine Script version:
    trailing stop that only tightens with the trend and flips when price
    crosses it.

    whipsaw_buffer_pct: require price to close beyond the stop by this
    percentage before registering a flip, instead of any close beyond it.
    Reduces flip-then-flip-back whipsaws in choppy conditions. 0 = off
    (original behavior).
    """
    atr_val = atr_mult * compute_atr(df, atr_len)
    close = df["Close"].values
    atr_arr = atr_val.values
    buf = whipsaw_buffer_pct / 100.0

    n = len(df)
    stop = np.full(n, np.nan)
    uptrend = np.full(n, True)
    extreme = np.full(n, np.nan)
    flip_up = np.full(n, False)
    flip_down = np.full(n, False)

    trend = True
    ext = np.nan
    stp = np.nan

    for i in range(n):
        src = close[i]
        a = atr_arr[i]
        if np.isnan(a):
            stop[i], uptrend[i], extreme[i] = stp, trend, ext
            continue

        ext = src if np.isnan(ext) else (max(ext, src) if trend else min(ext, src))
        new_stop = (ext - a) if trend else (ext + a)

        stp = new_stop if np.isnan(stp) else (max(stp, new_stop) if trend else min(stp, new_stop))

        flip = (src < stp * (1 - buf)) if trend else (src > stp * (1 + buf))
        if flip:
            trend = not trend
            ext = src
            stp = (src - a) if trend else (src + a)
            flip_up[i] = trend
            flip_down[i] = not trend

        stop[i], uptrend[i], extreme[i] = stp, trend, ext

    out = df.copy()
    out["ATR_STOP"] = atr_val
    out["VSTOP"] = stop
    out["UPTREND"] = uptrend
    out["FLIP_UP"] = flip_up
    out["FLIP_DOWN"] = flip_down
    return out


def compute_signals(df: pd.DataFrame, nifty_close: pd.Series = None) -> pd.DataFrame:
    """
    Full signal stack: VStop (with anti-whipsaw buffer) + KAMA trend filter
    + RSI + Volume + OBV + ADX + Relative Strength vs NIFTY (if provided).
    """
    min_len = max(KAMA_SLOW_LEN, ATR_LEN, RSI_LEN, VOL_MA_LEN, ADX_LEN, KAMA_SLOWEST_SC) + 2
    if df.empty or len(df) < min_len:
        return pd.DataFrame()

    out = compute_vstop(df, ATR_LEN, ATR_MULT, VSTOP_WHIPSAW_BUFFER_PCT)

    out["KAMA_FAST"] = compute_kama(out["Close"], KAMA_FAST_LEN, KAMA_FASTEST_SC, KAMA_SLOWEST_SC)
    out["KAMA_MID"] = compute_kama(out["Close"], KAMA_MID_LEN, KAMA_FASTEST_SC, KAMA_SLOWEST_SC)
    out["KAMA_SLOW"] = compute_kama(out["Close"], KAMA_SLOW_LEN, KAMA_FASTEST_SC, KAMA_SLOWEST_SC)

    out["RSI"] = compute_rsi(out["Close"], RSI_LEN)
    out["VOL_MA"] = out["Volume"].rolling(VOL_MA_LEN).mean()
    out["ADX"] = compute_adx(out, ADX_LEN)

    out["OBV"] = compute_obv(out)
    out["OBV_MA"] = out["OBV"].rolling(OBV_MA_LEN).mean()

    if REQUIRE_MA_STACK:
        ma_trend_ok = (out["KAMA_FAST"] > out["KAMA_MID"]) & (out["KAMA_MID"] > out["KAMA_SLOW"])
    else:
        ma_trend_ok = out["Close"] > out["KAMA_MID"]

    rsi_ok = (out["RSI"] > RSI_BUY_LEVEL) & (out["RSI"] < RSI_OVERBOUGHT)
    vol_ok = out["Volume"] > (out["VOL_MA"] * VOL_MULT_REQ)
    adx_ok = out["ADX"] > ADX_THRESHOLD
    obv_ok = (out["OBV"] > out["OBV_MA"]) if USE_OBV_FILTER else pd.Series(True, index=out.index)

    if USE_RELATIVE_STRENGTH_FILTER and nifty_close is not None and not nifty_close.empty:
        nifty_aligned = nifty_close.reindex(out.index).ffill()
        rs_line = out["Close"] / nifty_aligned.replace(0, np.nan)
        rs_ma = rs_line.rolling(RS_MA_LEN).mean()
        rs_ok = rs_line > rs_ma
        out["RS_LINE"] = rs_line
    else:
        rs_ok = pd.Series(True, index=out.index)
        out["RS_LINE"] = np.nan

    out["OBV_OK"] = obv_ok
    out["RS_OK"] = rs_ok

    # Core signal on THIS timeframe alone — regime and multi-timeframe
    # confluence are layered on afterward in scan(), since those need
    # data from other symbols/timeframes this function doesn't see.
    out["BUY_SIGNAL_CORE"] = out["FLIP_UP"] & ma_trend_ok & rsi_ok & vol_ok & adx_ok & obv_ok & rs_ok
    out["SELL_SIGNAL"] = out["FLIP_DOWN"]
    return out


# =========================================================================
# DATA FETCHING
# =========================================================================
def load_universe(path: str) -> pd.DataFrame:
    uni = pd.read_csv(path)
    uni.columns = [c.strip() for c in uni.columns]
    return uni


def resample(df_daily: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    r = df_daily.resample(rule).agg(agg).dropna(how="any")
    return r


def fetch_batch(tickers: list) -> dict:
    """Download daily OHLCV for a batch of tickers, return dict[ticker] -> DataFrame."""
    data = yf.download(
        tickers=tickers,
        period=HISTORY_PERIOD,
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        threads=True,
        progress=False,
    )
    result = {}
    if len(tickers) == 1:
        t = tickers[0]
        if not data.empty:
            result[t] = data.dropna(how="all")
        return result

    for t in tickers:
        try:
            sub = data[t].dropna(how="all")
            if not sub.empty:
                result[t] = sub
        except (KeyError, Exception):
            continue
    return result


def fetch_single(ticker: str, retries: int = 2, sleep_between: float = 3.0):
    """
    Fetch one ticker on its own, with retries. Used to recover symbols that
    failed inside a batch download (often a transient rate-limit/network
    blip rather than a genuinely delisted symbol).
    """
    for attempt in range(1, retries + 1):
        try:
            df = yf.download(
                tickers=ticker,
                period=HISTORY_PERIOD,
                interval="1d",
                auto_adjust=True,
                threads=False,
                progress=False,
            )
            df = df.dropna(how="all")
            if not df.empty:
                return df
        except Exception:
            pass
        time.sleep(sleep_between)
    return None


def fetch_nifty_daily():
    """Single download of NIFTY 50 daily data, reused for both the market
    regime filter and the relative-strength filter (avoids fetching twice)."""
    if not (USE_MARKET_REGIME_FILTER or USE_RELATIVE_STRENGTH_FILTER):
        return None
    print(f"Fetching {REGIME_INDEX_TICKER} (used for market regime + relative strength)...")
    try:
        df = yf.download(REGIME_INDEX_TICKER, period=HISTORY_PERIOD, interval="1d",
                          auto_adjust=True, progress=False)
        df = df.dropna(how="all")
        if df.empty:
            print("  Could not fetch index data — regime/RS filters disabled for this run.")
            return None
        return df[["Open", "High", "Low", "Close", "Volume"]]
    except Exception as e:
        print(f"  Index fetch failed ({e}) — regime/RS filters disabled for this run.")
        return None


def compute_market_regime(nifty_daily) -> dict:
    """
    Determine whether NIFTY 50 is currently in an uptrend on Weekly and
    Monthly timeframes, using the exact same VStop logic applied to stocks.
    Returns {'Weekly': bool, 'Monthly': bool}, defaulting to True (i.e. no
    filtering) if index data is unavailable, so a data hiccup doesn't
    silently suppress every signal.
    """
    regime = {"Weekly": True, "Monthly": True}
    if not USE_MARKET_REGIME_FILTER or nifty_daily is None:
        return regime

    for tf_name, rule in (("Weekly", "W"), ("Monthly", "ME")):
        df_tf = resample(nifty_daily, rule)
        vstop_df = compute_vstop(df_tf, ATR_LEN, ATR_MULT, VSTOP_WHIPSAW_BUFFER_PCT)
        if not vstop_df.empty:
            regime[tf_name] = bool(vstop_df.iloc[-1]["UPTREND"])
    print(f"  NIFTY 50 regime — Weekly: {'UP' if regime['Weekly'] else 'DOWN'}, "
          f"Monthly: {'UP' if regime['Monthly'] else 'DOWN'}")
    return regime


def get_next_earnings_days(ticker_symbol: str):
    """
    Days until this stock's next earnings report, or None if unavailable.
    Uses yfinance's earnings calendar — an unofficial/best-effort source,
    so failures are treated as 'unknown' (doesn't block the BUY) rather
    than as a reason to skip.
    """
    try:
        t = yf.Ticker(ticker_symbol)
        edf = t.get_earnings_dates(limit=4)
        if edf is None or edf.empty:
            return None
        now = pd.Timestamp.now(tz=edf.index.tz) if edf.index.tz else pd.Timestamp.now()
        future = edf[edf.index >= now]
        if future.empty:
            return None
        next_date = future.index.min()
        return (next_date - now).days
    except Exception:
        return None


def make_chart_png(sig: pd.DataFrame, symbol: str, timeframe: str, lookback: int) -> bytes:
    """
    Small inline chart for a BUY signal: price, VStop, and the KAMA trend
    stack over the last `lookback` bars. Deliberately tiny (small figsize +
    low DPI) so embedding several of these in one email stays lightweight.
    """
    df = sig.tail(lookback)
    fig, ax = plt.subplots(figsize=(EMAIL_CHART_WIDTH_IN, EMAIL_CHART_HEIGHT_IN), dpi=EMAIL_CHART_DPI)

    ax.plot(df.index, df["Close"], color="#1a73e8", linewidth=1.1, label="Close")
    ax.plot(df.index, df["VSTOP"], color="#e37400", linewidth=0.9, linestyle="--", label="VStop")
    if "KAMA_MID" in df.columns:
        ax.plot(df.index, df["KAMA_MID"], color="#888888", linewidth=0.8, label="KAMA")

    # Mark the buy bar
    ax.scatter([df.index[-1]], [df["Close"].iloc[-1]], color="#188038", marker="^", s=35, zorder=5)

    ax.set_title(f"{symbol} — {timeframe}", fontsize=8)
    ax.tick_params(labelsize=6)
    ax.legend(fontsize=5, loc="upper left", frameon=False)
    fig.autofmt_xdate(rotation=25)
    fig.tight_layout(pad=0.4)

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=EMAIL_CHART_DPI)
    plt.close(fig)
    return buf.getvalue()


# =========================================================================
# SCAN
# =========================================================================
def scan() -> pd.DataFrame:
    uni = load_universe(UNIVERSE_CSV)
    if not INCLUDE_OTHER_CATEGORY and "Category" in uni.columns:
        before = len(uni)
        uni = uni[uni["Category"].str.strip().str.lower() != "other"]
        skipped = before - len(uni)
        if skipped:
            print(f"Skipping {skipped} 'Other' category symbols (set INCLUDE_OTHER_CATEGORY=True to include).")

    tickers = [f"{s.strip()}{EXCHANGE_SUFFIX}" for s in uni["Symbol"]]
    symbol_map = dict(zip(tickers, uni["Symbol"]))
    category_map = dict(zip(uni["Symbol"], uni["Category"]))
    name_map = dict(zip(uni["Symbol"], uni["Name"]))

    rows = []
    charts = {}  # (symbol, timeframe) -> PNG bytes, only for BUY signals
    failures = []  # (symbol, reason) for the end-of-run report
    total = len(tickers)
    n_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"Scanning {total} symbols in {n_batches} batches of up to {BATCH_SIZE}...")
    run_start = time.time()

    nifty_daily = fetch_nifty_daily()
    market_regime = compute_market_regime(nifty_daily)

    for batch_num, i in enumerate(range(0, total, BATCH_SIZE), start=1):
        batch = tickers[i:i + BATCH_SIZE]
        elapsed = time.time() - run_start
        avg_per_batch = elapsed / (batch_num - 1) if batch_num > 1 else None
        eta_str = ""
        if avg_per_batch:
            remaining = avg_per_batch * (n_batches - batch_num + 1)
            eta_str = f" | ETA ~{remaining / 60:.1f} min"
        pct = (batch_num - 1) / n_batches * 100
        print(f"\n[Batch {batch_num}/{n_batches} | {pct:.0f}% done{eta_str}] "
              f"Fetching {i + 1}-{min(i + BATCH_SIZE, total)} of {total}")
        try:
            batch_data = fetch_batch(batch)
        except Exception as e:
            print(f"  Batch failed: {e}")
            batch_data = {}

        # Anything missing from the batch result gets a solo retry —
        # batch failures are frequently transient rate-limit blips, not
        # real delistings.
        missing = [t for t in batch if t not in batch_data]
        for tkr in missing:
            print(f"  Retrying {tkr} individually...")
            solo = fetch_single(tkr)
            if solo is not None and not solo.empty:
                batch_data[tkr] = solo
            else:
                symbol = symbol_map[tkr]
                failures.append((symbol, "No data after retry — check if ticker was renamed/delisted, "
                                          "or try the .BO (BSE) suffix instead of .NS"))

        for tkr, df_daily in batch_data.items():
            symbol = symbol_map[tkr]
            try:
                df_daily = df_daily[["Open", "High", "Low", "Close", "Volume"]].dropna()
                if df_daily.empty:
                    continue

                # Earnings avoidance check — one call per stock (not per
                # timeframe), only if enabled (adds real time at scale).
                earnings_days = None
                if USE_EARNINGS_AVOIDANCE:
                    earnings_days = get_next_earnings_days(tkr)
                earnings_soon = (earnings_days is not None
                                  and 0 <= earnings_days <= EARNINGS_AVOID_DAYS)

                # Compute Monthly FIRST — Weekly needs it for confluence.
                tf_signals = {}
                for tf_name, rule in (("Monthly", "ME"), ("Weekly", "W")):
                    df_tf = resample(df_daily, rule)
                    nifty_tf_close = None
                    if nifty_daily is not None and USE_RELATIVE_STRENGTH_FILTER:
                        nifty_tf = resample(nifty_daily, rule)
                        nifty_tf_close = nifty_tf["Close"]
                    sig = compute_signals(df_tf, nifty_close=nifty_tf_close)
                    tf_signals[tf_name] = sig

                monthly_sig = tf_signals["Monthly"]
                monthly_uptrend = bool(monthly_sig.iloc[-1]["UPTREND"]) if not monthly_sig.empty else True

                for tf_name in ("Weekly", "Monthly"):
                    sig = tf_signals[tf_name]
                    if sig.empty:
                        continue
                    last = sig.iloc[-1]

                    core_buy = bool(last["BUY_SIGNAL_CORE"])
                    regime_ok = market_regime.get(tf_name, True)

                    if tf_name == "Weekly" and REQUIRE_MONTHLY_CONFLUENCE_FOR_WEEKLY:
                        confluence_ok = monthly_uptrend
                    else:
                        confluence_ok = True

                    final_buy = core_buy and regime_ok and confluence_ok and not earnings_soon

                    if final_buy and EMAIL_INCLUDE_CHARTS:
                        try:
                            lookback = EMAIL_CHART_LOOKBACK_WEEKLY if tf_name == "Weekly" else EMAIL_CHART_LOOKBACK_MONTHLY
                            charts[(symbol, tf_name)] = make_chart_png(sig, symbol, tf_name, lookback)
                        except Exception as e:
                            print(f"  Chart generation failed for {symbol} ({tf_name}): {e}")

                    rows.append({
                        "Symbol": symbol,
                        "Name": name_map.get(symbol, ""),
                        "Category": category_map.get(symbol, ""),
                        "Timeframe": tf_name,
                        "Date": sig.index[-1].date(),
                        "Price": round(float(last["Close"]), 2),
                        "Volume": int(last["Volume"]),
                        "VolAboveAvg": bool(last["Volume"] > last["VOL_MA"] * VOL_MULT_REQ) if not np.isnan(last["VOL_MA"]) else False,
                        "RSI": round(float(last["RSI"]), 1),
                        "ADX": round(float(last["ADX"]), 1),
                        "TrendStrengthOK": bool(last["ADX"] > ADX_THRESHOLD),
                        "OBV_OK": bool(last["OBV_OK"]) if USE_OBV_FILTER else "N/A",
                        "RelStrengthOK": bool(last["RS_OK"]) if USE_RELATIVE_STRENGTH_FILTER else "N/A",
                        "MarketRegimeOK": regime_ok,
                        "MonthlyConfluence": confluence_ok if tf_name == "Weekly" else "N/A",
                        "EarningsDaysAway": earnings_days if earnings_days is not None else "",
                        "EarningsSoon": earnings_soon,
                        "Trend": "UP" if last["UPTREND"] else "DOWN",
                        "VStop": round(float(last["VSTOP"]), 2),
                        "Signal": "BUY" if final_buy else ("SELL" if last["SELL_SIGNAL"] else "-"),
                    })
            except Exception as e:
                print(f"  Skipping {tkr}: {e}")
                failures.append((symbol_map.get(tkr, tkr), str(e)))
                continue

        if batch_num % CHECKPOINT_EVERY_N_BATCHES == 0:
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            pd.DataFrame(rows).to_csv(os.path.join(OUTPUT_DIR, "_checkpoint_partial.csv"), index=False)
            print(f"  (checkpoint saved: {len(rows)} rows so far)")

        time.sleep(BATCH_SLEEP_SEC)

    # Symbols that returned data but had too little history for the
    # indicators (e.g. recent IPOs) never reach compute_signals successfully
    # and won't appear in `rows` OR `failures` — catch those too.
    seen_symbols = {r["Symbol"] for r in rows}
    attempted_symbols = {symbol_map[t] for t in tickers}
    failed_symbols = {f[0] for f in failures}
    silent_gaps = attempted_symbols - seen_symbols - failed_symbols
    for sym in silent_gaps:
        failures.append((sym, "Data fetched but insufficient history for the indicators "
                               "(e.g. recently listed stock) — needs ~8+ years of history "
                               "for a stable Monthly EMA100"))

    if failures:
        print(f"\n{'=' * 70}\n{len(failures)} SYMBOL(S) COULD NOT BE SCANNED\n{'=' * 70}")
        for sym, reason in failures:
            print(f"  {sym}: {reason}")
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        pd.DataFrame(failures, columns=["Symbol", "Reason"]).to_csv(
            os.path.join(OUTPUT_DIR, "failed_symbols.csv"), index=False)
        print(f"  (also saved to {OUTPUT_DIR}/failed_symbols.csv)")

    return pd.DataFrame(rows), charts


# =========================================================================
# NOTIFICATIONS
# =========================================================================
def send_email(subject: str, body: str):
    """Plain-text fallback (used if HTML/chart email isn't wanted)."""
    if not (EMAIL_FROM and EMAIL_TO and EMAIL_APP_PASSWORD):
        print("Email not configured — skipping (set SCANNER_EMAIL_* env vars).")
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_FROM, EMAIL_APP_PASSWORD)
        server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
    print("Email sent (plain text).")


def send_email_with_charts(subject: str, html_body: str, charts: dict):
    """
    HTML email with small inline chart images. `charts` is
    {cid_string: png_bytes}. Reports the final message size so you can see
    exactly how "small" it ended up.
    """
    if not (EMAIL_FROM and EMAIL_TO and EMAIL_APP_PASSWORD):
        print("Email not configured — skipping (set SCANNER_EMAIL_* env vars).")
        return

    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(html_body, "html"))

    for cid, png_bytes in charts.items():
        img = MIMEImage(png_bytes, _subtype="png")
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
        msg.attach(img)

    raw = msg.as_string()
    size_kb = len(raw.encode("utf-8")) / 1024
    print(f"Email size: {size_kb:.0f} KB ({len(charts)} chart(s) embedded)")

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_FROM, EMAIL_APP_PASSWORD)
        server.sendmail(EMAIL_FROM, [EMAIL_TO], raw)
    print("Email sent (HTML with charts).")


def send_telegram(text: str):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("Telegram not configured — skipping (set SCANNER_TELEGRAM_* env vars).")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"})
    print("Telegram message sent.")


def build_html_email(buys: pd.DataFrame, sells: pd.DataFrame, charts: dict, failures_count: int):
    """
    Builds a compact HTML email: a small results table plus up to
    EMAIL_MAX_CHARTS inline chart images for the highest-ADX BUY signals.
    Returns (html_string, {cid: png_bytes}) — the cid dict is only the
    charts actually being embedded (capped), not all charts generated.
    """
    # Prioritize by ADX (strongest trend) when there are more BUYs than
    # the embed cap allows.
    buys_sorted = buys.sort_values("ADX", ascending=False)
    to_embed = []
    for _, r in buys_sorted.iterrows():
        key = (r["Symbol"], r["Timeframe"])
        if key in charts and len(to_embed) < EMAIL_MAX_CHARTS:
            to_embed.append(r)

    embedded_charts = {}

    style = ("font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#202124;"
             "border-collapse:collapse;width:100%;")
    th = "text-align:left;padding:4px 8px;background:#f1f3f4;font-size:12px;"
    td = "padding:4px 8px;border-top:1px solid #eee;font-size:12px;"

    html = ["<div style='font-family:Arial,Helvetica,sans-serif;'>"]
    html.append(f"<h3 style='margin:0 0 8px 0;'>NSE Scanner — {datetime.now().strftime('%d-%b-%Y %H:%M')}</h3>")

    # --- BUY table ---
    html.append(f"<p style='margin:12px 0 4px 0;font-weight:bold;color:#188038;'>BUY signals ({len(buys)})</p>")
    if buys.empty:
        html.append("<p style='margin:0;'>None today.</p>")
    else:
        html.append(f"<table style='{style}'><tr>"
                     f"<th style='{th}'>Symbol</th><th style='{th}'>Cat</th>"
                     f"<th style='{th}'>TF</th><th style='{th}'>Price</th>"
                     f"<th style='{th}'>RSI</th><th style='{th}'>ADX</th></tr>")
        for _, r in buys_sorted.iterrows():
            html.append(f"<tr><td style='{td}'>{r['Symbol']}</td><td style='{td}'>{r['Category']}</td>"
                         f"<td style='{td}'>{r['Timeframe']}</td><td style='{td}'>{r['Price']}</td>"
                         f"<td style='{td}'>{r['RSI']}</td><td style='{td}'>{r['ADX']}</td></tr>")
        html.append("</table>")

    # --- Inline charts for the top signals ---
    if to_embed:
        html.append(f"<p style='margin:16px 0 4px 0;font-weight:bold;'>Charts (top {len(to_embed)} by ADX)</p>")
        html.append("<div>")
        for i, r in enumerate(to_embed):
            cid = f"chart{i}"
            embedded_charts[cid] = charts[(r["Symbol"], r["Timeframe"])]
            html.append(f"<img src='cid:{cid}' alt='{r['Symbol']} {r['Timeframe']}' "
                        f"style='display:block;margin:4px 0;max-width:100%;'/>")
        html.append("</div>")
        skipped = len(buys) - len(to_embed)
        if skipped > 0:
            html.append(f"<p style='font-size:11px;color:#666;margin:4px 0;'>"
                        f"+{skipped} more BUY signal(s) not charted here — see the full CSV.</p>")

    # --- SELL table ---
    html.append(f"<p style='margin:16px 0 4px 0;font-weight:bold;color:#c5221f;'>SELL / trend-break ({len(sells)})</p>")
    if sells.empty:
        html.append("<p style='margin:0;'>None today.</p>")
    else:
        html.append(f"<table style='{style}'><tr>"
                     f"<th style='{th}'>Symbol</th><th style='{th}'>Cat</th>"
                     f"<th style='{th}'>TF</th><th style='{th}'>Price</th>"
                     f"<th style='{th}'>RSI</th></tr>")
        for _, r in sells.iterrows():
            html.append(f"<tr><td style='{td}'>{r['Symbol']}</td><td style='{td}'>{r['Category']}</td>"
                         f"<td style='{td}'>{r['Timeframe']}</td><td style='{td}'>{r['Price']}</td>"
                         f"<td style='{td}'>{r['RSI']}</td></tr>")
        html.append("</table>")

    if failures_count:
        html.append(f"<p style='font-size:11px;color:#666;margin-top:12px;'>"
                    f"{failures_count} symbol(s) could not be scanned — see failed_symbols.csv</p>")

    html.append("<p style='font-size:11px;color:#999;margin-top:16px;'>"
                "Not financial advice. Verify against your own chart before acting.</p>")
    html.append("</div>")

    return "".join(html), embedded_charts


# =========================================================================
# MAIN
# =========================================================================
def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"=== NSE Scanner run started: {datetime.now()} ===")

    results, charts = scan()
    if results.empty:
        print("No results — check your universe CSV and internet connection.")
        return

    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    out_path = os.path.join(OUTPUT_DIR, f"scan_{ts}.csv")
    results.to_csv(out_path, index=False)
    print(f"\nFull results saved to: {out_path}")

    buys_all = results[results["Signal"] == "BUY"].sort_values(["Timeframe", "Category"])
    sells_all = results[results["Signal"] == "SELL"].sort_values(["Timeframe", "Category"])

    # Trim to the top N by trend strength (ADX) for the report/email — the
    # full, untrimmed lists are already saved in the CSV above.
    buys = buys_all.sort_values("ADX", ascending=False).head(TOP_N_BUY)
    sells = sells_all.sort_values("ADX", ascending=False).head(TOP_N_SELL)
    if len(buys_all) > TOP_N_BUY:
        print(f"({len(buys_all)} total BUY signals — reporting top {TOP_N_BUY} by ADX; full list in the CSV)")
    if len(sells_all) > TOP_N_SELL:
        print(f"({len(sells_all)} total SELL signals — reporting top {TOP_N_SELL} by ADX; full list in the CSV)")

    failures_path = os.path.join(OUTPUT_DIR, "failed_symbols.csv")
    failures_count = 0
    if os.path.exists(failures_path):
        try:
            failures_count = len(pd.read_csv(failures_path))
        except Exception:
            pass

    print(f"\n{'=' * 70}\nBUY SIGNALS ({len(buys)})\n{'=' * 70}")
    if not buys.empty:
        print(buys[["Symbol", "Category", "Timeframe", "Price", "RSI", "ADX", "VolAboveAvg"]].to_string(index=False))
    else:
        print("None today.")

    print(f"\n{'=' * 70}\nSELL / TREND-BREAK SIGNALS ({len(sells)})\n{'=' * 70}")
    if not sells.empty:
        print(sells[["Symbol", "Category", "Timeframe", "Price", "RSI"]].to_string(index=False))
    else:
        print("None today.")

    if NOTIFY_EMAIL or NOTIFY_TELEGRAM:
        # Plain-text version, used for Telegram and as the email fallback
        # if charts are disabled/unavailable.
        body_lines = [f"NSE Scanner — {datetime.now().strftime('%d-%b-%Y %H:%M')}", ""]
        if not buys.empty:
            body_lines.append(f"BUY ({len(buys)}):")
            for _, r in buys.iterrows():
                body_lines.append(f"  {r['Symbol']} [{r['Category']}/{r['Timeframe']}] @ {r['Price']} RSI {r['RSI']}")
        else:
            body_lines.append("BUY: none today.")

        if not sells.empty:
            body_lines.append(f"\nSELL ({len(sells)}):")
            for _, r in sells.iterrows():
                body_lines.append(f"  {r['Symbol']} [{r['Category']}/{r['Timeframe']}] @ {r['Price']} RSI {r['RSI']}")
        else:
            body_lines.append("\nSELL: none today.")

        if failures_count:
            body_lines.append(f"\n({failures_count} symbol(s) could not be scanned — see failed_symbols.csv)")

        body_lines.append("\n— Not financial advice. Verify against your own chart before acting.")
        body = "\n".join(body_lines)

        if NOTIFY_EMAIL:
            subject = f"NSE Scanner: {len(buys)} BUY / {len(sells)} SELL — {datetime.now().strftime('%d-%b-%Y')}"
            if EMAIL_INCLUDE_CHARTS and charts:
                html, embedded_charts = build_html_email(buys, sells, charts, failures_count)
                send_email_with_charts(subject, html, embedded_charts)
            else:
                send_email(subject, body)
        if NOTIFY_TELEGRAM:
            send_telegram(body)

    print(f"\n=== Run finished: {datetime.now()} ===")


if __name__ == "__main__":
    main()


# =========================================================================
# SCHEDULING — how to run this automatically every day
# =========================================================================
# OPTION A (recommended): OS-level scheduler — runs once a day, no process
# needs to stay alive in the background.
#
#   Linux / macOS (cron), run daily at 18:00 (after market close, IST):
#     1. crontab -e
#     2. Add this line (edit paths to match your setup):
#        0 18 * * 1-5 cd /path/to/nse_scanner && /usr/bin/python3 nse_scanner.py >> run.log 2>&1
#        (the "1-5" restricts it to Mon–Fri)
#
#   Windows (Task Scheduler):
#     1. Open Task Scheduler -> Create Basic Task
#     2. Trigger: Daily, 18:00
#     3. Action: Start a program
#        Program: python.exe
#        Arguments: nse_scanner.py
#        Start in: C:\path\to\nse_scanner
#
# OPTION B: keep a Python process running continuously and let it sleep
# until the scheduled time each day. Use this only if you can't set up
# cron/Task Scheduler (e.g. sharing one long-running server). See
# daily_runner.py in this same folder for a ready-made version of this.
