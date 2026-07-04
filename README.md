# League HQ

A phone-first command center for a private basketball sim league. One static page,
rebuilt automatically from live league data — free to host on GitHub Pages.

## What it does

| Tab | Purpose |
|---|---|
| **Home** | Standings (with net ratings), your cap situation, injuries, transaction feed |
| **Players** | Every rostered player + FAs, searchable, sortable by KV / BPM / OBPM / DBPM / PER / value. Tap for full card |
| **Teams** | Any roster with multi-year payroll vs the $70M cap / $95M hard cap, draft picks |
| **Trade** | Pick players on both sides → BPM in−out, salary deltas, post-trade cap + roster checks, copyable summary |
| **FA** | UFA / RFA / next class with ratings-projected BPM & PER, star shortlist, offer builder (MLE, vet-min, ≤7.5% non-compounded raises) |
| **Draft** | Draft-sheet grades + your own rank & notes (stored on your phone only) |
| **Depth** | Depth chart builder with rule checks (10 actives, 2 per position, ≤40 min, 240 total) → copy in exact forum format |

## The metrics

- **sBPM / sOBPM / sDBPM** — Box Plus/Minus 2.0 (basketball-reference methodology)
  computed from actual season stats per 100 possessions, team-adjusted so every
  roster sums to the team's lead-adjusted net rating. 0 = league average,
  +2 good starter, +4 all-star, +6 all-league.
- **pBPM / pOBPM / pDBPM / pPER** — same formulas applied to a stat line *projected
  from the 1-99 ratings*. The rating→stat regressions are re-fit on the whole
  league at every build, so they track the sim engine automatically. This is what
  makes FA/rookie evaluation possible before they play a minute.
- **PER** — Hollinger PER, league-scaled to 15.0.
- **KV (0-100)** — headline percentile: 70% BPM + 30% PER, where each blends
  stats-based and ratings-projected values by minutes played.

## Data sources (all fetched at build time)

- the league site: Standings, all team pages (ratings / stats / contracts / picks / team totals),
  Injuries, Transactions, Finances, FA preview
- Google Sheets: position eligibility, draft grades, RFA list, UFA list

## Deploy (free, ~5 minutes)

1. Create a GitHub account if needed, then a **public** repo (any name).
2. Push this folder to it.
3. Repo **Settings → Pages** → Source: *Deploy from a branch* → Branch: `main`, folder `/docs` → Save.
4. Repo **Settings → Actions → General** → Workflow permissions → *Read and write permissions* → Save.
5. Your app is at `https://<username>.github.io/<repo>/` — open it on your phone
   and "Add to Home Screen".

The **Rebuild the league app** workflow refreshes data every 6 hours. To refresh on
demand from your phone: repo → Actions → Rebuild the league app → Run workflow.

## Run locally

```
python kvbl_app/build.py          # scrape + compute + write docs/index.html
python kvbl_app/build.py --cached # re-render UI without re-scraping
python -m http.server 8377 --directory docs   # then open http://localhost:8377
```

No dependencies — pure Python 3 standard library.

## Notes

- Personal data (depth chart, draft notes, shortlist, saved trade) lives in your
  browser's localStorage — nothing private is published to the page.
- Your team defaults to Kings; change it via localStorage key `kvbl_myteam` if needed.
- `docs/data.json` is the raw scraped snapshot, handy for debugging.
