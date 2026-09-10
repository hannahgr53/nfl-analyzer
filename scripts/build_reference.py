#!/usr/bin/env python3
"""
Build data/reference.json — every reference table the generator used to make.

Why this exists: the Reference tab was the last thing in the app coming from a
hand-run generator, so it aged the moment the snapshot did. This rebuilds the
same tables from nflverse play-by-play, weekly player stats and the schedule,
and commits them, so opening the app on a phone is the only step left.

Output shape matches the snapshot the app already knows how to read:

    {"generated": ..., "tabs": {"<tab name>": {"columns": [...], "rows": [[...]]}}}

Three tables are deliberately NOT here:
  Injuries / Practice  — the app reads ESPN's live report, which is fresher.
  Matchup Predictor    — the app's own model computes it per game.
  Trades               — historical, not derivable from these feeds; the baked-in
                         copy stays in place for it.

No third-party packages: urllib, gzip, csv and json only.
"""

import csv
import gzip
import io
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

PBP = ("https://github.com/nflverse/nflverse-data/releases/download/pbp/"
       "play_by_play_{season}.csv.gz")
# nflverse has renamed this asset more than once (see build_players.py). Try
# each known name and use whichever answers; a 404 on the first is normal.
STATS_URLS = (
    "https://github.com/nflverse/nflverse-data/releases/download/stats_player/"
    "stats_player_week_{season}.csv.gz",
    "https://github.com/nflverse/nflverse-data/releases/download/stats_player/"
    "stats_player_reg_week_{season}.csv.gz",
    "https://github.com/nflverse/nflverse-data/releases/download/player_stats/"
    "player_stats_{season}.csv.gz",
)
GAMES = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"

FIX = {"OAK": "LV", "SD": "LAC", "STL": "LA", "WSH": "WAS", "LAR": "LA"}
TEAMS = ["ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
         "DET", "GB", "HOU", "IND", "JAX", "KC", "LV", "LAC", "LA", "MIA",
         "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SF", "SEA", "TB",
         "TEN", "WAS"]
OUT = "data/reference.json"
UA = {"User-Agent": "nfl-analyzer-reference/1"}


def tm(x):
    x = (x or "").upper().strip()
    return FIX.get(x, x)


def fetch_text(url, gz):
    print("fetching", url, flush=True)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=300) as r:
        raw = r.read()
    if gz:
        print("  %.1f MB compressed" % (len(raw) / 1e6), flush=True)
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


def fetch_stats_text(season):
    """Try each known nflverse weekly-stats asset name in turn."""
    last = None
    for tmpl in STATS_URLS:
        try:
            return fetch_text(tmpl.format(season=season), True)
        except Exception as e:                               # noqa: BLE001
            print("  no %s (%s)" % (tmpl.rsplit("/", 1)[-1], str(e)[:60]), flush=True)
            last = e
    raise RuntimeError("no player stats asset answered for %s (%s)" % (season, last))


def rows_of(text):
    return list(csv.DictReader(io.StringIO(text)))


def f(row, key, default=0.0):
    v = row.get(key)
    if v in (None, "", "NA"):
        return default
    try:
        return float(v)
    except ValueError:
        return default


def i(row, key, default=0):
    return int(f(row, key, default))


def r1(x):
    return round(x, 1)


def pct(num, den, digits=1):
    return round(100.0 * num / den, digits) if den else 0.0


