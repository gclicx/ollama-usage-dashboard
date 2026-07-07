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

# How many history points to retain. At the default ~17-min cron cadence that's
# ~84 points/day, so 1440 ≈ 17 days of usage-over-time history.
HISTORY_MAX = int(os.getenv("HISTORY_MAX", "1440"))


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
    # The plan badge sits right after the "Cloud usage" heading. Searching a
    # short window after that heading is far more robust than matching a
    # specific badge class (which changes with Tailwind utility churn).
    cu = html.find("Cloud usage")
    if cu != -1:
        pm = re.search(r"\b(Free|Pro|Max|Team)\b", html[cu:cu + 600])
        if pm:
            plan = pm.group(1)

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
    # to weekly (matches the page order). Strip a trailing period for display.
    def _clean_reset(raw):
        return raw.strip().rstrip(".").strip()

    resets = re.findall(r"Resets?\s+in\s+([^<]+)", html)
    if len(resets) >= 1:
        result["session"]["resets_in"] = _clean_reset(resets[0])
    if len(resets) >= 2:
        result["weekly"]["resets_in"] = _clean_reset(resets[1])

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

    got_data = any(
        result[k].get("percent") is not None or result[k].get("models")
        for k in ("session", "weekly")
    )
    if not got_data:
        raise ParseError(
            "No session/weekly usage found on ollama.com/settings. The page "
            "layout may have changed, or the cookie redirected to sign-in."
        )

    return result


class ParseError(RuntimeError):
    """Raised when the page was fetched but no usage values could be extracted."""


def _redact(text: str) -> str:
    """Strip obviously personal/opaque strings before exposing HTML snippets."""
    text = re.sub(r"[\w.+-]+@[\w.-]+\.\w{2,}", "[email]", text)
    # long base64-ish blobs (JWTs, session values, opaque tokens)
    text = re.sub(r"[A-Za-z0-9_-]{40,}", "[token]", text)
    return text


