#!/usr/bin/env python3
"""
Build data/salaries.json — DraftKings and FanDuel salaries for the next slate.

Why this exists: DraftKings answers a browser, FanDuel does not. Rather than
having half the app self-serve and half depend on pasting a CSV, both feeds are
fetched here (Actions is not a browser, so CORS is not in the way) and committed
as one small file the app reads from its own Pages site.

FanDuel has no public unauthenticated salary endpoint. Two routes, tried in
order:
  1. FANTASY_NERDS_KEY repository secret — api.fantasynerds.com/v1/nfl/dfs
     returns both books' salaries. Free tier is enough for one weekly call.
  2. FanDuel's own fixture-list API, in case the account-free path works from a
     server IP. Failure is logged, never fatal.

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
FN_DFS = "https://api.fantasynerds.com/v1/nfl/dfs?apikey={key}"
FD_FIXTURES = ("https://api.fanduel.com/fixture-lists?sport=nfl"
               "&include_fixtures=true&page_size=50")

POS_OK = {"QB", "RB", "WR", "TE", "DST", "D", "DEF"}


def get(url, headers=None, timeout=60):
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


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
def fanduel_via_nerds(key):
    d = get(FN_DFS.format(key=key))
    # The DFS endpoint returns one block per book; shapes vary by plan, so read
    # defensively rather than assuming a single layout.
    blocks = d.get("dfs") or d.get("salaries") or d
    rows = None
    if isinstance(blocks, dict):
        for k, v in blocks.items():
            if "fanduel" in str(k).lower() and isinstance(v, list):
                rows = v
                break
    if rows is None and isinstance(blocks, list):
        rows = [r for r in blocks if "fanduel" in str(r.get("site", "")).lower()] or blocks
    if not rows:
        raise RuntimeError("no FanDuel block in the Fantasy Nerds response")

    players, defs = [], {}
    for r in rows:
        pos = norm_pos(r.get("position") or r.get("pos"))
        sal = r.get("salary") or r.get("fanduel_salary") or r.get("fd_salary")
        try:
            sal = int(float(str(sal).replace("$", "").replace(",", "")))
        except (TypeError, ValueError):
            continue
        if pos not in ("QB", "RB", "WR", "TE", "DEF") or not sal:
            continue
        team = str(r.get("team") or "").upper()
        name = r.get("name") or r.get("player") or ""
        if pos == "DEF":
            if team:
                defs[team] = sal
            continue
        players.append({"id": r.get("playerId") or r.get("player_id"), "name": name,
                        "pos": pos, "team": team, "salary": sal})
    if len(players) < 30:
        raise RuntimeError("Fantasy Nerds returned only %d FanDuel players" % len(players))
    return {"book": "FanDuel", "cap": 60000, "slateId": None, "games": None,
            "single": False, "slate": "FanDuel main slate (Fantasy Nerds)",
            "kickoff": None, "count": len(players) + len(defs),
            "players": players, "def": defs}


def fanduel_direct():
    d = get(FD_FIXTURES, headers={
        "X-Brand": "FANDUEL",
        "X-Currency": "USD",
        "Accept": "application/json",
        "Referer": "https://www.fanduel.com/",
    })
    # If this ever answers without credentials the shape needs reading before it
    # can be trusted, so surface it rather than guessing at a parse.
    raise RuntimeError("FanDuel fixture list answered with keys %s — needs a parser"
                       % list(d)[:8])


def fanduel():
    key = os.environ.get("FANTASY_NERDS_KEY", "").strip()
    if key:
        try:
            fd = fanduel_via_nerds(key)
            print("  FanDuel via Fantasy Nerds: %d priced" % fd["count"], flush=True)
            return fd
        except Exception as e:                               # noqa: BLE001
            print("  Fantasy Nerds failed: %s" % e, flush=True)
    else:
        print("  FANTASY_NERDS_KEY not set", flush=True)
    try:
        return fanduel_direct()
    except Exception as e:                                   # noqa: BLE001
        print("  FanDuel direct failed: %s" % e, flush=True)
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

    fd = fanduel()
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
                 "draftables feed; FanDuel needs a server-side fetch because it "
                 "publishes nothing a browser may read. Captain and MVP rows are "
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