# --------------------------------------------------------------- schedule tables
def schedule_tables(games, season):
    played = [g for g in games
              if str(g.get("season")) == str(season)
              and (g.get("game_type") or "REG") == "REG"
              and g.get("home_score") not in (None, "", "NA")]
    rec = {t: {"w": 0, "l": 0, "t": 0, "pf": 0, "pa": 0, "g": 0,
               "hw": 0, "hl": 0, "aw": 0, "al": 0} for t in TEAMS}
    for g in played:
        h, a = tm(g.get("home_team")), tm(g.get("away_team"))
        hs, as_ = f(g, "home_score"), f(g, "away_score")
        for t, own, opp in ((h, hs, as_), (a, as_, hs)):
            if t not in rec:
                continue
            r = rec[t]
            r["g"] += 1
            r["pf"] += own
            r["pa"] += opp
            if own > opp:
                r["w"] += 1
            elif own < opp:
                r["l"] += 1
            else:
                r["t"] += 1
        if h in rec:
            rec[h]["hw" if hs > as_ else "hl"] += 1
        if a in rec:
            rec[a]["aw" if as_ > hs else "al"] += 1

    live = {t: r for t, r in rec.items() if r["g"]}
    records = {"columns": ["Team", "W", "L", "T", "Points For", "Points Against", "PPG", "PAPG"],
               "rows": [[t, r["w"], r["l"], r["t"], int(r["pf"]), int(r["pa"]),
                         r1(r["pf"] / r["g"]), r1(r["pa"] / r["g"])]
                        for t, r in sorted(live.items(), key=lambda kv: -(kv[1]["w"]))]}

    off = sorted(live.items(), key=lambda kv: -(kv[1]["pf"] / kv[1]["g"]))
    dfn = sorted(live.items(), key=lambda kv: (kv[1]["pa"] / kv[1]["g"]))
    offense = {"columns": ["Off Rank", "Team", "Points/Game"],
               "rows": [[n + 1, t, r1(r["pf"] / r["g"])] for n, (t, r) in enumerate(off)]}
    defense = {"columns": ["Def Rank", "Team", "Points Allowed/Game"],
               "rows": [[n + 1, t, r1(r["pa"] / r["g"])] for n, (t, r) in enumerate(dfn)]}
    return records, offense, defense, live


