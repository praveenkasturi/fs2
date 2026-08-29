"""S&P 500 50/200 SMA watcher.

You do not need to copy files around. In Cursor we edit this project together.
On a Mac, double-click Start_SP500_Watch.command
On Windows, double-click Start_SP500_Watch.bat
The first run installs packages by itself.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import webbrowser
import warnings
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
HTML_PATH = HERE / "sp500_sma_watch.html"
DATA_PATH = HERE / "sp500_sma_watch_data.json"
REQS_PATH = HERE / "requirements.txt"
VENV_DIR = HERE / ".venv"


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _have_packages() -> bool:
    try:
        import lxml  # noqa: F401
        import pandas  # noqa: F401
        import requests  # noqa: F401
        import yfinance  # noqa: F401
        return True
    except ImportError:
        return False


def _install_into(python_exe: str | Path) -> None:
    subprocess.check_call([str(python_exe), "-m", "pip", "install", "-q", "-r", str(REQS_PATH)])


def ensure_packages() -> None:
    """First run: make a local .venv if possible, otherwise install into this Python."""
    if _have_packages():
        return
    if os.environ.get("FS2_SETUP_DONE") == "1":
        print("Packages are still missing. Install Python 3 from https://www.python.org/downloads/")
        raise SystemExit(1)
    py = _venv_python()
    try:
        if not py.exists():
            import shutil
            import venv

            print("First-time setup — creating a local environment (about a minute)...")
            if VENV_DIR.exists():
                shutil.rmtree(VENV_DIR, ignore_errors=True)
            venv.EnvBuilder(with_pip=True).create(VENV_DIR)
        if py.exists():
            print("First-time setup — installing packages...")
            _install_into(py)
            if Path(sys.executable).resolve() != py.resolve():
                env = os.environ.copy()
                env["FS2_SETUP_DONE"] = "1"
                os.execve(str(py), [str(py), *sys.argv], env)
            return
    except (Exception, SystemExit):
        pass
    print("First-time setup — installing packages into this Python...")
    try:
        _install_into(sys.executable)
    except Exception as exc:
        print("Could not finish setup. Install Python 3 from https://www.python.org/downloads/")
        print(exc)
        raise SystemExit(1)
    env = os.environ.copy()
    env["FS2_SETUP_DONE"] = "1"
    os.execve(sys.executable, [sys.executable, *sys.argv], env)


ensure_packages()

import pandas as pd
import requests
import yfinance as yf

warnings.filterwarnings("ignore")

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
    cmd = f'"{sys.executable}" "{script}" --autoscan "%1"'
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


def download_closes(tickers: list[str]) -> pd.DataFrame:
    data = yf.download(
        tickers,
        period="2y",
        interval="1d",
        auto_adjust=True,
        threads=True,
        progress=True,
    )
    if isinstance(data.columns, pd.MultiIndex):
        level0 = set(data.columns.get_level_values(0))
        if "Close" in level0:
            closes = data["Close"]
        else:
            closes = pd.concat(
                {t: data[t]["Close"] for t in data.columns.get_level_values(0).unique()},
                axis=1,
            )
    else:
        closes = data["Close"].to_frame() if "Close" in data.columns else data
    closes.columns = [normalize_ticker(c) for c in closes.columns]
    return closes.dropna(axis=1, how="all")


def rsi_wilder(close: pd.Series, n: int = 14) -> float | None:
    delta = close.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    roll_up = up.ewm(alpha=1 / n, adjust=False).mean()
    roll_down = down.ewm(alpha=1 / n, adjust=False).mean()
    rs = roll_up / roll_down
    value = 100 - (100 / (1 + rs))
    last = value.iloc[-1]
    return None if pd.isna(last) else float(last)


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


def reclaim_events(close: pd.Series) -> list[dict]:
    """Close back above the 50 after at least 3 days under it, while 50 is still below 200."""
    close = close.dropna()
    sma50 = close.rolling(50).mean()
    sma200 = close.rolling(200).mean()
    under = close < sma50
    cross_up = (close > sma50) & (close.shift(1) <= sma50.shift(1))
    under_streak = under.rolling(3).sum() >= 3
    valid = cross_up & under_streak.shift(1).fillna(False) & (sma50 < sma200) & sma50.notna() & sma200.notna()
    idxs = [i for i, flag in enumerate(valid.tolist()) if flag]
    return _forward_from(close, sma50, idxs)


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
            "First dip into ~2% of the 50, only in an uptrend, coming from more than 2% above the 50. "
            "Then +5% before a close 2% under the 50, max 60 sessions."
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


def classify(close: pd.Series, close_pct: float) -> dict | None:
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
    reclaim_tests = reclaim_events(close)
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
    row["_tests"] = tests
    row["_reclaim_tests"] = reclaim_tests
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


def fetch_market_caps(tickers: list[str]) -> dict[str, float]:
    caps: dict[str, float] = {}

    def one(ticker: str):
        try:
            info = yf.Ticker(ticker).fast_info
            cap = info.get("marketCap") if hasattr(info, "get") else getattr(info, "market_cap", None)
            if cap:
                return ticker, float(cap)
        except Exception:
            pass
        return ticker, None

    with ThreadPoolExecutor(max_workers=16) as pool:
        for fut in as_completed([pool.submit(one, t) for t in tickers]):
            ticker, cap = fut.result()
            if cap:
                caps[ticker] = cap
    print(f"market caps: {len(caps)}/{len(tickers)}")
    return caps


def scan(close_pct: float, extra_from_ui: list[str] | None = None) -> dict:
    names, added, skipped = merge_universe(extra_from_ui)
    tickers = names["ticker"].tolist()
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {len(tickers)} names  added={added}  skipped_dupes={skipped}")
    closes = download_closes(tickers)
    info = names.set_index("ticker")

    all_rows = []
    all_tests: list[dict] = []
    all_reclaim: list[dict] = []
    for ticker in closes.columns:
        result = classify(closes[ticker], close_pct)
        if not result:
            continue
        meta = info.loc[ticker] if ticker in info.index else None
        result["ticker"] = ticker
        result["name"] = "" if meta is None else str(meta["name"])
        result["sector"] = "" if meta is None else str(meta["sector"])
        all_tests.extend(result.pop("_tests", []))
        all_reclaim.extend(result.pop("_reclaim_tests", []))
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
            **summarize_tests(all_tests),
            "reclaim": summarize_tests(
                all_reclaim,
                rule=(
                    "Close back above the 50 after at least 3 days under it, while the 50 is still below the 200. "
                    "Then +5% before a close 2% under the 50, max 60 sessions. Watch, not a buy — this tests the reclaim idea."
                ),
                stop="daily close 2% under the 50",
            ),
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
    if os.name == "nt":
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
        args.autoscan = True
    if args.install_protocol:
        install_protocol()
        raise SystemExit(0)
    if args.once:
        scan(args.close_pct)
    else:
        serve(args.close_pct, args.port, not args.no_open, args.autoscan)
