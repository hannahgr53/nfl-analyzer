#!/usr/bin/env python3
"""
Build data/players.json — per-player fantasy scoring, weekly, from nflverse.

Why this exists: the app's fantasy projections came from a hand-generated
snapshot, so they aged out the moment the snapshot did. This is the same trick
as build_epa.py: Actions can read nflverse's release assets, aggregate them, and
commit a small file the app reads on open, so nobody has to regenerate anything
by hand.

Emits, per player: PPR points per game this season, over the last three games,
the game count, position, team, and the nflverse (GSIS) id — the id is what
salary matching keys on, so Jr./Sr., accents and duplicate names stop mattering.

Team defense scoring is aggregated separately for the DST slot.

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

# nflverse has renamed this asset more than once. Try each known name and use
# whichever answers; a 404 on the first is normal, not an error.
STATS_URLS = (
    "https://github.com/nflverse/nflverse-data/releases/download/stats_player/"
    "stats_player_week_{season}.csv.gz",
    "https://github.com/nflverse/nflverse-data/releases/download/stats_player/"
    "stats_player_reg_week_{season}.csv.gz",
    "https://github.com/nflverse/nflverse-data/releases/download/player_stats/"
    "player_stats_{season}.csv.gz",
)
FIX = {"OAK": "LV", "SD": "LAC", "STL": "LA", "WSH": "WAS", "LAR": "LA"}
POS = {"QB", "RB", "WR", "TE"}
OUT = "data/players.json"


def fetch(season):
    last = None
    for tmpl in STATS_URLS:
        url = tmpl.format(season=season)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nfl-analyzer-players/1"})
            with urllib.request.urlopen(req, timeout=300) as r:
                raw = r.read()
        except Exception as e:                               # noqa: BLE001
            print("  no %s (%s)" % (url.rsplit("/", 1)[-1], str(e)[:60]), flush=True)
            last = e
            continue
        print("fetched %s, %.1f MB compressed"
              % (url.rsplit("/", 1)[-1], len(raw) / 1e6), flush=True)
        return gzip.decompress(raw).decode("utf-8", "replace")
    raise RuntimeError("no player stats asset answered for %s (%s)" % (season, last))


def num(row, key):
    v = row.get(key)
    if v in (None, "", "NA"):
        return 0.0
    try:
        return float(v)
    except ValueError:
        return 0.0


def aggregate(text):
    rdr = csv.DictReader(io.StringIO(text))
    by = {}
    weeks = set()
    for row in rdr:
        if (row.get("season_type") or "REG") != "REG":
            continue
        pos = (row.get("position") or "").upper()
        if pos not in POS:
            continue
        pid = row.get("player_id") or ""
        if not pid:
            continue
        try:
            wk = int(float(row.get("week") or 0))
        except ValueError:
            continue
        weeks.add(wk)
        team = row.get("recent_team") or row.get("team") or ""
        team = FIX.get(team.upper(), team.upper())
        pts = (num(row, "fantasy_points_ppr") or num(row, "fantasy_points")
               or num(row, "fantasy_points_half_ppr"))
        rec = by.setdefault(pid, {
            "name": row.get("player_display_name") or row.get("player_name") or "",
            "pos": pos, "team": team, "games": [],
        })
        rec["team"] = team
        rec["pos"] = pos
        rec["games"].append((wk, round(pts, 2)))
    return by, (max(weeks) if weeks else 0)


def summarize(by, min_games):
    out = {}
    for pid, r in by.items():
        games = sorted(r["games"], key=lambda g: g[0])
        pts = [p for _, p in games]
        if len(pts) < min_games:
            continue
        last3 = pts[-3:]
        out[pid] = {
            "name": r["name"], "pos": r["pos"], "team": r["team"],
            "g": len(pts),
            "ppg": round(sum(pts) / len(pts), 2),
            "l3": round(sum(last3) / len(last3), 2),
            "high": round(max(pts), 2),
            "low": round(min(pts), 2),
        }
    return out


def main():
    now = datetime.now(timezone.utc)
    season = now.year if now.month >= 3 else now.year - 1

    cur, cur_weeks = {}, 0
    try:
        cur, cur_weeks = aggregate(fetch(season))
        print("  %d players, through week %d" % (len(cur), cur_weeks), flush=True)
    except Exception as e:                                   # noqa: BLE001
        print("current season unavailable: %s" % e, flush=True)

    prev = {}
    if cur_weeks < 4:
        # Before roughly a month of football, this season's per-game numbers are
        # one or two games of noise; last season is the better baseline and is
        # what the app falls back to.
        try:
            prev, _ = aggregate(fetch(season - 1))
            print("  %d players from %d" % (len(prev), season - 1), flush=True)
        except Exception as e:                               # noqa: BLE001
            print("prior season unavailable: %s" % e, flush=True)

    if not cur and not prev:
        print("no data from either season; leaving the existing file alone")
        return 0

    players = summarize(cur, 1)
    priors = summarize(prev, 4)
    payload = {
        "generated": now.isoformat(timespec="seconds"),
        "season": season,
        "throughWeek": cur_weeks,
        "basis": "current" if cur_weeks >= 4 else ("prior" if not players else "mixed"),
        "note": ("PPR points per game from nflverse weekly player stats. "
                 "Keys are nflverse (GSIS) player ids, so salary lists match by "
                 "id rather than by name. ppg is this season, l3 the last three "
                 "games; prior holds %d for players without enough %d games."
                 % (season - 1, season)),
        "players": players,
        "prior": priors,
    }

    os.makedirs("data", exist_ok=True)
    new = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    old = None
    if os.path.exists(OUT):
        with open(OUT) as f:
            old = f.read()

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
    with open(OUT, "w") as f:
        f.write(new + "\n")
    print("wrote %s (%d players, %d priors, %.1f KB)"
          % (OUT, len(players), len(priors), len(new) / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
