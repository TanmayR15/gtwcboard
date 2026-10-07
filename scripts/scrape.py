#!/usr/bin/env python3
"""Fetch GTWC Europe standings and race results and write data/*.json.

Safety rules:
- A table is only replaced when the new fetch parsed at least one valid row.
- If a fetch or parse fails, the old data for that table is kept.
- If NOTHING could be parsed, the script exits with an error so the GitHub
  Action fails and emails you, instead of silently committing empty data.
- Files are only rewritten when the content actually changed.
"""
import argparse
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

BASE = "https://www.gt-world-challenge-europe.com"
SEASON = int(os.environ.get("GTWC_SEASON", "2026"))
DELAY = float(os.environ.get("GTWC_DELAY", "1.5"))   # seconds between requests
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
HEADERS = {"User-Agent": "gtwc-standings-personal-project/1.0 (low-volume, personal use)"}

# Real filter values read from the official page's dropdown (verified):
# filter_standing_type = <cup id>_<class id>_<teams|drivers>
CUP_IDS = {"overall": 0, "endurance": 42, "sprint": 43}
CLASS_IDS = {"overall": 0, "gold": 81, "silver": 82, "bronze": 80}
# filter_season_id: 26 = 2026, 27 = 2027, ... (year minus 2000)
SEASON_ID = int(os.environ.get("GTWC_SEASON_ID", str(SEASON - 2000)))

# Which cup each 2026 round counts for (from the official standings header).
CUP_BY_SLUG = {
    "circuit-paul-ricard": "Endurance Cup",
    "brands-hatch": "Sprint Cup",
    "monza": "Endurance Cup",
    "crowdstrike-24-hours-of-spa": "Endurance Cup",
    "misano": "Sprint Cup",
    "magny-cours": "Sprint Cup",
    "nurburgring": "Endurance Cup",
    "zandvoort": "Sprint Cup",
    "barcelona": "Sprint Cup",
    "portimao": "Endurance Cup",
}

NUM_RE = re.compile(r"^\d+(\.\d+)?$")
RACE_SESSION_RE = re.compile(r"race|hour|\d+\s*hr", re.I)
SKIP_SESSION_RE = re.compile(r"qualif|practice|warm|test|pit|superpole", re.I)


# ---------------------------------------------------------------- helpers
def ascii_slug(s: str) -> str:
    s = unquote(s)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return s.lower()


class Blocked(Exception):
    """Raised when the site keeps refusing us; the run stops instead of pushing on."""


MAX_CONSECUTIVE_FAILS = int(os.environ.get("GTWC_MAX_FAILS", "4"))
_consecutive_fails = 0


def _retry_after_seconds(resp, attempt):
    """How long to wait after a 429/503: the site's Retry-After, else a growing default."""
    try:
        return min(int(resp.headers.get("Retry-After", "")), 120)
    except ValueError:
        return 30 * attempt


def fetch(url: str, retries: int = 3):
    """GET politely. Returns HTML text, or None for this page.

    - waits DELAY seconds after every successful request
    - 429/503 (slow down): waits as the site asks, then retries
    - 401/403 (blocked): no retry, we do not push against a block
    - after several pages in a row fail, raises Blocked and the run stops
    """
    global _consecutive_fails
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=(10, 25))
            if r.status_code == 200:
                _consecutive_fails = 0
                time.sleep(DELAY)
                return r.text
            print(f"  ! {r.status_code} for {url} (attempt {attempt})", flush=True)
            if r.status_code in (401, 403):
                break
            if r.status_code in (429, 503):
                wait = _retry_after_seconds(r, attempt)
                print(f"  ... site asked us to slow down, waiting {wait}s", flush=True)
                time.sleep(wait)
                continue
        except requests.RequestException as e:
            print(f"  ! {type(e).__name__} for {url} (attempt {attempt})", flush=True)
        time.sleep(DELAY * attempt)
    _consecutive_fails += 1
    if _consecutive_fails >= MAX_CONSECUTIVE_FAILS:
        raise Blocked(f"{_consecutive_fails} pages in a row failed; stopping so we do not hammer the site")
    return None


def cell_text(cell, sep=" "):
    return re.sub(r"\s+", " ", cell.get_text(sep, strip=True)).strip()


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def strip_updated(d):
    return {k: v for k, v in d.items() if k != "updated"}


# --------------------------------------------------------------- standings
def parse_standings(html: str, entity: str):
    """Return rows [{pos, name, total, (team)}] from a standings page."""
    soup = BeautifulSoup(html, "lxml")
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        header_idx = None
        for i, tr in enumerate(rows):
            texts = [cell_text(c).upper() for c in tr.find_all(["th", "td"])]
            if "POS" in texts and "TOTAL" in texts:
                header_idx = i
                break
        if header_idx is None:
            continue
        out = []
        for tr in rows[header_idx + 1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) < 3:
                continue
            texts = [cell_text(c) for c in cells]
            if not texts[0].isdigit():
                continue
            name = texts[1]
            if not name:
                continue
            total = None
            team = ""
            for t in texts[2:]:
                if NUM_RE.match(t):
                    total = float(t)
                    break
                if t and not team:
                    team = t           # a text cell before the points = team column
            if total is None:
                continue
            row = {"pos": int(texts[0]), "name": name, "total": total}
            if entity == "drivers" and team:
                row["team"] = team
            out.append(row)
        if out:
            return out
    return []


