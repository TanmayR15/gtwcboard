"""Points history for the progression chart (data/history.json).

- Every run takes a snapshot of the official standings and stores it under the number of
  the latest round that has results. Running again in the same round just refreshes it.
- Rounds before the first official snapshot are ESTIMATED from data/results.json using the
  published points tables (race positions only: poles and class rules are not included).
  Estimated snapshots are marked "estimated": true and are replaced by official ones as
  soon as an official snapshot exists for that round.
"""
import re
import unicodedata

SPRINT = [16.5, 12, 9.5, 7.5, 6, 4, 3, 2, 1, 0.5]
E3 = [25, 18, 15, 12, 10, 8, 6, 4, 2, 1]
E6 = [33, 24, 19, 15, 12, 9, 6, 4, 2, 1]
SPA_CHECK = [12, 9, 7, 6, 5, 4, 3, 2, 1, 0]
SPA_FINISH = E3
CHECK_RE = re.compile(r"after[\s-]+\d+\s*(hours?|hrs?|h)\b", re.I)
INTERIM_RE = re.compile(r"after[\s-]+\d+\s*[h:\s-]\s*30", re.I)


def norm(s):
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    for a, b in (("\u00f8", "o"), ("\u00e6", "ae"), ("\u0153", "oe"), ("\u00df", "ss"), ("\u0142", "l"), ("\u0111", "d")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _table_for(round_id, cup, session):
    name = (session.get("name") or "") + " " + (session.get("id") or "")
    if cup.startswith("Sprint"):
        return SPRINT
    if "spa" in round_id:
        return SPA_CHECK if CHECK_RE.search(name) else SPA_FINISH
    return E6 if "paul-ricard" in round_id else E3


def _counts(session):
    name = (session.get("name") or "") + " " + (session.get("id") or "")
    return not INTERIM_RE.search(name)


def estimate_snapshots(results):
    """Cumulative estimated points after each round with results, per cup (class 'overall')."""
    acc = {c: {"drivers": {}, "teams": {}} for c in ("overall", "sprint", "endurance")}
    names = {}
    snaps = []
    for rnd in sorted(results.get("rounds", []), key=lambda r: r["round"]):
        cup_key = "sprint" if (rnd.get("cup") or "").startswith("Sprint") else "endurance"
        for s in rnd.get("sessions", []):
            if not _counts(s):
                continue
            table = _table_for(rnd["id"], rnd.get("cup") or "", s)
            for row in s.get("rows", []):
                pos = row.get("pos")
                if not isinstance(pos, int) or not 1 <= pos <= len(table) or not table[pos - 1]:
                    continue
                pts = table[pos - 1]
                for d in re.split(r",\s*", row.get("drivers") or ""):
                    if d.strip():
                        k = norm(d)
                        names.setdefault(k, d.strip())
                        for c in ("overall", cup_key):
                            acc[c]["drivers"][k] = acc[c]["drivers"].get(k, 0) + pts
                if row.get("team"):
                    k = norm(row["team"])
                    names.setdefault(k, row["team"])
                    for c in ("overall", cup_key):
                        acc[c]["teams"][k] = acc[c]["teams"].get(k, 0) + pts
        snaps.append({
            "round": rnd["round"], "name": rnd.get("name", ""), "estimated": True,
            "tables": {c: {e: {k: round(v, 1) for k, v in acc[c][e].items()} for e in ("drivers", "teams")} for c in acc},
        })
    return snaps


def official_snapshot(standings, round_no, round_name):
    tables = {}
    for cup, classes in (standings.get("tables") or {}).items():
        for cls, ents in classes.items():
            for ent, rows in ents.items():
                if rows:
                    tables.setdefault(f"{cup}/{cls}", {})[ent] = {norm(r["name"]): r["total"] for r in rows}
    return {"round": round_no, "name": round_name, "estimated": False, "tables": tables}


def update_history(old, standings, results):
    """Return the new history dict. Estimated snapshots use keys 'cup' (class overall); official use 'cup/class'."""
    rounds = [r for r in results.get("rounds", []) if r.get("sessions")]
    hist = {"season": standings.get("season"), "snapshots": []}
    official = {s["round"]: s for s in (old or {}).get("snapshots", []) if not s.get("estimated")}
    if rounds and (standings.get("tables") or {}):
        last = max(rounds, key=lambda r: r["round"])
        official[last["round"]] = official_snapshot(standings, last["round"], last.get("name", ""))
    est = estimate_snapshots(results)
    for e in est:
        if e["round"] not in official:
            tables = {}
            for cup, ents in e["tables"].items():
                tables[f"{cup}/overall"] = ents
            e["tables"] = tables
            hist["snapshots"].append(e)
    hist["snapshots"] += list(official.values())
    hist["snapshots"].sort(key=lambda s: s["round"])
    return hist
