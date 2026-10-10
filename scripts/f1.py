#!/usr/bin/env python3
"""Formula 1 data for the Series dropdown -> data/f1/*.json

Source: the Jolpica F1 API (a free, Ergast-compatible service), not the f1 website, so there is
no HTML to scrape. It asks for a handful of small JSON files per run and waits between requests.

Writes (same shapes the site already reads for GTWC):
  data/f1/standings.json   drivers and constructors ("teams"), tables.overall.overall
  data/f1/results.json     Grand Prix and Sprint classifications, round by round
  data/f1/schedule.json    sessions in UTC (end times are the usual scheduled lengths)
  data/f1/history.json     exact standings after every round, for the progression chart

Run:  python scripts/f1.py            (everything)
      python scripts/f1.py --only schedule   (standings | results | schedule | history)
"""
import argparse
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from history import norm  # noqa: E402

BASE = os.environ.get("F1_API", "https://api.jolpi.ca/ergast/f1").rstrip("/")
SEASON = int(os.environ.get("F1_SEASON", "2026"))
DELAY = float(os.environ.get("F1_DELAY", "1.0"))     # seconds between requests (the API allows ~4 per second)
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "f1"
HEADERS = {"User-Agent": "pitboard-personal-project/1.0 (low-volume, personal use)"}
MAX_CONSECUTIVE_FAILS = 4


class Blocked(Exception):
    """Raised when the API keeps refusing us; the run stops instead of pushing on."""


_fails = 0


def get_json(path, retries=3):
    """GET one API path (like '/2026.json'). Returns the 'MRData' dict, or None."""
    global _fails
    url = BASE + path
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=(10, 25))
            if r.status_code == 200:
                _fails = 0
                time.sleep(DELAY)
                return r.json().get("MRData", {})
            print(f"  ! {r.status_code} for {url} (attempt {attempt})", flush=True)
            if r.status_code in (401, 403):
                break
            if r.status_code in (429, 503):
                try:
                    wait = min(int(r.headers.get("Retry-After", "")), 120)
                except ValueError:
                    wait = 15 * attempt
                print(f"  ... asked to slow down, waiting {wait}s", flush=True)
                time.sleep(wait)
                continue
        except (requests.RequestException, ValueError) as e:
            print(f"  ! {type(e).__name__} for {url} (attempt {attempt})", flush=True)
        time.sleep(DELAY * attempt)
    _fails += 1
    if _fails >= MAX_CONSECUTIVE_FAILS:
        raise Blocked(f"{_fails} requests in a row failed; stopping so we do not hammer the API")
    return None


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def strip_updated(d):
    return {k: v for k, v in d.items() if k != "updated"}


def num(v):
    f = float(v)
    return int(f) if f == int(f) else f


def short_name(race_name):
    return (race_name or "").replace("Grand Prix", "GP").strip()


