"""
================================================================================
 GARTLEY PATTERN SCANNER — Indian Stock Market (NSE)
================================================================================
Implements Ross L. Beck's "Price Retracement + Price Extension + Cluster"
method exactly as described in:
    - Gartley_Price_Retracement_and_Extension_Study_Guide.pdf
    - Reliance_Gartley_Retracement_Extension_Practical_Guide.pdf

THE METHOD (Beck), step by step:
    1. Identify X -> A (the main impulsive move).
    2. Draw the Fibonacci PRICE RETRACEMENT of XA.
       Levels used: 38.2%, 48.6%, 61.8%, 78.6%, 100%
    3. Identify A -> B -> C (the corrective structure).
    4. Apply the PRICE EXTENSION tool to project AB = CD.
       Beck states the preferred/default projection is 100% (AB = CD),
       though other ratios can also be checked.
    5. COMPARE the AB=CD extension projection (candidate D) against the
       XA retracement levels. Whichever XA retracement level the AB=CD
       projection lands CLOSEST to defines the "cluster" and the bias
       for where the Gartley pattern (D point) will complete.
    6. A "good cluster" = extension and retracement are close together
       (tight zone). If they are far apart, the signal is unclear/invalid.
    7. COMPLETION RULE (critical distinction from the guide):
       The setup is only considered COMPLETE when price actually TOUCHES
       the Fibonacci RETRACEMENT level -- NOT the extension level.
       A completed setup is not automatically a trade entry (Beck
       separates completion from entry).

This script finds swing points algorithmically (ZigZag-style), builds
candidate X-A-B-C legs from the most recent swings, computes the
retracement/extension cluster, and reports:
    - which XA retracement level the cluster points to
    - the projected D price
    - whether price has already touched that retracement level
      (SETUP COMPLETE) or is still approaching it (SETUP FORMING)

Works across ANY timeframe supported by yfinance intraday/daily/weekly
data (1m, 5m, 15m, 30m, 60m/1h, 1d, 1wk, 1mo) -- pass --interval.

--------------------------------------------------------------------------------
USAGE EXAMPLES
--------------------------------------------------------------------------------
    # Scan default Nifty 50 list on the daily timeframe
    python gartley_scanner.py

    # Scan specific stocks on 1-hour timeframe
    python gartley_scanner.py --symbols RELIANCE,TCS,INFY --interval 1h

    # Scan on 15-minute intraday timeframe (yfinance limits lookback ~60 days)
    python gartley_scanner.py --symbols RELIANCE --interval 15m --period 30d

    # Weekly timeframe, wider swing sensitivity
    python gartley_scanner.py --interval 1wk --period 5y --swing-order 3

--------------------------------------------------------------------------------
REQUIREMENTS
--------------------------------------------------------------------------------
    pip install yfinance pandas numpy scipy
================================================================================
"""

import argparse
import sys
import warnings
from dataclasses import dataclass, field
from typing import List, Optional, Dict

import numpy as np
import pandas as pd
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    IST = None

warnings.filterwarnings("ignore")

try:
    import yfinance as yf
except ImportError:
    print("Missing dependency. Run: pip install yfinance pandas numpy scipy")
    sys.exit(1)

from scipy.signal import argrelextrema


# ==============================================================================
# CONFIG
# ==============================================================================

# Fibonacci retracement levels Beck uses for the Gartley XA retracement
# (Step 1 in the study guide: "Levels used by Beck: 38.2%, 48.6%, 61.8%,
#  78.6%, and 100% for the Gartley retracement framework.")
XA_RETRACEMENT_LEVELS = [0.382, 0.486, 0.618, 0.786, 1.000]

# Beck's preferred AB=CD price-extension ratio (Step 2 in the study guide:
# "Beck states that the preferred number for the price-extension tool is 100%.")
# Kept as a list so you can widen the scan if you want to test other ratios too.
AB_CD_EXTENSION_RATIOS = [1.000]

