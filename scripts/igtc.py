#!/usr/bin/env python3
"""Intercontinental GT Challenge data for the Series dropdown -> data/igtc/*.json

Source: the official intercontinentalgtchallenge.com pages (a few requests per run, with a pause between them).
  calendar page   -> the rounds and their event pages
  event pages     -> session timetable (a GMT column is published, so no time zone guessing)
  standings pages -> Drivers, GT3 Manufacturers and Independent Cup, with the points of every round (exact history)
  results pages   -> link to the race classification, which is a PDF; the PDF text is read here

Writes (same shapes the site reads for the other series):
  data/igtc/standings.json   tables.overall.overall (drivers + manufacturers as "teams"), tables.overall.independent
  data/igtc/results.json     race classifications, with the class of every car (Pro, Gold, Silver, Bronze, Pro-Am, Am)
  data/igtc/schedule.json    sessions in UTC (end times are typical scheduled lengths)
  data/igtc/history.json     exact points after every round

Run:  python scripts/igtc.py             (everything)
      python scripts/igtc.py --only standings   (standings | results | schedule | history)
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
from history import norm  # noqa: E402

BASE = os.environ.get("IGTC_BASE", "https://www.intercontinentalgtchallenge.com").rstrip("/")
SEASON = int(os.environ.get("IGTC_SEASON", "2026"))
SEASON_ID = int(os.environ.get("IGTC_SEASON_ID", str(SEASON - 2010)))      # the site numbers seasons from 2010 = 0
DELAY = float(os.environ.get("IGTC_DELAY", "1.5"))
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "igtc"
HEADERS = {"User-Agent": "pitboard-personal-project/1.0 (low-volume, personal use)", "Accept-Language": "en"}
MAX_CONSECUTIVE_FAILS = 4
STANDING_TYPES = [("drivers", 2), ("manufacturers", 4), ("independent", 7)]
WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
          "october", "november", "december"]
COUNTRIES = ["Australia", "Germany", "Belgium", "Japan", "United States", "South Africa", "Italy", "France",
             "Spain", "United Kingdom", "Malaysia", "Thailand", "Saudi Arabia", "Bahrain", "Qatar"]
CLASS_NAMES = {"PRO": "Pro", "GOLD": "Gold", "GLD": "Gold", "SIL": "Silver", "SILVER": "Silver", "BRO": "Bronze",
               "BRONZE": "Bronze", "PAM": "Pro-Am", "PROAM": "Pro-Am", "PRO-AM": "Pro-Am", "AM": "Am", "IND": "Independent", "I": "Other"}


class Blocked(Exception):
    """Raised when the site keeps refusing us; the run stops instead of pushing on."""


_fails = 0
_cache = {}


def fetch(url, binary=False, retries=3):
    """GET politely. Returns text (or bytes), or None. 403 is never retried; repeated failures stop the run."""
    global _fails
    key = (url, binary)
    if key in _cache:
        return _cache[key]
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=(10, 40))
            if r.status_code == 200:
                _fails = 0
                time.sleep(DELAY)
                _cache[key] = r.content if binary else r.text
                return _cache[key]
            print(f"  ! {r.status_code} for {url} (attempt {attempt})", flush=True)
            if r.status_code in (401, 403, 404):
                break
            if r.status_code in (429, 503):
                try:
                    wait = min(int(r.headers.get("Retry-After", "")), 120)
                except ValueError:
                    wait = 30 * attempt
                print(f"  ... site asked us to slow down, waiting {wait}s", flush=True)
                time.sleep(wait)
                continue
        except requests.RequestException as e:
            print(f"  ! {type(e).__name__} for {url} (attempt {attempt})", flush=True)
        time.sleep(DELAY * attempt)
    _fails += 1
    if _fails >= MAX_CONSECUTIVE_FAILS:
        raise Blocked(f"{_fails} requests in a row failed; stopping so we do not hammer the site")
    return None


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def strip_updated(d):
    return {k: v for k, v in d.items() if k != "updated"}


def text_of(node, sep=" "):
    return re.sub(r"\s+", " ", node.get_text(sep, strip=True)).strip()


def to_int(v, default=0):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def num(v):
    f = float(v)
    return int(f) if f == int(f) else f


def slugify(s):
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower())
    return s.strip("-")


# ------------------------------------------------------------------ calendar
DATE_RANGE_RE = re.compile(r"(\d{1,2})(?:\s+([A-Za-z]+))?\s*-\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})")


def parse_range(text):
    m = DATE_RANGE_RE.search(text or "")
    if not m:
        return None
    end_month = MONTHS.index(m.group(4).lower()) + 1 if m.group(4).lower() in MONTHS else None
    start_month = (MONTHS.index(m.group(2).lower()) + 1) if m.group(2) and m.group(2).lower() in MONTHS else end_month
    if not end_month:
        return None
    year = int(m.group(5))
    return (f"{year}-{start_month:02d}-{int(m.group(1)):02d}", f"{year}-{end_month:02d}-{int(m.group(3)):02d}")


def calendar_events():
    """[{'id', 'slug', 'name', 'country', 'start', 'end'}] for the season, in date order."""
    html = fetch(f"{BASE}/calendar")
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    events = {}
    for a in soup.find_all("a", href=True):
        m = re.search(r"/event/(\d+)/([^?#]*)", a["href"])
        if not m or m.group(1) in events:
            continue
        box = a
        for _ in range(5):                       # climb until the block holds a date range
            if parse_range(text_of(box)):
                break
            box = box.parent if box.parent is not None else box
        rng = parse_range(text_of(box))
        if not rng or not rng[0].startswith(str(SEASON)):
            continue
        name = ""
        for x in box.find_all("a", href=re.compile(r"/event/%s/" % m.group(1))):
            t = text_of(x)
            if len(t) > 6 and t.lower() not in ("event page", "more info", "details", "view event"):
                name = t
                break
        if not name:
            name = m.group(2).replace("-", " ").replace("%20", " ").title()
        country = next((c for c in COUNTRIES if c.lower() in text_of(box).lower()), "")
        events[m.group(1)] = {"id": m.group(1), "slug": m.group(2), "name": name, "country": country,
                              "start": rng[0], "end": rng[1]}
    return sorted(events.values(), key=lambda e: e["start"])


# ----------------------------------------------------------------- schedule
DAY_RE = re.compile(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+(\d{1,2})\s+([A-Za-z]+)", re.I)
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def session_kind_len(name, event_name):
    n = name.lower()
    if re.fullmatch(r"(race|the race)", n) or n.startswith("race"):
        h = re.search(r"(\d+)\s*hours?|(\d+)h\b", event_name, re.I)
        if h:
            return "race", int(h.group(1) or h.group(2)) * 60
        km = re.search(r"(\d+)\s*km", event_name, re.I)
        return "race", (int(int(km.group(1)) * 0.42) if km else 480)
    if "shootout" in n:
        return "qualifying", 15
    if "pre" in n and "qualif" in n:
        return "qualifying", 30
    if "qualif" in n:
        return "qualifying", 20
    if "test" in n:
        return "practice", 90
    return "practice", 60


def parse_timetable(html, event_name):
    """Day heading lines, then session name, local time, GMT time. GMT gives the exact UTC start."""
    soup = BeautifulSoup(html, "lxml")
    lines = [re.sub(r"\s+", " ", x).strip() for x in soup.get_text("\n").split("\n")]
    lines = [x for x in lines if x]
    out, seen, day = [], set(), None
    for i, line in enumerate(lines):
        d = DAY_RE.search(line)
        if d and d.group(3).lower() in MONTHS:
            day = datetime(SEASON, MONTHS.index(d.group(3).lower()) + 1, int(d.group(2)))
        t1 = TIME_RE.match(line)
        if not t1 or day is None or i + 1 >= len(lines):
            continue
        t2 = TIME_RE.match(lines[i + 1])
        if not t2:
            continue
        name = lines[i - 1] if i > 0 else ""
        if TIME_RE.match(name) or DAY_RE.search(name):
            # day and name may share a line: "Thursday, 8 October Test Session 2"
            name = DAY_RE.sub("", name).strip(" -:|,") if not TIME_RE.match(name) else ""
        if not name or len(name) > 60:
            continue
        local = day.replace(hour=int(t1.group(1)), minute=int(t1.group(2)))
        gmt_minutes = int(t2.group(1)) * 60 + int(t2.group(2))
        loc_minutes = int(t1.group(1)) * 60 + int(t1.group(2))
        offset = ((loc_minutes - gmt_minutes + 720) % 1440) - 720          # local minus GMT, within -12h..+11h59
        start = (local - timedelta(minutes=offset)).replace(tzinfo=timezone.utc)
        if (name.lower(), start) in seen:
            continue
        seen.add((name.lower(), start))
        kind, mins = session_kind_len(name, event_name)
        out.append({"name": name, "kind": kind, "day": WEEKDAYS[start.weekday()],
                    "utc": start.strftime("%Y-%m-%dT%H:%M:00Z"),
                    "end_utc": (start + timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:00Z")})
    out.sort(key=lambda s: s["utc"])
    return out


def scrape_schedule(old, events, latest_done):
    have = {r["round"]: r for r in old.get("rounds", [])}
    result = []
    for n, ev in enumerate(events, start=1):
        prev = have.get(n)
        if prev and prev.get("sessions") and n < latest_done - 1:
            result.append(prev)
            continue
        html = fetch(f"{BASE}/event/{ev['id']}/{ev['slug']}")
        sessions = parse_timetable(html, ev["name"]) if html else []
        if html and not sessions:
            print(f"  ! {ev['name']}: no timetable found in the event page", flush=True)
            lines = [x for x in (re.sub(r"\s+", " ", t).strip() for t in BeautifulSoup(html, "lxml").get_text("\n").split("\n")) if x]
            first = next((i for i, x in enumerate(lines) if DAY_RE.search(x)), None)
            for x in (lines[first:first + 10] if first is not None else lines[:4]):
                print("    sample line:", x[:90], flush=True)
        if not sessions and prev:
            sessions = prev.get("sessions", [])
        result.append({"round": n, "name": ev["name"], "country": ev["country"], "start": ev["start"],
                       "end": ev["end"], "sessions": sessions})
    old["rounds"] = result
    print(f"schedule: {len(result)} rounds, {sum(len(r['sessions']) for r in result)} sessions")
    return old, len(result)


# ---------------------------------------------------------------- standings
def table_headers(tbl):
    tr = tbl.find("tr")
    return [text_of(c).lower() for c in tr.find_all(["th", "td"])] if tr else []


def cell_points(txt):
    m = re.search(r"\d+(?:\.\d+)?", txt or "")
    return num(m.group(0)) if m else None


def parse_standings_page(html):
    """-> list of {pos, name, team, total, rounds:[pts|None per R column]} from the first standings table."""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    for tbl in soup.find_all("table"):
        heads = table_headers(tbl)
        if not heads or not heads[0].startswith("pos"):
            continue
        name_i = next((i for i, h in enumerate(heads) if "driver" in h), None)
        if name_i is None:
            name_i = next((i for i, h in enumerate(heads) if re.search(r"team|entrant|competitor|manufacturer", h)), 1)
        man_i = next((i for i, h in enumerate(heads) if "manufacturer" in h and i != name_i), None)
        total_i = next((i for i, h in enumerate(heads) if "total" in h or h.startswith("pts") or "points" in h), None)
        r_cols = sorted((int(h[1:]), i) for i, h in enumerate(heads) if re.fullmatch(r"r\d+", h))
        rows = []
        for tr in tbl.find_all("tr")[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) <= name_i:
                continue
            pos = to_int(text_of(cells[0]).rstrip("."), len(rows) + 1)
            name = text_of(cells[name_i])
            if not name:
                continue
            rounds = [cell_points(text_of(cells[i])) if i < len(cells) else None for _, i in r_cols]
            total = cell_points(text_of(cells[total_i])) if total_i is not None and total_i < len(cells) else None
            if total is None:
                total = num(sum(v for v in rounds if v is not None))
            rows.append({"pos": pos, "name": name, "team": text_of(cells[man_i]) if man_i is not None and man_i < len(cells) else "",
                         "total": total, "_r": rounds})
        if rows:
            return rows
    return []


def read_standings():
    out = {}
    for key, type_id in STANDING_TYPES:
        html = fetch(f"{BASE}/standings?filter_season_id={SEASON_ID}&filter_igtcstandingtype_id={type_id}")
        rows = parse_standings_page(html)
        out[key] = rows
        print(f"standings {key}: {len(rows)} rows", flush=True)
    return out


def clean(rows):
    return [{k: v for k, v in r.items() if k != "_r"} for r in rows]


def scrape_standings(old, st):
    if not st["drivers"] and not st["manufacturers"]:
        print("standings: nothing read, keeping the old file")
        return old, 0
    tables = {"overall": {"drivers": clean(st["drivers"]),
                          "teams": [{k: v for k, v in r.items() if k not in ("_r", "team")} for r in st["manufacturers"]]}}
    if st["independent"]:
        tables["independent"] = {"drivers": clean(st["independent"])}
    old["tables"] = {"overall": tables}
    return old, 1


def latest_round_with_points(st):
    latest = 0
    for rows in st.values():
        for r in rows:
            for i, v in enumerate(r["_r"], start=1):
                if v is not None:
                    latest = max(latest, i)
    return latest


def scrape_history(st, schedule):
    names = {r["round"]: r["name"] for r in schedule.get("rounds", [])}
    nrounds = max((len(r["_r"]) for rows in st.values() for r in rows), default=0)
    tables = {"overall/overall": {"drivers": st["drivers"], "teams": st["manufacturers"]},
              "overall/independent": {"drivers": st["independent"]}}
    snaps = []
    for n in range(1, nrounds + 1):
        out, any_points = {}, False
        for key, ents in tables.items():
            tab = {}
            for ent, rows in ents.items():
                m = {}
                for r in rows:
                    got = [v for v in r["_r"][:n] if v is not None]
                    m[norm(r["name"])] = num(sum(got)) if got else 0
                    if n <= len(r["_r"]) and r["_r"][n - 1] is not None:
                        any_points = True
                tab[ent] = m
            out[key] = tab
        if any_points:
            snaps.append({"round": n, "name": names.get(n, f"Round {n}"), "estimated": False, "tables": out})
            print(f"history: round {n} saved", flush=True)
    return {"season": SEASON, "snapshots": snaps}


# ------------------------------------------------------------------ results
def pdf_text(data):
    """Text with the page layout kept. pdftotext is best; pypdf is the fallback."""
    if shutil.which("pdftotext"):
        try:
            r = subprocess.run(["pdftotext", "-layout", "-", "-"], input=data, capture_output=True, timeout=60)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            pass
    from pypdf import PdfReader
    return "\n".join((p.extract_text(extraction_mode="layout") or "") for p in PdfReader(BytesIO(data)).pages)


def secs(t):
    parts = [float(x) for x in t.split(":")]
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def fmt_gap(s):
    if s >= 60:
        m, r = divmod(s, 60)
        if m >= 60:
            h, m = divmod(m, 60)
            return f"+{int(h)}:{int(m):02d}:{r:06.3f}"
        return f"+{int(m)}:{r:06.3f}"
    return f"+{s:.3f}"


def add_gaps(rows):
    """Winner keeps the race time; the rest get the gap (time behind, or laps behind)."""
    if not rows:
        return rows
    lead = rows[0]
    for r in rows:
        if r is lead:
            continue
        if r["pos"] == 0 or r.get("_nc"):
            r["time"] = "NC"
        elif r["laps"] == lead["laps"] and r.get("_secs") is not None and lead.get("_secs") is not None:
            r["time"] = fmt_gap(r["_secs"] - lead["_secs"])
        else:
            behind = lead["laps"] - r["laps"]
            r["time"] = f"+{behind} Lap{'s' if behind != 1 else ''}"
    lead["time"] = lead.get("_time", "")
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]


def class_name(code):
    code = (code or "").strip().upper()
    return CLASS_NAMES.get(code, code.title() if code else "")


def title_name(raw):
    """'Alessio PICARIELLO (BEL)' -> 'Alessio Picariello'; names cut off by the PDF (no country) are dropped."""
    raw = raw.strip()
    if "(" not in raw:
        return ""
    raw = re.sub(r"\s*\([A-Z]{2,3}\)\s*", "", raw).strip()
    out = []
    for tok in raw.split():
        if tok.isupper() and len(tok) > 1:
            tok = tok.capitalize()
            if tok.lower().startswith("mc") and len(tok) > 3:
                tok = "Mc" + tok[2:].capitalize()
        out.append(tok)
    return " ".join(out)


NAT_ROW = re.compile(r"^\s*(\d{1,3}|NC)\s+(\d{1,4})\s{2,}\S")
MAKE_RE = re.compile(r"(?<![A-Za-z])(Mercedes|Porsche|BMW|Audi|Ferrari|McLaren|Lamborghini|Chevrolet|Ford|Aston|KTM|Lexus|Nissan|Honda|Acura|Toyota|Bentley|Corvette|Cadillac|Hyundai|Dodge|Lotus|Maserati)")
NAT_TAIL = re.compile(r"\s([A-Z]{1,4})\s+(\d{1,4})\s+((?:\d{1,2}:)?\d{1,2}:\d{2}\.\d+)\s")


def parse_natsoft(text):
    """Layout of the Bathurst PDF: Pos Car Competitor/Team Driver Vehicle Cap CL Laps Race.Time Fastest."""
    lines = text.splitlines()
    head = next((l for l in lines if "Competitor/Team" in l and "Driver" in l and "Vehicle" in l), None)
    if head is None:
        return []
    c_team, c_drv, c_veh = head.index("Competitor/Team"), head.index("Driver"), head.index("Vehicle")
    blocks, cur = [], None
    for line in lines[lines.index(head) + 1:]:
        if NAT_ROW.match(line):
            cur = [line]
            blocks.append(cur)
        elif cur is not None and line.strip() and not line.strip().startswith(("PENALTY", "_", "Competitor#")) \
                and not line[:c_drv].strip() and len(cur) < 5:
            cur.append(line)
        elif cur is not None and not line.strip():
            cur = None
    rows = []
    for b in blocks:
        m0 = NAT_ROW.match(b[0])
        full = " ".join(b) + " "
        t = NAT_TAIL.search(full)
        if not t:
            continue
        pieces, vehicle = [], ""
        for i, l in enumerate(b):
            seg = l[c_drv:]
            tl = NAT_TAIL.search(l + " ")
            if tl:
                seg = l[c_drv:tl.start() + 1]
                vm = MAKE_RE.search(seg)
                if vm:
                    pieces.append(seg[:vm.start()].strip())
                    vehicle = seg[vm.start():].strip()
                else:
                    parts = re.split(r"\s{2,}", seg.strip())
                    pieces.append(" ".join(parts[:-1]))
                    vehicle = parts[-1]
            else:
                pieces.append(seg.strip())
        names = [re.sub(r"\s*\([A-Z]{2,3}\)", "", n).strip() for n in "".join(pieces).split("/") if n.strip()]
        pos = 0 if m0.group(1) == "NC" else int(m0.group(1))
        rows.append({"pos": pos, "car": m0.group(2), "drivers": ", ".join(names),
                     "team": b[0][c_team:c_drv].strip(), "model": vehicle, "laps": int(t.group(2)),
                     "cls": class_name(t.group(1)), "_secs": secs(t.group(3)), "_time": t.group(3),
                     "_nc": m0.group(1) == "NC"})
    return rows


TSL_ROW = re.compile(r"^\s*(?P<pos>\d{1,3}|NC)\s+(?P<car>\d{1,4})\s+(?P<cl>[A-Z][A-Z-]*)\s+(?P<team>.+?)\s{2,}(?P<model>.+?)\s{2,}"
                     r"(?P<laps>\d+)\s+(?P<time>(?:\d{1,2}:)?\d{1,2}:\d{2}\.\d+)\s")


def parse_tsl(text):
    """Layout of the Suzuka PDF: POS NO CL TEAM CAR LAPS TIME GAP DIFF ... with the drivers on the next lines."""
    lines = text.splitlines()
    rows = []
    for i, line in enumerate(lines):
        m = TSL_ROW.match(line + " ")
        if not m:
            continue
        names = []
        for nxt in lines[i + 1:i + 4]:
            s = nxt.strip()
            if not s or re.fullmatch(r"\d{1,3}", s):
                continue
            if TSL_ROW.match(nxt + " "):
                break
            names = [title_name(x) for x in s.split(" / ")]
            break
        pos = 0 if m.group("pos") == "NC" else int(m.group("pos"))
        rows.append({"pos": pos, "car": m.group("car"), "drivers": ", ".join(n for n in names if n),
                     "team": re.sub(r"\s*\*+\s*", " ", m.group("team")).strip(), "model": m.group("model").strip(),
                     "laps": int(m.group("laps")), "cls": class_name(m.group("cl")),
                     "_secs": secs(m.group("time")), "_time": m.group("time"), "_nc": m.group("pos") == "NC"})
    return rows


def parse_result_pdf(data):
    text = pdf_text(data)
    if re.search(r"Competitor/Team", text) and re.search(r"\bPos\b.*\bCar\b", text):
        rows = parse_natsoft(text)
        layout = "natsoft"
    elif re.search(r"\bPOS\b\s+\bNO\b\s+\bCL\b", text):
        rows = parse_tsl(text)
        layout = "tsl"
    else:
        rows, layout = [], "unknown"
        print("  ! unknown PDF layout. first lines:", flush=True)
        for l in [x for x in text.splitlines() if x.strip()][:8]:
            print("    ", re.sub(r"\s+", " ", l)[:110], flush=True)
    # pos 0 (NC) rows sit at the end and carry no position; number the rest as they come
    ranked = [r for r in rows if not r["_nc"]]
    for i, r in enumerate(ranked, start=1):
        r["pos"] = i if not r["pos"] else r["pos"]
    for r in rows:
        if r["_nc"]:
            r["pos"] = 0
    return layout, add_gaps(rows)


def race_pdf_link(meeting_id):
    url = f"{BASE}/results?filter_season_id={SEASON_ID}&filter_meeting_id={meeting_id}"
    html = fetch(url)
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    pdfs = [(text_of(a), a["href"]) for a in soup.find_all("a", href=True) if ".pdf" in a["href"].lower()]
    best = None
    for label, href in pdfs:
        l = label.lower().strip()
        if l in ("race", "race result", "race results", "race classification"):
            best = href
            break
    if best is None:
        print(f"  ! no 'Race' PDF link for meeting {meeting_id}. PDF links: {[l for l, _ in pdfs][:8]}", flush=True)
        return None
    return urljoin(BASE, best)


def scrape_results(old, events, latest_done, schedule):
    have = {r["round"]: r for r in old.get("rounds", [])}
    refresh = set(sorted(have)[-2:])
    now = datetime.now(timezone.utc)
    ok = 0
    sched = {r["round"]: r for r in schedule.get("rounds", [])}
    for n, ev in enumerate(events, start=1):
        races = [s for s in sched.get(n, {}).get("sessions", []) if s["kind"] == "race"]
        raced = bool(races) and datetime.strptime(races[-1]["end_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) < now
        if n > latest_done and not raced:
            continue
        if n in have and n not in refresh and have[n].get("sessions"):
            continue
        link = race_pdf_link(ev["id"])
        if not link:
            continue
        data = fetch(link, binary=True)
        if not data:
            continue
        layout, rows = parse_result_pdf(data)
        print(f"results round {n} {ev['name']}: {len(rows)} rows ({layout})", flush=True)
        if not rows:
            continue
        m = re.search(r"(\d+)\s*hours?|(\d+)h\b", ev["name"], re.I)
        km = re.search(r"(\d+)\s*km", ev["name"], re.I)
        cup = f"{m.group(1) or m.group(2)} Hours" if m else (f"{km.group(1)} km" if km else "Race")
        have[n] = {"id": slugify(ev["name"]), "round": n, "name": ev["name"], "cup": cup,
                   "sessions": [{"id": "main-race", "name": "Race", "rows": rows}]}
        ok += 1
    old["rounds"] = sorted(have.values(), key=lambda r: r["round"])
    return old, ok


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
    got, blocked = 0, False
    try:
        events = calendar_events()
        print(f"calendar: {len(events)} events")
        st = read_standings()
        latest = latest_round_with_points(st)
        if want("schedule") or want("results") or not new["schedule"].get("rounds"):
            new["schedule"], c = scrape_schedule(new["schedule"], events, latest)
            got += c
        if want("standings"):
            new["standings"], c = scrape_standings(new["standings"], st)
            got += c
        if want("results"):
            new["results"], c = scrape_results(new["results"], events, latest, new["schedule"])
            got += c
        if want("history") and latest:
            new["history"] = scrape_history(st, new["schedule"])
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
            print(f"wrote igtc/{path.name}")
    if not changed:
        print("igtc: no changes")
    if blocked or (args.only is None and got == 0):
        print("ERROR: no IGTC data could be read.")
        sys.exit(1)


if __name__ == "__main__":
    main()
