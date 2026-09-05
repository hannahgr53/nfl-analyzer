#!/usr/bin/env python3
"""
Build data/epa.json from nflverse play-by-play.

Why this exists: EPA is the one number the app cannot fetch itself. nflverse
publishes play-by-play as GitHub release assets, and those redirect to a host
that sends no CORS headers, so a browser is refused. GitHub Actions is not a
browser, so it can download the file, aggregate it, and commit a small JSON
that the app CAN read from its own Pages site.

Output is deliberately tiny (a few KB) so the app pays almost nothing to read
it on every open.

No third-party packages: urllib, gzip and csv only.
"""

import csv
import gzip
import io
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

PBP = ("https://github.com/nflverse/nflverse-data/releases/download/pbp/"
       "play_by_play_{season}.csv.gz")

# nflverse uses the same abbreviations the app does, with a couple of historical
# spellings that still show up in older files.
FIX = {"OAK": "LV", "SD": "LAC", "STL": "LA", "WSH": "WAS", "LAR": "LA"}

TEAMS = ["ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
         "DET", "GB", "HOU", "IND", "JAX", "KC", "LV", "LAC", "LA", "MIA",
         "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SF", "SEA", "TB",
         "TEN", "WAS"]


def fetch(season):
    url = PBP.format(season=season)
    print("fetching", url, flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "nfl-analyzer-epa/1"})
    with urllib.request.urlopen(req, timeout=300) as r:
        raw = r.read()
    print("  %.1f MB compressed" % (len(raw) / 1e6), flush=True)
    return gzip.decompress(raw).decode("utf-8", "replace")


def aggregate(text):
    """Per-team offensive, defensive and dropback EPA per play, regular season."""
    rdr = csv.DictReader(io.StringIO(text))
    off = {t: [0.0, 0] for t in TEAMS}      # [epa sum, plays]
    dfn = {t: [0.0, 0] for t in TEAMS}
    dbk = {t: [0.0, 0] for t in TEAMS}
    weeks = set()

    for row in rdr:
        if (row.get("season_type") or "REG") != "REG":
            continue
        ptype = row.get("play_type") or ""
        if ptype not in ("pass", "run"):
            continue
        epa = row.get("epa")
        if epa in (None, "", "NA"):
            continue
        try:
            epa = float(epa)
        except ValueError:
            continue

        pos = FIX.get(row.get("posteam") or "", row.get("posteam") or "")
        dfs = FIX.get(row.get("defteam") or "", row.get("defteam") or "")
        try:
            weeks.add(int(float(row.get("week") or 0)))
        except ValueError:
            pass

        if pos in off:
            off[pos][0] += epa
            off[pos][1] += 1
            if ptype == "pass":
                dbk[pos][0] += epa
                dbk[pos][1] += 1
        if dfs in dfn:
            dfn[dfs][0] += epa
            dfn[dfs][1] += 1

    out = {}
    for t in TEAMS:
        if off[t][1] < 30:          # not enough football to mean anything
            continue
        out[t] = {
            "offEpa": round(off[t][0] / off[t][1], 4),
            "defEpa": round(dfn[t][0] / dfn[t][1], 4) if dfn[t][1] else 0.0,
            "passEpa": round(dbk[t][0] / dbk[t][1], 4) if dbk[t][1] else 0.0,
            "plays": off[t][1],
        }
    return out, (max(weeks) if weeks else 0)


def blend(cur, prev, cur_weeks):
    """
    Same reasoning the app's team ratings use: at week 1 last season carries the
    weight, and by about game five this season has taken over. Without this,
    early-season EPA is four teams' worth of noise.
    """
    w = max(0.0, min(1.0, cur_weeks / 5.0))
    teams = {}
    for t in TEAMS:
        c, p = cur.get(t), prev.get(t)
        if not c and not p:
            continue
        if not c:
            teams[t] = dict(p, source="prior")
            continue
        if not p:
            teams[t] = dict(c, source="current")
            continue
        teams[t] = {
            "offEpa": round(c["offEpa"] * w + p["offEpa"] * (1 - w), 4),
            "defEpa": round(c["defEpa"] * w + p["defEpa"] * (1 - w), 4),
            "passEpa": round(c["passEpa"] * w + p["passEpa"] * (1 - w), 4),
            "plays": c["plays"],
            "source": "blend",
        }
    return teams, w


def main():
    now = datetime.now(timezone.utc)
    # The NFL season is named for the year it starts, so January and February
    # still belong to the previous season.
    season = now.year if now.month >= 3 else now.year - 1

    cur, cur_weeks = {}, 0
    try:
        cur, cur_weeks = aggregate(fetch(season))
        print("  %d teams, through week %d" % (len(cur), cur_weeks), flush=True)
    except Exception as e:                                   # noqa: BLE001
        print("current season unavailable: %s" % e, flush=True)

    prev = {}
    try:
        prev, _ = aggregate(fetch(season - 1))
        print("  %d teams from %d" % (len(prev), season - 1), flush=True)
    except Exception as e:                                   # noqa: BLE001
        print("prior season unavailable: %s" % e, flush=True)

    if not cur and not prev:
        print("no data from either season; leaving the existing file alone")
        # Deliberate no-op, not a failure: nflverse has not published this
        # season's file yet. Exiting non-zero here would send a failure email
        # every week until it does.
        return 0

    teams, w = blend(cur, prev, cur_weeks)
    payload = {
        "generated": now.isoformat(timespec="seconds"),
        "season": season,
        "throughWeek": cur_weeks,
        "curWeight": round(w, 3),
        "seasonsUsed": [s for s, d in ((season, cur), (season - 1, prev)) if d],
        "note": (
            "Offensive, defensive and dropback EPA per play from nflverse "
            "play-by-play. {pct}% of this rating is {cur} football, the rest "
            "{prev}. Def EPA is EPA allowed, so a good defense is negative."
        ).format(pct=round(w * 100), cur=season, prev=season - 1),
        "teams": teams,
    }

    os.makedirs("data", exist_ok=True)
    path = "data/epa.json"
    new = json.dumps(payload, indent=1, sort_keys=True)
    old = None
    if os.path.exists(path):
        with open(path) as f:
            old = f.read()
    # Ignore the timestamp when deciding whether anything changed, so an
    # unchanged week does not create a pointless commit.
    def strip(s):
        try:
            d = json.loads(s)
            d.pop("generated", None)
            return json.dumps(d, sort_keys=True)
        except Exception:                                    # noqa: BLE001
            return s
    if old is not None and strip(old) == strip(new):
        print("no change")
        return 0
    with open(path, "w") as f:
        f.write(new + "\n")
    print("wrote %s (%d teams, %.1f KB)" % (path, len(teams), len(new) / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