# A handy default universe: Nifty 50 (NSE symbols, yfinance format with .NS)
NIFTY_50 = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "HINDUNILVR",
    "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK",
    "BAJFINANCE", "ASIANPAINT", "MARUTI", "SUNPHARMA", "TITAN",
    "ULTRACEMCO", "WIPRO", "NESTLEIND", "ONGC", "NTPC", "POWERGRID",
    "M&M", "TATASTEEL", "ADANIENT", "ADANIPORTS", "COALINDIA",
    "BAJAJFINSV", "HCLTECH", "TECHM", "GRASIM", "JSWSTEEL", "DRREDDY",
    "CIPLA", "BRITANNIA", "EICHERMOT", "HEROMOTOCO", "DIVISLAB",
    "APOLLOHOSP", "TATACONSUM", "BPCL", "HINDALCO", "SBILIFE",
    "HDFCLIFE", "INDUSINDBK", "BAJAJ-AUTO", "TATAMOTORS", "SHRIRAMFIN",
    "UPL",
]


# ==============================================================================
# DATA STRUCTURES
# ==============================================================================

@dataclass
class SwingPoint:
    index: int          # position in the dataframe
    date: pd.Timestamp
    price: float
    kind: str            # 'high' or 'low'


@dataclass
class GartleyCluster:
    symbol: str
    interval: str
    direction: str                 # 'bullish' (XA down, D is a low) or 'bearish' (XA up, D is a high)
    X: SwingPoint
    A: SwingPoint
    B: SwingPoint
    C: SwingPoint
    xa_retracements: Dict[float, float]      # level -> price
    extension_ratio: float                   # which AB=CD ratio was used (Beck default 1.000)
    extension_D: float                       # AB=CD projected price
    cluster_level: float                     # the XA retracement % the extension is closest to
    cluster_price: float                     # price of that retracement level
    cluster_distance_pct: float              # how far apart extension vs retracement are (tightness)
    current_price: float
    latest_candle_date: pd.Timestamp         # timestamp of the latest CLOSED candle current_price came from
    setup_complete: bool                     # has price touched the retracement (cluster) level?
    touch_date: Optional[pd.Timestamp] = None


# ==============================================================================
# STEP 0: DATA FETCHING
# ==============================================================================

# Duration of one bar for each intraday interval yfinance supports.
# Used to work out whether the LAST bar returned by yfinance has actually
# finished (closed) yet, or is still "live"/forming.
INTRADAY_BAR_DURATION = {
    "1m": timedelta(minutes=1),
    "2m": timedelta(minutes=2),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "60m": timedelta(minutes=60),
    "1h": timedelta(minutes=60),
    "90m": timedelta(minutes=90),
}

# NSE cash market hours (IST)
NSE_MARKET_CLOSE = (15, 30)   # 15:30 IST


def _now_ist() -> datetime:
    if IST is not None:
        return datetime.now(IST)
    # Fallback: assume the machine clock is already IST-ish; still usable.
    return datetime.now()


