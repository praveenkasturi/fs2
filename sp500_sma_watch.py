"""S&P 500 50/200 SMA watcher. Serves the HTML and runs live scans on button click."""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import webbrowser
import warnings
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import requests
import yfinance as yf

warnings.filterwarnings("ignore")

HERE = Path(__file__).resolve().parent
HTML_PATH = HERE / "sp500_sma_watch.html"
DATA_PATH = HERE / "sp500_sma_watch_data.json"

CLOSE_PCT = 2.0
RECENT_DAYS = 10
SLOPE_LOOKBACK = 5
PORT = 8765
TARGET_PCT = 0.05
STOP_UNDER_50 = 0.02
ZONE_PCT = 0.02
MAX_HOLD = 60
NEAR_ENTRY_PCT = 0.025

# Add extra names here (Yahoo format: BRK-B not BRK.B).
# Duplicates of S&P 500 names are skipped automatically.
EXTRA_TICKERS = [
    # "SMCI",
    # "PLTR",
    # "ARM",
    # "SOFI",
]

SCAN_LOCK = threading.Lock()
LAST_PAYLOAD: dict | None = None
PROTOCOL = "sp500watch"


def install_protocol() -> None:
    """Let the HTML button start this script via sp500watch:// (current user, no admin)."""
    import winreg

    script = str(HTML_PATH.with_name("sp500_sma_watch.py"))
    cmd = f'"{sys.executable}" "{script}" --no-open "%1"'
    base = winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{PROTOCOL}")
    winreg.SetValueEx(base, "", 0, winreg.REG_SZ, "URL:SP500 SMA Watch")
    winreg.SetValueEx(base, "URL Protocol", 0, winreg.REG_SZ, "")
    cmd_key = winreg.CreateKey(base, r"shell\open\command")
    winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, cmd)
    print("HTML one-click is enabled (sp500watch://)")


def port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def normalize_ticker(raw: str) -> str:
    t = str(raw).strip().upper().replace(".", "-")
    return "".join(ch for ch in t if ch.isalnum() or ch == "-")


def parse_ticker_list(values: list[str] | None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in values or []:
        for part in str(raw).replace("\n", ",").replace(";", ",").replace(" ", ",").split(","):
            t = normalize_ticker(part)
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out


def sp500_constituents() -> pd.DataFrame:
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        html = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers=headers,
            timeout=30,
        ).text
        table = pd.read_html(html)[0]
        out = table.rename(columns={"Symbol": "ticker", "Security": "name", "GICS Sector": "sector"})
        out["ticker"] = out["ticker"].map(normalize_ticker)
        return out[["ticker", "name", "sector"]].drop_duplicates("ticker")
    except Exception:
        df = pd.read_csv(
            "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv"
        )
        out = df.rename(columns={"Symbol": "ticker", "Name": "name", "Sector": "sector"})
        out["ticker"] = out["ticker"].map(normalize_ticker)
        return out[["ticker", "name", "sector"]].drop_duplicates("ticker")


def merge_universe(extra_from_ui: list[str] | None = None) -> tuple[pd.DataFrame, list[str], list[str]]:
    names = sp500_constituents()
    sp_set = set(names["ticker"])
    extras = parse_ticker_list(list(EXTRA_TICKERS) + list(extra_from_ui or []))
    added, skipped = [], []
    rows = []
    for t in extras:
        if t in sp_set:
            skipped.append(t)
            continue
        added.append(t)
        sp_set.add(t)
        rows.append({"ticker": t, "name": t, "sector": "Extra"})
    if rows:
        names = pd.concat([names, pd.DataFrame(rows)], ignore_index=True)
    names = names.drop_duplicates("ticker")
    return names, added, skipped


def _panel(data: pd.DataFrame, field: str) -> pd.DataFrame | None:
    if isinstance(data.columns, pd.MultiIndex):
        level0 = set(data.columns.get_level_values(0))
        if field not in level0:
            return None
        out = data[field]
    else:
        if field not in data.columns:
            return None
        out = data[field].to_frame()
    out = out.copy()
    out.columns = [normalize_ticker(c) for c in out.columns]
    return out.dropna(axis=1, how="all")


def download_market(tickers: list[str]) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    data = yf.download(
        tickers,
        period="2y",
        interval="1d",
        auto_adjust=True,
        threads=True,
        progress=True,
    )
    closes = _panel(data, "Close")
    if closes is None:
        closes = data.copy() if not isinstance(data.columns, pd.MultiIndex) else data
        closes.columns = [normalize_ticker(c) for c in closes.columns]
        closes = closes.dropna(axis=1, how="all")
    return closes, _panel(data, "Volume")


