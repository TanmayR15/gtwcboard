"""Print what the scraper sees in an event's timetable PDF. Run:  python scripts/show_pdf.py 248 monza
(248 = event number, monza = the last part of the event page address). Paste the output to Claude if end times fail."""
import sys

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, __file__.rsplit("scripts", 1)[0] + "scripts")
import scrape

eid, slug = sys.argv[1], sys.argv[2]
html = requests.get(f"{scrape.BASE}/event/{eid}/{slug}", headers=scrape.HEADERS, timeout=30).text
url = scrape.find_timetable_pdf(html)
print("PDF link:", url)
if url:
    data = requests.get(url, headers=scrape.HEADERS, timeout=60).content
    text = scrape.pdf_text(data)
    lines = [l for l in text.splitlines() if l.strip()]
    print("lines:", len(lines))
    for l in lines[:80]:
        print(repr(l))
    print("rows the scraper understands:", len(scrape.parse_timetable_ranges(text)))
