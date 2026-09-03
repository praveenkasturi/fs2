"""S&P 500 50/200 SMA watcher. Serves the HTML and runs live scans on button click."""

from __future__ import annotations

import argparse
import json
import math
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
SCRIPT_PATH = HERE / "scan_data.js"


def fix_stdio() -> None:
    """Background launches (sp500watch://) often have no console — print/tqdm then crash."""
    import os

    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        try:
            if stream is None:
                raise OSError("missing stream")
            stream.write("")
            stream.flush()
        except OSError:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8", errors="replace"))


def log(msg: str) -> None:
    try:
        print(msg, flush=True)
    except OSError:
        pass


def json_safe(obj):
    """Turn NaN / Inf into None so browsers can parse the payload."""
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [json_safe(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    try:
        # numpy / pandas scalars
        if hasattr(obj, "item"):
            return json_safe(obj.item())
    except Exception:
        pass
    if pd.isna(obj):
        return None
    return obj


def dumps_json(payload, **kwargs) -> str:
    return json.dumps(json_safe(payload), allow_nan=False, **kwargs)

CLOSE_PCT = 2.0
RECENT_DAYS = 10
SLOPE_LOOKBACK = 5
PORT = 8765
TARGET_PCT = 0.05
STOP_UNDER_50 = 0.02
ZONE_PCT = 0.02
MAX_HOLD = 60
NEAR_ENTRY_PCT = 0.025

OBV_LOOK = 10          # days of money flow to read
OBV_IN = 0.10          # net flow >= 10% of traded volume counts as money coming in
TEST_ZONE = 0.05       # how far under a line still counts as a live test
STAGE_ZONE = 0.08      # how far under a line still counts as the TEST stage
HOLD_ZONE = 0.025      # how far above its line a name can sit and still be "holding" it
CONFIRM_CLOSES = 2     # daily closes needed before a break counts
RSI_BUY = (35, 68)     # RSI band allowed in the BUY THIS pack
EARNINGS_WARN = 7      # flag a name reporting within this many days
ENGINE = "v2"          # bumped when the scoring changes, shown on the page

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
    """Let the HTML button start this script via sp500watch:// (current user, no admin).

    Browser protocol launches hide the console. Wrap with `start` so a real
    black window opens and stays open while the watcher runs.
    """
    import winreg

    script = str(HTML_PATH.with_name("sp500_sma_watch.py"))
    work = str(HERE)
    # /D sets the working folder; title "FinSense Watcher" must come right after start.
    cmd = (
        f'cmd.exe /c start "FinSense Watcher" /D "{work}" '
        f'"{sys.executable}" "{script}" --no-open'
    )
    base = winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{PROTOCOL}")
    winreg.SetValueEx(base, "", 0, winreg.REG_SZ, "URL:SP500 SMA Watch")
    winreg.SetValueEx(base, "URL Protocol", 0, winreg.REG_SZ, "")
    cmd_key = winreg.CreateKey(base, r"shell\open\command")
    winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, cmd)
    log("HTML one-click is enabled (sp500watch://) — opens a visible Python window")

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


def _download_chunk(tickers: list[str]) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """One Yahoo batch. Returns (closes, volumes) or (None, None) on hard failure."""
    try:
        data = yf.download(
            tickers,
            period="2y",
            interval="1d",
            auto_adjust=False,
            threads=True,
            progress=False,
            group_by="column",
        )
    except Exception as exc:
        log(f"download batch failed ({len(tickers)} names): {exc}")
        return None, None
    if data is None or getattr(data, "empty", True):
        return None, None
    closes = _panel(data, "Close")
    if closes is None:
        closes = data.copy() if not isinstance(data.columns, pd.MultiIndex) else data
        closes.columns = [normalize_ticker(c) for c in closes.columns]
        closes = closes.dropna(axis=1, how="all")
    return closes, _panel(data, "Volume")


def download_market(tickers: list[str]) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    """Raw closes in small batches so Yahoo rate limits do not wipe the whole scan.

    auto_adjust=False keeps dividend-unadjusted closes so the 50/200 match IBKR.
    """
    batch = 40
    pause = 1.5
    got_close: dict[str, pd.Series] = {}
    got_vol: dict[str, pd.Series] = {}
    pending = list(tickers)

    for attempt in range(3):
        if not pending:
            break
        if attempt:
            wait = pause * (attempt + 1)
            log(f"retrying {len(pending)} missing names after {wait:.0f}s (Yahoo rate limit)")
            time.sleep(wait)
        next_pending: list[str] = []
        for i in range(0, len(pending), batch):
            chunk = pending[i : i + batch]
            closes, volumes = _download_chunk(chunk)
            if closes is None or closes.empty:
                next_pending.extend(chunk)
            else:
                for col in closes.columns:
                    series = closes[col].dropna()
                    if len(series) >= 200:
                        got_close[col] = series
                    else:
                        next_pending.append(col)
                if volumes is not None:
                    for col in volumes.columns:
                        if col in got_close:
                            got_vol[col] = volumes[col]
                missed = [t for t in chunk if t not in got_close]
                next_pending.extend(missed)
            time.sleep(pause)
        pending = [t for t in dict.fromkeys(next_pending) if t not in got_close]
        log(f"download pass {attempt + 1}: {len(got_close)}/{len(tickers)} names")

    if not got_close:
        return pd.DataFrame(), None
    closes = pd.DataFrame(got_close).sort_index()
    volumes = pd.DataFrame(got_vol).reindex(closes.index) if got_vol else None
    return closes, volumes


def download_benchmark() -> pd.Series | None:
    """S&P 500 index closes, for relative strength."""
    try:
        data = yf.download("^GSPC", period="2y", interval="1d", auto_adjust=False, progress=False)
        panel = _panel(data, "Close")
        if panel is None or panel.empty:
            return None
        return panel.iloc[:, 0].dropna()
    except Exception:
        return None


def market_state(last_bar) -> dict:
    """Is the last daily bar final, or is the session still running?"""
    bar = pd.Timestamp(last_bar)
    out = {"bar_date": bar.strftime("%Y-%m-%d"), "provisional": False, "market": "unknown"}
    try:
        now = pd.Timestamp.now(tz="America/New_York")
    except Exception:
        return out
    weekday = now.weekday() < 5
    open_at = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_at = now.replace(hour=16, minute=0, second=0, microsecond=0)
    is_open = weekday and open_at <= now <= close_at
    out["market"] = "open" if is_open else "closed"
    out["provisional"] = bool(is_open and bar.date() == now.date())
    out["checked_at"] = now.strftime("%Y-%m-%d %H:%M %Z")
    return out


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


def obv_tape(close: pd.Series, volume: pd.Series | None, look: int = OBV_LOOK) -> dict:
    """Net money flow over ~10 days as a share of the volume actually traded.

    OBV is a running total whose level depends on where the download starts, so a percent
    change of OBV is meaningless. Dividing by (average daily volume x days) gives a number
    between about -1 and +1: "net buying was 24% of everything traded".
    """
    n = len(close)
    price_dir = "up" if n > look and float(close.iloc[-1]) > float(close.iloc[-1 - look]) else "down"
    out = {
        "price_dir": price_dir,
        "obv_dir": None,
        "obv_flow": None,
        "obv_rank": "unknown",
        "obv_why": "No volume data — treated as unknown, not good.",
    }
    if volume is None or n < look + 20:
        return out
    v = volume.reindex(close.index).fillna(0).astype(float)
    avg = float(v.iloc[-20:].mean())
    if avg <= 0:
        return out
    obv = obv_series(close, v)
    flow = (float(obv.iloc[-1]) - float(obv.iloc[-1 - look])) / (avg * look)
    pct = flow * 100
    if flow >= OBV_IN:
        obv_dir, rank = "up", "best"
        why = f"money coming in — net buying is {pct:.0f}% of the last {look} days of volume"
    elif flow <= -OBV_IN:
        obv_dir, rank = "down", "leave"
        why = f"money leaving — net selling is {abs(pct):.0f}% of the last {look} days of volume"
    else:
        obv_dir, rank = "flat", "ok"
        why = f"money held — net flow is only {pct:+.0f}% of recent volume"
    return {
        "price_dir": price_dir,
        "obv_dir": obv_dir,
        "obv_flow": round(flow, 3),
        "obv_rank": rank,
        "obv_why": why,
    }


def dip_quality(
    close: pd.Series,
    line: pd.Series | None,
    line_name: str,
    volume: pd.Series | None,
) -> dict:
    """Buy the dip vs leave it, scored against the line the name is actually holding.

    A GAME ON sitting on the 200 is judged on the 200, not on a 50 far overhead.
    """
    if line is None or pd.isna(line.iloc[-1]):
        return {
            "dip_verdict": "leave",
            "dip_label": "LEAVE THE DIP",
            "dip_score": -3,
            "dip_why": "No line under price to dip into.",
            "dip_line": None,
        }
    px = float(close.iloc[-1])
    s50 = float(line.iloc[-1])
    n = len(close)
    above_50 = px >= s50 * 0.995
    lost_50 = px < s50 * 0.98
    look = min(SLOPE_LOOKBACK, n - 1)
    rising = bool(s50 > float(line.iloc[-1 - look]))
    sma50 = line

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
        reasons.append(f"close lost the {line_name}")
    elif above_50:
        score += 2
        reasons.append(f"close still holds the {line_name}")
    else:
        reasons.append(f"sitting right on the {line_name}")

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
        reasons.append(f"{line_name} is still rising")
    else:
        score -= 1
        reasons.append(f"{line_name} is flat or falling")

    if first_touch:
        score += 1
        reasons.append(f"early visit to the {line_name}")
    else:
        reasons.append(f"{line_name} already tagged a few times")

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
        "dip_line": line_name,
        "dip_vol_ratio": vol_ratio,
        "dip_vol_shrink": vol_shrink,
        "dip_obv_holds": obv_holds,
        "dip_obv_new_low": obv_new_low,
        "dip_rsi_div": rsi_div,
        "dip_first_touch": first_touch,
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
        "test_vol_shrink": vol_shrink,
        "test_vol_ratio": None if vol_ratio is None else round(float(vol_ratio), 2),
        "test_vol_hot": vol_hot,
        "test_obv_holds": obv_holds,
        "test_obv_new_low": obv_new_low,
        "test_gap_pct": round(gap * 100, 2),
        "test_line": line,
    }

    fails: list[str] = []
    if death:
        fails.append("death / near-death")
    if gap > TEST_ZONE:
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


