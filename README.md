# FinSense

S&P 500 50/200 stair desk. Runs on your machine. Windows and Mac.

## What to clone

The whole repo. Not just the `.py`. You need these in the **same folder**:

- `sp500_sma_watch.py`
- `sp500_sma_watch.html`
- `requirements.txt`

Skip nothing. The `.bat` files are Windows. The `.sh` files are Mac / Linux.

## One-time install

**Windows** (PowerShell or cmd, in this folder):

```text
python -m pip install -r requirements.txt
```

**Mac / Linux** (Terminal, in this folder):

```bash
python3 -m pip install -r requirements.txt
```

Need Python 3. If `python3 --version` fails on a Mac: [python.org](https://www.python.org/downloads/) or `brew install python`.

## Start (keep the window open)

**Windows:** double-click `Start_SP500_Watch.bat`

**Mac:** in Terminal:

```bash
chmod +x Start_SP500_Watch.sh
./Start_SP500_Watch.sh
```

Or:

```bash
python3 sp500_sma_watch.py --autoscan
```

The page opens at http://127.0.0.1:8765/

Do not double-click the HTML in Finder or Explorer. Use the local page above.

## Desk vs full tape

The home page is **Desk**: names over $750B, BUY / WAIT / SELL. **Full tape** is the whole S&P screen.
