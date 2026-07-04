"""
KVBL scraper — pulls everything the app needs:
  kvbl.net:  Standings (also yields the live team list), team pages
             (ratings / stats / contracts / draft picks / team+opp totals),
             Injuries, Transactions, Finances, FA preview
  Google Sheets (public CSV export): position eligibility, draft grades,
             RFA list, UFA list

All stats are normalized to PER-GAME here. kvbl.net mixes season totals
(fgm/fga/ftm/fta/3gm/3ga) with per-game values (orb/reb/ast/stl/to/blk/pf/ppg)
in the same table, so shooting counts get divided by games played.
"""

import csv
import html as htmlmod
import io
import re
import time
import urllib.request

BASE = "https://www.kvbl.net/"

SHEETS = {
    # name: (spreadsheet id, gid or None for first sheet)
    "eligibility": ("1leBvbv_CEXz6NmP2Pxje-7x8DO5-e73uV-7EZI7IajY", "0"),
    "draft":       ("1pXiH0xjhIlVT-dJhRwWQN3dAOF92yFyCTXwWCuMd1Ds", None),
    "rfa":         ("1eiKWEtdjo_Ax99zv5g8jJIkuTZM9x2f_eg-KgGUlEPo", "0"),
    "ufa":         ("1dwYxpoHY7rmwTFRO7d-yol43E8kZZ8tJlFL3EjUekW0", "1665067806"),
}

RATING_COLS = ["2ga", "2g%", "fta", "ft%", "3ga", "3g%",
               "orb", "drb", "ast", "stl", "to", "blk",
               "o-o", "d-o", "p-o", "t-o", "o-d", "d-d", "p-d", "t-d"]

# stats table columns that are season TOTALS (everything else is per-game)
TOTAL_COLS = {"fgm", "fga", "ftm", "fta", "3gm", "3ga"}
STAT_COLS = ["g", "gs", "min", "fgm", "fga", "fgp", "ftm", "fta", "ftp",
             "3gm", "3ga", "3gp", "orb", "reb", "ast", "stl", "to", "blk",
             "pf", "ppg"]


def fetch(url, tries=3):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 KVBL-Tool/2.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


def strip_tags(s):
    return htmlmod.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def page_rows(page_html):
    """Whole page as a flat list of rows (each row = list of cell strings)."""
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", page_html, flags=re.S | re.I)
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S | re.I):
        cells = [strip_tags(c) for c in
                 re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)]
        if any(c for c in cells):
            rows.append(cells)
    return rows


def num(v, default=0.0):
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return default


# ────────────────────────────────────────────────────────────
# Standings → team list + records
# ────────────────────────────────────────────────────────────
def scrape_standings():
    rows = page_rows(fetch(BASE + "Standings.htm"))
    teams, conference = [], ""
    for r in rows:
        low = [c.lower() for c in r]
        if len(r) == 1 and r[0] and "standings" not in low[0] and "-" not in r[0]:
            conference = r[0].strip()
            continue
        if "team" in low and "pct" in low:
            continue
        if len(r) >= 3 and re.match(r"^\d+-\d+$", r[1].strip()):
            name = r[0].strip()
            if any(t["team"] == name for t in teams):
                continue          # division tables repeat the conference tables
            w, l = r[1].strip().split("-")
            teams.append({
                "team": name, "conf": conference,
                "w": int(w), "l": int(l), "pct": num(r[2]),
                "gb": r[3].strip() if len(r) > 3 else "",
            })
    return teams


# ────────────────────────────────────────────────────────────
# Team page → ratings, stats (per-game), contracts, picks, team totals
# ────────────────────────────────────────────────────────────
def team_page_name(team):
    """Standings name → page name (strip spaces; e.g. 'Trail Blazers' page quirks)."""
    return team.replace(" ", "")