# -------------------------------------------------------------------- pbp tables
def pbp_tables(pbp, recs):
    off = defaultdict(lambda: defaultdict(float))
    drives = defaultdict(lambda: defaultdict(float))
    seen_games = defaultdict(set)

    for p in pbp:
        if (p.get("season_type") or "REG") != "REG":
            continue
        pos, dfs = tm(p.get("posteam")), tm(p.get("defteam"))
        gid = p.get("game_id") or ""
        if pos in recs:
            seen_games[pos].add(gid)
        ptype = p.get("play_type") or ""
        is_pass = ptype == "pass"
        is_run = ptype == "run"
        epa = f(p, "epa", None) if p.get("epa") not in (None, "", "NA") else None
        yards = f(p, "yards_gained")
        down = i(p, "down")
        qtr = i(p, "qtr")

        if pos in recs and (is_pass or is_run):
            o = off[pos]
            o["plays"] += 1
            o["yards"] += yards
            if epa is not None:
                o["epa"] += epa
                o["epaN"] += 1
            if f(p, "success"):
                o["succ"] += 1
            if i(p, "no_huddle"):
                o["nohuddle"] += 1
            if down in (1, 2):
                o["early"] += 1
                if is_pass:
                    o["earlyPass"] += 1
            if is_run:
                o["carries"] += 1
                o["rushYds"] += yards
                if yards >= 10:
                    o["explRun"] += 1
                if f(p, "yardline_100") <= 20:
                    o["rzRun"] += 1
            if is_pass:
                o["att"] += 1
                o["passYds"] += yards
                if yards >= 20:
                    o["explPass"] += 1
                if f(p, "complete_pass"):
                    o["comp"] += 1
                if epa is not None:
                    o["dbEpa"] += epa
                    o["dbN"] += 1
            if f(p, "yardline_100") <= 20:
                o["rzPlays"] += 1
            if down == 3:
                o["third"] += 1
                if f(p, "first_down") or f(p, "touchdown"):
                    o["thirdConv"] += 1
            if down == 4 and qtr <= 4:
                o["fourth"] += 1
                if is_pass or is_run:
                    o["fourthGo"] += 1
            if f(p, "interception") or f(p, "fumble_lost"):
                o["giveaways"] += 1
        if pos in recs and f(p, "sack"):
            off[pos]["sacks_taken"] += 1
            off[pos]["dropbacks"] += 1
        if pos in recs and is_pass:
            off[pos]["dropbacks"] += 1

        if dfs in recs and (is_pass or is_run):
            d = off[dfs]
            d["dPlays"] += 1
            if epa is not None:
                d["dEpa"] += epa
                d["dEpaN"] += 1
            if f(p, "success"):
                d["dSucc"] += 1
            if is_run:
                d["dRunPlays"] += 1
                if epa is not None:
                    d["dRunEpa"] += epa
                    d["dRunN"] += 1
                if f(p, "success"):
                    d["dRunSucc"] += 1
        if dfs in recs:
            if f(p, "sack"):
                off[dfs]["sacks"] += 1
            if f(p, "interception"):
                off[dfs]["ints"] += 1
            if f(p, "fumble_lost") and pos != dfs:
                off[dfs]["fumRec"] += 1
            if f(p, "touchdown") and tm(p.get("td_team")) == dfs and pos != dfs:
                off[dfs]["defTds"] += 1

        # Opening drive: first drive of each half, offense's own.
        dnum = i(p, "fixed_drive")
        if pos in recs and dnum in (1, 2) and (is_pass or is_run or ptype == "punt"):
            key = (pos, gid, dnum)
            dr = drives[key]
            dr["plays"] += 1
            if is_run:
                dr["runs"] += 1
            if not dr.get("start"):
                dr["start"] = 100 - f(p, "yardline_100")
            res = (p.get("fixed_drive_result") or "").lower()
            dr["td"] = 1.0 if "touchdown" in res else dr.get("td", 0.0)
            dr["score"] = 1.0 if ("touchdown" in res or "field goal" in res) else dr.get("score", 0.0)
            dr["punt"] = 1.0 if "punt" in res else dr.get("punt", 0.0)
            dr["to"] = 1.0 if ("interception" in res or "fumble" in res) else dr.get("to", 0.0)
            dr["pts"] = 7.0 if "touchdown" in res else (3.0 if "field goal" in res else 0.0)

    games_played = {t: max(1, len(seen_games.get(t, ()))) for t in recs}

    # First drives, aggregated per team.
    fd = defaultdict(lambda: defaultdict(float))
    for (t, _gid, dnum), dr in drives.items():
        if dnum != 1:
            continue
        a = fd[t]
        a["n"] += 1
        for k in ("td", "score", "punt", "to", "pts", "plays", "runs", "start"):
            a[k] += dr.get(k, 0.0)
        if dr.get("plays", 0) <= 3 and dr.get("punt"):
            a["threeOut"] += 1

    first_odds = {"columns": ["Team", "Games", "Off Score%", "Off Punt%", "Off Turnover%",
                              "Def Score%", "Def Punt%", "Def Turnover%"], "rows": []}
    first_detail = {"columns": ["Team", "Games", "Opening Drive Pts/Gm", "Opening Drive TD%",
                                "3-and-Out%", "Avg Plays", "Avg Start (own yd line)", "Run%"],
                    "rows": []}
    for t in sorted(fd):
        a = fd[t]
        n = a["n"] or 1
        first_odds["rows"].append([t, int(n), pct(a["score"], n), pct(a["punt"], n), pct(a["to"], n),
                                   "\u2014", "\u2014", "\u2014"])
        first_detail["rows"].append([t, int(n), r1(a["pts"] / n), pct(a["td"], n),
                                     pct(a["threeOut"], n), r1(a["plays"] / n),
                                     r1(a["start"] / n), pct(a["runs"], a["plays"] or 1)])

    run_game = {"columns": ["Team", "Rush Yds/Gm", "Yds/Carry", "Carries",
                            "Explosive Run Rate%", "Def Rush EPA/Play",
                            "Def Run Success% Allowed"], "rows": []}
    pass_game = {"columns": ["Team", "QB EPA/Dropback", "Completion%", "Yds/Attempt",
                             "Sack Rate%", "Pressure Rate%*", "Explosive Pass Rate%"], "rows": []}
    advanced = {"columns": ["Team", "Record", "Home Record", "Away Record", "Point Diff/Gm",
                            "Off Yds/Play", "Off EPA/Play", "Def EPA/Play", "Success Rate%",
                            "Turnover Diff", "Red Zone TD%", "3rd Down%", "Time of Poss",
                            "Plays/Game"], "rows": []}
    coaching = {"columns": ["Team", "4th Down Go-For-It%", "Early-Down Pass%",
                            "Red Zone Run%", "No-Huddle%"], "rows": []}
    dst = {"columns": ["Team", "Games", "Sacks", "INTs", "Fumble Rec", "Def/ST TDs",
                       "Pts Allowed/Gm", "Avg Fantasy Pts"], "rows": []}

    for t in sorted(recs):
        o = off.get(t)
        if not o:
            continue
        g = games_played[t]
        r = recs[t]
        run_game["rows"].append([
            t, r1(o["rushYds"] / g), round(o["rushYds"] / o["carries"], 2) if o["carries"] else 0,
            int(o["carries"]), pct(o["explRun"], o["carries"] or 1),
            round(o["dRunEpa"] / o["dRunN"], 3) if o["dRunN"] else 0,
            pct(o["dRunSucc"], o["dRunPlays"] or 1)])
        pass_game["rows"].append([
            t, round(o["dbEpa"] / o["dbN"], 3) if o["dbN"] else 0,
            pct(o["comp"], o["att"] or 1), round(o["passYds"] / o["att"], 2) if o["att"] else 0,
            pct(o["sacks_taken"], o["dropbacks"] or 1), "\u2014",
            pct(o["explPass"], o["att"] or 1)])
        pd = (r["pf"] - r["pa"]) / r["g"] if r["g"] else 0
        advanced["rows"].append([
            t, "%d-%d%s" % (r["w"], r["l"], ("-%d" % r["t"]) if r["t"] else ""),
            "%d-%d" % (r["hw"], r["hl"]), "%d-%d" % (r["aw"], r["al"]), r1(pd),
            round(o["yards"] / o["plays"], 2) if o["plays"] else 0,
            round(o["epa"] / o["epaN"], 3) if o["epaN"] else 0,
            round(o["dEpa"] / o["dEpaN"], 3) if o["dEpaN"] else 0,
            pct(o["succ"], o["plays"] or 1),
            int(o["ints"] + o["fumRec"] - o["giveaways"]),
            "\u2014",
            pct(o["thirdConv"], o["third"] or 1), "\u2014", r1(o["plays"] / g)])
        coaching["rows"].append([
            t, pct(o["fourthGo"], o["fourth"] or 1), pct(o["earlyPass"], o["early"] or 1),
            pct(o["rzRun"], o["rzPlays"] or 1), pct(o["nohuddle"], o["plays"] or 1)])
        pa_pg = r["pa"] / r["g"] if r["g"] else 0
        # Standard DFS defense scoring: sacks 1, takeaways 2, TDs 6, plus the
        # points-allowed tier.
        tier = (10 if pa_pg < 1 else 7 if pa_pg < 7 else 4 if pa_pg < 14 else
                1 if pa_pg < 21 else 0 if pa_pg < 28 else -1 if pa_pg < 35 else -4)
        fpts = ((o["sacks"] + 2 * (o["ints"] + o["fumRec"]) + 6 * o["defTds"]) / g) + tier
        dst["rows"].append([t, g, int(o["sacks"]), int(o["ints"]), int(o["fumRec"]),
                            int(o["defTds"]), r1(pa_pg), r1(fpts)])

    dst["rows"].sort(key=lambda row: -row[-1])
    return {"First Drive Odds": first_odds, "First Drive Detail": first_detail,
            "Run Game": run_game, "Passing Game": pass_game,
            "Advanced Team Stats": advanced, "Coaching Tendencies": coaching,
            "Team Defense (DST)": dst}