def rsi_series(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    roll_up = up.ewm(alpha=1 / n, adjust=False).mean()
    roll_down = down.ewm(alpha=1 / n, adjust=False).mean()
    rs = roll_up / roll_down
    return 100 - (100 / (1 + rs))


def rsi_wilder(close: pd.Series, n: int = 14) -> float | None:
    last = rsi_series(close, n).iloc[-1]
    return None if pd.isna(last) else float(last)


def obv_series(close: pd.Series, volume: pd.Series) -> pd.Series:
    ch = close.diff()
    signed = volume.astype(float).copy()
    signed[ch < 0] = -signed[ch < 0]
    signed[ch == 0] = 0
    signed[ch.isna()] = 0
    return signed.cumsum()


def dip_quality(close: pd.Series, sma50: pd.Series, volume: pd.Series | None) -> dict:
    """Buy the dip to the 50 vs leave it — volume, RSI, OBV, 50 slope."""
    px = float(close.iloc[-1])
    s50 = float(sma50.iloc[-1])
    n = len(close)
    above_50 = px >= s50 * 0.995
    lost_50 = px < s50 * 0.98
    look = min(SLOPE_LOOKBACK, n - 1)
    rising = bool(s50 > float(sma50.iloc[-1 - look]))

    vol_ratio = None
    vol_shrink = False
    vol_hot = False
    if volume is not None:
        v = volume.reindex(close.index).fillna(0)
        if len(v) >= 25 and float(v.tail(25).sum()) > 0:
            recent = float(v.tail(5).mean())
            prior = float(v.iloc[-25:-5].mean())
            if prior > 0:
                vol_ratio = round(recent / prior, 2)
                vol_shrink = vol_ratio <= 0.9
                vol_hot = vol_ratio >= 1.15

    rsi = rsi_series(close)
    rsi_now = None if pd.isna(rsi.iloc[-1]) else float(rsi.iloc[-1])
    rsi_ok = rsi_now is not None and 40 <= rsi_now <= 58
    rsi_dead = rsi_now is not None and rsi_now < 35
    rsi_div = False
    if n >= 30:
        recent = close.iloc[-10:]
        prior = close.iloc[-30:-10]
        i1, i0 = recent.idxmin(), prior.idxmin()
        p1, p0 = float(recent.min()), float(prior.min())
        r1, r0 = rsi.loc[i1], rsi.loc[i0]
        if pd.notna(r1) and pd.notna(r0) and p1 <= p0 * 1.002 and float(r1) > float(r0) + 1:
            rsi_div = True

    obv_holds = False
    obv_new_low = False
    if volume is not None and n >= 30:
        v = volume.reindex(close.index).fillna(0)
        if float(v.tail(30).sum()) > 0:
            obv = obv_series(close, v)
            o_recent, o_prior = float(obv.iloc[-10:].min()), float(obv.iloc[-30:-10].min())
            p_recent, p_prior = float(close.iloc[-10:].min()), float(close.iloc[-30:-10].min())
            obv_new_low = o_recent < o_prior and p_recent <= p_prior
            obv_holds = (not obv_new_low) and (o_recent >= o_prior or (p_recent < p_prior and o_recent > o_prior))

    near = (close <= sma50 * 1.02) & (close >= sma50 * 0.99) & sma50.notna()
    first_touch = int(near.iloc[-40:-3].sum()) <= 2 if n >= 40 else True

    score = 0
    reasons: list[str] = []
    if lost_50:
        score -= 2
        reasons.append("close lost the 50 — 200 is in play")
    elif above_50:
        score += 2
        reasons.append("close still holds the 50")
    else:
        reasons.append("sitting right on the 50")

    if vol_shrink:
        score += 1
        reasons.append("volume dried up on the dip")
    elif vol_hot:
        score -= 1
        reasons.append("volume expanded on the dip")

    if rsi_ok:
        score += 1
        reasons.append("RSI cooled, not dead")
    elif rsi_dead:
        score -= 1
        reasons.append("RSI washed out")
    if rsi_div:
        score += 1
        reasons.append("RSI divergence (selling weaker)")

    if obv_holds:
        score += 1
        reasons.append("OBV held — money not leaving")
    if obv_new_low:
        score -= 1
        reasons.append("OBV made a new low")

    if rising:
        score += 1
        reasons.append("50 is still rising")
    else:
        score -= 1
        reasons.append("50 is flat or falling")

    if first_touch:
        score += 1
        reasons.append("early visit to the 50")
    else:
        reasons.append("50 already tagged a few times")

    if lost_50 or score <= 0:
        verdict, label = "leave", "LEAVE THE DIP"
    elif score >= 3 and above_50:
        verdict, label = "buy", "BUY THE DIP"
    else:
        verdict, label = "wait", "WAIT — MIXED"

    return {
        "dip_verdict": verdict,
        "dip_label": label,
        "dip_score": score,
        "dip_why": "; ".join(reasons[:6]),
        "vol_ratio": vol_ratio,
        "vol_shrink": vol_shrink,
        "obv_holds": obv_holds,
        "obv_new_low": obv_new_low,
        "rsi_div": rsi_div,
        "first_touch": first_touch,
    }


def test_quality(close: pd.Series, sma50: pd.Series, sma200: pd.Series, volume: pd.Series | None, signal: str) -> dict:
    """GOOD TEST = walk-up into the line after earlier fails. Else LEAVE TEST. Not a buy."""
    px = float(close.iloc[-1])
    s50 = float(sma50.iloc[-1])
    s200 = float(sma200.iloc[-1])
    n = len(close)
    death = signal in ("NEAR_DEATH", "DEATH_TODAY")
    cands = []
    if px < s50:
        cands.append(("50", s50, sma50))
    if px < s200:
        cands.append(("200", s200, sma200))
    cands.sort(key=lambda x: x[1])

    def leave(why: str, approaches: int = 0, coming_back: bool = False, **extra) -> dict:
        out = {
            "test_verdict": "leave",
            "test_label": "LEAVE TEST",
            "test_score": 0,
            "test_why": why,
            "coming_back": coming_back,
            "test_approaches": approaches,
        }
        out.update(extra)
        return out

    if not cands:
        return leave("Not testing a line from below.")

    line, resist, sma = cands[0]
    gap = (resist - px) / resist

    vol_shrink = False
    vol_hot = False
    vol_ratio = None
    if volume is not None:
        v = volume.reindex(close.index).fillna(0)
        if len(v) >= 25 and float(v.tail(25).sum()) > 0:
            recent = float(v.tail(5).mean())
            prior = float(v.iloc[-25:-5].mean())
            if prior > 0:
                vol_ratio = recent / prior
                vol_shrink = vol_ratio <= 0.85
                vol_hot = vol_ratio >= 1.15

    rsi = rsi_series(close)
    rsi_now = None if pd.isna(rsi.iloc[-1]) else float(rsi.iloc[-1])
    rsi_hot = rsi_now is not None and rsi_now >= 62
    rsi_dead = rsi_now is not None and rsi_now < 38

    obv_holds = False
    obv_new_low = False
    if volume is not None and n >= 30:
        v = volume.reindex(close.index).fillna(0)
        if float(v.tail(30).sum()) > 0:
            obv = obv_series(close, v)
            o_recent, o_prior = float(obv.iloc[-10:].min()), float(obv.iloc[-30:-10].min())
            p_recent, p_prior = float(close.iloc[-10:].min()), float(close.iloc[-30:-10].min())
            obv_new_low = o_recent < o_prior and p_recent <= p_prior
            obv_holds = (not obv_new_low) and (o_recent >= o_prior or (p_recent < p_prior and o_recent > o_prior))

    look = 60 if n >= 60 else max(n - 3, 1)
    in_zone = sma.notna() & (close < sma) & (close >= sma * (1 - ZONE_PCT))
    from_below = close.shift(1) < sma.shift(1) * (1 - ZONE_PCT)
    first = in_zone & ~in_zone.shift(1).fillna(False) & from_below.fillna(False)
    approaches = int(first.iloc[-look:].sum())
    coming_back = approaches >= 2
    rising_px = n >= 10 and float(close.iloc[-1]) > float(close.iloc[-10])
    extras = {
        "vol_shrink": vol_shrink,
        "vol_ratio": None if vol_ratio is None else round(float(vol_ratio), 2),
        "vol_hot": vol_hot,
        "obv_holds": obv_holds,
        "obv_new_low": obv_new_low,
    }

    fails: list[str] = []
    if death:
        fails.append("death / near-death")
    if gap > 0.04:
        fails.append(f"still {gap * 100:.1f}% under the {line} — not in the test zone yet")
    if not coming_back:
        fails.append("first slam at the line — usually fails")
    if not rising_px:
        fails.append("last days not rising into the line")
    if vol_hot:
        fails.append("volume expanding into the ceiling")
    if rsi_hot:
        fails.append("RSI already hot into resistance")
    elif rsi_dead:
        fails.append("RSI washed out")
    if obv_new_low:
        fails.append("OBV made a new low")

    if fails:
        return leave("; ".join(fails[:5]), approaches, coming_back, **extras)

    notes = [
        f"walk-up into the {line} ({gap * 100:.1f}% under)",
        f"coming back — {approaches} tests",
        "last days rising",
    ]
    if vol_shrink:
        notes.append("volume quiet")
    if obv_holds:
        notes.append("OBV held")
    notes.append("Wait for a close through — not a buy under the line.")
    return {
        "test_verdict": "good",
        "test_label": "GOOD TEST",
        "test_score": 6,
        "test_why": "; ".join(notes),
        "coming_back": True,
        "test_approaches": approaches,
        **extras,
    }


def method_events(close: pd.Series) -> tuple[dict | None, list[dict]]:
    """Last real 50-tag (display) plus first-from-above dips (walk-forward +5% vs stop)."""
    close = close.dropna()
    sma50 = close.rolling(50).mean()
    sma200 = close.rolling(200).mean()
    bull = (close > sma200) & (sma50 > sma200)
    in_zone = bull & (close <= sma50 * (1 + ZONE_PCT)) & (close >= sma50 * (1 - 0.01))
    from_above = close.shift(1) > sma50.shift(1) * (1 + ZONE_PCT)
    first = in_zone & ~in_zone.shift(1).fillna(False) & from_above.fillna(False)
    idxs = [i for i, flag in enumerate(first.tolist()) if flag]
    tests = _forward_from(close, sma50, idxs)
    n = len(close)
    zone_idxs = [
        i for i, flag in enumerate(in_zone.tolist())
        if flag and pd.notna(sma50.iloc[i])
    ]
    if not zone_idxs:
        return None, tests
    i = zone_idxs[-1]
    fill = float(close.iloc[i])
    fair = float(sma50.iloc[i])
    px = float(close.iloc[-1])
    return {
        "entry_price": round(fair, 2),
        "entry_fill": round(fill, 2),
        "entry_days_ago": int(n - 1 - i),
        "entry_date": close.index[i].strftime("%Y-%m-%d"),
        "pct_since_entry": round((px / fill - 1) * 100, 2),
        "near_entry": bool(abs(px / fill - 1) <= NEAR_ENTRY_PCT),
        "entry_result": tests[-1]["result"] if tests else None,
    }, tests


def _forward_from(close: pd.Series, sma50: pd.Series, idxs: list[int]) -> list[dict]:
    tests: list[dict] = []
    n = len(close)
    for i in idxs:
        entry = float(close.iloc[i])
        resolved = None
        hold_end = min(i + MAX_HOLD, n - 1)
        for j in range(i + 1, hold_end + 1):
            px = float(close.iloc[j])
            s50 = sma50.iloc[j]
            days = j - i
            if px >= entry * (1 + TARGET_PCT):
                resolved = {"result": "win", "days": days, "pct": round((px / entry - 1) * 100, 2)}
                break
            if pd.notna(s50) and px <= float(s50) * (1 - STOP_UNDER_50):
                resolved = {"result": "loss", "days": days, "pct": round((px / entry - 1) * 100, 2)}
                break
        if resolved is None:
            px = float(close.iloc[hold_end])
            still_open = hold_end == n - 1 and (n - 1 - i) < MAX_HOLD
            resolved = {
                "result": "open" if still_open else "timeout",
                "days": hold_end - i,
                "pct": round((px / entry - 1) * 100, 2),
            }
        tests.append(resolved)
    return tests


def poke_stats(close: pd.Series) -> dict:
    """How often a first poke at an SMA from below fails vs breaks through."""
    close = close.dropna()
    sma50 = close.rolling(50).mean()
    sma200 = close.rolling(200).mean()
    approaches = failed = broke = 0
    n = len(close)
    for sma in (sma50, sma200):
        in_zone = sma.notna() & (close < sma) & (close >= sma * (1 - ZONE_PCT))
        from_below = close.shift(1) < sma.shift(1) * (1 - ZONE_PCT)
        first = in_zone & ~in_zone.shift(1).fillna(False) & from_below.fillna(False)
        idxs = [i for i, flag in enumerate(first.tolist()) if flag]
        for i in idxs:
            approaches += 1
            resolved = False
            for j in range(i + 1, min(i + 25, n)):
                s = sma.iloc[j]
                if pd.isna(s):
                    continue
                px = float(close.iloc[j])
                s = float(s)
                if px > s:
                    broke += 1
                    resolved = True
                    break
                if px < s * (1 - ZONE_PCT):
                    failed += 1
                    resolved = True
                    break
            if not resolved:
                failed += 1
    return {"approaches": approaches, "failed": failed, "broke": broke}


def held_events(close: pd.Series) -> list[dict]:
    """After a close through an SMA, first pullback that holds it. Target = the other SMA if still above, else +5%."""
    close = close.dropna()
    sma50 = close.rolling(50).mean()
    sma200 = close.rolling(200).mean()
    tests: list[dict] = []
    n = len(close)
    for sma, other in ((sma50, sma200), (sma200, sma50)):
        cross = (close > sma) & (close.shift(1) <= sma.shift(1)) & sma.notna()
        idxs = [i for i, flag in enumerate(cross.tolist()) if flag]
        for i in idxs:
            entry_j = None
            search_end = min(i + MAX_HOLD, n - 1)
            for j in range(i + 1, search_end + 1):
                s = sma.iloc[j]
                if pd.isna(s):
                    continue
                px = float(close.iloc[j])
                s = float(s)
                if px <= s * (1 - STOP_UNDER_50):
                    break
                if s * 0.995 <= px <= s * (1 + ZONE_PCT):
                    entry_j = j
                    break
            if entry_j is None:
                continue
            entry = float(close.iloc[entry_j])
            o = other.iloc[entry_j]
            target = float(o) if pd.notna(o) and float(o) > entry * 1.003 else None
            resolved = None
            hold_end = min(entry_j + MAX_HOLD, n - 1)
            for k in range(entry_j + 1, hold_end + 1):
                px = float(close.iloc[k])
                s = sma.iloc[k]
                days = k - entry_j
                if target is not None and px >= target:
                    resolved = {"result": "win", "days": days, "pct": round((px / entry - 1) * 100, 2)}
                    break
                if target is None and px >= entry * (1 + TARGET_PCT):
                    resolved = {"result": "win", "days": days, "pct": round((px / entry - 1) * 100, 2)}
                    break
                if pd.notna(s) and px <= float(s) * (1 - STOP_UNDER_50):
                    resolved = {"result": "loss", "days": days, "pct": round((px / entry - 1) * 100, 2)}
                    break
            if resolved is None:
                px = float(close.iloc[hold_end])
                still_open = hold_end == n - 1 and (n - 1 - entry_j) < MAX_HOLD
                resolved = {
                    "result": "open" if still_open else "timeout",
                    "days": hold_end - entry_j,
                    "pct": round((px / entry - 1) * 100, 2),
                }
            tests.append(resolved)
    return tests


def summarize_tests(tests: list[dict], rule: str | None = None, stop: str | None = None) -> dict:
    wins = [t for t in tests if t["result"] == "win"]
    losses = [t for t in tests if t["result"] == "loss"]
    timeouts = [t for t in tests if t["result"] == "timeout"]
    opens = [t for t in tests if t["result"] == "open"]
    closed = len(wins) + len(losses)
    median_days = None
    if wins:
        days = sorted(t["days"] for t in wins)
        median_days = days[len(days) // 2]
    return {
        "entries": len(tests),
        "wins": len(wins),
        "losses": len(losses),
        "timeouts": len(timeouts),
        "open": len(opens),
        "win_rate_closed": None if not closed else round(100.0 * len(wins) / closed, 1),
        "median_days_to_5": median_days,
        "target": "+5%",
        "stop": stop or "daily close 2% under the 50",
        "hold_max": MAX_HOLD,
        "rule": rule or (
            "Full uptrend only: first dip into ~2% of the 50, coming from more than 2% above it. "
            "Then +5% before a close 2% under the 50, max 60 sessions. Last stair — 50 is already support."
        ),
    }


def regime(price: float, s50: float, s200: float) -> str:
    if price > s50 and price > s200 and s50 > s200:
        return "bull"
    if price > s50 and price > s200 and s50 < s200:
        return "repairing"
    if price < s50 and price < s200:
        return "bear"
    return "mixed"


def blurb(d: dict) -> str:
    gap = abs(d["gap_pct"])
    side = "above" if d["gap_pct"] >= 0 else "below"
    rsi_txt = "RSI is missing." if d["rsi"] is None else f"RSI is {d['rsi']:.0f}."
    rising = "Blue 50 is rising." if d["sma50_rising"] else "Blue 50 is not rising yet."
    price_vs = {
        "bull": "Price is above both averages.",
        "repairing": "Price is already above both averages, but 50 has not crossed 200 yet.",
        "mixed": "Price is between the two averages.",
        "bear": "Price is still below both averages.",
    }[d["regime"]]
    if d["signal"] == "GOLDEN_TODAY":
        return f"Blue 50 crossed above red 200 on the latest daily bar. {price_vs} {rsi_txt} Chart it in IBKR — do not chase blindly."
    if d["signal"] == "RECENT_GOLDEN":
        days = d["days_since_golden"]
        return f"Golden cross {days} day{'s' if days != 1 else ''} ago. {price_vs} {rising} {rsi_txt}"
    if d["signal"] == "NEAR_GOLDEN":
        return f"Blue 50 is {gap:.2f}% {side} red 200. {rising} {price_vs} {rsi_txt} Watch for the actual cross; a near-gap is not an entry."
    if d["signal"] == "NEAR_DEATH":
        return f"Blue 50 is only {gap:.2f}% above red 200 and is not rising. That is close to a death cross. {price_vs} {rsi_txt} Treat rallies as suspect until the 50 firms up again."
    if d["signal"] == "DEATH_TODAY":
        return f"Blue 50 crossed below red 200 today (death cross). {price_vs} {rsi_txt}"
    if d["signal"] == "BULL":
        return f"Uptrend: 50 is above 200. {price_vs} {rsi_txt} Prefer a pullback toward the 50 over chasing."
    if d["signal"] == "REPAIRING":
        return f"Repairing: price is back above both lines, but 50 is still {gap:.2f}% below 200. {rising} {rsi_txt}"
    if d["signal"] == "BEAR":
        return f"Downtrend: price is below both averages. 50 is {gap:.2f}% {side} 200. {rsi_txt} Wait for a close back above the 50."
    return f"Mixed: price is between the two averages. 50 is {gap:.2f}% {side} 200. {rising} {rsi_txt}"


def catchup_clock(sma50: pd.Series, s50: float, s200: float) -> dict:
    """If the 50 keeps moving at the last 20-session pace, how long until it meets the 200."""
    look = min(20, len(sma50) - 1)
    slope = (float(s50) - float(sma50.iloc[-1 - look])) / look
    gap_pts = float(s200) - float(s50)
    months = None
    if gap_pts <= 0:
        months = 0.0
    elif slope > 0:
        months = round((gap_pts / slope) / 21.0, 1)
        if months > 24:
            months = 24.0
    return {"sma50_slope": round(slope, 4), "months_to_cross": months}


def classify(close: pd.Series, close_pct: float, volume: pd.Series | None = None) -> dict | None:
    close = close.dropna()
    if len(close) < 200:
        return None
    sma50 = close.rolling(50).mean()
    sma200 = close.rolling(200).mean()
    s50, s200 = sma50.iloc[-1], sma200.iloc[-1]
    prev50, prev200 = sma50.iloc[-2], sma200.iloc[-2]
    px = close.iloc[-1]
    if pd.isna(s50) or pd.isna(s200) or pd.isna(prev50) or pd.isna(prev200) or pd.isna(px):
        return None

    gap_pct = (s50 - s200) / s200 * 100.0
    lookback = min(SLOPE_LOOKBACK, len(sma50) - 1)
    rising = bool(s50 > sma50.iloc[-1 - lookback])
    golden_today = bool((prev50 <= prev200) and (s50 > s200))
    death_today = bool((prev50 >= prev200) and (s50 < s200))
    crossed = (sma50 > sma200) & (sma50.shift(1) <= sma200.shift(1))
    last_cross = crossed[crossed].index.max() if crossed.any() else pd.NaT
    days_since = (close.index[-1] - last_cross).days if pd.notna(last_cross) else None
    recent = days_since is not None and days_since <= RECENT_DAYS and s50 > s200
    reg = regime(float(px), float(s50), float(s200))

    if golden_today:
        signal = "GOLDEN_TODAY"
    elif recent:
        signal = "RECENT_GOLDEN"
    elif s50 < s200 and abs(gap_pct) <= close_pct and rising:
        signal = "NEAR_GOLDEN"
    elif death_today:
        signal = "DEATH_TODAY"
    elif s50 > s200 and abs(gap_pct) <= close_pct and not rising:
        signal = "NEAR_DEATH"
    else:
        signal = {"bull": "BULL", "repairing": "REPAIRING", "mixed": "MIXED", "bear": "BEAR"}[reg]

    row = {
        "signal": signal,
        "price": round(float(px), 2),
        "sma50": round(float(s50), 2),
        "sma200": round(float(s200), 2),
        "gap_pct": round(float(gap_pct), 2),
        "sma50_rising": rising,
        "days_since_golden": None if days_since is None else int(days_since),
        "rsi": None if (r := rsi_wilder(close)) is None else round(r, 1),
        "regime": reg,
        "market_cap": None,
        "spark": [round(float(v), 2) for v in close.tail(40).tolist()],
    }
    row.update(catchup_clock(sma50, float(s50), float(s200)))
    row["blurb"] = blurb(row)
    last_snap, tests = method_events(close)
    if last_snap:
        row.update(last_snap)
    else:
        row["entry_price"] = None
        row["entry_fill"] = None
        row["entry_days_ago"] = None
        row["entry_date"] = None
        row["pct_since_entry"] = None
        row["near_entry"] = False
        row["entry_result"] = None
    row.update(dip_quality(close, sma50, volume))
    row.update(test_quality(close, sma50, sma200, volume, signal))
    row["_tests"] = tests
    row["_held"] = held_events(close)
    row["_pokes"] = poke_stats(close)
    return row


def load_last() -> dict | None:
    global LAST_PAYLOAD
    if LAST_PAYLOAD:
        return LAST_PAYLOAD
    if DATA_PATH.exists():
        LAST_PAYLOAD = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        return LAST_PAYLOAD
    return None


def inject(payload: dict) -> None:
    html = HTML_PATH.read_text(encoding="utf-8")
    blob = json.dumps(payload, separators=(",", ":"))
    repl = f"<!--SCAN_DATA-->\n  <script>window.SCAN_DATA = {blob};</script>\n  <!--/SCAN_DATA-->"
    updated, n = re.subn(
        r"<!--SCAN_DATA-->.*?<!--/SCAN_DATA-->",
        lambda _: repl,
        html,
        count=1,
        flags=re.S,
    )
    if n != 1:
        raise RuntimeError("Could not find SCAN_DATA markers in the HTML file.")
    HTML_PATH.write_text(updated, encoding="utf-8")


def save_last(payload: dict) -> None:
    global LAST_PAYLOAD
    LAST_PAYLOAD = payload
    DATA_PATH.write_text(json.dumps(payload), encoding="utf-8")
    inject(payload)


def _cached_caps() -> dict[str, float]:
    payload = load_last()
    out: dict[str, float] = {}
    if not payload:
        return out
    for row in payload.get("all") or []:
        ticker = row.get("ticker")
        cap = row.get("market_cap")
        if ticker and cap:
            out[str(ticker)] = float(cap)
    return out


def _cap_from_yahoo(ticker: str) -> float | None:
    try:
        info = yf.Ticker(ticker).fast_info
        if hasattr(info, "get"):
            cap = info.get("marketCap") or info.get("market_cap")
            shares = info.get("shares") or info.get("sharesOutstanding")
            last = info.get("last_price") or info.get("lastPrice")
        else:
            cap = getattr(info, "market_cap", None)
            shares = getattr(info, "shares", None)
            last = getattr(info, "last_price", None)
        if cap:
            return float(cap)
        if shares and last:
            return float(shares) * float(last)
    except Exception:
        return None
    return None


def fetch_market_caps(tickers: list[str]) -> dict[str, float]:
    """Ask Yahoo in small waves, then keep last scan's cap if a name still misses."""
    fresh: dict[str, float] = {}
    pending = list(tickers)
    for i, workers in enumerate((6, 3, 1)):
        if not pending:
            break
        if i:
            time.sleep(1.2)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_cap_from_yahoo, t): t for t in pending}
            for fut in as_completed(futs):
                ticker = futs[fut]
                cap = fut.result()
                if cap:
                    fresh[ticker] = cap
        pending = [t for t in pending if t not in fresh]

    cached = _cached_caps()
    reused = 0
    for ticker in pending:
        if ticker in cached:
            fresh[ticker] = cached[ticker]
            reused += 1
    print(f"market caps: {len(fresh)}/{len(tickers)}  reused={reused}  still_blank={len(tickers) - len(fresh)}")
    return fresh