def slug(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def driver_name(d):
    return f"{d.get('givenName', '')} {d.get('familyName', '')}".strip()


# ----------------------------------------------------------------- schedule
# (API key, label, kind, scheduled length in minutes)
SESSION_KEYS = [
    ("FirstPractice", "Practice 1", "practice", 60),
    ("SecondPractice", "Practice 2", "practice", 60),
    ("ThirdPractice", "Practice 3", "practice", 60),
    ("SprintQualifying", "Sprint Qualifying", "qualifying", 45),
    ("SprintShootout", "Sprint Qualifying", "qualifying", 45),
    ("Sprint", "Sprint", "race", 30),
    ("Qualifying", "Qualifying", "qualifying", 60),
]
WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def parse_when(block):
    """{'date': '2026-03-07', 'time': '05:00:00Z'} -> datetime in UTC, or None."""
    if not block or not block.get("date"):
        return None
    t = (block.get("time") or "").rstrip("Z") or "00:00:00"
    try:
        return datetime.strptime(f"{block['date']} {t[:5]}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def session_rec(label, kind, start, minutes):
    return {"name": label, "kind": kind, "day": WEEKDAYS[start.weekday()],
            "utc": start.strftime("%Y-%m-%dT%H:%M:00Z"),
            "end_utc": (start + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:00Z")}


def build_schedule(races):
    rounds = []
    for race in races:
        sessions = []
        for key, label, kind, minutes in SESSION_KEYS:
            when = parse_when(race.get(key))
            if when:
                sessions.append(session_rec(label, kind, when, minutes))
        main = parse_when(race)
        if main:
            sessions.append(session_rec("Race", "race", main, 120))
        sessions.sort(key=lambda s: s["utc"])
        loc = (race.get("Circuit") or {}).get("Location") or {}
        first = sessions[0]["utc"][:10] if sessions else race.get("date", "")
        rounds.append({"round": int(race["round"]), "name": short_name(race.get("raceName")),
                       "country": loc.get("country", ""), "start": first, "end": race.get("date", first),
                       "sessions": sessions})
    return {"season": SEASON, "rounds": rounds}


def scrape_schedule(old):
    data = get_json(f"/{SEASON}.json?limit=100")
    races = ((data or {}).get("RaceTable") or {}).get("Races") or []
    if not races:
        print("schedule: nothing returned, keeping the old file")
        return old, 0
    new = build_schedule(races)
    print(f"schedule: {len(new['rounds'])} rounds, {sum(len(r['sessions']) for r in new['rounds'])} sessions")
    return new, len(new["rounds"])


# ------------------------------------------------------------------ results
def rows_from(results):
    rows = []
    for x in results:
        t = (x.get("Time") or {}).get("time")
        rows.append({
            "pos": int(x.get("position") or 0), "car": str(x.get("number", "")),
            "drivers": driver_name(x.get("Driver") or {}),
            "team": (x.get("Constructor") or {}).get("name", ""), "model": "",
            "time": t or x.get("status", ""), "laps": int(x.get("laps") or 0), "cls": "",
        })
    return rows


def scrape_results(old, schedule):
    have = {r["round"]: r for r in old.get("rounds", [])}
    done_rounds = sorted(have)
    refresh = set(done_rounds[-2:])                         # penalties can change a recent classification
    now = datetime.now(timezone.utc)
    ok = 0
    for rnd in schedule.get("rounds", []):
        n = rnd["round"]
        started = [s for s in rnd["sessions"] if s["kind"] == "race"]
        if not started or datetime.strptime(started[0]["utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) > now:
            continue
        if n in have and n not in refresh and have[n].get("sessions"):
            continue
        print(f"results round {n} {rnd['name']}", flush=True)
        sessions = []
        if any(s["name"] == "Sprint" for s in rnd["sessions"]):
            d = get_json(f"/{SEASON}/{n}/sprint.json?limit=100")
            races = ((d or {}).get("RaceTable") or {}).get("Races") or []
            if races and races[0].get("SprintResults"):
                sessions.append({"id": "sprint", "name": "Sprint", "rows": rows_from(races[0]["SprintResults"])})
        d = get_json(f"/{SEASON}/{n}/results.json?limit=100")
        races = ((d or {}).get("RaceTable") or {}).get("Races") or []
        if races and races[0].get("Results"):
            sessions.append({"id": "main-race", "name": "Race", "rows": rows_from(races[0]["Results"])})
        if sessions:
            have[n] = {"id": slug(rnd["name"]), "round": n, "name": rnd["name"],
                       "cup": "Sprint Weekend" if any(s["id"] == "sprint" for s in sessions) else "Grand Prix",
                       "sessions": sessions}
            ok += 1
            print(f"  {[(s['name'], len(s['rows'])) for s in sessions]}", flush=True)
        else:
            print("  - no classification yet")
    old["rounds"] = sorted(have.values(), key=lambda r: r["round"])
    return old, ok


# ---------------------------------------------------------------- standings
def parse_standings(path_tail):
    """Returns (round_number, driver_rows, team_rows) for '/2026/driverstandings.json'-style paths."""
    d = get_json(path_tail)
    st = (d or {}).get("StandingsTable") or {}
    lists = st.get("StandingsLists") or []
    if not lists:
        return None
    return int(lists[0].get("round") or st.get("round") or 0), lists[0]


def driver_rows(lst):
    rows = []
    for x in lst.get("DriverStandings", []):
        cons = x.get("Constructors") or [{}]
        rows.append({"pos": int(x.get("position") or x.get("positionText") or 0), "name": driver_name(x.get("Driver") or {}),
                     "team": cons[-1].get("name", ""), "total": num(x.get("points", 0)), "wins": int(x.get("wins") or 0)})
    return rows


def team_rows(lst):
    return [{"pos": int(x.get("position") or 0), "name": (x.get("Constructor") or {}).get("name", ""),
             "total": num(x.get("points", 0)), "wins": int(x.get("wins") or 0)}
            for x in lst.get("ConstructorStandings", [])]


def scrape_standings(old):
    dd = parse_standings(f"/{SEASON}/driverstandings.json?limit=100")
    tt = parse_standings(f"/{SEASON}/constructorstandings.json?limit=100")
    drivers = driver_rows(dd[1]) if dd else []
    teams = team_rows(tt[1]) if tt else []
    if not drivers and not teams:
        print("standings: nothing returned, keeping the old file")
        return old, 0, 0
    old["tables"] = {"overall": {"overall": {"drivers": drivers, "teams": teams}}}
    rnd = max(dd[0] if dd else 0, tt[0] if tt else 0)
    print(f"standings: {len(drivers)} drivers, {len(teams)} constructors, after round {rnd}")
    return old, 1, rnd


# ------------------------------------------------------------------ history
def scrape_history(old, schedule, latest_round):
    """Exact standings after each completed round (the API keeps them for every round)."""
    snaps = {s["round"]: s for s in (old or {}).get("snapshots", []) if not s.get("estimated")}
    names = {r["round"]: r["name"] for r in schedule.get("rounds", [])}
    for n in range(1, latest_round + 1):
        if n in snaps and n != latest_round:               # old rounds never change; refresh only the latest
            continue
        dd = parse_standings(f"/{SEASON}/{n}/driverstandings.json?limit=100")
        tt = parse_standings(f"/{SEASON}/{n}/constructorstandings.json?limit=100")
        if not dd and not tt:
            continue
        snaps[n] = {"round": n, "name": names.get(n, f"Round {n}"), "estimated": False, "tables": {"overall/overall": {
            "drivers": {norm(r["name"]): r["total"] for r in (driver_rows(dd[1]) if dd else [])},
            "teams": {norm(r["name"]): r["total"] for r in (team_rows(tt[1]) if tt else [])}}}}
        print(f"history: round {n} saved", flush=True)
    return {"season": SEASON, "snapshots": [snaps[k] for k in sorted(snaps)]}


# --------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["standings", "results", "schedule", "history"])
    args = ap.parse_args()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    files = {n: DATA_DIR / f"{n}.json" for n in ("standings", "results", "schedule", "history")}
    old = {"standings": read_json(files["standings"], {"season": SEASON, "tables": {}}),
           "results": read_json(files["results"], {"season": SEASON, "rounds": []}),
           "schedule": read_json(files["schedule"], {"season": SEASON, "rounds": []}),
           "history": read_json(files["history"], {"season": SEASON, "snapshots": []})}
    new = json.loads(json.dumps(old))
    want = lambda n: args.only in (None, n)       # noqa: E731
    blocked = False
    got = 0
    latest = 0
    try:
        if want("schedule") or want("results") or want("history"):
            if want("schedule") or not new["schedule"].get("rounds"):
                new["schedule"], c = scrape_schedule(new["schedule"])
                got += c
        if want("standings") or want("history"):
            new["standings"], c, latest = scrape_standings(new["standings"])
            got += c
        if want("results"):
            new["results"], c = scrape_results(new["results"], new["schedule"])
            got += c
        if want("history") and latest:
            new["history"] = scrape_history(new["history"], new["schedule"], latest)
    except Blocked as e:
        blocked = True
        print(f"STOPPED: {e}", flush=True)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    changed = False
    for name, path in files.items():
        new[name]["season"] = SEASON
        if strip_updated(new[name]) != strip_updated(old[name]):
            new[name]["updated"] = stamp
            path.write_text(json.dumps(new[name], indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
            changed = True
            print(f"wrote f1/{path.name}")
    if not changed:
        print("f1: no changes")
    if blocked or (args.only is None and got == 0):
        print("ERROR: no F1 data could be read.")
        sys.exit(1)


if __name__ == "__main__":
    main()