# ----------------------------------------------------------------- player tables
def _per_player(stats):
    per = defaultdict(lambda: {"name": "", "pos": "", "team": "", "weeks": [],
                               "tds": 0.0, "targets": 0.0, "tshare": [], "rz": 0.0})
    for row in stats:
        if (row.get("season_type") or "REG") != "REG":
            continue
        pos = (row.get("position") or "").upper()
        if pos not in ("QB", "RB", "WR", "TE"):
            continue
        pid = row.get("player_id") or row.get("player_display_name")
        if not pid:
            continue
        r = per[pid]
        r["name"] = row.get("player_display_name") or row.get("player_name") or ""
        r["pos"] = pos
        r["team"] = tm(row.get("recent_team") or row.get("team"))
        pts = f(row, "fantasy_points_ppr") or f(row, "fantasy_points")
        r["weeks"].append((i(row, "week"), round(pts, 2)))
        r["tds"] += f(row, "passing_tds") + f(row, "rushing_tds") + f(row, "receiving_tds")
        r["targets"] += f(row, "targets")
        ts = f(row, "target_share", None) if row.get("target_share") not in (None, "", "NA") else None
        if ts is not None:
            r["tshare"].append(ts)
    return per


def player_tables(stats, season, prev_stats=None):
    # Early in a season almost nobody has a current-season row yet, so a
    # player who has not taken a 2026 snap (hurt, or just a normal team that
    # has not played its week-1 game) would otherwise vanish from this table
    # entirely rather than being merely stale. Last season's numbers fill
    # that gap; current-season rows always win once they exist. This means a
    # player's team can still lag behind an offseason trade or free-agent
    # signing until he actually plays a game — no feed here can know a
    # roster move before it shows up in a box score — but at least the whole
    # league stays populated instead of thinning out to whoever already has
    # a game in the book.
    per = dict(_per_player(prev_stats)) if prev_stats else {}
    per.update(_per_player(stats))

    qb_td = {"columns": ["Player", "Team", "Games", "TDs", "TD/Game"], "rows": []}
    rb_td = {"columns": ["Player", "Team", "Games", "TDs", "TD/Game"], "rows": []}
    fantasy = {"columns": ["Player", "Team", "Pos", "Opponent", "Division Game", "Season Avg",
                           "L3 Avg", "Trend", "Snap%", "Target Share%", "RZ Touches", "Status",
                           "Projected"], "rows": []}

    for r in per.values():
        weeks = sorted(r["weeks"])
        pts = [p for _, p in weeks]
        if not pts:
            continue
        g = len(pts)
        avg = sum(pts) / g
        l3 = sum(pts[-3:]) / len(pts[-3:])
        trend = "up" if l3 > avg + 1.5 else "down" if l3 < avg - 1.5 else "flat"
        tdpg = r["tds"] / g
        if r["pos"] == "QB":
            qb_td["rows"].append([r["name"], r["team"], g, int(r["tds"]), round(tdpg, 2)])
        if r["pos"] == "RB":
            rb_td["rows"].append([r["name"], r["team"], g, int(r["tds"]), round(tdpg, 2)])
        tshare = round(100 * sum(r["tshare"]) / len(r["tshare"]), 1) if r["tshare"] else ""
        fantasy["rows"].append([
            r["name"], r["team"], r["pos"], "", "", r1(avg), r1(l3), trend, "",
            tshare, "", "", r1(l3 * 0.6 + avg * 0.4)])

    qb_td["rows"].sort(key=lambda x: -x[4])
    rb_td["rows"].sort(key=lambda x: -x[4])
    fantasy["rows"].sort(key=lambda x: -x[5])
    fantasy["rows"] = fantasy["rows"][:250]
    return {"QB TD/Game": qb_td, "RB TD/Game": rb_td, "Fantasy Projections": fantasy}