def scan(close_pct: float, extra_from_ui: list[str] | None = None) -> dict:
    names, added, skipped = merge_universe(extra_from_ui)
    tickers = names["ticker"].tolist()
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {len(tickers)} names  added={added}  skipped_dupes={skipped}")
    closes, volumes = download_market(tickers)
    info = names.set_index("ticker")

    all_rows = []
    all_tests: list[dict] = []
    all_held: list[dict] = []
    poke_tot = {"approaches": 0, "failed": 0, "broke": 0}
    for ticker in closes.columns:
        vol = None
        if volumes is not None and ticker in volumes.columns:
            vol = volumes[ticker]
        result = classify(closes[ticker], close_pct, vol)
        if not result:
            continue
        meta = info.loc[ticker] if ticker in info.index else None
        result["ticker"] = ticker
        result["name"] = "" if meta is None else str(meta["name"])
        result["sector"] = "" if meta is None else str(meta["sector"])
        all_tests.extend(result.pop("_tests", []))
        all_held.extend(result.pop("_held", []))
        pokes = result.pop("_pokes", {})
        for key in poke_tot:
            poke_tot[key] += int(pokes.get(key, 0) or 0)
        all_rows.append(result)

    caps = fetch_market_caps([r["ticker"] for r in all_rows])
    for row in all_rows:
        cap = caps.get(row["ticker"])
        row["market_cap"] = None if cap is None else int(cap)

    hit_names = {"GOLDEN_TODAY", "NEAR_GOLDEN", "RECENT_GOLDEN", "NEAR_DEATH", "DEATH_TODAY"}
    hits = [r for r in all_rows if r["signal"] in hit_names]
    order = {"GOLDEN_TODAY": 0, "NEAR_GOLDEN": 1, "RECENT_GOLDEN": 2, "NEAR_DEATH": 3, "DEATH_TODAY": 4}
    hits.sort(key=lambda h: (order[h["signal"]], abs(h["gap_pct"])))
    all_rows.sort(key=lambda r: r["ticker"])
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "universe": len(tickers),
        "close_pct": close_pct,
        "hits": hits,
        "all": all_rows,
        "added": added,
        "skipped_duplicates": skipped,
        "extra_tickers": parse_ticker_list(list(EXTRA_TICKERS) + list(extra_from_ui or [])),
        "method_test": {
            "held": summarize_tests(
                all_held,
                rule=(
                    "After a daily close through the 50 or the 200, wait for the first pullback that holds that SMA "
                    "(within 2% above it). Target is the other SMA if it is still overhead; otherwise +5%. "
                    "Stop is a close 2% under the SMA you just claimed. Max 60 sessions. This is the trade."
                ),
                stop="daily close 2% under the held SMA",
            ),
            "dip": summarize_tests(all_tests),
            "pokes": {
                **poke_tot,
                "fail_rate": None if not poke_tot["approaches"] else round(
                    100.0 * poke_tot["failed"] / poke_tot["approaches"], 1
                ),
            },
        },
    }
    save_last(payload)
    print(f"{len(hits)} watch hits · {len(all_rows)} universe rows")
    return payload


