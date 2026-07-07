# Ollama Cloud Usage — public, no-sign-in dashboard

A static site that publishes **your** Ollama Cloud usage (session + weekly %,
reset countdowns, and per-model request counts) to a public URL that anyone can
view **without signing in**.

Ollama has no official usage API ([ollama/ollama#12532](https://github.com/ollama/ollama/issues/12532)),
so the session/weekly numbers live only on the authenticated
`ollama.com/settings` page. This project scrapes that page with your session
cookie inside a scheduled GitHub Action, commits the result to `usage.json`, and
GitHub Pages renders it. Viewers never see your cookie — only the numbers.

```
  ┌──────────── GitHub Actions (cron, every 17 min) ────────────┐
  │  scraper.py ──cookie──▶ ollama.com/settings ──▶ usage.json   │
  │            (OLLAMA_CLOUD_COOKIE is a repo secret)           │
  └──────────────────────────┬──────────────────────────────────┘
                             │ committed to the repo
  ┌──────────────────────────▼──────────────────────────────────┐
  │  GitHub Pages serves index.html + usage.json  → public URL  │
  └────────────────────────────────────────────────────────────┘
```

> ⚠️ **What's public:** the committed `usage.json` (your plan tier, usage
> percentages, model names, request counts) and the repo's commit history of
> those values. **What stays secret:** your session cookie (stored as an Actions
> secret, never written to the repo). If even the usage numbers being public
> bothers you, make the repo **private** — GitHub Pages from a private repo is
> available on paid plans, or self-host the page instead.

## Setup (~3 minutes)

1. **Fork / clone** this repo to your GitHub account.
2. **Get your session cookie:**
   - Open <https://ollama.com/settings> in your browser (be signed in).
   - Open DevTools → **Application** → **Cookies** → `https://ollama.com`.
   - Find `__Secure-session` and copy its **Value**.
   - Set the secret as `OLLAMA_CLOUD_COOKIE=__Secure-session=<that value>`
     (you can include other cookies too, semicolon-separated, exactly as the
     browser sends the `Cookie:` header).
3. **Add the repo secret:** repo → **Settings** → **Secrets and variables** →
   **Actions** → **New repository secret**. Name: `OLLAMA_CLOUD_COOKIE`,
   Value: the string from step 2.
4. **Enable GitHub Pages:** **Settings** → **Pages** → Source: **Deploy from a
   branch**, Branch: `main` / root, **Save**. Your site will appear at
   `https://<your-handle>.github.io/<repo-name>/`.
5. **Run it once:** **Actions** → **Scrape Ollama Cloud usage** → **Run workflow**
   → `main`. Watch the run; when it commits `usage.json`, refresh your Pages URL.

The scheduled cron takes over from there (every ~17 minutes).

## Files

| Path | Role |
|------|------|
| `scraper.py` | Fetches `ollama.com/settings` with the cookie, parses plan + session/weekly %, reset timers, model counts; computes a *pace* ratio and a status; writes `usage.json`. |
| `index.html` | Static page. Fetches `usage.json`, renders two stat-tile meters + model tables. Light/dark via `prefers-color-scheme`. No build step, no JS framework. |
| `usage.json` | The committed snapshot the page renders. |
| `.github/workflows/scrape.yml` | Cron + manual dispatch; runs the scraper and commits `usage.json` when it changes. |

## How the meter color works

The fill color is a **status** (not a series color): it reflects whether your
usage is on pace for the elapsed fraction of the window.

- `pace = used% / (elapsed time / window)` — `1.0×` means you're spending
  exactly in line with a constant burn rate.
- `< 0.85×` good (blue/green), `< 1.10×` warning, `< 1.30×` serious, else
  critical. The status label is always shown next to the swatch, so meaning is
  never color-alone.

If the reset countdown can't be parsed, it falls back to coloring by raw
percent thresholds.

## Maintenance & caveats

- **Brittle by design.** This scrapes HTML. If Ollama changes
  `ollama.com/settings`, parsing will break (the scraper writes a structured
  `error` into `usage.json` instead of crashing the page). Update the regexes
  in `scraper.py` (`parse_settings`) when that happens. Track
  [ollama/ollama#12532](https://github.com/ollama/ollama/issues/12532) for an
  official endpoint — when one ships, swap `fetch_settings` for a real API
  call and drop the cookie.
- **Cookie expiry.** Session cookies expire. If the dashboard goes stale or
  shows an error, re-grab `__Secure-session` and update the secret.
- **No third-party deps.** `scraper.py` uses only the standard library, so the
  workflow needs no `pip install`.

## Local testing

```bash
export OLLAMA_CLOUD_COOKIE='__Secure-session=...'
python3 scraper.py usage.json      # writes usage.json
python3 -m http.server 8000        # open http://localhost:8000/
```

## Credits

The scraping logic is adapted from
[`rabilrbl/hermes-ollama-cloud-usage`](https://github.com/rabilrbl/hermes-ollama-cloud-usage)
(aria-label percentages, "Resets in …" timers, `data-model`/`data-requests`
counts). The visualization follows a design-system-agnostic data-viz method.