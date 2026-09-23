#!/usr/bin/env python3
"""Wind Tracker: find 10+ MPH NFL/NCAAF games, track totals, pull final scores.

Data lives in games.json. Every run also rewrites docs/data.js so docs/index.html
can be opened straight from disk.

  python3 wind.py scout [--date 2026-09-26]   add/refresh windy games
  python3 wind.py settle                      fill scores for finished games
  python3 wind.py daily                       settle, then scout (the 7am job)
  python3 wind.py csv                         export in the Google Sheet's column order
"""
import argparse
import csv
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DB = ROOT / "games.json"
UI_DATA = ROOT / "docs" / "data.js"
ET = ZoneInfo("America/New_York")

CONFIG = {
    "windThresholdMph": 10,
    "lineSource": "draftkings",  # ESPN's feed only carries DraftKings
    "juice": -110,
}

LEAGUES = {
    "NCAAF": {
        "msw": "https://www.mysportsweather.com/ncaaf",
        "espn": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?groups=80&limit=400&dates={d}",
        "odds": "https://sports.core.api.espn.com/v2/sports/football/leagues/college-football/events/{id}/competitions/{id}/odds",
    },
    "NFL": {
        "msw": "https://www.mysportsweather.com/nfl",
        "espn": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates={d}",
        "odds": "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/events/{id}/competitions/{id}/odds",
    },
}


# ── fetching ──────────────────────────────────────────────────────────────

def fetch(url):
    # mysportsweather wants a browser UA; ESPN rejects browser UAs but accepts
    # Python's default, so only send one where it's needed.
    headers = {"User-Agent": "Mozilla/5.0"} if "mysportsweather" in url else {}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


_espn_cache = {}


def espn_events(league, date):
    key = (league, date)
    if key not in _espn_cache:
        url = LEAGUES[league]["espn"].format(d=date.strftime("%Y%m%d"))
        _espn_cache[key] = json.loads(fetch(url)).get("events", [])
    return _espn_cache[key]


# ── mysportsweather parsing ───────────────────────────────────────────────

# mysportsweather code -> ESPN code, where they differ
NFL_ALIASES = {"was": "wsh", "jac": "jax", "la": "lar", "oak": "lv", "sd": "lac"}


def _num(pattern, text, cast=float):
    m = re.search(pattern, text)
    return cast(m.group(1)) if m else None


def parse_msw(league, html):
    """Return one dict per game card: date (ET), wind, team keys, url."""
    cards = []
    for block in re.split(r'(?=<a href="[^"]*" class="cheat-card)', html)[1:]:
        block = block.split("</a>", 1)[0]
        href = re.match(r'<a href="([^"]*)"', block).group(1)
        # Team keys are taken from ESPN logo URLs: numeric team ids for college,
        # lowercase abbreviations for the NFL. Both match ESPN's own feed.
        # FCS opponents often have no logo, so a card may carry only one key.
        logos = [NFL_ALIASES.get(k, k) if league == "NFL" else k
                 for k in re.findall(r"teamlogos/[a-z]+/500/([a-z0-9]+)\.png", block)]
        if not logos:
            continue
        if league == "NCAAF":
            m = re.match(r"/ncaaf/(\d{4}-\d{2}-\d{2})/", href)
            if not m:
                continue
            date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            wind = _num(r'wx-stat--wind">\s*<div class="wx-stat__primary[^"]*">(\d+)', block, int)
            direction = re.search(r'wx-stat__label">([A-Z]{1,3})<', block)
            direction = direction.group(1) if direction else None
            url = "https://www.mysportsweather.com" + href
        else:
            md = re.search(r'cheat-card__date">(\d{1,2})/(\d{1,2})<', block)
            if not md:
                continue
            today = datetime.now(ET).date()
            date = today.replace(month=int(md.group(1)), day=int(md.group(2)))
            if date < today - timedelta(days=180):  # window crosses New Year
                date = date.replace(year=date.year + 1)
            wind = _num(r'cheat-card__wind">(\d+) mph', block, int)
            direction = None
            url = LEAGUES["NFL"]["msw"] + href
        if wind is None:
            continue
        cards.append({"date": date, "wind": wind, "dir": direction,
                      "teams": frozenset(logos), "url": url,
                      "dome": "badge--dome" in block})
    return cards


# ── ESPN parsing ──────────────────────────────────────────────────────────

