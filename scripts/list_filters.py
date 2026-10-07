"""Print the real filter options on the official standings page.

Run once from the gtwc-site folder:  python scripts/list_filters.py
Then paste the output back to Claude.
"""
import sys

import requests
from bs4 import BeautifulSoup

sys.stdout.reconfigure(encoding="utf-8")

URL = "https://www.gt-world-challenge-europe.com/standings"
html = requests.get(URL, headers={"User-Agent": "gtwc-standings-personal-project/1.0"}, timeout=30).text
soup = BeautifulSoup(html, "lxml")

print("=== <select> dropdowns ===")
for sel in soup.find_all("select"):
    print("SELECT name=%r id=%r" % (sel.get("name"), sel.get("id")))
    for o in sel.find_all("option"):
        print("   value=%r | %s" % (o.get("value"), o.get_text(strip=True)))

print()
print("=== links containing 'filter' ===")
seen = set()
for a in soup.find_all("a", href=True):
    if "filter" in a["href"] and a["href"] not in seen:
        seen.add(a["href"])
        print("  ", a["href"], "|", a.get_text(strip=True))

print()
print("=== forms ===")
for f in soup.find_all("form"):
    print("FORM action=%r method=%r" % (f.get("action"), f.get("method")))
