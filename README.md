<div align="center">

# 🏁 GTWC EUROPE PIT BOARD

**A retro pixel-style dashboard for the GT World Challenge Europe season.**
Standings, race results, schedule in IST, points guide and points progression. All in one fast static page.

[![Live site](https://img.shields.io/badge/LIVE%20SITE-open-FF9F1C?style=for-the-badge)](https://tanmayr15.github.io/gtwcboard/)
![Series](https://img.shields.io/badge/SERIES-GTWC%20EUROPE-38B6FF?style=for-the-badge)
![Updates](https://img.shields.io/badge/UPDATES-AUTOMATIC-7ED957?style=for-the-badge)
![Cost](https://img.shields.io/badge/COST-FREE-FF5A36?style=for-the-badge)

**https://tanmayr15.github.io/gtwcboard/**

</div>

---

## 🕹️ What it does

- **Series dropdown**: switch between GTWC Europe, Formula 1, WEC and IGTC. The choice is remembered on every page. Formula 1 is live; WEC and IGTC are coming.
- **Home**: a landing page with a live countdown to the next session, the season progress, the championship leaders and a round selector. Two looks to pick from: the arcade style and the paddock tower.
- **Standings**: Overall, Sprint Cup and Endurance Cup, split by class (Overall, Gold, Silver, Bronze), for drivers or teams, with search.
- **Results**: every race classification, round by round, including the Spa 24 Hours checkpoints.
- **Schedule**: every round with all sessions in **IST**, end times, days left, Sprint or Endurance tags, and DONE / LIVE NOW badges.
- **Progression**: a line chart and a table of how points grew round by round.
- **Points guide (?)**: how many points each position earns in every race type, with colours for each cup.

## 🎨 Look and feel

- Pixel fonts, scanlines and chunky borders, in the style of an old arcade terminal.
- Colour code used everywhere: **orange** for Sprint, **blue** for Endurance, **amber** for Overall, and gold, silver and bronze for the classes.
- Original pixel logo (a chequered pit board with "GT").
- No frameworks, no build step. Just HTML, CSS and plain JavaScript.

## ⚙️ How it works

```
 official GTWC Europe site
          │   (polite scraper, a few requests per run)
          ▼
   scripts/scrape.py ──► data/standings.json
                    ├──► data/results.json
                    ├──► data/schedule.json
                    └──► data/history.json
          │
          ▼   committed by GitHub Actions
   index.html  reads the JSON files in the browser
          │
          ▼
   GitHub Pages serves the site
```

1. A scheduled **GitHub Action** runs the scraper after every race day (Saturday and Sunday night, and Monday morning, UTC).
2. The scraper only commits when the data actually changed. If a fetch fails, the old data is kept.
3. GitHub Pages redeploys, and the site shows the new numbers.

## 🗂️ Repository layout

| Path | What it is |
| --- | --- |
| `index.html` | The whole website (markup, styles and scripts in one file) |
| `data/standings.json` | Championship tables for every cup, class and entity |
| `data/results.json` | Race classifications for each round |
| `data/schedule.json` | Sessions in UTC, with end times where the official timetable lists them |
| `data/history.json` | Points after each round, for the progression chart |
| `scripts/scrape.py` | The scraper |
| `scripts/history.py` | Builds the points history |
| `scripts/f1.py` | Formula 1 updater (uses a free data API), writes `data/f1/` |
| `data/f1/` | Formula 1 standings, results, schedule and history |
| `scripts/show_pdf.py` | Helper to inspect an official timetable PDF |
| `logo.svg` | Site logo and tab icon |
| `.github/workflows/update.yml` | The scheduled update job |
| `requirements.txt` | Python dependencies |

## 🧪 Run it locally

```bash
# 1. serve the site (the page loads JSON files, so opening the file directly will not work)
python -m http.server
# then open http://localhost:8000

# 2. refresh the data (optional)
pip install -r requirements.txt
python scripts/scrape.py                 # everything
python scripts/scrape.py --only results  # or just one part: standings | results | schedule
```

## 🔄 Run an update by hand

Go to **Actions → Update GTWC data → Run workflow**.

## 📝 Good to know

- **Times** are stored in UTC and converted to IST in the browser.
- **Sprint or Endurance** is worked out from the number of race sessions in a round, so a changed calendar needs no code change.
- **Estimated history**: rounds from before the history file existed are estimated from race results (poles and class rules are not included). They are marked with `*` and dashed lines. Every later round is saved exactly from the official standings.
- Some official timetable PDFs only cover part of an event, so a few sessions can be missing an end time.

## ⚖️ Disclaimer

This is an unofficial fan project and is not affiliated with, endorsed by or connected to SRO Motorsports Group or the GT World Challenge Europe. All series names and data belong to their owners. Data is read from the official public website at a very low rate for personal use.

## 📜 Licence

© Tanmay Rastogi. All rights reserved. The code in this repository may not be copied, reused or redistributed without permission.