def team_key(league, competitor):
    """Same key mysportsweather exposes: the ESPN logo's file name
    (team id for college, e.g. 251; lowercase code for NFL, e.g. was)."""
    t = competitor["team"]
    m = re.search(r"/500/([a-z0-9]+)\.png", t.get("logo", ""))
    if m:
        return m.group(1)
    return str(t["id"]) if league == "NCAAF" else t["abbreviation"].lower()


def _line(s):
    """'o55.5' / 'u55.5' / '55.5' -> 55.5"""
    if s in (None, ""):
        return None
    try:
        return float(str(s).lstrip("ou"))
    except ValueError:
        return None


def espn_totals(comp):
    """(opening, current) DraftKings total from a scoreboard competition."""
    for o in comp.get("odds") or []:
        if "draftkings" not in o.get("provider", {}).get("name", "").lower():
            continue
        over = (o.get("total") or {}).get("over") or {}
        opening = _line((over.get("open") or {}).get("line"))
        current = _line((over.get("close") or {}).get("line")) or o.get("overUnder")
        return opening, current
    return None, None


def closing_totals(league, event_id):
    """(opening, closing) DraftKings total for a finished game. The scoreboard
    drops odds once a game ends; the core odds endpoint keeps them."""
    d = json.loads(fetch(LEAGUES[league]["odds"].format(id=event_id)))
    for o in d.get("items", []):
        if "draftkings" not in o.get("provider", {}).get("name", "").lower():
            continue
        get = lambda k: _line(((o.get(k) or {}).get("total") or {}).get("american"))
        return get("open"), get("close") or o.get("overUnder")
    return None, None


def find_event(league, card):
    for ev in espn_events(league, card["date"]):
        comp = ev["competitions"][0]
        if card["teams"] <= {team_key(league, c) for c in comp["competitors"]}:
            return ev
    return None


def team_name(competitor):
    """City/school like the sheet uses, except where that's ambiguous."""
    t = competitor["team"]
    return t["displayName"] if t["location"] in ("New York", "Los Angeles") else t["location"]


def side(comp, home_away):
    return next(c for c in comp["competitors"] if c["homeAway"] == home_away)


# ── db ────────────────────────────────────────────────────────────────────

def load():
    if DB.exists():
        return json.loads(DB.read_text())
    return {"config": CONFIG, "games": []}


