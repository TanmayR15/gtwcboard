#!/usr/bin/env python3
"""FIA World Endurance Championship data for the Series dropdown -> data/wec/*.json

Source: the official fiawec.com pages (a few requests per run, with a pause between them).
  season page      -> list of rounds
  race pages       -> session times (shown in track time) and the link to the race classification
  results page     -> final race classification, one table per class (Hypercar, LMGT3)
  standings page   -> points of every round for every table, so the history is exact

Writes (same shapes the site reads for the other series):
  data/wec/standings.json   tables.overall.hypercar / lmgt3  ->  drivers (crews) and teams
                            (Hypercar "teams" are the manufacturers, LMGT3 "teams" are the teams)
  data/wec/results.json     race classifications, round by round, rows tagged Hypercar / LMGT3
  data/wec/schedule.json    sessions in UTC (end times are the usual scheduled lengths)
  data/wec/history.json     exact points after every round

Run:  python scripts/wec.py             (everything)
      python scripts/wec.py --only standings   (standings | results | schedule | history)
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
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
from history import norm  # noqa: E402

BASE = os.environ.get("WEC_BASE", "https://www.fiawec.com").rstrip("/")
SEASON = int(os.environ.get("WEC_SEASON", "2026"))
DELAY = float(os.environ.get("WEC_DELAY", "1.5"))
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "wec"
HEADERS = {"User-Agent": "pitboard-personal-project/1.0 (low-volume, personal use)", "Accept-Language": "en"}
MAX_CONSECUTIVE_FAILS = 4
STANDINGS_URL = BASE + "/en/page/manufacturers-classification"

# Track time zones, matched against the race slug.
TRACK_TZ = [
    ("imola", "Europe/Rome"), ("monza", "Europe/Rome"), ("spa", "Europe/Brussels"),
    ("le-mans", "Europe/Paris"), ("paulo", "America/Sao_Paulo"), ("lone-star", "America/Chicago"),
    ("fuji", "Asia/Tokyo"), ("barcelona", "Europe/Madrid"), ("bahrain", "Asia/Bahrain"),
    ("qatar", "Asia/Qatar"), ("silverstone", "Europe/London"),
]
COUNTRY = [
    ("imola", "Italy"), ("monza", "Italy"), ("spa", "Belgium"), ("le-mans", "France"), ("paulo", "Brazil"),
    ("lone-star", "United States"), ("fuji", "Japan"), ("barcelona", "Spain"), ("bahrain", "Bahrain"),
    ("qatar", "Qatar"), ("silverstone", "United Kingdom"),
]
WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
          "october", "november", "december"]
DT_RE = re.compile(r"(%s)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{1,2}):(\d{2})\s*(AM|PM)" % "|".join(m.title() for m in MONTHS), re.I)
SESSION_WORDS = re.compile(r"practice|qualif|hyperpole|race|warm|test", re.I)
PARTICLES = {"van", "de", "di", "da", "von", "der", "del", "dos", "den", "le", "la", "du", "ten", "ter"}


class Blocked(Exception):
    """Raised when the site keeps refusing us; the run stops instead of pushing on."""


_fails = 0
_cache = {}


def fetch(url, retries=3):
    """GET politely. Returns HTML text or None. 403 is never retried; repeated failures stop the run."""
    global _fails
    if url in _cache:
        return _cache[url]
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=(10, 25))
            if r.status_code == 200:
                _fails = 0
                time.sleep(DELAY)
                _cache[url] = r.text
                return r.text
            print(f"  ! {r.status_code} for {url} (attempt {attempt})", flush=True)
            if r.status_code in (401, 403):
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
        raise Blocked(f"{_fails} pages in a row failed; stopping so we do not hammer the site")
    return None


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def strip_updated(d):
    return {k: v for k, v in d.items() if k != "updated"}


def slugify(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


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


def lookup(table, slug, default=""):
    for key, val in table:
        if key in slug:
            return val
    return default


def surname(full):
    parts = full.split()
    if not parts:
        return ""
    i = len(parts) - 1
    while i > 1 and parts[i - 1].lower() in PARTICLES:
        i -= 1
    return " ".join(parts[i:]) if len(parts) > 1 else parts[0]


# ------------------------------------------------------------------- rounds
def season_rounds():
    """[(slug, url)] in calendar order, for this season, without the prologue."""
    html = fetch(f"{BASE}/en/season/{SEASON}")
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        m = re.search(r"/en/race/([a-z0-9-]+-%d)/?$" % SEASON, a["href"])
        if not m:
            continue
        slug = m.group(1)
        if slug in seen or "prologue" in slug or "test" in slug:
            continue
        seen.add(slug)
        out.append((slug, urljoin(BASE, a["href"])))
    return out


def race_page(slug):
    return fetch(f"{BASE}/en/race/{slug}")


def pretty_name(slug, soup=None):
    if soup is not None:
        h = soup.find("h1")
        if h and text_of(h):
            return text_of(h).title().replace(" Of ", " of ").replace("Totalenergies", "TotalEnergies")
    name = re.sub(r"-%d$" % SEASON, "", slug).replace("-", " ").title().replace(" Of ", " of ")
    return name


# ----------------------------------------------------------------- schedule
def session_kind_len(name, round_name):
    n = name.lower()
    if "race" in n:
        m = re.search(r"(\d+)\s*hours?", round_name, re.I)
        return "race", (int(m.group(1)) * 60 if m else 360)
    if "hyperpole" in n:
        return "qualifying", 12
    if "qualif" in n:
        return "qualifying", 15
    if "3" in n and "practice" in n:
        return "practice", 60
    return "practice", 90


def parse_sessions(html, slug, round_name):
    tz = ZoneInfo(lookup(TRACK_TZ, slug, "UTC"))
    soup = BeautifulSoup(html, "lxml")
    lines = [re.sub(r"\s+", " ", x).strip() for x in soup.get_text("\n").split("\n")]
    lines = [x for x in lines if x]
    out, seen = [], set()
    for i, line in enumerate(lines):
        m = DT_RE.search(line)
        if not m:
            continue
        name = line[:m.start()].strip(" -:·|,")
        if not name:
            j = i - 1
            while j >= 0 and (DT_RE.search(lines[j]) or lines[j].lower() in ("live", "replay")):
                j -= 1
            name = lines[j] if j >= 0 else ""
        if not SESSION_WORDS.search(name) or len(name) > 60:
            continue
        if any("cancel" in x.lower() for x in lines[i:i + 3]):
            continue
        month = MONTHS.index(m.group(1).lower()) + 1
        hour = int(m.group(3)) % 12 + (12 if m.group(5).upper() == "PM" else 0)
        try:
            local = datetime(SEASON, month, int(m.group(2)), hour, int(m.group(4)), tzinfo=tz)
        except ValueError:
            continue
        start = local.astimezone(timezone.utc)
        key = (name.lower(), start)
        if key in seen:
            continue
        seen.add(key)
        kind, mins = session_kind_len(name, round_name)
        out.append({"name": name.title().replace("Lmgt3", "LMGT3"), "kind": kind, "day": WEEKDAYS[local.weekday()],
                    "utc": start.strftime("%Y-%m-%dT%H:%M:00Z"),
                    "end_utc": (start + timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:00Z")})
    out.sort(key=lambda s: s["utc"])
    return out


def scrape_schedule(old, rounds, latest_done):
    have = {r["round"]: r for r in old.get("rounds", [])}
    result = []
    for n, (slug, _url) in enumerate(rounds, start=1):
        prev = have.get(n)
        if prev and prev.get("sessions") and n < latest_done - 1:      # old rounds never change
            result.append(prev)
            continue
        html = race_page(slug)
        if not html:
            if prev:
                result.append(prev)
            continue
        soup = BeautifulSoup(html, "lxml")
        name = pretty_name(slug, soup)
        sessions = parse_sessions(html, slug, name)
        if not sessions and prev:
            sessions = prev.get("sessions", [])
        first = sessions[0]["utc"][:10] if sessions else (prev or {}).get("start", "")
        last = sessions[-1]["utc"][:10] if sessions else (prev or {}).get("end", "")
        result.append({"round": n, "name": name, "country": lookup(COUNTRY, slug), "start": first, "end": last,
                       "sessions": sessions})
    old["rounds"] = result
    print(f"schedule: {len(result)} rounds, {sum(len(r['sessions']) for r in result)} sessions")
    return old, len(result)


# ---------------------------------------------------------------- standings
def table_headers(tbl):
    tr = tbl.find("tr")
    return [text_of(c).lower() for c in tr.find_all(["th", "td"])] if tr else []


def heading_before(tbl):
    for prev in tbl.find_all_previous(["h1", "h2", "h3", "h4", "h5", "caption"], limit=3):
        t = text_of(prev)
        if t:
            return t.lower()
    return ""


def classify_table(tbl, idx):
    heads = " ".join(table_headers(tbl))
    if "manufacturer" in heads:
        return "mfr"
    if "team" in heads:
        return "lmgt3_teams"
    return "lmgt3_drivers" if "lmgt3" in heading_before(tbl) or idx >= 3 else "hyper_drivers"


def cell_names(cell):
    links = [text_of(a) for a in cell.find_all("a") if text_of(a)]
    if links:
        return links
    return [x.strip() for x in text_of(cell).split(",") if x.strip()]


def cell_points(txt):
    """'36 +1' -> 37, '-' -> None."""
    nums = re.findall(r"\d+(?:\.\d+)?", txt or "")
    return num(sum(float(x) for x in nums)) if nums else None


def parse_standings_table(tbl):
    """Rows of {pos, car, names, man, rounds:[pts or None], total}."""
    rows = tbl.find_all("tr")
    if not rows:
        return []
    heads = table_headers(tbl)
    ncol = len(heads)
    try:
        total_i = max(i for i, h in enumerate(heads) if "total" in h)
    except ValueError:
        total_i = ncol - 1
    name_i = next((i for i, h in enumerate(heads) if re.search(r"driver|team|manufacturer", h)), 1)
    car_i = next((i for i, h in enumerate(heads) if h.startswith("n")), None)
    man_i = next((i for i, h in enumerate(heads) if h.startswith("man")), None)
    out = []
    for tr in rows[1:]:
        cells = tr.find_all(["td", "th"])
        if len(cells) < total_i + 1:
            continue
        pos = to_int(text_of(cells[0]).rstrip("."), 0)
        names = cell_names(cells[name_i])
        if not names or not pos:
            continue
        man = ""
        if man_i is not None:
            img = cells[man_i].find("img")
            man = (img.get("alt") or img.get("title") or "").strip() if img else text_of(cells[man_i])
        car = re.sub(r"\D", "", text_of(cells[car_i])) if car_i is not None else ""
        rnds = [cell_points(text_of(c)) for c in cells[name_i + 1:total_i]]
        out.append({"pos": pos, "car": car, "names": names, "man": man, "rounds": rnds,
                    "total": cell_points(text_of(cells[total_i])) or 0})
    return out


def parse_standings(html):
    soup = BeautifulSoup(html, "lxml")
    kinds = {}
    for idx, tbl in enumerate(soup.find_all("table")):
        kind = classify_table(tbl, idx)
        if kind in kinds:
            continue
        rows = parse_standings_table(tbl)
        if rows:
            kinds[kind] = rows
    return kinds


def crew_names(kinds):
    """{('Hypercar'|'LMGT3', car number): 'A, B, C'} from the drivers tables."""
    crews = {}
    for kind, cls in (("hyper_drivers", "Hypercar"), ("lmgt3_drivers", "LMGT3")):
        for r in kinds.get(kind, []):
            lst = crews.setdefault((cls, r["car"]), [])
            for n in r["names"]:
                if n not in lst:
                    lst.append(n)
    return {k: ", ".join(v) for k, v in crews.items()}


def entry_name(r, with_surnames):
    if with_surnames:
        sn = " / ".join(surname(n) for n in r["names"])
        return f"#{r['car']} {sn}".strip() if r["car"] else sn
    base = " ".join(r["names"])
    return f"#{r['car']} {base}".strip() if r["car"] and with_surnames is None else base


def build_tables(kinds):
    """-> (standings tables, {'overall/hypercar': {'drivers': rows, 'teams': rows}}, rounds_with_points)"""
    def drv(rows):
        return [{"pos": r["pos"], "name": entry_name(r, True), "team": r["man"], "total": r["total"], "_r": r["rounds"]}
                for r in rows]

    def tms(rows, numbered):
        out = []
        for r in rows:
            nm = " ".join(r["names"])
            out.append({"pos": r["pos"], "name": f"#{r['car']} {nm}" if numbered and r["car"] else nm,
                        "total": r["total"], "_r": r["rounds"]})
        return out

    tables = {
        "hypercar": {"drivers": drv(kinds.get("hyper_drivers", [])), "teams": tms(kinds.get("mfr", []), False)},
        "lmgt3": {"drivers": drv(kinds.get("lmgt3_drivers", [])), "teams": tms(kinds.get("lmgt3_teams", []), True)},
    }
    latest = 0
    for cls in tables.values():
        for rows in cls.values():
            for r in rows:
                for i, v in enumerate(r["_r"], start=1):
                    if v is not None:
                        latest = max(latest, i)
    return tables, latest


def scrape_standings(old, kinds, tables):
    clean = {c: {e: [{k: v for k, v in r.items() if k != "_r"} for r in rows] for e, rows in ents.items()}
             for c, ents in tables.items()}
    n = sum(len(rows) for ents in clean.values() for rows in ents.values())
    if not n:
        print("standings: nothing read, keeping the old file")
        return old, 0
    old["tables"] = {"overall": clean}
    print("standings: " + ", ".join(f"{c} {len(e['drivers'])} crews / {len(e['teams'])} teams" for c, e in clean.items()))
    return old, 1


def scrape_history(old, tables, schedule):
    names = {r["round"]: r["name"] for r in schedule.get("rounds", [])}
    nrounds = max((len(r["_r"]) for ents in tables.values() for rows in ents.values() for r in rows), default=0)
    snaps = []
    for n in range(1, nrounds + 1):
        out, any_points = {}, False
        for cls, ents in tables.items():
            tab = {}
            for ent, rows in ents.items():
                m = {}
                for r in rows:
                    got = [v for v in r["_r"][:n] if v is not None]
                    m[norm(r["name"])] = num(sum(got)) if got else 0
                    if r["_r"][n - 1] is not None:
                        any_points = True
                tab[ent] = m
            out[f"overall/{cls}"] = tab
        if any_points:
            snaps.append({"round": n, "name": names.get(n, f"Round {n}"), "estimated": False, "tables": out})
            print(f"history: round {n} saved", flush=True)
    return {"season": SEASON, "snapshots": snaps}


# ------------------------------------------------------------------ results
def result_link(html):
    """URL of the race classification page, from the race page links."""
    soup = BeautifulSoup(html, "lxml")
    best = None
    for a in soup.find_all("a", href=True):
        if "raceId=" not in a["href"] or "sessionId=" not in a["href"]:
            continue
        t = text_of(a).lower()
        if t == "race":
            return urljoin(BASE, a["href"])
        if "race" in t and "recap" not in t and "summary" not in t and best is None:
            best = urljoin(BASE, a["href"])
    return best


def parse_results(html, crews):
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for idx, tbl in enumerate(soup.find_all("table")):
        heads = table_headers(tbl)
        if not any(h.startswith("pos") for h in heads) or not any("laps" in h for h in heads):
            continue
        head_text = heading_before(tbl)
        cls = "LMGT3" if "lmgt3" in head_text else ("Hypercar" if "hypercar" in head_text else ("Hypercar" if idx == 0 else "LMGT3"))
        col = lambda *keys: next((i for i, h in enumerate(heads) if any(h.startswith(k) for k in keys)), None)  # noqa: E731
        i_pos, i_car, i_team = col("pos"), col("n"), col("team")
        i_laps, i_total, i_gap, i_best = col("laps"), col("total"), col("gap"), col("best")
        first = True
        for tr in tbl.find_all("tr")[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) < len(heads) - 1:
                continue
            g = lambda i: text_of(cells[i]) if i is not None and i < len(cells) else ""  # noqa: E731
            car = re.sub(r"\D", "", g(i_car))
            total, gap = g(i_total), g(i_gap)
            if first:
                time_txt = total
            elif gap and gap != "-":
                time_txt = "+" + gap if re.match(r"^\d", gap) and "lap" not in gap.lower() else gap
            else:
                time_txt = total if total and total != "-" else ""
            rows.append({"pos": to_int(g(i_pos).rstrip("."), len(rows) + 1), "car": car,
                         "drivers": crews.get((cls, car), ""), "team": g(i_team),
                         "model": "", "time": time_txt, "laps": to_int(g(i_laps)), "cls": cls})
            first = False
    return rows


def scrape_results(old, rounds, schedule, latest_done, crews):
    have = {r["round"]: r for r in old.get("rounds", [])}
    refresh = set(sorted(have)[-2:])
    ok = 0
    names = {r["round"]: r for r in schedule.get("rounds", [])}
    for n, (slug, _url) in enumerate(rounds, start=1):
        if n > latest_done:
            continue
        if n in have and n not in refresh and have[n].get("sessions"):
            continue
        html = race_page(slug)
        link = result_link(html) if html else None
        if not link:
            print(f"results round {n}: no classification link yet")
            continue
        page = fetch(link)
        rows = parse_results(page, crews) if page else []
        info = names.get(n, {})
        rname = info.get("name") or pretty_name(slug)
        print(f"results round {n} {rname}: {len(rows)} rows", flush=True)
        if not rows:
            continue
        m = re.search(r"(\d+)\s*hours?", rname, re.I)
        have[n] = {"id": slugify(rname), "round": n, "name": rname,
                   "cup": f"{m.group(1)} Hours" if m else "Race",
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
        rounds = season_rounds()
        print(f"season page: {len(rounds)} rounds")
        html = fetch(STANDINGS_URL)
        kinds = parse_standings(html) if html else {}
        print("standings tables read: " + (", ".join(f"{k}={len(v)}" for k, v in kinds.items()) or "none"))
        tables, latest = build_tables(kinds)
        crews = crew_names(kinds)
        if want("schedule") or want("history") or not new["schedule"].get("rounds"):
            new["schedule"], c = scrape_schedule(new["schedule"], rounds, latest)
            got += c
        if want("standings"):
            new["standings"], c = scrape_standings(new["standings"], kinds, tables)
            got += c
        if want("results"):
            new["results"], c = scrape_results(new["results"], rounds, new["schedule"], latest, crews)
            got += c
            fill_team_from_results(new["standings"], new["results"])
        if want("history") and latest:
            new["history"] = scrape_history(new["history"], tables, new["schedule"])
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
            print(f"wrote wec/{path.name}")
    if not changed:
        print("wec: no changes")
    if blocked or (args.only is None and got == 0):
        print("ERROR: no WEC data could be read.")
        sys.exit(1)


def fill_team_from_results(standings, results):
    """Crew rows get the entry name (e.g. TOYOTA RACING) from the latest race result when known."""
    team_of = {}
    for rnd in results.get("rounds", []):
        for s in rnd.get("sessions", []):
            for r in s.get("rows", []):
                if r.get("car") and r.get("team"):
                    team_of[(r["cls"], r["car"])] = r["team"]
    for cls_key, cls in (("hypercar", "Hypercar"), ("lmgt3", "LMGT3")):
        for r in (((standings.get("tables") or {}).get("overall") or {}).get(cls_key) or {}).get("drivers", []):
            m = re.match(r"#(\d+)", r["name"])
            if m and (cls, m.group(1)) in team_of:
                r["team"] = team_of[(cls, m.group(1))]


if __name__ == "__main__":
    main()
