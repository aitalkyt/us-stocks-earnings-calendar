"""
Builds earnings.json: the most anticipated US earnings reports,
using the free yfinance library (Yahoo Finance data, no API key).

Run by GitHub Actions on a schedule (see .github/workflows/earnings.yml).
"""
import json
import math
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

ET = ZoneInfo("America/New_York")
OUT = "earnings.json"

DAYS_BACK = 7          # keep last week's reports so results and surprises stay visible
DAYS_AHEAD = 14        # how far ahead to look
PER_DAY = 15           # how many "most anticipated" reports to keep per day (largest companies)
MIN_CAP = 2e9          # ignore companies smaller than this


def num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def fetch_window(cal, start, end, market_cap, most_active):
    frames, offset = [], 0
    while True:
        df = cal.get_earnings_calendar(
            market_cap=market_cap, filter_most_active=most_active,
            start=start, end=end, limit=100, offset=offset, force=True,
        )
        if df is None or df.empty:
            break
        frames.append(df)
        # Yahoo does not allow paging the "most active" filter
        if most_active or len(df) < 100:
            break
        offset += 100
        time.sleep(1)
    return frames


def collect(start, end):
    """Return {(symbol, date): row dict} for every company above MIN_CAP."""
    cal = yf.Calendars(start=start, end=end)
    found = {}
    day = start
    while day <= end:
        stop = day + timedelta(days=1)
        s, e = day.strftime("%Y-%m-%d"), stop.strftime("%Y-%m-%d")
        for cap, active in ((MIN_CAP, False),):
            try:
                frames = fetch_window(cal, s, e, cap, active)
            except Exception as err:  # keep going if one window fails
                print(f"Window {s}..{e} failed: {err}")
                continue
            for df in frames:
                for sym, r in df.iterrows():
                    ts = pd.Timestamp(r.get("Event Start Date"))
                    if pd.isna(ts):
                        continue
                    if ts.tzinfo is None:
                        ts = ts.tz_localize("UTC")
                    ts = ts.tz_convert(ET)
                    key = (str(sym), ts.strftime("%Y-%m-%d"))
                    item = found.get(key) or {
                        "symbol": str(sym),
                        "name": str(r.get("Company") or sym),
                        "cap": num(r.get("Marketcap")),
                        "date": key[1],
                        "time": ts.isoformat(),
                        "timing": str(r.get("Timing") or "").upper(),
                        "event": str(r.get("Event Name") or ""),
                        "epsExpected": num(r.get("EPS Estimate")),
                        "epsReported": num(r.get("Reported EPS")),
                    }
                    found[key] = item
            time.sleep(1)
        day = stop
    return found


def looks_foreign(symbol):
    # OTC tickers for foreign shares end in F or Y (e.g. SSNLF, TOELY)
    return len(symbol) == 5 and symbol[-1] in "FY" and "-" not in symbol


def is_us_company(symbol, companies):
    """True if Yahoo lists the company as based in the United States (cached)."""
    if looks_foreign(symbol):
        return False
    if symbol in companies:
        return companies[symbol]
    us = None
    try:
        info = yf.Ticker(symbol).info or {}
        country = info.get("country")
        if country:
            us = country == "United States"
    except Exception as err:
        print(f"{symbol} info failed: {err}")
    time.sleep(0.5)
    if us is None:
        return False  # unknown this run, try again next run
    companies[symbol] = us
    return us


def session(item):
    t = item["timing"]
    if t == "BMO":
        return "bmo"
    if t == "AMC":
        return "amc"
    ts = datetime.fromisoformat(item["time"])
    if ts.hour or ts.minute:
        if (ts.hour, ts.minute) < (9, 30):
            return "bmo"
        if ts.hour >= 16:
            return "amc"
        return "dmh"
    return "tbd"


def period(event):
    # "Q3 2026 Earnings Release" -> "Q3 2026"
    parts = event.split()
    if len(parts) >= 2 and parts[0].upper().startswith("Q") and parts[1].isdigit():
        return parts[0].upper() + " " + parts[1]
    return ""


def revenue(symbol, date_str, reported, previous):
    """Expected revenue (consensus) and reported revenue for one report."""
    exp = previous.get("revExpected")
    rep = previous.get("revReported")
    t = yf.Ticker(symbol)
    if exp is None and not reported:
        try:
            est = t.revenue_estimate
            if est is not None and "0q" in est.index:
                exp = num(est.loc["0q", "avg"])
        except Exception as err:
            print(f"{symbol} revenue estimate failed: {err}")
    if rep is None and reported:
        try:
            inc = t.quarterly_income_stmt
            if inc is not None and "Total Revenue" in inc.index:
                event = pd.Timestamp(date_str)
                for col in inc.columns:
                    end = pd.Timestamp(col)
                    if event - timedelta(days=75) < end <= event:
                        rep = num(inc.loc["Total Revenue", col])
                        break
        except Exception as err:
            print(f"{symbol} income statement failed: {err}")
    return exp, rep


FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "").strip()


def finnhub_day(date_str):
    """Revenue estimate and actual for every report on one past day (needs FINNHUB_KEY).

    Used only by this script on GitHub, a few calls per run, so website
    visitors never touch Finnhub or its limits."""
    if not FINNHUB_KEY:
        return {}
    try:
        res = requests.get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={"from": date_str, "to": date_str, "token": FINNHUB_KEY},
            timeout=30,
        )
        res.raise_for_status()
        rows = res.json().get("earningsCalendar") or []
    except Exception as err:
        print(f"Finnhub {date_str} failed: {err}")
        return {}
    out = {}
    for r in rows:
        sym = r.get("symbol")
        if sym:
            out[sym.replace(".", "-")] = {
                "revExpected": num(r.get("revenueEstimate")),
                "revReported": num(r.get("revenueActual")),
            }
    time.sleep(1.1)  # stay well under Finnhub's 60 calls per minute
    return out


def find_previous(previous, symbol, date_str):
    """Saved values for this report, even if Yahoo moved the date by a few days."""
    if (symbol, date_str) in previous:
        return previous[(symbol, date_str)]
    day = datetime.strptime(date_str, "%Y-%m-%d")
    for shift in (1, -1, 2, -2, 3, -3):
        other = (day + timedelta(days=shift)).strftime("%Y-%m-%d")
        if (symbol, other) in previous:
            return previous[(symbol, other)]
    return {}


def main():
    today = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    start, end = today - timedelta(days=DAYS_BACK), today + timedelta(days=DAYS_AHEAD)

    previous, companies = {}, {}
    if os.path.exists(OUT):
        try:
            with open(OUT) as f:
                old = json.load(f)
                companies = old.get("companies", {})
                for items in old.get("days", {}).values():
                    for it in items:
                        previous[(it["symbol"], it["date"])] = it
                for it in old.get("saved", []):
                    previous.setdefault((it["symbol"], it["date"]), it)
        except Exception:
            pass

    found = collect(start, end)
    by_day = {}
    for item in found.values():
        by_day.setdefault(item["date"], []).append(item)

    today_str = today.strftime("%Y-%m-%d")
    days = {}
    for date_str in sorted(by_day):
        backfill = finnhub_day(date_str) if date_str <= today_str else {}
        ranked, seen_names = [], set()
        for it in sorted(by_day[date_str], key=lambda it: it["cap"] or 0, reverse=True):
            name_key = it["name"].lower()
            if name_key in seen_names:  # skip duplicate share classes (GOOG / GOOGL)
                continue
            if not is_us_company(it["symbol"], companies):
                continue
            seen_names.add(name_key)
            ranked.append(it)
            if len(ranked) >= PER_DAY:
                break
        out = []
        for it in ranked:
            prev = find_previous(previous, it["symbol"], date_str)
            reported = it["epsReported"] is not None
            rev_exp, rev_rep = revenue(it["symbol"], date_str, reported, prev)
            fh = backfill.get(it["symbol"], {})
            if rev_exp is None:
                rev_exp = fh.get("revExpected")  # estimate Yahoo already deleted
            if rev_rep is None and reported:
                rev_rep = fh.get("revReported")  # usually available sooner than Yahoo
            out.append({
                "symbol": it["symbol"],
                "name": it["name"],
                                "date": date_str,
                "time": it["time"],
                "session": session(it),
                "period": period(it["event"]),
                "epsExpected": it["epsExpected"] if it["epsExpected"] is not None else prev.get("epsExpected"),
                "epsReported": it["epsReported"],
                "revExpected": rev_exp,
                "revReported": rev_rep,
            })
            time.sleep(0.5)
        days[date_str] = out

    with open(OUT, "w") as f:
        # remember expected revenue for reports that fell out of the top list, so it is not lost
        shown = {(it["symbol"], it["date"]) for items in days.values() for it in items}
        cutoff = (start - timedelta(days=3)).strftime("%Y-%m-%d")
        saved = [
            {"symbol": k[0], "date": k[1], "revExpected": v.get("revExpected"), "epsExpected": v.get("epsExpected")}
            for k, v in previous.items()
            if k not in shown and v.get("revExpected") is not None and k[1] >= cutoff
        ]
        json.dump({"updated": datetime.now(ET).isoformat(), "days": days, "companies": companies, "saved": saved}, f, indent=1)
    print(f"Wrote {sum(len(v) for v in days.values())} reports across {len(days)} days")


if __name__ == "__main__":
    main()