def scrape_team(team):
    """Section titles on team pages are not table rows, so sections are
    identified by their header signatures:
      ratings:   header contains 'o-o'
      stats:     header contains 'fgm' (1st occurrence = season, 2nd = playoffs)
      contracts: header contains 'year 1'
      picks:     rows matching 'YYYY-YYYY round N'
    """
    page = fetch(BASE + team_page_name(team) + ".htm")
    rows = page_rows(page)

    section = None
    header = {}
    players = {}          # name -> player dict
    contracts = []
    picks = []
    team_off = team_def = None
    stats_seen = 0

    for r in rows:
        first = r[0].strip().lower() if r else ""
        low = [c.strip().lower() for c in r]

        if "player" in low:                       # header row → detect section
            # section titles ('Contracts', …) can appear as extra leading
            # header cells that data rows don't have — anchor on the first
            # real column name ('po'/'pos') so indices line up with data rows
            skip = next((i for i, c in enumerate(low) if c in ("po", "pos")),
                        low.index("player"))
            header = {c: i - skip for i, c in enumerate(low) if c and i >= skip}
            if "o-o" in header:
                section = "ratings"
            elif "fgm" in header:
                stats_seen += 1
                section = "stats" if stats_seen == 1 else "playoffs"
            elif "year 1" in header:
                section = "contracts"
            else:
                section = None
            continue

        if re.match(r"^\d{4}-\d{4}\s+round\s+\d", first):
            picks.append({"pick": r[0].strip(),
                          "from": r[1].strip() if len(r) > 1 else ""})
            continue

        if not header or "player" not in header:
            continue
        name = r[header["player"]].strip() if len(r) > header["player"] else ""

        if section == "ratings" and name:
            pos = r[header.get("po", 0)].strip().upper()
            if pos not in ("PG", "SG", "SF", "PF", "C", "G", "F"):
                continue
            p = players.setdefault(name, {"name": name, "team": team})
            p["pos"] = pos
            p["age"] = int(num(r[header.get("age", 2)]))
            p["r"] = {c: int(num(r[header[c]])) for c in RATING_COLS if c in header}

        elif section == "stats":
            if first in ("offense", "defense"):
                g = num(r[header.get("g", 2)])
                tot = {}
                for c in STAT_COLS:
                    if c in header and len(r) > header[c]:
                        v = num(r[header[c]])
                        tot[c] = round(v / g, 3) if (c in TOTAL_COLS and g > 0) else v
                tot["g"] = g
                if first == "offense":
                    team_off = tot
                else:
                    team_def = tot
            elif name:
                p = players.setdefault(name, {"name": name, "team": team})
                g = num(r[header.get("g", 2)])
                st = {}
                for c in STAT_COLS:
                    if c in header and len(r) > header[c]:
                        v = num(r[header[c]])
                        st[c] = round(v / g, 3) if (c in TOTAL_COLS and g > 0) else v
                st["g"] = g
                p["s"] = st

        elif section == "contracts" and name:
            years = []
            for c in ("year 1", "year 2", "year 3", "year 4", "year 5", "year 6"):
                if c in header and len(r) > header[c] and r[header[c]].strip():
                    years.append(num(r[header[c]]))
            contracts.append({"name": name, "years": years})
            if name in players:
                players[name]["contract"] = years

    return {"team": team, "players": list(players.values()),
            "contracts": contracts, "picks": picks,
            "team_off": team_off, "team_def": team_def}


# ────────────────────────────────────────────────────────────
# Injuries / Transactions / Finances / FA preview
# ────────────────────────────────────────────────────────────
def scrape_injuries():
    rows = page_rows(fetch(BASE + "Injuries.htm"))
    out = []
    for r in rows:
        if len(r) >= 7 and r[0].strip().isdigit():
            out.append({"date": f"{r[0]}/{r[1]}/{r[2]}", "name": r[3].strip(),
                        "team": r[4].strip(), "days": r[5].strip(),
                        "injury": r[6].strip()})
    return out


def scrape_transactions(limit=60):
    rows = page_rows(fetch(BASE + "Transactions.htm"))
    out = []
    for r in rows:
        if len(r) >= 4 and r[0].strip().isdigit():
            date = f"{r[0]}/{r[1]}/{r[2]}"
            rest = " ".join(c.strip() for c in r[3:] if c.strip())
            if rest:
                out.append({"date": date, "text": rest})
    return out[-limit:][::-1]   # newest first


def scrape_finances():
    rows = page_rows(fetch(BASE + "finances.htm"))
    header, out = {}, []
    for r in rows:
        low = [c.strip().lower() for c in r]
        if "team" in low and "salary" in low:
            header = {c: i for i, c in enumerate(low)}
            continue
        if header and len(r) > header.get("salary", 1) and r[0].strip() and num(r[header["salary"]], -1) >= 0:
            if r[0].strip().lower() in ("team",) or any(o["team_city"] == r[0].strip() for o in out):
                continue
            out.append({"team_city": r[0].strip(),
                        "salary": num(r[header["salary"]]),
                        "profit": num(r[header["profit"]]) if "profit" in header and len(r) > header["profit"] else None})
    return out