def save(db):
    db["config"] = CONFIG
    db["updatedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db["games"].sort(key=lambda g: g["kickoff"])
    DB.write_text(json.dumps(db, indent=2) + "\n")
    UI_DATA.parent.mkdir(exist_ok=True)
    UI_DATA.write_text("window.WIND_DATA = " + json.dumps(db) + ";\n")


def et_day(iso):
    return datetime.fromisoformat(iso).astimezone(ET).date()


# ── commands ──────────────────────────────────────────────────────────────

def scout(db, only_date=None):
    by_id = {g["id"]: g for g in db["games"]}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    added = refreshed = 0
    for league, src in LEAGUES.items():
        try:
            cards = parse_msw(league, fetch(src["msw"]))
        except Exception as e:  # one site hiccup shouldn't sink the other league
            print(f"  ! {league}: could not read mysportsweather ({e})", file=sys.stderr)
            continue
        for card in cards:
            if only_date and card["date"] != only_date:
                continue
            ev = find_event(league, card)
            if ev is None:
                if card["wind"] >= CONFIG["windThresholdMph"]:
                    print(f"  ! {league} {card['date']} {sorted(card['teams'])}: "
                          f"{card['wind']} MPH but no ESPN match, skipped", file=sys.stderr)
                continue
            gid = f"{league}-{ev['id']}"
            comp = ev["competitions"][0]
            if ev["status"]["type"]["state"] != "pre":
                continue  # started or finished; the forecast is no longer a forecast
            opening, current = espn_totals(comp)
            reading = {"at": now, "windMph": card["wind"], "windDir": card["dir"], "total": current}

            game = by_id.get(gid)
            if game is None:
                # Only games at or above the threshold get onto the list; once
                # on it, keep tracking even if the forecast calms down.
                if card["wind"] < CONFIG["windThresholdMph"]:
                    continue
                home, away = side(comp, "home"), side(comp, "away")
                game = {
                    "id": gid,
                    "league": league,
                    "season": ev.get("season", {}).get("year"),
                    "week": (ev.get("week") or {}).get("number"),
                    "kickoff": ev["date"],
                    "homeTeam": team_name(home),
                    "awayTeam": team_name(away),
                    "homeAbbr": home["team"]["abbreviation"],
                    "awayAbbr": away["team"]["abbreviation"],
                    "neutralSite": bool(comp.get("neutralSite")),
                    "dome": card["dome"],
                    "venue": (comp.get("venue") or {}).get("fullName"),
                    "espnId": ev["id"],
                    "weatherUrl": card["url"],
                    "firstSeen": now,
                    "openingTotal": opening,
                    "readings": [],
                    "closingTotal": None,
                    "score": None,
                    "bet": None,
                }
                db["games"].append(game)
                by_id[gid] = game
                added += 1
                print(f"  + {league} {game['awayTeam']} @ {game['homeTeam']}  "
                      f"{card['wind']} MPH  total {current} (open {opening})")
            else:
                refreshed += 1
            game["kickoff"] = ev["date"]  # kickoff times get set/moved midweek
            if game["openingTotal"] is None:
                game["openingTotal"] = opening
            # One reading per ET day: a re-run the same morning replaces it.
            r = game["readings"]
            if r and et_day(r[-1]["at"]) == et_day(now):
                r[-1] = reading
            else:
                r.append(reading)
    print(f"scout: {added} new, {refreshed} refreshed")


def settle(db):
    now = datetime.now(timezone.utc)
    done = 0
    for g in db["games"]:
        if g["score"] is not None:
            continue
        kickoff = datetime.fromisoformat(g["kickoff"].replace("Z", "+00:00"))
        if kickoff > now:
            continue
        ev = next((e for e in espn_events(g["league"], kickoff.astimezone(ET).date())
                   if e["id"] == g["espnId"]), None)
        if ev is None or not ev["status"]["type"]["completed"]:
            continue
        comp = ev["competitions"][0]
        opening, closing = closing_totals(g["league"], g["espnId"])
        if g["openingTotal"] is None:
            g["openingTotal"] = opening
        g["score"] = {"home": int(side(comp, "home")["score"]),
                      "away": int(side(comp, "away")["score"])}
        g["closingTotal"] = closing if closing is not None else (
            g["readings"][-1]["total"] if g["readings"] else None)
        done += 1
        pts = g["score"]["home"] + g["score"]["away"]
        print(f"  ✓ {g['awayTeam']} {g['score']['away']} @ {g['homeTeam']} {g['score']['home']}"
              f"  ({pts} pts, close {g['closingTotal']})")
    print(f"settle: {done} games scored")


def export_csv(db, path):
    """Same column order as the existing Wind Tracker tab (A–M)."""
    cols = ["LEAGUE", "HOME TEAM", "AWAY TEAM", "WEEK", "DATE RAN", "WIND RAN", "WIND CLOSE",
            "TOTAL RAN", "TOTAL OPEN", "TOTAL CLOSE", "MOVEMENT", "HOME SCORE", "AWAY SCORE"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for g in db["games"]:
            r = g["readings"]
            first, last = (r[0], r[-1]) if r else ({}, {})
            neutral = " (Neutral)" if g["neutralSite"] else ""
            ran = datetime.fromisoformat(first["at"]).astimezone(ET).strftime("%m/%d/%Y") if r else ""
            close = g["closingTotal"] if g["closingTotal"] is not None else last.get("total")
            move = (g["openingTotal"] - close) if None not in (g["openingTotal"], close) else ""
            s = g["score"] or {}
            w.writerow([g["league"], g["homeTeam"] + neutral, g["awayTeam"] + neutral, g["week"], ran,
                        f"{first.get('windMph')} MPH" if r else "",
                        f"{last.get('windMph')} MPH" if r else "",
                        first.get("total", ""), g["openingTotal"], close, move,
                        s.get("home", ""), s.get("away", "")])
    print(f"wrote {path}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scout")
    sc.add_argument("--date", help="only games on this ET date, e.g. 2026-09-26")
    sub.add_parser("settle")
    sub.add_parser("daily")
    ex = sub.add_parser("csv")
    ex.add_argument("--out", default=str(ROOT / "wind-tracker.csv"))
    a = p.parse_args()

    db = load()
    if a.cmd == "scout":
        scout(db, datetime.strptime(a.date, "%Y-%m-%d").date() if a.date else None)
    elif a.cmd == "settle":
        settle(db)
    elif a.cmd == "daily":
        settle(db)
        scout(db)
    elif a.cmd == "csv":
        export_csv(db, a.out)
        return
    save(db)


if __name__ == "__main__":
    main()
