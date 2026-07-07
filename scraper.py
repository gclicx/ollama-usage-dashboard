#!/usr/bin/env python3
"""Scrape Ollama Cloud usage (session + weekly) and emit usage.json.

Ollama has no official usage API (see ollama/ollama#12532). The session/weekly
percentages and reset timers live only on the authenticated
https://ollama.com/settings page, so this script fetches that page with your
session cookie and parses the values out of the HTML.

The parsing logic mirrors the proven approach in
rabilrbl/hermes-ollama-cloud-usage (aria-label percentages, "Resets in ..."
timers, data-model/data-requests counts). It is intentionally regex-based and
will need updating if Ollama changes the page markup.

Auth: set the OLLAMA_CLOUD_COOKIE env var to the Cookie header value sent by
your browser to ollama.com (at minimum the __Secure-session cookie). See README.
"""

import json
import os
import re
import sys
import time
import urllib.request

SETTINGS_URL = "https://ollama.com/settings"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)

# Ollama Cloud usage windows (used to turn "Resets in ..." into a pace ratio).
SESSION_WINDOW_S = 5 * 3600
WEEKLY_WINDOW_S = 7 * 24 * 3600


def _parse_duration(text: str):
    """Parse a human duration like '3h 12m', '45m', '2d 4h 10m' into seconds.

    Returns None if nothing recognizable is found. Robust to extra whitespace,
    newlines, commas, and trailing/leading words.
    """
    if not text:
        return None
    cleaned = re.sub(r"\s+", " ", text.strip().replace("\n", " "))
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    total = 0
    found = False
    for num, unit in re.findall(r"(\d+)\s*([smhd])", cleaned, re.IGNORECASE):
        total += int(num) * units[unit.lower()]
        found = True
    return total if found else None


def _pace_for_window(percent: float, remaining_s, window_s):
    if percent is None or remaining_s is None or remaining_s <= 0:
        return None
    elapsed_s = max(0, window_s - remaining_s)
    if elapsed_s <= 0:
        return None
    elapsed_fraction = elapsed_s / window_s
    if elapsed_fraction <= 0:
        return None
    return (percent / 100.0) / elapsed_fraction


def _status_for_pace(pace):
    """Map a pace ratio to a status slug + the meter should be colored by it.

    Status palette (fixed): good / warning / serious / critical.
    """
    if pace is None:
        return None
    if pace < 0.85:
        return "good"
    if pace < 1.10:
        return "warning"
    if pace < 1.30:
        return "serious"
    return "critical"


def _status_for_percent(percent):
    """Fallback status from raw percent when pace can't be computed."""
    if percent is None:
        return None
    if percent < 75:
        return "good"
    if percent < 90:
        return "warning"
    if percent < 98:
        return "serious"
    return "critical"


def parse_settings(html: str) -> dict:
    """Parse the ollama.com/settings HTML into a usage dict.

    Returns keys: plan, session{percent, resets_in, resets_in_seconds, pace,
    status, models}, weekly{...}, updated_at. Raises RuntimeError if nothing
    could be parsed (likely an expired cookie or a markup change).
    """
    plan = "Unknown"
    plan_match = re.search(
        r'<span[^>]*class="[^"]*(?:inline-flex|rounded|badge)[^"]*"[^>]*>'
        r"\s*(Pro|Max|Free)\s*</span>",
        html,
        re.IGNORECASE,
    )
    if plan_match:
        plan = plan_match.group(1).strip()

    result = {
        "plan": plan,
        "session": {},
        "weekly": {},
    }

    # Percentages — prefer the aria-label form (most stable), then visible text.
    def _find_percent(label_word):
        m = re.search(
            rf'aria-label="{label_word}\s+usage\s+([\d.]+)%\s+used"',
            html,
            re.IGNORECASE,
        )
        if m:
            return float(m.group(1))
        m = re.search(
            rf"{label_word}\s+usage[^<]*>[\s\S]*?([\d.]+)%\s*used",
            html,
            re.IGNORECASE,
        )
        return float(m.group(1)) if m else None

    result["session"]["percent"] = _find_percent("Session")
    result["weekly"]["percent"] = _find_percent("Weekly")

    # Reset timers — "Resets in ...". The first belongs to session, the second
    # to weekly (matches the page order).
    resets = re.findall(r"Resets?\s+in\s+([^<]+)", html)
    if len(resets) >= 1:
        result["session"]["resets_in"] = resets[0].strip()
    if len(resets) >= 2:
        result["weekly"]["resets_in"] = resets[1].strip()

    # Per-model request counts: data-model="..." data-requests="N". Session
    # models appear before the "Weekly usage" heading, weekly models after.
    weekly_start = html.find("Weekly usage")
    session_models, weekly_models = [], []
    for m in re.finditer(r'data-model="([^"]+)"\s+data-requests="(\d+)"', html):
        model, requests = m.group(1), int(m.group(2))
        bucket = weekly_models if (
            weekly_start != -1 and m.start() > weekly_start
        ) else session_models
        bucket.append({"model": model, "requests": requests})
    if session_models:
        result["session"]["models"] = session_models
    if weekly_models:
        result["weekly"]["models"] = weekly_models

    # Enrich each window with remaining-seconds, pace, and a status slug.
    for key, window in (("session", SESSION_WINDOW_S), ("weekly", WEEKLY_WINDOW_S)):
        win = result[key]
        pct = win.get("percent")
        remaining = _parse_duration(win.get("resets_in"))
        if remaining is not None:
            win["resets_in_seconds"] = remaining
        pace = _pace_for_window(pct, remaining, window)
        win["pace"] = pace
        win["status"] = _status_for_pace(pace) or _status_for_percent(pct)

    if not result["session"] and not result["weekly"]:
        raise RuntimeError(
            "Could not parse usage data from ollama.com/settings. The page "
            "layout may have changed, or the cookie is invalid/expired."
        )

    return result


def fetch_settings(cookie: str) -> str:
    req = urllib.request.Request(
        SETTINGS_URL,
        headers={
            "Cookie": cookie,
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "usage.json"
    cookie = os.getenv("OLLAMA_CLOUD_COOKIE", "").strip()
    if not cookie:
        print("OLLAMA_CLOUD_COOKIE env var not set", file=sys.stderr)
        sys.exit(1)

    try:
        data = parse_settings(fetch_settings(cookie))
    except Exception as exc:  # surface a structured error to the page
        data = {
            "error": str(exc),
            "updated_at": int(time.time()),
        }
        with open(out_path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"Scrape failed: {exc}", file=sys.stderr)
        sys.exit(0)  # don't fail the workflow — keep the last good page up

    data["updated_at"] = int(time.time())
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Wrote {out_path}: session "
          f"{data.get('session', {}).get('percent')}% / weekly "
          f"{data.get('weekly', {}).get('percent')}%")


if __name__ == "__main__":
    main()