def scrape_fapreview():
    rows = page_rows(fetch(BASE + "fapreview.htm"))
    header, out = {}, []
    for r in rows:
        low = [c.strip().lower() for c in r]
        if "player" in low and "2ga" in low:
            header = {c: i for i, c in enumerate(low)}
            continue
        if header and len(r) > header["player"]:
            name = r[header["player"]].strip()
            pos = r[header.get("po", 0)].strip().upper()
            if not name or pos not in ("PG", "SG", "SF", "PF", "C", "G", "F"):
                continue
            out.append({"name": name, "pos": pos,
                        "age": int(num(r[header.get("age", 2)])),
                        "r": {c: int(num(r[header[c]])) for c in RATING_COLS if c in header}})
    return out


# ────────────────────────────────────────────────────────────
# Google Sheets
# ────────────────────────────────────────────────────────────
def fetch_sheet(key):
    sid, gid = SHEETS[key]
    url = f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv"
    if gid is not None:
        url += f"&gid={gid}"
    return list(csv.reader(io.StringIO(fetch(url))))


def scrape_eligibility():
    """name -> list of eligible positions, e.g. 'PF/SF/C' -> ['PF','SF','C']"""
    out = {}
    for row in fetch_sheet("eligibility"):
        if len(row) >= 2 and row[0].strip() and row[0].strip().lower() != "pos":
            out[row[1].strip()] = [p.strip().upper() for p in row[0].split("/") if p.strip()]
    return out


def scrape_draft_sheet():
    rows = fetch_sheet("draft")
    if not rows:
        return []
    header = [c.strip().lower() for c in rows[0]]
    idx = {c: i for i, c in enumerate(header)}
    out = []
    for row in rows[1:]:
        if len(row) < 2 or not row[idx.get("name", 1)].strip():
            continue
        out.append({"pos": row[idx.get("pos", 0)].strip(),
                    "name": row[idx.get("name", 1)].strip(),
                    "age": int(num(row[idx.get("age", 2)])) if len(row) > idx.get("age", 2) else 0,
                    "grade": row[idx.get("grade", 3)].strip() if len(row) > idx.get("grade", 3) else ""})
    return out


def scrape_fa_sheet(key):
    """RFA/UFA sheets: same rating columns as team pages, plus 'yrs' on UFA."""
    rows = fetch_sheet(key)
    if not rows:
        return []
    header = [c.strip().lower() for c in rows[0]]
    idx = {c: i for i, c in enumerate(header)}
    out = []
    for row in rows[1:]:
        if len(row) < 3 or not row[idx.get("player", 1)].strip():
            continue
        pos = row[idx.get("po", 0)].strip().upper()
        if pos not in ("PG", "SG", "SF", "PF", "C", "G", "F"):
            continue
        p = {"name": row[idx["player"]].strip(), "pos": pos,
             "age": int(num(row[idx.get("age", 2)])),
             "r": {c: int(num(row[idx[c]])) for c in RATING_COLS if c in idx and len(row) > idx[c]}}
        if "yrs" in idx and len(row) > idx["yrs"]:
            p["yrs"] = int(num(row[idx["yrs"]]))
        out.append(p)
    return out


# ────────────────────────────────────────────────────────────
# Everything
# ────────────────────────────────────────────────────────────
def scrape_all(log=print):
    data = {}
    log("Standings...")
    data["standings"] = scrape_standings()
    teams = [t["team"] for t in data["standings"]]
    log(f"  {len(teams)} teams: {', '.join(teams)}")

    data["teams"] = {}
    for t in teams:
        try:
            data["teams"][t] = scrape_team(t)
            log(f"  {t}: {len(data['teams'][t]['players'])} players")
        except Exception as e:
            log(f"  [WARN] {t}: {e}")

    for key, fn in [("injuries", scrape_injuries),
                    ("transactions", scrape_transactions),
                    ("finances", scrape_finances),
                    ("fapreview", scrape_fapreview),
                    ("eligibility", scrape_eligibility),
                    ("draft", scrape_draft_sheet)]:
        try:
            log(f"{key}...")
            data[key] = fn()
        except Exception as e:
            log(f"  [WARN] {key}: {e}")
            data[key] = [] if key != "eligibility" else {}

    for key in ("rfa", "ufa"):
        try:
            log(f"{key} sheet...")
            data[key] = scrape_fa_sheet(key)
        except Exception as e:
            log(f"  [WARN] {key}: {e}")
            data[key] = []

    return data
