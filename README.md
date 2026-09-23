# Wind Tracker

Finds NFL/NCAAF games forecast at 10+ MPH wind (mysportsweather.com), tracks the
DraftKings total from first sighting to kickoff (ESPN), and pulls final scores.

```
python3 wind.py daily                      # settle finished games, then scout upcoming ones
python3 wind.py scout --date 2026-09-26    # just one day's slate
python3 wind.py settle                     # just pull scores
python3 wind.py csv                        # export in the old Google Sheet column order
open docs/index.html                       # view it locally
```

No installs needed (Python 3.9+ standard library).

## How it works
- **games.json** is the whole database. Each game keeps one `readings` entry per day
  (wind + total). The first reading = "Wind/Total Ran", the last before kickoff = "Wind Close".
- **Opening/closing totals** come from DraftKings via ESPN. ESPN doesn't carry FanDuel.
- **Grading** isn't stored; the UI computes it the same way the sheet does:
  margin = close total − (home + away); under wins if margin > 0; profit = wins − 1.1 × losses.
- A game gets added once it hits 10 MPH and keeps being tracked even if the forecast drops.
- The `bet` field on each game is reserved for logging your own bets.

## Hosting and the 7am run
The site is GitHub Pages serving `docs/`. `.github/workflows/daily.yml` runs
`wind.py daily` every morning at 7am Eastern, commits the new data, and Pages
republishes. Run it by hand from the repo's **Actions** tab ("Run workflow").