def bands_of(px: float, s50: float, s200: float) -> tuple[tuple[str, float] | None, tuple[str, float] | None]:
    """Nearest line overhead (ceiling) and nearest line underneath (support)."""
    lines = [("50", s50), ("200", s200)]
    over = sorted([l for l in lines if px < l[1]], key=lambda l: l[1])
    under = sorted([l for l in lines if px >= l[1]], key=lambda l: -l[1])
    return (over[0] if over else None), (under[0] if under else None)


def line_run(close: pd.Series, line: pd.Series) -> int:
    """Consecutive daily closes above the line (+) or below it (-)."""
    above = (close > line) & line.notna()
    last = bool(above.iloc[-1])
    run = 0
    for flag in reversed(above.tolist()):
        if bool(flag) != last:
            break
        run += 1
    return run if last else -run


def was_below(close: pd.Series, line: pd.Series, look: int = 40) -> bool:
    """Did price trade under this line recently? Compares each day to the line on that day."""
    c, s = close.iloc[-look:-1], line.iloc[-look:-1]
    if c.empty:
        return False
    return bool((c < s).any())


def compression(close: pd.Series, line: pd.Series, look: int = 20) -> dict:
    """Pre-break coil: gap to the line closing, lows lifting, daily range tightening.

    This is the Friday-Tesla tell — a walk-up that is winding tighter into the ceiling.
    """
    n = len(close)
    out = {"coil_score": 0, "coil_why": "", "coil_gap_now": None, "coil_gap_then": None}
    if n < look * 2 or pd.isna(line.iloc[-1]):
        return out
    px, lv = float(close.iloc[-1]), float(line.iloc[-1])
    then_px, then_lv = float(close.iloc[-1 - look]), float(line.iloc[-1 - look])
    if any(pd.isna(x) or math.isinf(x) for x in (px, lv, then_px, then_lv)) or lv <= 0 or then_lv <= 0:
        return out
    gap_now = (lv - px) / lv
    gap_then = (then_lv - then_px) / then_lv
    if any(pd.isna(x) or math.isinf(x) for x in (gap_now, gap_then)):
        return out
    lows_now = float(close.iloc[-look // 2:].min())
    lows_then = float(close.iloc[-look:-look // 2].min())
    rng_now = float(close.iloc[-look // 2:].max()) - lows_now
    rng_then = float(close.iloc[-look:-look // 2].max()) - lows_then
    if any(pd.isna(x) for x in (lows_now, lows_then, rng_now, rng_then)):
        return out
    score = 0
    why = []
    if gap_now < gap_then:
        score += 1
        why.append("gap to the line is closing")
    if lows_now > lows_then:
        score += 1
        why.append("lows are lifting")
    if rng_then > 0 and rng_now < rng_then:
        score += 1
        why.append("daily range is tightening")
    out.update({
        "coil_score": score,
        "coil_why": "; ".join(why) if why else "no coil — drifting, not winding up",
        "coil_gap_now": round(gap_now * 100, 2),
        "coil_gap_then": round(gap_then * 100, 2),
    })
    return out


def rel_strength(close: pd.Series, bench: pd.Series | None, look: int = 60) -> float | None:
    """Return over ~3 months minus the S&P's, in points."""
    if bench is None or len(close) <= look:
        return None
    b = bench.reindex(close.index).ffill().dropna()
    if len(b) <= look:
        return None
    mine = float(close.iloc[-1]) / float(close.iloc[-1 - look]) - 1
    theirs = float(b.iloc[-1]) / float(b.iloc[-1 - look]) - 1
    return round((mine - theirs) * 100, 1)


def stage_of(row: dict, close: pd.Series, sma50: pd.Series, sma200: pd.Series) -> dict:
    """The one place a name gets its stair label. The page only renders this."""
    px, s50, s200 = row["price"], row["sma50"], row["sma200"]
    rsi, reg, signal = row["rsi"], row["regime"], row["signal"]
    resist, support = bands_of(px, s50, s200)
    bouncing = len(close) >= 6 and float(close.iloc[-1]) >= float(close.iloc[-6])
    death = signal in ("NEAR_DEATH", "DEATH_TODAY")

    def out(tag: str, why: str) -> dict:
        return {"stage": tag, "stage_why": why}

    if death:
        return out("AVOID", "The 50 is losing the 200. Rallies off this usually fail.")
    if (
        reg == "bull" and row["sma50_rising"] and px <= s50 * 1.02
        and rsi is not None and 35 <= rsi < 60 and bouncing
    ):
        return out("GROWTH", "Uptrend, dip to a rising 50, RSI cooled, last days already turning up. The cleanest last-stair dip.")
    if reg == "bull":
        if px <= s50 * 1.02 and (rsi is None or rsi < 60):
            return out("RIZZ", "Uptrend and price is back on the 50. Last stair — the 50 is support.")
        return out("HOLD", "Uptrend already running. Only interesting on a dip to the 50 — not up here.")
    if signal in ("GOLDEN_TODAY", "RECENT_GOLDEN"):
        return out("HOLD", "The cross happened. Don't chase the first green day. Wait for a pullback to the 50.")
    if (
        support and not death
        and 0 <= (px - support[1]) / support[1] <= HOLD_ZONE
        and was_below(close, sma50 if support[0] == "50" else sma200)
        and (rsi is None or rsi < 68)
    ):
        nxt = resist[0] if resist else "open air"
        return out("GAME ON", f"Broke the {support[0]}, came back, and is holding it. That line is support now. Next ceiling is the {nxt}.")
    if resist and not death:
        gap = (resist[1] - px) / resist[1]
        rsi_ok = rsi is None or 38 <= rsi < 62
        rising = len(close) >= 10 and float(close.iloc[-1]) > float(close.iloc[-10])
        if 0 <= gap <= STAGE_ZONE and rising and rsi_ok:
            return out("TEST", f"Climbing into the {resist[0]} from below. That line is still the ceiling. Early tests usually fail — wait for a close through.")
    if reg == "mixed":
        return out("RANGE", "Price is between the two averages. One is support, one is the ceiling. Sit it out.")
    if reg == "repairing":
        return out("CLEAR", "Above both lines, but the 50 has not crossed the 200 yet. Don't chase — wait for a retest.")
    if reg == "bear":
        return out("AVOID", "Below both averages and not in a clean test of the near line.")
    return out("RANGE", "Stuck between the two averages.")


def pack_of(row: dict) -> dict:
    """BUY THIS = every check green after the break. BREAK WATCH = the walk-up before it."""
    stage = row["stage"]
    px = row["price"]
    rsi = row["rsi"]
    lo, hi = RSI_BUY

    if stage in ("TEST", "AVOID"):
        if row.get("test_verdict") == "good":
            return {
                "pack": "WATCH",
                "pack_why": "Walk-up into the line. Not a buy yet — the entry is a daily close through it.",
                "pack_checks": [],
            }
        return {"pack": None, "pack_why": "", "pack_checks": []}

    if stage not in ("GAME ON", "RIZZ", "GROWTH"):
        return {"pack": None, "pack_why": "", "pack_checks": []}

    line_px = row.get("line_px")
    over = None if not line_px else (px - line_px) / line_px * 100
    run = row.get("line_run") or 0
    checks = [
        {
            "label": "Stage is a buy stage",
            "ok": True,
            "note": f"{stage} — price already owns a line",
        },
        {
            "label": "Holding its line",
            "ok": bool(line_px and px >= line_px * 0.995),
            "note": "no line underneath" if not line_px else f"{over:+.1f}% vs the {row.get('line_name')} at ${line_px:,.2f}",
        },
        {
            "label": "Break is confirmed",
            "ok": run >= CONFIRM_CLOSES,
            "note": f"{abs(run)} closes {'above' if run > 0 else 'below'} the line (need {CONFIRM_CLOSES} above)",
        },
        {
            "label": "Not stretched",
            "ok": over is not None and over <= HOLD_ZONE * 100 * 2,
            "note": "no line underneath" if over is None else f"{over:+.1f}% above the line (want under {HOLD_ZONE * 200:.0f}%)",
        },
        {
            "label": "Dip flag says buy",
            "ok": row.get("dip_verdict") == "buy",
            "note": row.get("dip_label") or "no dip read",
        },
        {
            "label": "Money not leaving",
            "ok": row.get("obv_rank") in ("best", "ok"),
            "note": row.get("obv_why") or "",
        },
        {
            "label": "RSI sane",
            "ok": rsi is not None and lo <= rsi <= hi,
            "note": "no RSI" if rsi is None else f"RSI {rsi:.0f} (want {lo}–{hi})",
        },
        {
            "label": "No death cross nearby",
            "ok": row["signal"] not in ("NEAR_DEATH", "DEATH_TODAY"),
            "note": "50 is not losing the 200",
        },
    ]
    misses = [c["label"] for c in checks if not c["ok"]]
    if misses:
        return {
            "pack": None,
            "pack_why": "Missing: " + ", ".join(misses).lower() + ".",
            "pack_checks": checks,
        }
    strong = row.get("obv_rank") == "best"
    return {
        "pack": "BUY",
        "pack_why": (
            "Every check is green: it owns its line, the break is confirmed, the dip reads clean, "
            + ("money is coming in" if strong else "money is holding")
            + ", RSI is sane. Confirm the last bar on IBKR before you act."
        ),
        "pack_checks": checks,
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
            # Stop first: if one bar could have hit both, score the bad outcome.
            if pd.notna(s50) and px <= float(s50) * (1 - STOP_UNDER_50):
                resolved = {"result": "loss", "days": days, "pct": round((px / entry - 1) * 100, 2)}
                break
            if px >= entry * (1 + TARGET_PCT):
                resolved = {"result": "win", "days": days, "pct": round((px / entry - 1) * 100, 2)}
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
    approaches = failed = broke = unresolved = 0
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
                # Still in progress at the end of the data. Not a failure — just unknown.
                unresolved += 1
    return {"approaches": approaches, "failed": failed, "broke": broke, "unresolved": unresolved}


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
                # Stop first: if one bar could have hit both, score the bad outcome.
                if pd.notna(s) and px <= float(s) * (1 - STOP_UNDER_50):
                    resolved = {"result": "loss", "days": days, "pct": round((px / entry - 1) * 100, 2)}
                    break
                if target is not None and px >= target:
                    resolved = {"result": "win", "days": days, "pct": round((px / entry - 1) * 100, 2)}
                    break
                if target is None and px >= entry * (1 + TARGET_PCT):
                    resolved = {"result": "win", "days": days, "pct": round((px / entry - 1) * 100, 2)}
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


def classify(
    close: pd.Series,
    close_pct: float,
    volume: pd.Series | None = None,
    bench: pd.Series | None = None,
) -> dict | None:
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
        "bar_date": close.index[-1].strftime("%Y-%m-%d"),
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
    series = {"50": sma50, "200": sma200}
    resist, support = bands_of(float(px), float(s50), float(s200))

    row.update(stage_of(row, close, sma50, sma200))
    row.update(obv_tape(close, volume))
    row["rs_60"] = rel_strength(close, bench)

    # The line the plan hangs on: what price is standing on, else the ceiling above it.
    plan = support or resist
    row["line_name"] = None if not plan else plan[0]
    row["line_px"] = None if not plan else round(plan[1], 2)
    row["line_kind"] = "support" if support else ("ceiling" if resist else None)
    row["ceiling_name"] = None if not resist else resist[0]
    row["ceiling_px"] = None if not resist else round(resist[1], 2)
    row["line_run"] = 0 if not support else line_run(close, series[support[0]])
    row["confirmed_break"] = bool(support and row["line_run"] >= CONFIRM_CLOSES)

    coil_line = series[resist[0]] if resist else series[support[0]] if support else None
    if coil_line is not None:
        row.update(compression(close, coil_line))

    row.update(dip_quality(close, series[support[0]] if support else None, support[0] if support else "line", volume))
    row.update(test_quality(close, sma50, sma200, volume, signal))
    row.update(pack_of(row))

    if row["stage"] in ("TEST", "AVOID"):
        row["flag_verdict"] = row.get("test_verdict")
        row["flag_label"] = row.get("test_label")
        row["flag_why"] = row.get("test_why")
    elif row["stage"] in ("GAME ON", "RIZZ", "GROWTH"):
        row["flag_verdict"] = row.get("dip_verdict")
        row["flag_label"] = row.get("dip_label")
        row["flag_why"] = row.get("dip_why")
    else:
        row["flag_verdict"] = None
        row["flag_label"] = None
        row["flag_why"] = None

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
    """Write the scan next to the page instead of rewriting the page itself.

    The HTML used to carry ~800 KB of embedded data, so every scan dirtied a tracked
    source file. Now the page loads scan_data.js and the HTML never changes.
    """
    blob = dumps_json(payload, separators=(",", ":"))
    tmp = SCRIPT_PATH.with_suffix(".js.tmp")
    tmp.write_text(f"window.SCAN_DATA = {blob};\n", encoding="utf-8")
    tmp.replace(SCRIPT_PATH)


def save_last(payload: dict) -> None:
    global LAST_PAYLOAD
    LAST_PAYLOAD = payload
    DATA_PATH.write_text(dumps_json(payload), encoding="utf-8")
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
    log(f"market caps: {len(fresh)}/{len(tickers)}  reused={reused}  still_blank={len(tickers) - len(fresh)}")
    return fresh


def fetch_earnings(tickers: list[str]) -> dict[str, str]:
    """Next earnings date per name. Yahoo's calendar is approximate — confirm before acting.

    Only called for names that are actually actionable, so a scan does not slow to a crawl.
    """
    out: dict[str, str] = {}

    def one(ticker: str):
        try:
            cal = yf.Ticker(ticker).calendar
            raw = cal.get("Earnings Date") if hasattr(cal, "get") else None
            if not raw:
                return ticker, None
            first = raw[0] if isinstance(raw, (list, tuple)) else raw
            return ticker, first.isoformat() if hasattr(first, "isoformat") else str(first)
        except Exception:
            return ticker, None

    with ThreadPoolExecutor(max_workers=6) as pool:
        for fut in as_completed([pool.submit(one, t) for t in tickers]):
            ticker, when = fut.result()
            if when:
                out[ticker] = when
    log(f"earnings dates: {len(out)}/{len(tickers)}")
    return out


def add_earnings(all_rows: list[dict]) -> int:
    """Tag each actionable name with its next report date. A warning, never a filter."""
    actionable = {"GAME ON", "RIZZ", "GROWTH", "TEST"}
    need = [r["ticker"] for r in all_rows if r.get("stage") in actionable or r.get("pack")]
    dates = fetch_earnings(need) if need else {}
    today = datetime.now().date()
    soon = 0
    for row in all_rows:
        iso = dates.get(row["ticker"])
        row["earnings_date"] = iso
        row["earnings_in"] = None
        row["earnings_soon"] = False
        if not iso:
            continue
        try:
            when = datetime.fromisoformat(iso).date()
        except ValueError:
            continue
        days = (when - today).days
        row["earnings_in"] = days
        row["earnings_soon"] = 0 <= days <= EARNINGS_WARN
        if row["earnings_soon"]:
            soon += 1
    return soon


def scan(close_pct: float, extra_from_ui: list[str] | None = None) -> dict:
    names, added, skipped = merge_universe(extra_from_ui)
    tickers = names["ticker"].tolist()
    log(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {len(tickers)} names  added={added}  skipped_dupes={skipped}")
    closes, volumes = download_market(tickers)
    got_n = 0 if closes is None or closes.empty else len(closes.columns)
    need = max(1, int(0.85 * len(tickers)))
    if got_n < need:
        missing = [t for t in tickers if t not in (closes.columns if closes is not None else [])]
        raise RuntimeError(
            f"Yahoo only returned {got_n} of {len(tickers)} names (need ~{need}). "
            f"Rate limited — wait a few minutes and scan once. "
            f"Missing sample: {', '.join(missing[:12])}{'…' if len(missing) > 12 else ''}"
        )
    bench = download_benchmark()
    info = names.set_index("ticker")
    skipped_short: list[str] = []

    all_rows = []
    all_tests: list[dict] = []
    all_held: list[dict] = []
    poke_tot = {"approaches": 0, "failed": 0, "broke": 0, "unresolved": 0}
    for ticker in closes.columns:
        vol = None
        if volumes is not None and ticker in volumes.columns:
            vol = volumes[ticker]
        result = classify(closes[ticker], close_pct, vol, bench)
        if not result:
            skipped_short.append(ticker)
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

    reporting_soon = add_earnings(all_rows)

    total = max(len(all_rows), 1)
    breadth = {
        "above_200": round(100.0 * sum(1 for r in all_rows if r["price"] > r["sma200"]) / total),
        "above_50": round(100.0 * sum(1 for r in all_rows if r["price"] > r["sma50"]) / total),
        "bull": round(100.0 * sum(1 for r in all_rows if r["regime"] == "bull") / total),
        "buy_pack": sum(1 for r in all_rows if r.get("pack") == "BUY"),
        "watch_pack": sum(1 for r in all_rows if r.get("pack") == "WATCH"),
        "earnings_soon": reporting_soon,
    }
    breadth["mood"] = (
        "strong" if breadth["above_200"] >= 60
        else "mixed" if breadth["above_200"] >= 40
        else "weak"
    )
    last_bar = closes.index[-1] if len(closes.index) else datetime.now()

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "engine": ENGINE,
        "earnings_warn_days": EARNINGS_WARN,
        "universe": len(all_rows),
        "universe_requested": len(tickers),
        "close_pct": close_pct,
        "price_basis": "raw daily closes (split-adjusted, not dividend-adjusted) — matches IBKR",
        "market": market_state(last_bar),
        "breadth": breadth,
        "hits": hits,
        "all": all_rows,
        "added": added,
        "skipped_duplicates": skipped,
        "skipped_short_history": skipped_short,
        "download_missed": [t for t in tickers if t not in {r["ticker"] for r in all_rows} and t not in skipped_short],
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
                # Only pokes that actually resolved. Ones still running are not failures.
                "fail_rate": None if not (poke_tot["failed"] + poke_tot["broke"]) else round(
                    100.0 * poke_tot["failed"] / (poke_tot["failed"] + poke_tot["broke"]), 1
                ),
            },
        },
    }
    save_last(payload)
    log(f"{len(hits)} watch hits · {len(all_rows)} universe rows")
    return payload


def make_handler(close_pct: float):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log(f"[watch] {self.address_string()} {fmt % args}")

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            # Chrome blocks file:// → localhost POST without this private-network reply.
            self.send_header("Access-Control-Allow-Private-Network", "true")

        def _json(self, code: int, payload: dict):
            body = dumps_json(payload).encode("utf-8")
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
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _file(self, path: Path, mime: str):
            data = path.read_bytes() if path.exists() else b""
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html", "/sp500_sma_watch.html"):
                self._file(HTML_PATH, "text/html; charset=utf-8")
                return
            if path == "/scan_data.js":
                self._file(SCRIPT_PATH, "application/javascript; charset=utf-8")
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
    fix_stdio()
    try:
        install_protocol()
    except Exception as exc:
        log(f"Could not register HTML launcher: {exc}")

    load_last()
    url = f"http://127.0.0.1:{port}/"
    if autoscan:
        url += "?autoscan=1"
    if port_busy(port):
        log("Watcher already running — opening the page")
        if open_browser:
            webbrowser.open(url)
        return
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(close_pct))
    log(f"Watcher running at {url}")
    log("Keep this window open.")
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
        args.no_open = True
    if args.install_protocol:
        install_protocol()
        raise SystemExit(0)
    if args.once:
        scan(args.close_pct)
    else:
        serve(args.close_pct, args.port, not args.no_open, args.autoscan)