def debug_html(html: str, width: int = 220, max_hits: int = 4) -> str:
    """Return redacted snippets around usage-related markers, for diagnosing
    markup changes. Safe to commit (emails/tokens redacted) and to paste.

    Falls back to a redacted slice of the body if none of the expected markers
    are present — that happens when the fetch landed on a Cloudflare challenge,
    a sign-in redirect, or a client-rendered SPA shell, and the slice tells us
    which.
    """
    markers = [
        "Session usage", "Weekly usage", "Cloud usage",
        "aria-label", "data-model", "data-requests", "data-time",
        "Resets in", "% used", "usage", "session", "weekly",
        "percent", "reset", "limit", "quota", "plan",
        "__NEXT_DATA__", "self.__next_f", "application/json",
    ]
    out, total = [], 0
    for marker in markers:
        start, hits = 0, 0
        while total < 24:
            idx = html.lower().find(marker.lower(), start)
            if idx < 0 or hits >= max_hits:
                break
            a = max(0, idx - width // 2)
            b = min(len(html), idx + width // 2)
            out.append(f"…{_redact(html[a:b].replace(chr(10), ' '))}…")
            start = idx + 1
            hits += 1
            total += 1
    if out:
        return "\n".join(out)

    # No markers at all — show a redacted slice of the body so we can tell
    # whether this is a Cloudflare/Sign-in/SPA page.
    body = re.sub(r"<script[\s\S]*?</script>", "[script]", html)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    if not body:
        return "(empty body — likely a Cloudflare challenge or empty shell)"
    return "NO MARKERS — body slice:\n" + _redact(body[:2500])


def update_history(path: str, data: dict) -> None:
    """Append this snapshot to history.json and cap to HISTORY_MAX newest points.

    Each point is intentionally minimal: {t, s, w} — unix timestamp plus the
    session and weekly percentages. Reset timers / model counts / pace are not
    stored per point (they're only meaningful for the current snapshot in
    usage.json), which keeps history.json small.
    """
    s = data.get("session", {}).get("percent")
    w = data.get("weekly", {}).get("percent")
    if s is None and w is None:
        return  # nothing useful to record

    history = []
    try:
        with open(path) as f:
            history = json.load(f)
            if not isinstance(history, list):
                history = []
    except (FileNotFoundError, ValueError):
        history = []

    history.append({"t": data["updated_at"], "s": s, "w": w})
    if len(history) > HISTORY_MAX:
        history = history[-HISTORY_MAX:]

    with open(path, "w") as f:
        json.dump(history, f, separators=(",", ":"))
    print(f"Wrote {path}: {len(history)} points (cap {HISTORY_MAX})")


def normalize_cookie(raw: str) -> str:
    """Coerce a cookie value into a valid HTTP ``Cookie:`` header.

    Accepts either:
      * a ready-made header string, e.g. ``name=value; name2=value2``
      * a Netscape ``cookies.txt`` file (the format browser cookie-export
        extensions emit, with a ``# Netscape HTTP Cookie File`` header and
        tab-separated rows) — this is the common gotcha: people paste the whole
        exported file into the secret, which then fails as an HTTP header.

    For the Netscape form, every non-comment row's name/value is turned into a
    ``name=value`` pair. Lines prefixed with ``#HttpOnly_`` are real cookie rows
    (the ``#`` is a marker, not a comment) and are kept.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""

    looks_netscape = (
        "# Netscape" in raw
        or raw.startswith("#HttpOnly_")
        or "\tTRUE\t" in raw
        or "\tFALSE\t" in raw
    )
    if not looks_netscape:
        # Bare session value (no '=', no newlines) — i.e. someone pasted just
        # the copied cookie value without the "name=" prefix. Wrap it so the
        # header is well-formed. A real Cookie header always contains '='.
        if "=" not in raw and "\n" not in raw:
            return f"__Secure-session={raw}"
        return raw  # already a header string

    pairs = []
    for line in raw.splitlines():
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        name, value = parts[5], parts[6]
        if name:
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def fetch_settings(cookie: str) -> str:
    cookie = normalize_cookie(cookie)
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
    history_path = sys.argv[2] if len(sys.argv) > 2 else "history.json"
    cookie = os.getenv("OLLAMA_CLOUD_COOKIE", "").strip()
    if not cookie:
        print("OLLAMA_CLOUD_COOKIE env var not set", file=sys.stderr)
        sys.exit(1)

    header = normalize_cookie(cookie)
    if not header or "__Secure-session" not in header:
        print(
            "OLLAMA_CLOUD_COOKIE didn't yield a usable Cookie header "
            "(no __Secure-session found). If you pasted a cookies.txt export, "
            "make sure it includes the __Secure-session row.",
            file=sys.stderr,
        )

    # 1) Fetch the page. A failure here is usually a bad/expired cookie or a
    #    network/Cloudflare block — keep the last good usage.json up rather than
    #    blanking the dashboard.
    try:
        html = fetch_settings(cookie)
    except Exception as exc:
        data = {"error": f"fetch failed: {exc}", "updated_at": int(time.time())}
        with open(out_path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"Fetch failed: {exc}", file=sys.stderr)
        sys.exit(0)

    # 2) Parse. A failure here means the cookie worked but the markup doesn't
    #    match — capture redacted snippets so we can fix the regex from the
    #    committed usage.json (or stderr with OLLAMA_DEBUG=1).
    try:
        data = parse_settings(html)
    except ParseError as exc:
        data = {
            "error": str(exc),
            "debug": debug_html(html),
            "updated_at": int(time.time()),
        }
        with open(out_path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"Parse failed: {exc}\n{debug_html(html)}", file=sys.stderr)
        sys.exit(0)

    if os.getenv("OLLAMA_DEBUG"):
        print(debug_html(html), file=sys.stderr)

    data["updated_at"] = int(time.time())
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Wrote {out_path}: session "
          f"{data.get('session', {}).get('percent')}% / weekly "
          f"{data.get('weekly', {}).get('percent')}%")

    update_history(history_path, data)


if __name__ == "__main__":
    main()