def selected_standing_type(html: str):
    """Value of the currently selected standing-type option, or None if unmarked.

    The site silently shows the default Overall table when it does not
    recognise a filter value, so we check that we got what we asked for.
    """
    soup = BeautifulSoup(html, "lxml")
    sel = soup.find("select", id="filter_standing_type")
    if sel is None:
        return None
    opt = sel.find("option", selected=True)
    return opt.get("value") if opt is not None else None


def scrape_standings(old):
    tables = old.get("tables", {})
    parsed_ok = 0
    failures = 0
    for cup, cup_id in CUP_IDS.items():
        for cls, cls_id in CLASS_IDS.items():
            for entity in ("drivers", "teams"):
                value = f"{cup_id}_{cls_id}_{entity}"
                url = f"{BASE}/standings?filter_season_id={SEASON_ID}&filter_standing_type={value}"
                print(f"standings {cup}/{cls}/{entity}", flush=True)
                html = fetch(url)
                if html is None:
                    failures += 1
                    continue
                if str(SEASON) not in html[:8000]:
                    print(f"  ! page does not look like season {SEASON}; keeping old data", flush=True)
                    failures += 1
                    continue
                selected = selected_standing_type(html)
                if selected is not None and selected != value:
                    print(f"  ! site returned '{selected}' instead of '{value}'; keeping old data", flush=True)
                    failures += 1
                    continue
                rows = parse_standings(html, entity)
                if not rows:
                    print("  - no rows parsed; keeping old data", flush=True)
                    continue
                tables.setdefault(cup, {}).setdefault(cls, {})[entity] = rows
                parsed_ok += 1
    old["tables"] = tables
    return old, parsed_ok, failures


# ----------------------------------------------------------------- results
def map_columns(headers):
    cols = {}
    for i, h in enumerate(headers):
        h = h.lower().strip()
        if h in ("pos", "pos.", "position") and "pos" not in cols:
            cols["pos"] = i
        elif h in ("car #", "car#", "car no", "car no.", "no", "no.", "#", "nr", "nr.") and "car" not in cols:
            cols["car"] = i
        elif "driver" in h and "drivers" not in cols:
            cols["drivers"] = i
        elif ("team" in h or "entrant" in h) and "team" not in cols:
            cols["team"] = i
        elif h in ("car", "vehicle", "model") and "model" not in cols:
            cols["model"] = i
        elif h in ("time", "total time", "race time") and "time" not in cols:
            cols["time"] = i
        elif "lap" in h and "best" not in h and "laps" not in cols:
            cols["laps"] = i
        elif h in ("class", "cup", "cat", "category") and "cls" not in cols:
            cols["cls"] = i
    return cols


def parse_results(html: str):
    soup = BeautifulSoup(html, "lxml")
    out = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        cols = None
        for tr in rows:
            cells = tr.find_all(["th", "td"])
            texts = [cell_text(c, ", ") for c in cells]
            if cols is None:
                cand = map_columns(texts)
                if "drivers" in cand and "team" in cand:
                    cols = cand
                continue
            if len(texts) <= max(cols.values()):
                continue

            def get(key):
                return texts[cols[key]] if key in cols else ""

            if not get("drivers"):
                continue
            pos_txt = get("pos")
            laps_txt = get("laps")
            out.append({
                "pos": int(pos_txt) if pos_txt.isdigit() else len(out) + 1,
                "car": get("car"),
                "drivers": get("drivers"),
                "team": get("team"),
                "model": get("model"),
                "time": get("time"),
                "laps": int(laps_txt) if laps_txt.isdigit() else None,
                "cls": get("cls"),
            })
    return out


def discover_rounds(html: str):
    """Ordered list of (slug, display name) for 2026 meetings, test days excluded."""
    soup = BeautifulSoup(html, "lxml")
    pat = re.compile(rf"/results/{SEASON}/([^/?#]+)$")
    seen, rounds = set(), []
    for a in soup.find_all("a", href=True):
        m = pat.search(a["href"])
        if not m:
            continue
        raw = m.group(1)
        key = ascii_slug(raw)
        if "test" in key or key in seen:
            continue
        seen.add(key)
        rounds.append((raw, a.get_text(strip=True) or raw.replace("-", " ").title()))
    return rounds


def discover_sessions(html: str, round_raw: str):
    """Race sessions for one meeting, as (slug, name)."""
    soup = BeautifulSoup(html, "lxml")
    pat = re.compile(rf"/results/{SEASON}/{re.escape(unquote(round_raw))}/([^/?#]+)$", re.I)
    found = {}
    for tag in soup.find_all(["a", "option"]):
        ref = unquote(tag.get("href") or tag.get("value") or "")
        m = pat.search(ref)
        if not m:
            continue
        slug = m.group(1)
        name = tag.get_text(strip=True) or slug.replace("-", " ").title()
        if SKIP_SESSION_RE.search(slug) or SKIP_SESSION_RE.search(name):
            continue
        if not (RACE_SESSION_RE.search(slug) or RACE_SESSION_RE.search(name)):
            continue
        found.setdefault(slug, name)
    return list(found.items())