# ------------------------------------------------------- live roster overlay
# nflverse's weekly stats only know a player's team as of his last recorded
# game, so a trade or free-agent signing is invisible to every table above
# until he actually plays for his new team. ESPN's roster endpoint has no
# such lag — a signing shows up there the day it happens — so it is used
# here only to correct the Team column after the fact, never to add or
# drop a row.
ROSTER_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{id}/roster"
ESPN_TEAM_IDS = {
    "ARI": 22, "ATL": 1, "BAL": 33, "BUF": 2, "CAR": 29, "CHI": 3, "CIN": 4,
    "CLE": 5, "DAL": 6, "DEN": 7, "DET": 8, "GB": 9, "HOU": 34, "IND": 11,
    "JAX": 30, "KC": 12, "LV": 13, "LAC": 24, "LA": 14, "MIA": 15, "MIN": 16,
    "NE": 17, "NO": 18, "NYG": 19, "NYJ": 20, "PHI": 21, "PIT": 23, "SF": 25,
    "SEA": 26, "TB": 27, "TEN": 10, "WAS": 28,
}


def _name_key(n):
    n = (n or "").lower()
    n = re.sub(r"[.’‘',]", "", n)
    n = re.sub(r"[-–]", " ", n)
    n = re.sub(r"\s+(jr|sr|ii|iii|iv|v)$", "", n)
    return re.sub(r"\s+", " ", n).strip()