def make_handler(close_pct: float):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            print(f"[watch] {self.address_string()} {fmt % args}")

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def _json(self, code: int, payload: dict):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html", "/sp500_sma_watch.html"):
                data = HTML_PATH.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self._cors()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/health":
                self._json(200, {"ok": True, "extra_tickers": parse_ticker_list(EXTRA_TICKERS)})
                return
            if path == "/api/last":
                last = load_last()
                self._json(200, last or {"hits": None})
                return
            self._json(404, {"error": "not found"})

        def do_POST(self):
            path = urlparse(self.path).path
            if path != "/api/scan":
                self._json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {}
            extras = body.get("extra_tickers") or []
            if isinstance(extras, str):
                extras = [extras]
            if not SCAN_LOCK.acquire(blocking=False):
                self._json(409, {"error": "A scan is already running. Wait for it to finish."})
                return
            try:
                payload = scan(close_pct, extras)
                self._json(200, payload)
            except Exception as exc:
                self._json(500, {"error": str(exc)})
            finally:
                SCAN_LOCK.release()

    return Handler


def serve(close_pct: float, port: int, open_browser: bool, autoscan: bool = False) -> None:
    try:
        install_protocol()
    except Exception as exc:
        print("Could not register HTML launcher:", exc)

    load_last()
    url = f"http://127.0.0.1:{port}/"
    if autoscan:
        url += "?autoscan=1"
    if port_busy(port):
        print("Watcher already running — opening the page")
        if open_browser:
            webbrowser.open(url)
        return
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(close_pct))
    print(f"Watcher running at {url}")
    print("Keep this window open.")
    if open_browser:
        webbrowser.open(url)
    httpd.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--close-pct", type=float, default=CLOSE_PCT)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--once", action="store_true", help="Scan once, write JSON, exit (no server)")
    parser.add_argument("--autoscan", action="store_true", help="Open the page and scan immediately")
    parser.add_argument("--install-protocol", action="store_true")
    parser.add_argument("protocol_url", nargs="?", help="Filled in when launched from the HTML button")
    args = parser.parse_args()

    if args.protocol_url:
        args.autoscan = False
    if args.install_protocol:
        install_protocol()
        raise SystemExit(0)
    if args.once:
        scan(args.close_pct)
    else:
        serve(args.close_pct, args.port, not args.no_open, args.autoscan)
