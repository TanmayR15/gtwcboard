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
import io
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

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


def fetch(url: str, retries: int = 3, binary: bool = False):
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
                return r.content if binary else r.text
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


# ---------------------------------------------------------------- schedule
MONTHS = {}
for _i, _m in enumerate(["january", "february", "march", "april", "may", "june", "july",
                         "august", "september", "october", "november", "december"], 1):
    MONTHS[_m] = _i
    MONTHS[_m[:3]] = _i
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Built-in 2026 calendar (taken from the official calendar page): round, event id, url slug,
# name, country, first day, last day. Session times are read from each event page.
SCHEDULE_2026 = [
    (1, 246, "circuit-paul-ricard", "Circuit Paul Ricard", "France", "2026-04-10", "2026-04-12"),
    (2, 247, "brands-hatch", "Brands Hatch", "Great Britain", "2026-05-02", "2026-05-03"),
    (3, 248, "monza", "Monza", "Italy", "2026-05-28", "2026-05-31"),
    (4, 249, "crowdstrike-24-hours-of-spa", "CrowdStrike 24 Hours of Spa", "Belgium", "2026-06-23", "2026-06-28"),
    (5, 250, "misano", "Misano", "Italy", "2026-07-16", "2026-07-19"),
    (6, 251, "magny-cours", "Magny-Cours", "France", "2026-07-30", "2026-08-02"),
    (7, 252, "n\u00fcrburgring", "N\u00fcrburgring", "Germany", "2026-08-28", "2026-08-30"),
    (8, 253, "zandvoort", "Zandvoort", "Netherlands", "2026-09-17", "2026-09-20"),
    (9, 254, "barcelona", "Barcelona", "Spain", "2026-10-01", "2026-10-04"),
    (10, 255, "portimao", "Portimao", "Portugal", "2026-10-15", "2026-10-18"),
]

_TIME = r"(\d{1,2}):(\d{2})"
_GMT_AFTER = re.compile(_TIME + r"(?:\s*[-\u2013]\s*" + _TIME + r")?\s*(?:GMT|UTC)\b", re.I)
_GMT_BEFORE = re.compile(r"(?:GMT|UTC)\s*[:\-]?\s*" + _TIME + r"(?:\s*[-\u2013]\s*" + _TIME + r")?", re.I)
_LOCAL_AFTER = re.compile(_TIME + r"(?:\s*[-\u2013]\s*" + _TIME + r")?\s*(?:Local|CES?T|WES?T|BST|WET|CET)\b", re.I)
_ANY_TIME = re.compile(_TIME + r"(?:\s*[-\u2013]\s*" + _TIME + r")?")
_WORDS_TO_DROP = re.compile(r"\b(local|gmt|utc|cest|cet|west|wet|bst|time)\b", re.I)


def _mins(h, m):
    return int(h) * 60 + int(m)


