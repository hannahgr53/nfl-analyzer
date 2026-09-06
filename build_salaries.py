#!/usr/bin/env python3
"""
Build data/salaries.json — DraftKings and FanDuel salaries for the next slate.

Why this exists: DraftKings answers a browser, FanDuel does not. Rather than
having half the app self-serve and half depend on pasting a CSV, both feeds are
fetched here (Actions is not a browser, so CORS is not in the way) and committed
as one small file the app reads from its own Pages site.

FanDuel salaries come from the GraphQL endpoint behind FanDuel Research's own
public DFS projections page (fanduel.com/research/nfl/fantasy/dfs-projections).
No account, no key, no cookies — the page works logged out, so the same POST
works from a server. Two calls: one to list slates, one for the player pool.

This is an internal API, not a published one: FanDuel can change the schema
whenever they like. Every failure is logged and non-fatal, and the previous
FanDuel block is kept rather than blanked, so a schema change costs you slightly
old prices and a log line, not a broken app.

A missing FanDuel feed leaves the previous FanDuel block in place instead of
blanking it, so a bad week degrades to slightly old prices rather than none.

No third-party packages: urllib and json only.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

UA = "nfl-analyzer-salaries/1"
OUT = "data/salaries.json"

DK_LOBBY = "https://www.draftkings.com/lobby/getcontests?sport=NFL"
DK_DRAFTABLES = "https://api.draftkings.com/draftgroups/v1/draftgroups/{gid}/draftables?format=json"
FD_GRAPHQL = "https://www.fanduel.com/research/api/graphql"
FD_REFERER = "https://www.fanduel.com/research/nfl/fantasy/dfs-projections"

# Only the NflSkill and NflDefenseSt fragments the app actually reads. The live
# page asks for every sport in one query; there is no reason to copy that.
FD_PROJECTIONS_QUERY = """
query GetProjections($input: ProjectionsInput!) {
  getProjections(input: $input) {
    ... on NflSkill {
      player { numberFireId name position }
      team { abbreviation }
      gameInfo {
        homeTeam { abbreviation }
        awayTeam { abbreviation }
        gameTime
      }
      salary
      fantasy
    }
    ... on NflDefenseSt {
      player { numberFireId name position }
      team { abbreviation }
      gameInfo {
        homeTeam { abbreviation }
        awayTeam { abbreviation }
        gameTime
      }
      salary
      fantasy
    }
  }
}
"""

# The slate picker's own query, taken verbatim from the page's JS bundle. It
# returns id and name only — no game count, no start time — so the main slate is
# chosen by name. Passing `range` at all is an error; sport alone is correct.
# Names come back wrapped in literal single quotes: "'Main'", "'NE @ SEA'",
# "'1pm Only'", "'SuperFlex'", "'Sun-Mon'". "'Main'" is the full main slate.
FD_SLATES_QUERY = """
query GetSlates($sport: ProjectionSports!, $range: Range) {
  getSlates(sport: $sport, range: $range) {
    id
    name
  }
}
"""

POS_OK = {"QB", "RB", "WR", "TE", "DST", "D", "DEF"}


def get(url, headers=None, timeout=60):
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def post_json(url, body, headers=None, timeout=60):
    h = {"User-Agent": UA, "Accept": "*/*", "Content-Type": "application/json"}
    h.update(headers or {})
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def fd_graphql(query, variables, operation):
    d = post_json(FD_GRAPHQL,
                  {"query": query, "variables": variables, "operationName": operation},
                  headers={"Origin": "https://www.fanduel.com", "Referer": FD_REFERER})
    if d.get("errors"):
        msg = "; ".join(str((e or {}).get("message", e))[:160] for e in d["errors"][:3])
        raise RuntimeError("GraphQL error: %s" % msg)
    return d.get("data") or {}


def norm_pos(p):
    p = (p or "").upper()
    if p in ("DST", "D", "DEF", "D/ST"):
        return "DEF"
    return p


# --------------------------------------------------------------------- DraftKings
def draftkings():
    lobby = get(DK_LOBBY)
    groups = lobby.get("DraftGroups") or []
    now = datetime.now(timezone.utc).timestamp() * 1000

    cands = []
    for g in groups:
        if (g.get("ContestType") or {}).get("Sport") != "NFL" and g.get("Sport") != "NFL":
            pass  # the lobby is already NFL-scoped; keep everything it returns
        start = g.get("StartDateEst") or g.get("StartDate") or ""
        # "/Date(1757102400000)/" or an ISO string, depending on the field.
        ms = None
        if "Date(" in str(start):
            try:
                ms = int(str(start).split("Date(")[1].split(")")[0])
            except (IndexError, ValueError):
                ms = None
        elif start:
            try:
                ms = datetime.fromisoformat(str(start).replace("Z", "+00:00")).timestamp() * 1000
            except ValueError:
                ms = None
        n = g.get("GameCount") or g.get("GameTypeGameCount") or 0
        cands.append({"gid": g.get("DraftGroupId"), "ms": ms, "n": n,
                      "tag": g.get("DraftGroupTag") or "", "suffix": g.get("ContestStartTimeSuffix") or ""})

    # Soonest slate that has not started, biggest first inside a window; the
    # main slate before the showdowns.
    ahead = [c for c in cands if c["gid"] and c["ms"] and c["ms"] > now - 4 * 3600e3]
    behind = [c for c in cands if c["gid"] and (not c["ms"] or c["ms"] <= now - 4 * 3600e3)]
    ahead.sort(key=lambda c: (c["ms"], -c["n"]))
    behind.sort(key=lambda c: -(c["ms"] or 0))
    order = ahead + behind

    for c in order[:6]:
        try:
            d = get(DK_DRAFTABLES.format(gid=c["gid"]))
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
            print("  draftgroup %s failed: %s" % (c["gid"], e), flush=True)
            continue
        players, defs, seen = [], {}, set()
        for p in d.get("draftables") or []:
            pos = norm_pos(p.get("position"))
            sal = p.get("salary")
            if pos not in ("QB", "RB", "WR", "TE", "DEF") or not sal:
                continue
            # Showdown groups list the same man twice (CPT and FLEX). Keep the
            # cheaper, non-captain row; the app applies the 1.5x itself.
            key = (p.get("playerId"), pos)
            if key in seen:
                continue
            seen.add(key)
            name = p.get("displayName") or ""
            team = (p.get("teamAbbreviation") or "").upper()
            if pos == "DEF":
                if team and (team not in defs or sal < defs[team]):
                    defs[team] = sal
                continue
            players.append({"id": p.get("playerId"), "name": name, "pos": pos,
                            "team": team, "salary": sal})
        if len(players) >= 30:
            when = ""
            if c["ms"]:
                when = datetime.fromtimestamp(c["ms"] / 1000, timezone.utc).strftime("%a %b %d")
            return {
                "book": "DraftKings", "cap": 50000, "slateId": c["gid"],
                "games": c["n"], "single": c["n"] == 1,
                "slate": "%s-game DraftKings slate%s" % (c["n"], (", " + when) if when else ""),
                "kickoff": c["ms"], "count": len(players) + len(defs),
                "players": players, "def": defs,
            }
        print("  draftgroup %s had %d priced players; trying the next" % (c["gid"], len(players)), flush=True)
    raise RuntimeError("no DraftKings draft group had a full priced slate")


# ----------------------------------------------------------------------- FanDuel
def fd_name(r):
    return str(r.get("name") or "").strip().strip("'").strip('"').strip()


def fd_pick_slate(rows):
    """'Main' first, then other full-field slates. Single-game slates ('NE @ SEA'),
    alternate scoring ('SuperFlex', 'Snake Draft') and partial windows ('1pm Only',
    'Wed-Thu') go to the back — the app models the main slate."""
    def rank(r):
        n = fd_name(r)
        low = n.lower()
        if low == "main":
            return (0, n)
        if " @ " in n or " vs " in low:            # single game
            return (3, n)
        if any(w in low for w in ("superflex", "snake", "showdown", "captain", "mvp")):
            return (4, n)
        if "only" in low or "-" in n:              # partial windows
            return (2, n)
        return (1, n)
    return sorted([r for r in rows if r.get("id")], key=rank)


def fd_slate_id():
    """Resolve the NFL slate id, or None if the slate list cannot be read."""
    override = os.environ.get("FD_SLATE_ID", "").strip()
    if override:
        print("  FanDuel slate pinned by FD_SLATE_ID=%s" % override, flush=True)
        return override, "pinned"

    # sport alone; supplying `range` is rejected by the schema.
    for variables in ({"sport": "NFL"},):
        try:
            data = fd_graphql(FD_SLATES_QUERY, variables, "GetSlates")
        except Exception as e:                               # noqa: BLE001
            print("  slates %s failed: %s" % (variables, str(e)[:140]), flush=True)
            continue
        rows = data.get("getSlates") or []
        if not rows:
            continue
        ordered = fd_pick_slate(rows)
        print("  FanDuel slates: %s" % ", ".join(
            "%s=%s" % (r.get("id"), fd_name(r)) for r in ordered[:6]), flush=True)
        if ordered:
            pick = ordered[0]
            return str(pick["id"]), fd_name(pick)
    return None, None


def fanduel_via_research(slate_id):
    data = fd_graphql(FD_PROJECTIONS_QUERY,
                      {"input": {"type": "DAILY", "position": "NFL_SKILL",
                                 "sport": "NFL", "slateId": str(slate_id)}},
                      "GetProjections")
    rows = data.get("getProjections") or []

    # Team defenses are a separate position enum in the same query. The schema's
    # NFL position enums are NFL_SKILL, NFL_D_ST, NFL_KICKER, NFL_IDP; only the
    # first two matter here.
    dst = []
    try:
        dst = fd_graphql(FD_PROJECTIONS_QUERY,
                         {"input": {"type": "DAILY", "position": "NFL_D_ST",
                                    "sport": "NFL", "slateId": str(slate_id)}},
                         "GetProjections").get("getProjections") or []
    except Exception as e:                                   # noqa: BLE001
        print("  FanDuel D/ST fetch failed: %s" % str(e)[:140], flush=True)

    players, defs, kick = [], {}, None
    # Rows from the D/ST call are defenses whatever their position field says.
    for r, force_def in [(r, False) for r in rows] + [(r, True) for r in dst]:
        p = r.get("player") or {}
        pos = "DEF" if force_def else norm_pos(p.get("position"))
        try:
            sal = int(float(str(r.get("salary")).replace("$", "").replace(",", "")))
        except (TypeError, ValueError):
            continue
        if pos not in ("QB", "RB", "WR", "TE", "DEF") or not sal:
            continue
        team = str((r.get("team") or {}).get("abbreviation") or "").upper()
        gi = r.get("gameInfo") or {}
        t = gi.get("gameTime")
        if t and kick is None:
            try:
                kick = datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp() * 1000
            except ValueError:
                pass
        if pos == "DEF":
            if team and (team not in defs or sal < defs[team]):
                defs[team] = sal
            continue
        players.append({"id": p.get("numberFireId"), "name": p.get("name") or "",
                        "pos": pos, "team": team, "salary": sal})

    if len(players) < 30:
        raise RuntimeError("FanDuel Research returned only %d priced players" % len(players))

    teams = set()
    for r in list(rows) + list(dst):
        gi = r.get("gameInfo") or {}
        for side in ("homeTeam", "awayTeam"):
            ab = ((gi.get(side) or {}).get("abbreviation") or "").upper()
            if ab:
                teams.add(ab)
    games = len(teams) // 2 or None
    when = ""
    if kick:
        when = datetime.fromtimestamp(kick / 1000, timezone.utc).strftime("%a %b %d")

    return {"book": "FanDuel", "cap": 60000, "slateId": str(slate_id),
            "games": games, "single": games == 1,
            "slate": "%sFanDuel slate%s" % (("%s-game " % games) if games else "",
                                            (", " + when) if when else ""),
            "kickoff": kick, "count": len(players) + len(defs),
            "players": players, "def": defs}


def fanduel(prev_fd=None):
    slate_id, name = fd_slate_id()
    tried = []
    if slate_id:
        tried.append(slate_id)
    # If the slate call cannot be read, last week's id is worth one attempt: an
    # expired slate errors or returns nothing rather than returning wrong prices.
    if not slate_id and (prev_fd or {}).get("slateId"):
        tried.append(str(prev_fd["slateId"]))
        print("  no slate list; retrying last week's slate %s" % tried[-1], flush=True)

    for sid in tried:
        try:
            fd = fanduel_via_research(sid)
            if name and name != "pinned":
                fd["slate"] = "FanDuel %s" % name
            print("  FanDuel via Research GraphQL: %s, %d priced"
                  % (fd["slate"], fd["count"]), flush=True)
            return fd
        except Exception as e:                               # noqa: BLE001
            print("  FanDuel slate %s failed: %s" % (sid, str(e)[:200]), flush=True)
    if not tried:
        print("  FanDuel skipped: no slate id could be resolved", flush=True)
    return None


def main():
    now = datetime.now(timezone.utc)
    old = {}
    if os.path.exists(OUT):
        try:
            with open(OUT) as f:
                old = json.load(f)
        except (OSError, ValueError):
            old = {}
    prev = old.get("sources") or {}

    sources = {}
    try:
        sources["dk"] = draftkings()
        print("DraftKings: %s, %d priced" % (sources["dk"]["slate"], sources["dk"]["count"]), flush=True)
    except Exception as e:                                   # noqa: BLE001
        print("DraftKings failed: %s" % e, flush=True)
        if prev.get("dk"):
            sources["dk"] = dict(prev["dk"], stale=True)

    fd = fanduel(prev.get("fd"))
    if fd:
        sources["fd"] = fd
    elif prev.get("fd"):
        sources["fd"] = dict(prev["fd"], stale=True)

    if not sources:
        print("neither book answered; leaving the existing file alone")
        return 0

    payload = {
        "generated": now.isoformat(timespec="seconds"),
        "books": sorted(sources),
        "note": ("Salaries for the next slate. DraftKings comes from its own "
                 "draftables feed; FanDuel from the public FanDuel Research "
                 "projections GraphQL endpoint. Captain and MVP rows are "
                 "dropped — the app applies the 1.5x multiplier itself."),
        "sources": sources,
    }

    os.makedirs("data", exist_ok=True)
    new = json.dumps(payload, indent=1, sort_keys=True)

    def strip(s):
        try:
            d = json.loads(s) if isinstance(s, str) else s
            d = dict(d)
            d.pop("generated", None)
            return json.dumps(d, sort_keys=True)
        except Exception:                                    # noqa: BLE001
            return str(s)

    if old and strip(old) == strip(payload):
        print("no change")
        return 0
    with open(OUT, "w") as f:
        f.write(new + "\n")
    print("wrote %s (%.1f KB, books: %s)" % (OUT, len(new) / 1024, ", ".join(sorted(sources))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