def drop_incomplete_last_candle(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    """
    Ensures the LAST row in `df` is a fully CLOSED candle, not one that is
    still forming.

    Why this matters: yfinance's intraday download often includes the
    currently-in-progress bar as the final row (e.g. if a 15m bar started
    4 minutes ago, that partial bar is still returned). If we don't drop
    it, "current price" / swing detection would be reading an unfinished
    candle instead of the latest COMPLETED one.

    Daily/weekly/monthly bars: a bar is only "closed" once the NSE session
    for that date has ended (15:30 IST). If today's date already appears
    as the last row but the market hasn't closed yet, that row is dropped
    too, so you don't act on a live/incomplete daily candle mid-session.
    """
    if df.empty:
        return df

    now = _now_ist()
    last_ts = df.index[-1]
    # Normalize timestamp to a plain (tz-naive) datetime for comparison
    if getattr(last_ts, "tzinfo", None) is not None:
        last_ts_ist = last_ts.tz_convert(IST) if IST is not None else last_ts
        last_naive = last_ts_ist.replace(tzinfo=None)
        now_naive = now.replace(tzinfo=None) if getattr(now, "tzinfo", None) else now
    else:
        last_naive = last_ts.to_pydatetime() if hasattr(last_ts, "to_pydatetime") else last_ts
        now_naive = now.replace(tzinfo=None) if getattr(now, "tzinfo", None) else now

    interval_key = interval.lower()

    if interval_key in INTRADAY_BAR_DURATION:
        bar_end = last_naive + INTRADAY_BAR_DURATION[interval_key]
        if now_naive < bar_end:
            # Bar hasn't finished yet -- drop it, keep only closed candles
            return df.iloc[:-1]
        return df

    # Daily / weekly / monthly: bar is only closed once market close has
    # passed for that trading date (only relevant if the last bar's date
    # is "today").
    if interval_key in ("1d", "5d", "1wk", "1mo", "3mo"):
        last_date = last_naive.date()
        today = now_naive.date()
        if last_date == today:
            close_h, close_m = NSE_MARKET_CLOSE
            market_close_today = now_naive.replace(
                hour=close_h, minute=close_m, second=0, microsecond=0)
            if now_naive < market_close_today:
                # Today's daily bar is still "live" -- drop it
                return df.iloc[:-1]
        return df

    # Unknown interval string -- leave as-is
    return df


def fetch_data(symbol: str, interval: str = "1d", period: str = "2y") -> Optional[pd.DataFrame]:
    """
    Fetch OHLC data for an NSE stock from yfinance.
    `symbol` should be the plain NSE ticker (e.g. 'RELIANCE'); '.NS' is
    appended automatically unless already present.

    Works for any timeframe yfinance supports:
        intraday: 1m, 2m, 5m, 15m, 30m, 60m/1h, 90m
        daily+:   1d, 5d, 1wk, 1mo, 3mo
    Note: yfinance restricts how far back intraday data goes (e.g. ~60 days
    for anything below 1h, ~730 days for 1h). Adjust --period accordingly.
    """
    ticker = symbol if symbol.upper().endswith(".NS") else f"{symbol.upper()}.NS"
    try:
        df = yf.download(ticker, period=period, interval=interval,
                          progress=False, auto_adjust=True)
    except Exception as e:
        print(f"  [{symbol}] download error: {e}")
        return None

    if df is None or df.empty:
        return None

    # yfinance sometimes returns MultiIndex columns for single tickers
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.dropna()
    df.index.name = "Date"

    # Guarantee the last row is a fully CLOSED candle, not a forming one
    # (this is the fix for "using the previous candle instead of the
    # latest completed candle" -- see drop_incomplete_last_candle above).
    df = drop_incomplete_last_candle(df, interval)

    return df


# ==============================================================================
# STEP 1 (part A): SWING POINT DETECTION (finds candidate X, A, B, C, D pivots)
# ==============================================================================

def find_swing_points(df: pd.DataFrame, order: int = 5) -> List[SwingPoint]:
    """
    Detects local swing highs/lows -- these are the candidate turning points
    the Gartley method's X, A, B, C, D labels get assigned to.

    `order` = number of bars on each side that must be lower/higher for a
    point to count as a swing (this is the "swing sensitivity" knob --
    higher order = fewer, more significant swings; useful when moving
    between intraday and higher timeframes).
    """
    highs = df["High"].values
    lows = df["Low"].values

    hi_idx = argrelextrema(highs, np.greater_equal, order=order)[0]
    lo_idx = argrelextrema(lows, np.less_equal, order=order)[0]

    swings: List[SwingPoint] = []
    for i in hi_idx:
        swings.append(SwingPoint(index=int(i), date=df.index[i], price=float(highs[i]), kind="high"))
    for i in lo_idx:
        swings.append(SwingPoint(index=int(i), date=df.index[i], price=float(lows[i]), kind="low"))

    swings.sort(key=lambda s: s.index)

    # Collapse consecutive swings of the same kind (keep the most extreme one)
    cleaned: List[SwingPoint] = []
    for s in swings:
        if cleaned and cleaned[-1].kind == s.kind:
            if (s.kind == "high" and s.price >= cleaned[-1].price) or \
               (s.kind == "low" and s.price <= cleaned[-1].price):
                cleaned[-1] = s
            # else: keep the existing, more extreme one
        else:
            cleaned.append(s)

    return cleaned


# ==============================================================================
# STEP 1 (part B) & STEP 2: RETRACEMENT + EXTENSION MATH
# ==============================================================================

def xa_fib_retracement(X: SwingPoint, A: SwingPoint) -> Dict[float, float]:
    """
    Step 1 of Beck's method: draw the Fibonacci retracement of the XA move.
    Returns {level: price} for 38.2 / 48.6 / 61.8 / 78.6 / 100 %.

    If XA moved UP (bullish leg, X=low -> A=high), retracement measures
    back DOWN from A. If XA moved DOWN, retracement measures back UP from A.
    (Matches Figure 3.7 in the study guide.)
    """
    xa_range = A.price - X.price
    levels = {}
    for lvl in XA_RETRACEMENT_LEVELS:
        levels[lvl] = A.price - (xa_range * lvl)
    return levels


def ab_cd_extension(A: SwingPoint, B: SwingPoint, C: SwingPoint, ratio: float = 1.000) -> float:
    """
    Step 2 of Beck's method: price-extension tool for AB = CD.
    Three data points: A and B define the first leg's range; C is where
    that range is projected FROM.

    The CD leg is projected in the SAME direction as the AB leg (mirroring
    the A->B move starting from C), which is what makes AB = CD:
        D = C + (B - A) * ratio
    e.g. practical-guide example: A=1400, B=1280 (AB=-120), C=1396.4
         -> D = 1396.4 + (1280-1400)*1.0 = 1276.4  (matches the PDF exactly)
    """
    ab_range = B.price - A.price   # signed distance of first leg
    D = C.price + (ab_range * ratio)
    return D


def find_cluster(xa_levels: Dict[float, float], extension_D: float) -> (float, float, float):
    """
    Step 3 (Compare) + Step 4 (Cluster) of Beck's method:
    Find which XA retracement level the AB=CD extension projection is
    closest to. Returns (level, level_price, distance_pct).

    distance_pct is how far apart the extension and the nearest
    retracement level are, as a % of the XA range -- this is the
    "tightness" of the cluster. Beck: tight cluster = valid signal,
    wide separation = unclear signal.
    """
    best_level, best_price, best_dist = None, None, float("inf")
    for lvl, price in xa_levels.items():
        dist = abs(extension_D - price)
        if dist < best_dist:
            best_level, best_price, best_dist = lvl, price, dist

    xa_range = abs(max(xa_levels.values()) - min(xa_levels.values()))
    dist_pct = (best_dist / xa_range * 100) if xa_range else float("inf")
    return best_level, best_price, dist_pct


# ==============================================================================
# STEP 5: COMPLETION CHECK
#   "The trade setup is considered complete when price touches the
#    Fibonacci retracement level, not the Fibonacci extension level."
# ==============================================================================

def check_completion(df: pd.DataFrame, after_index: int, level_price: float,
                      direction: str, tolerance_pct: float = 0.15):
    """
    Scans bars AFTER point C to see whether price has touched the
    cluster's Fibonacci RETRACEMENT level (not the extension level).

    direction:
      'bullish' -> XA moved down, so D is a LOW; look for price.Low
                   touching/undercutting the retracement level.
      'bearish' -> XA moved up, so D is a HIGH; look for price.High
                   touching/exceeding the retracement level.

    Returns (is_complete: bool, touch_date or None)
    """
    tolerance = level_price * (tolerance_pct / 100)
    sub = df.iloc[after_index:]

    if direction == "bullish":
        touched = sub[sub["Low"] <= level_price + tolerance]
    else:
        touched = sub[sub["High"] >= level_price - tolerance]

    if not touched.empty:
        return True, touched.index[0]
    return False, None


# ==============================================================================
# CORE SCAN LOGIC: build candidate X-A-B-C from the most recent swings and
# run the full Beck sequence (Steps 1-5 from the memorization table)
# ==============================================================================

CLUSTER_TIGHTNESS_THRESHOLD_PCT = 3.0   # how close extension must land to a
                                          # retracement level to count as a
                                          # valid "cluster" (tune as needed)


def scan_symbol(symbol: str, interval: str, period: str, swing_order: int,
                 tolerance_pct: float) -> List[GartleyCluster]:
    """
    Runs Beck's full sequence on one symbol/timeframe and returns any
    valid clusters found (most recent candidate X-A-B-C window first).
    """
    df = fetch_data(symbol, interval=interval, period=period)
    if df is None or len(df) < swing_order * 4:
        return []

    swings = find_swing_points(df, order=swing_order)
    if len(swings) < 4:
        return []

    results: List[GartleyCluster] = []

    # Slide a window over the last several swings looking for valid
    # alternating X-A-B-C patterns (high/low/high/low or low/high/low/high)
    for i in range(len(swings) - 4, max(len(swings) - 12, -1), -1):
        if i < 0:
            break
        X, A, B, C = swings[i], swings[i + 1], swings[i + 2], swings[i + 3]

        # Must alternate high/low/high/low (or reverse)
        kinds = [X.kind, A.kind, B.kind, C.kind]
        if kinds not in (["low", "high", "low", "high"], ["high", "low", "high", "low"]):
            continue

        direction = "bullish" if X.kind == "low" else "bearish"

        # Basic structural sanity: B must retrace between A and X (not exceed X),
        # C must not exceed A (standard AB=CD / Gartley structural rule)
        if direction == "bullish":
            if not (X.price < B.price < A.price):
                continue
            if not (C.price < A.price):
                continue
        else:
            if not (A.price < B.price < X.price):
                continue
            if not (C.price > A.price):
                continue

        # STEP 1: XA Fibonacci retracement
        xa_levels = xa_fib_retracement(X, A)

        # STEP 2: AB=CD price extension (Beck's preferred 100% ratio; also
        # checks any additional ratios configured above)
        for ratio in AB_CD_EXTENSION_RATIOS:
            extension_D = ab_cd_extension(A, B, C, ratio=ratio)

            # STEP 3/4: Compare + find cluster
            level, level_price, dist_pct = find_cluster(xa_levels, extension_D)
            if level is None or dist_pct > CLUSTER_TIGHTNESS_THRESHOLD_PCT:
                continue  # not a tight cluster -> Beck says signal is unclear

            # STEP 5: Completion check -- has price touched the RETRACEMENT level?
            complete, touch_date = check_completion(
                df, after_index=C.index, level_price=level_price,
                direction=direction, tolerance_pct=tolerance_pct
            )

            # df has already had any still-forming candle stripped in
            # fetch_data(), so iloc[-1] here is guaranteed to be the
            # latest CLOSED candle -- not one before it, and never a
            # partially-formed live one.
            current_price = float(df["Close"].iloc[-1])
            latest_candle_date = df.index[-1]

            results.append(GartleyCluster(
                symbol=symbol, interval=interval, direction=direction,
                X=X, A=A, B=B, C=C,
                xa_retracements=xa_levels,
                extension_ratio=ratio,
                extension_D=extension_D,
                cluster_level=level,
                cluster_price=level_price,
                cluster_distance_pct=dist_pct,
                current_price=current_price,
                latest_candle_date=latest_candle_date,
                setup_complete=complete,
                touch_date=touch_date,
            ))

    return results


# ==============================================================================
# REPORTING
# ==============================================================================

def print_cluster(gc: GartleyCluster):
    status = "SETUP COMPLETE (price touched retracement level)" if gc.setup_complete \
        else "SETUP FORMING (waiting for retracement touch)"
    arrow = "BULLISH (D = low, expect reversal up)" if gc.direction == "bullish" \
        else "BEARISH (D = high, expect reversal down)"

    print("-" * 78)
    print(f"{gc.symbol}  |  interval={gc.interval}  |  {arrow}")
    print(f"  X: {gc.X.date.date()}  {gc.X.price:.2f}   ->   A: {gc.A.date.date()}  {gc.A.price:.2f}")
    print(f"  B: {gc.B.date.date()}  {gc.B.price:.2f}   ->   C: {gc.C.date.date()}  {gc.C.price:.2f}")
    print(f"  XA retracement levels: " +
          ", ".join(f"{lvl*100:.1f}%={p:.2f}" for lvl, p in gc.xa_retracements.items()))
    print(f"  AB=CD extension ({gc.extension_ratio*100:.0f}%) projects D = {gc.extension_D:.2f}")
    print(f"  >>> CLUSTER: closest to {gc.cluster_level*100:.1f}% retracement "
          f"({gc.cluster_price:.2f}), gap = {gc.cluster_distance_pct:.2f}% of XA range")
    print(f"  Current price: {gc.current_price:.2f}  "
          f"(latest CLOSED candle: {gc.latest_candle_date})")
    print(f"  STATUS: {status}" + (f"  (touched {gc.touch_date.date()})" if gc.touch_date is not None else ""))


# ==============================================================================
# MAIN
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="NSE Gartley Scanner (Beck's Retracement + Extension + Cluster method)")
    parser.add_argument("--symbols", type=str, default=None,
                         help="Comma-separated NSE symbols, e.g. RELIANCE,TCS,INFY. "
                              "Default: Nifty 50 list.")
    parser.add_argument("--interval", type=str, default="1d",
                         help="Any yfinance interval: 1m,5m,15m,30m,60m/1h,1d,1wk,1mo. Default 1d.")
    parser.add_argument("--period", type=str, default="2y",
                         help="History length, e.g. 30d,60d,6mo,1y,2y,5y. "
                              "Note: yfinance limits intraday history depth.")
    parser.add_argument("--swing-order", type=int, default=5,
                         help="Swing-point sensitivity (bars each side). "
                              "Lower = more swings (good for intraday), "
                              "higher = fewer/major swings (good for weekly).")
    parser.add_argument("--tolerance", type=float, default=0.15,
                         help="%% tolerance band around the retracement level "
                              "to count as 'touched'. Default 0.15%%.")
    parser.add_argument("--only-complete", action="store_true",
                         help="Only print setups where price has already "
                              "touched the retracement level.")
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else NIFTY_50

    print("=" * 78)
    print("GARTLEY SCANNER -- NSE  |  Ross L. Beck: Retracement + Extension + Cluster")
    print(f"Timeframe: {args.interval}  |  History: {args.period}  |  "
          f"Swing order: {args.swing_order}")
    print("=" * 78)

    matched_symbols = []   # symbols that had at least one qualifying cluster
    errored_symbols = []

    for sym in symbols:
        try:
            clusters = scan_symbol(sym, args.interval, args.period,
                                    args.swing_order, args.tolerance)
        except Exception as e:
            print(f"[{sym}] error: {e}")
            errored_symbols.append(sym)
            continue

        # Apply the --only-complete filter (if set) BEFORE deciding whether
        # this symbol counts as a "match" -- each stock is judged on its
        # own, independent of how the rest of the list performs.
        shown_clusters = [gc for gc in clusters
                           if not (args.only_complete and not gc.setup_complete)]

        if shown_clusters:
            matched_symbols.append(sym)
            for gc in shown_clusters:
                print_cluster(gc)

    # ---- Summary: only matching stocks are ever printed above; this just
    # ---- tells you the match rate out of everything you submitted.
    print("=" * 78)
    print(f"Input stocks        : {len(symbols)}")
    print(f"Matched conditions  : {len(matched_symbols)}")
    if matched_symbols:
        print(f"Matched symbols     : {', '.join(matched_symbols)}")
    if errored_symbols:
        print(f"Skipped (data error): {', '.join(errored_symbols)}")
    if not matched_symbols:
        print("No qualifying Gartley clusters found for the given symbols/timeframe.")
        print("Try a lower --swing-order (more sensitivity) or a wider --period.")
    print("=" * 78)


if __name__ == "__main__":
    main()