def current_teams():
    """Live name -> team abbreviation map from ESPN rosters. One request per
    team; a team that fails to answer just contributes nothing to the map
    rather than failing the whole build."""
    out = {}
    ok = 0
    for abbr, tid in ESPN_TEAM_IDS.items():
        try:
            # Deliberately no custom User-Agent here: ESPN's edge (Akamai)
            # blocks this particular endpoint for anything that looks like a
            # named client or a spoofed browser, but answers urllib's own
            # plain default string. Every other fetch in this file talks to
            # GitHub/nflverse instead, which does not care either way.
            req = urllib.request.Request(ROSTER_URL.format(id=tid))
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:                               # noqa: BLE001
            print("  roster %s failed: %s" % (abbr, str(e)[:100]), flush=True)
            continue
        ok += 1
        for group in d.get("athletes") or []:
            for p in group.get("items") or []:
                k = _name_key(p.get("displayName"))
                if k:
                    out[k] = abbr
    print("  live rosters: %d of %d teams answered, %d players"
          % (ok, len(ESPN_TEAM_IDS), len(out)), flush=True)
    return out


def apply_live_teams(tabs, teams_map):
    """Correct the Team column on the player tables using ESPN's current
    rosters, wherever it disagrees with what the stats-derived team was.
    Never adds, drops or reorders a row -- only fixes the one field that a
    box score cannot know before it happens."""
    if not teams_map:
        return 0
    changed = 0
    for name in ("QB TD/Game", "RB TD/Game", "Fantasy Projections"):
        tab = tabs.get(name)
        if not tab:
            continue
        cols = tab["columns"]
        pi, ti = cols.index("Player"), cols.index("Team")
        for row in tab["rows"]:
            live = teams_map.get(_name_key(row[pi]))
            if live and live != row[ti]:
                row[ti] = live
                changed += 1
    return changed


def main():
    now = datetime.now(timezone.utc)
    season = now.year if now.month >= 3 else now.year - 1

    games = rows_of(fetch_text(GAMES, False))
    records, offense, defense, recs = schedule_tables(games, season)
    if not recs:
        # Preseason: no completed games this year, so last season is the table.
        season -= 1
        records, offense, defense, recs = schedule_tables(games, season)
        print("no %d results yet; built from %d" % (season + 1, season), flush=True)
    if not recs:
        print("no completed games in either season; leaving the existing file alone")
        return 0

    tabs = {"Team Records": records, "Offense Rating": offense, "Defense Rating": defense}

    try:
        tabs.update(pbp_tables(rows_of(fetch_text(PBP.format(season=season), True)), recs))
    except Exception as e:                                   # noqa: BLE001
        print("play-by-play tables skipped: %s" % e, flush=True)

    try:
        cur_stats = rows_of(fetch_stats_text(season))
        prev_stats = None
        try:
            prev_stats = rows_of(fetch_stats_text(season - 1))
        except Exception as e:                               # noqa: BLE001
            print("  prior-season stats unavailable, early-season table will be thin: %s" % e, flush=True)
        tabs.update(player_tables(cur_stats, season, prev_stats))
    except Exception as e:                                   # noqa: BLE001
        print("player tables skipped: %s" % e, flush=True)

    try:
        changed = apply_live_teams(tabs, current_teams())
        if changed:
            print("  corrected team for %d rows using live ESPN rosters" % changed, flush=True)
    except Exception as e:                                   # noqa: BLE001
        print("live roster overlay skipped: %s" % e, flush=True)

    payload = {
        "generated": now.isoformat(timespec="seconds"),
        "season": season,
        "status": ("Reference tables rebuilt from nflverse: %d season, "
                   "play-by-play and weekly player stats." % season),
        "note": ("Same shape as the generator snapshot, so the app reads it in "
                 "place of the baked-in copy. Injuries come from ESPN live, the "
                 "matchup predictor is computed in the app, and the trade table "
                 "still comes from the snapshot."),
        "tabs": tabs,
    }

    os.makedirs("data", exist_ok=True)
    new = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    old = None
    if os.path.exists(OUT):
        with open(OUT) as fh:
            old = fh.read()

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
    with open(OUT, "w") as fh:
        fh.write(new + "\n")
    print("wrote %s (%d tables, %.1f KB)" % (OUT, len(tabs), len(new) / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
