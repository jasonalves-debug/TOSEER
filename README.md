# Toronto Live Emergency Map — GitHub Actions + Pages (free, self-updating)

One repo = the whole live site. GitHub runs the fetch script every 15 min
and hosts the map itself. No server, no card on file, $0/month.

## 1. Push this to a new GitHub repo

```
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

## 2. Turn on GitHub Pages

Repo → **Settings → Pages**:
- Source: **Deploy from a branch**
- Branch: **main**, folder: **/docs**
- Save

Site will be at `https://<your-username>.github.io/<your-repo>/` within a
minute or two of saving.

## 3. Give the workflow permission to push back to the repo

**Do this before running the workflow, not after it fails.** New repos
default their Actions token to read-only, which means the workflow's
`git push` step will fail with a permissions error the first time it
tries to save data — not an obvious failure to debug from the log alone.

Repo → **Settings → Actions → General** → scroll to **Workflow
permissions** → select **Read and write permissions** → Save.

## 4. Run the workflow once manually

Repo → **Actions** tab → "Update emergency data" → **Run workflow**.

Click into the run, open the "Fetch TPS + TFS..." step, read the log
directly — it prints exactly what happened (incident counts, field
mappings, any anomalies flagged, any errors with the real reason). If
something's wrong, paste that log output back for a fix grounded in the
actual error, not a guess.

## 5. Reload the site

Once the workflow succeeds and Pages redeploys (automatic on every push to
`main`), reload your site URL. Badge should read `LIVE · TPS + TFS`.

## How "runs by itself" actually works here

- `.github/workflows/update.yml` runs `scripts/update_data.py` every 15
  min on GitHub's own infrastructure — not your computer. Close your
  laptop, it keeps running.
- Each run commits `docs/data/incidents.json`, `geocode_cache.json`, and
  `history_stats.json` back to the repo. That last one is what makes
  anomaly detection actually work over time — it's the long-term baseline
  file and must persist across runs, which committing it back does.
- Every push to `main` auto-redeploys Pages. No separate deploy step.
- The map (`docs/index.html`) polls its own `data/incidents.json` on a
  timer — same-origin fetch, no CORS question, since both are served from
  the same GitHub Pages domain.

## Cost & limits

- **$0.** GitHub Actions gives 2,000 free minutes/month on a free personal
  account; this workflow takes well under a minute per run, ~35 min/month
  even running every 15 min continuously. GitHub Pages bandwidth is free
  for personal projects at this traffic scale.
- Cron timing isn't exact — GitHub can delay a scheduled run by several
  minutes under platform load. Irrelevant for a dashboard like this.
- The repo's commit history grows forever (one commit per run with actual
  changes). That's normal, not a problem — git compresses these small JSON
  diffs well. If it ever bothers you cosmetically, that's a `git`
  history-squashing exercise, not something to worry about for a long
  time.

## Notes carried over from the self-hosted version

- TFS's feed only lists *currently active* incidents — empty or
  near-empty is normal.
- New fire intersections take a couple of cron cycles to get coordinates
  (rate-limited geocoding, respecting Nominatim's usage policy).
- TPS excludes domestic violence, sexual assault, and medical distress
  calls from this feed by their own policy — not fixable here.
- Anomaly detection stays silent until a given division has at least 3
  observed occurrences of a given day-of-week/hour slot in
  `history_stats.json`. That's correct behavior on a fresh deployment, not
  a bug — give it a few weeks.

## New: news articles, politician tracking, road restrictions

Runs automatically alongside the existing pipeline — no separate cron
entries needed, the workflow now runs all three scripts every 15 min.
Both are set `continue-on-error: true` in the workflow — if either fails,
the core police/fire pipeline still updates normally. News and roadwork
are bonus layers, not load-bearing.

### Before this is useful: fill in `scripts/politicians.json`

I deliberately did NOT hardcode current officeholders — elections change
who's in these roles, and guessing wrong puts an incorrect name on a real
map. Open `scripts/politicians.json` and replace the placeholder entries
with real names, roles, and default locations (City Hall for the Mayor,
Queen's Park for MPPs, etc — used when an article mentions them but
doesn't have its own clear location). Add as many entries as you want
tracked. Entries still showing `REPLACE_WITH_REAL_NAME` are automatically
skipped, not matched against anything.

Add headshot photos to `docs/photos/` and reference them by relative path
in each politician's `"photo"` field. Missing/broken images just get
hidden in the UI, they don't break anything.

### Honest expectations

- **News geocoding will have a lower hit-rate than the dispatch data.**
  TFS/TPS text is short and templated; news articles are long-form prose
  where the location might be mentioned vaguely, mid-paragraph, or not at
  all. Articles with no confidently-extracted location are skipped
  entirely rather than placed on a guess — check the Actions log
  (`[news] N articles fetched, M geocoded...`) to see the real hit rate.
- **A politician being tagged on an article means their name appears in
  the article text** — it is not a claim they were physically present at
  that location. An article quoting a politician's reaction to an event
  still gets tagged and placed at the event's location.
- **RSS reachability from a script wasn't verified in advance** — my own
  fetch tool got blocked reaching both feeds directly during research
  (bot detection). That may or may not reflect how they treat other
  script clients; the real answer is whatever the first Action run's log
  shows. If `source_status` for either feed shows an error every run,
  that's the first thing to look at, not a bug to keep chasing blind.
- **Road restrictions schema wasn't independently confirmed** — the
  script resolves the real data URL dynamically (no hardcoded resource
  ID) and prints the first raw record it gets back
  (`[road] sample raw record: ...`) so the actual field names are visible
  immediately in the log rather than assumed.