def scrape_results(old):
    rounds_old = {r["id"]: r for r in old.get("rounds", [])}
    index_html = fetch(f"{BASE}/results")
    if index_html is None:
        print("results index could not be fetched")
        return old, 0, 1
    discovered = discover_rounds(index_html)
    print(f"found {len(discovered)} meetings")

    # Re-fetch the two most recent rounds that already have data (penalties can
    # change a classification later); older complete rounds are left alone.
    have_data = [i for i, (raw, _) in enumerate(discovered)
                 if rounds_old.get(ascii_slug(raw), {}).get("sessions")]
    refresh = set(have_data[-2:])

    parsed_ok = failures = 0
    for idx, (raw, name) in enumerate(discovered, start=1):
        rid = ascii_slug(raw)
        existing = rounds_old.get(rid)
        if existing and existing.get("sessions") and (idx - 1) not in refresh:
            continue
        print(f"results {rid}", flush=True)
        page = fetch(f"{BASE}/results/{SEASON}/{raw}")
        if page is None:
            failures += 1
            continue
        found = discover_sessions(page, raw)
        print(f"  race sessions found: {[s[0] for s in found]}", flush=True)
        sessions = []
        for slug, sname in found:
            print(f"  fetching {slug} ...", flush=True)
            html = fetch(f"{BASE}/results/{SEASON}/{raw}/{slug}")
            if html is None:
                failures += 1
                continue
            rows = parse_results(html)
            print(f"    {len(rows)} rows", flush=True)
            if rows:
                sessions.append({"id": slug, "name": sname, "rows": rows})
                parsed_ok += 1
        if sessions:
            rounds_old[rid] = {
                "id": rid, "round": idx, "name": name,
                "cup": CUP_BY_SLUG.get(rid, ""), "sessions": sessions,
            }
        else:
            print("  - no race classification yet")
    old["rounds"] = sorted(rounds_old.values(), key=lambda r: r["round"])
    return old, parsed_ok, failures


# -------------------------------------------------------------------- main
def verify_overall(tables):
    """Sanity check: Sprint + Endurance points should equal the official Overall."""
    for entity in ("teams", "drivers"):
        def as_map(cup):
            rows = tables.get(cup, {}).get("overall", {}).get(entity, [])
            return {r["name"]: r["total"] for r in rows}
        overall, sprint, endurance = as_map("overall"), as_map("sprint"), as_map("endurance")
        if not (overall and sprint and endurance):
            print(f"check {entity}: skipped (a table is missing)")
            continue
        bad = [n for n, t in overall.items()
               if abs(t - (sprint.get(n, 0) + endurance.get(n, 0))) > 0.01]
        if bad:
            print(f"check {entity}: MISMATCH for {len(bad)} of {len(overall)}, e.g. {bad[:5]}")
        else:
            print(f"check {entity}: OK (Sprint + Endurance = Overall for all {len(overall)})")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["standings", "results"],
                        help="run just one part (default: both)")
    args = parser.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    stand_path = DATA_DIR / "standings.json"
    res_path = DATA_DIR / "results.json"
    stand_old = read_json(stand_path, {"season": SEASON, "tables": {}})
    res_old = read_json(res_path, {"season": SEASON, "rounds": []})

    stand_new, res_new = json.loads(json.dumps(stand_old)), json.loads(json.dumps(res_old))
    s_ok = s_fail = r_ok = r_fail = 0
    blocked = False
    if args.only != "results":
        try:
            stand_new, s_ok, s_fail = scrape_standings(stand_new)
        except Blocked as e:
            blocked = True
            print(f"STOPPED: {e}", flush=True)
        verify_overall(stand_new.get("tables", {}))
        print(f"standings tables parsed: {s_ok}, failed fetches: {s_fail}")
    if args.only != "standings" and not blocked:
        try:
            res_new, r_ok, r_fail = scrape_results(res_new)
        except Blocked as e:
            blocked = True
            print(f"STOPPED: {e}", flush=True)
        print(f"result sessions parsed: {r_ok}, failed fetches: {r_fail}")

    if args.only != "results" and s_ok == 0 and not blocked:
        print("ERROR: no standings table could be parsed. The site layout may have changed.")
        sys.exit(1)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    changed = False
    for path, new, old in ((stand_path, stand_new, stand_old), (res_path, res_new, res_old)):
        new["season"] = SEASON
        if strip_updated(new) != strip_updated(old):
            new["updated"] = stamp
            path.write_text(json.dumps(new, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
            changed = True
            print(f"wrote {path.name}")
    if not changed:
        print("no changes")
    if blocked:
        sys.exit(1)   # make the GitHub Action fail so you get an email


if __name__ == "__main__":
    main()