def _flatten_lines(html: str):
    """Turn a page into plain text lines. Every table row becomes ONE line (cells joined by ' | ')."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for tr in soup.find_all("tr"):
        cells = [cell_text(c) for c in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        tr.replace_with("\n" + " | ".join(cells) + "\n")
    lines = []
    for raw in soup.get_text("\n").splitlines():
        t = re.sub(r"\s+", " ", raw).strip()
        if t:
            lines.append(t)
    return lines


def _parse_day(line, year, start, end):
    """Return an ISO date if this line is a day heading such as 'Thursday, 15 October'."""
    if _ANY_TIME.search(line):
        return None
    low = line.lower()
    wd = next((w for w in WEEKDAYS if w in low), None)
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?(?:\s+(\d{4}))?", line)
    if m and m.group(2).lower() in MONTHS and (wd or len(line) <= 24):
        try:
            return datetime(int(m.group(3) or year), MONTHS[m.group(2).lower()], int(m.group(1))).strftime("%Y-%m-%d")
        except ValueError:
            return None
    if wd and len(line) <= 24 and start and end:
        s, e = datetime.strptime(start, "%Y-%m-%d"), datetime.strptime(end, "%Y-%m-%d")
        for k in range((e - s).days + 1):
            d = s + timedelta(days=k)
            if d.weekday() == WEEKDAYS.index(wd):
                return d.strftime("%Y-%m-%d")
    return None


def _kind(name):
    n = name.lower()
    if re.search(r"qualif|superpole", n):
        return "qualifying"
    if re.search(r"\brace\b", n) and not re.search(r"pre[- ]?race|warm", n):
        return "race"
    if re.search(r"practice|\bfp\b", n):
        return "practice"
    if re.search(r"test|bronze|prologue|briefing|parade|warm|pit", n):
        return "other"
    return "other"


def parse_event_schedule(html: str, year: int, start=None, end=None):
    """Read an event page and return sessions [{name, kind, day, utc, (end_utc)}].

    Needs a GMT/UTC time for each session (the official page shows Local and GMT). Rows without a
    clear GMT time are skipped, never guessed.
    """
    lines = _flatten_lines(html)
    page_text = " ".join(lines).lower()
    local_first = page_text.find("local") != -1 and (page_text.find("gmt") == -1 or page_text.find("local") < page_text.find("gmt"))
    sessions, day, prev_name, pending = [], None, "", []

    def emit(name, g, loc):
        nonlocal sessions
        name = re.sub(r"[|/]+", " ", _WORDS_TO_DROP.sub("", _ANY_TIME.sub("", name)))
        name = re.sub(r"\s+", " ", name).strip(" -\u2013:|/,")
        if not name or not day or g is None:
            return
        d = datetime.strptime(day, "%Y-%m-%d")
        if loc is not None and _mins(*g[:2]) - _mins(*loc[:2]) > 720:
            d -= timedelta(days=1)          # local time is just after midnight, GMT is the day before
        rec = {"name": name, "kind": _kind(name), "day": day,
               "utc": d.strftime("%Y-%m-%d") + "T%02d:%02d:00Z" % (int(g[0]), int(g[1]))}
        if loc is not None:
            rec["local"] = "%02d:%02d" % (int(loc[0]), int(loc[1]))
        if g[2] is not None:
            e2 = d
            if _mins(g[2], g[3]) < _mins(g[0], g[1]):
                e2 += timedelta(days=1)
            rec["end_utc"] = e2.strftime("%Y-%m-%d") + "T%02d:%02d:00Z" % (int(g[2]), int(g[3]))
        if rec not in sessions:
            sessions.append(rec)

    def times_from(line):
        """(gmt_tuple, local_tuple) from a single line, using labels or the header order."""
        gm = _GMT_AFTER.search(line)
        g = gm.groups() if gm else None
        if g is None:
            gm = _GMT_BEFORE.search(line)
            g = gm.groups() if gm else None
        lm = _LOCAL_AFTER.search(line)
        loc = lm.groups() if lm else None
        if g is None:
            groups = list(_ANY_TIME.finditer(line))
            # two separate time groups with no label: use the header order (Local first or GMT first)
            if len(groups) == 2:
                a, b = groups[0].groups(), groups[1].groups()
                g, loc = (b, a) if local_first else (a, b)
        return g, loc

    for line in lines:
        d = _parse_day(line, year, start, end)
        if d:
            day, prev_name, pending = d, "", []
            continue
        if not _ANY_TIME.search(line):
            prev_name, pending = line, []
            continue
        pure = not re.sub(r"[\d:\s\-\u2013|/]|local|gmt|utc", "", line, flags=re.I)
        if pure and prev_name and not re.search(r"[A-Za-z]{4,}", _WORDS_TO_DROP.sub("", line)):
            # the cells of one row are on separate lines: name, then time, then time
            pending.append(line)
            if _GMT_AFTER.search(line) or _GMT_BEFORE.search(line) or len(pending) == 2:
                g, loc = times_from(" ".join(pending))
                emit(prev_name, g, loc)
                pending = []
            continue
        g, loc = times_from(line)
        emit(line, g, loc)
    return sessions


# The event page lists start times only; the "Event Timetable PDF" it links to has the time ranges.
SCHEDULE_VERSION = 2          # bump to make the next run re-read every event once


def find_timetable_pdf(html: str):
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".pdf" in href.lower() and ("timetable" in a.get_text(" ").lower() or "timetable" in href.lower()):
            u = urlsplit(urljoin(BASE + "/", href))
            return urlunsplit((u.scheme, u.netloc, quote(u.path, safe="/%"), u.query, ""))
    return None


def parse_timetable_ranges(text: str):
    """Rows like 'Free Practice 1  09:00 - 10:00' -> [(normalised line, start 'HH:MM', end 'HH:MM')]."""
    out = []
    for line in text.splitlines():
        for m in re.finditer(r"(\d{1,2})[:.](\d{2})\s*[-–—]\s*(\d{1,2})[:.](\d{2})", line):
            out.append((normname_simple(line), "%02d:%02d" % (int(m.group(1)), int(m.group(2))),
                        "%02d:%02d" % (int(m.group(3)), int(m.group(4)))))
    return out


def normname_simple(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def add_end_times(sessions, ranges):
    """Give each session an end_utc when exactly one PDF row clearly belongs to it."""
    added = 0
    for sess in sessions:
        if sess.get("end_utc") or not sess.get("local"):
            continue
        cands = [r for r in ranges if r[1] == sess["local"]]
        named = [r for r in cands if normname_simple(sess["name"]) in r[0]]
        pick = named[0] if named else (cands[0] if len(cands) == 1 else None)
        if not pick:
            continue
        sh, sm = map(int, pick[1].split(":"))
        eh, em = map(int, pick[2].split(":"))
        dur = ((eh * 60 + em) - (sh * 60 + sm)) % 1440
        if 0 < dur <= 26 * 60:
            end = datetime.strptime(sess["utc"], "%Y-%m-%dT%H:%M:%SZ") + timedelta(minutes=dur)
            sess["end_utc"] = end.strftime("%Y-%m-%dT%H:%M:%SZ")
            added += 1
    return added


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader       # imported here so a missing library only disables end times
    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)


def scrape_schedule(old):
    """Build data/schedule.json. Dates come from the built-in calendar; times from each event page.

    Polite: only events that are running, upcoming within 21 days, or never fetched are requested.
    """
    if SEASON != 2026:
        print("schedule: no built-in calendar for this season, skipped")
        return old, 0, 0
    old_by_round = {r.get("round"): r for r in old.get("rounds", [])}
    today = datetime.now(timezone.utc).date()
    out, ok, fail = [], 0, 0
    for rnd, eid, slug, name, country, start, end in SCHEDULE_2026:
        prev = old_by_round.get(rnd, {})
        rec = {"round": rnd, "event_id": eid, "name": name, "country": country,
               "start": start, "end": end, "sessions": prev.get("sessions", [])}
        if prev.get("tried"):
            rec["tried"] = True
        s_d, e_d = datetime.strptime(start, "%Y-%m-%d").date(), datetime.strptime(end, "%Y-%m-%d").date()
        active = (e_d + timedelta(days=2) >= today) and (s_d - timedelta(days=21) <= today)
        redo = old.get("v") != SCHEDULE_VERSION
        if active or redo or (not rec["sessions"] and not rec.get("tried")):
            rec["tried"] = True     # a past round whose page gave nothing is not requested again
            html = fetch(f"{BASE}/event/{eid}/{quote(slug)}")
            if html is None:
                fail += 1
            else:
                sessions = parse_event_schedule(html, SEASON, start, end)
                if sessions:
                    msg = ""
                    pdf_url = find_timetable_pdf(html)
                    if pdf_url:
                        try:
                            data = fetch(pdf_url, binary=True)
                            n = add_end_times(sessions, parse_timetable_ranges(pdf_text(data))) if data else 0
                            msg = f", {n} end times"
                        except Blocked:
                            raise
                        except Exception as e:
                            msg = f", end times skipped ({type(e).__name__})"
                    else:
                        msg = ", no timetable PDF link"
                    rec["sessions"] = sorted(sessions, key=lambda s: s["utc"])
                    ok += 1
                    print(f"schedule {name}: {len(sessions)} sessions{msg}", flush=True)
                else:
                    fail += 1
                    print(f"schedule {name}: no sessions could be read (kept old data)", flush=True)
        out.append(rec)
    new = dict(old)
    new["rounds"] = out
    if fail == 0:
        new["v"] = SCHEDULE_VERSION      # everything was re-read, so do not repeat next run
    return new, ok, fail


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["standings", "results", "schedule"],
                        help="run just one part (default: both)")
    args = parser.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    stand_path = DATA_DIR / "standings.json"
    res_path = DATA_DIR / "results.json"
    sched_path = DATA_DIR / "schedule.json"
    stand_old = read_json(stand_path, {"season": SEASON, "tables": {}})
    res_old = read_json(res_path, {"season": SEASON, "rounds": []})
    sched_old = read_json(sched_path, {"season": SEASON, "rounds": []})

    stand_new, res_new = json.loads(json.dumps(stand_old)), json.loads(json.dumps(res_old))
    sched_new = json.loads(json.dumps(sched_old))
    s_ok = s_fail = r_ok = r_fail = 0
    blocked = False
    if args.only in (None, "standings"):
        try:
            stand_new, s_ok, s_fail = scrape_standings(stand_new)
        except Blocked as e:
            blocked = True
            print(f"STOPPED: {e}", flush=True)
        verify_overall(stand_new.get("tables", {}))
        print(f"standings tables parsed: {s_ok}, failed fetches: {s_fail}")
    if args.only in (None, "results") and not blocked:
        try:
            res_new, r_ok, r_fail = scrape_results(res_new)
        except Blocked as e:
            blocked = True
            print(f"STOPPED: {e}", flush=True)
        print(f"result sessions parsed: {r_ok}, failed fetches: {r_fail}")
    if args.only in (None, "schedule") and not blocked:
        try:
            sched_new, c_ok, c_fail = scrape_schedule(sched_new)
            print(f"schedule events parsed: {c_ok}, problems: {c_fail}")
        except Blocked as e:
            blocked = True
            print(f"STOPPED: {e}", flush=True)
        except Exception as e:      # the schedule is a bonus: never fail the whole run for it
            print(f"schedule skipped: {type(e).__name__}: {e}", flush=True)

    if args.only in (None, "standings") and s_ok == 0 and not blocked:
        print("ERROR: no standings table could be parsed. The site layout may have changed.")
        sys.exit(1)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    changed = False
    for path, new, old in ((stand_path, stand_new, stand_old), (res_path, res_new, res_old),
                           (sched_path, sched_new, sched_old)):
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
