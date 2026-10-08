"""Show how the scraper reads an event's timetable PDF. Run:  python scripts/show_pdf.py 248 monza
(248 = event number, monza = last part of the event page address). Paste the output to Claude if end times fail."""
import sys

import requests

sys.path.insert(0, __file__.rsplit("scripts", 1)[0] + "scripts")
import scrape

eid, slug = sys.argv[1], sys.argv[2]
html = requests.get(f"{scrape.BASE}/event/{eid}/{slug}", headers=scrape.HEADERS, timeout=30).text
url = scrape.find_timetable_pdf(html)
print("PDF link:", url)
if url:
    text = scrape.pdf_text(requests.get(url, headers=scrape.HEADERS, timeout=60).content)
    keep = [l for l in text.splitlines() if l.strip() and ":" in l and any(w in l.lower() for w in scrape._SESSION_WORDS)]
    print("lines with a time and a session word:", len(keep))
    for l in keep[:45]:
        print(repr(l))
    rows = scrape.parse_timetable_ranges(text)
    print("rows the scraper understands:", len(rows))
    for r in rows[:25]:
        print("  ", r["start"] // 60, ":", r["start"] % 60, "->", r["end"] // 60, ":", r["end"] % 60, "|", r["text"][:70])
