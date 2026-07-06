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
import json
import os
import re
import subprocess
import time
import unicodedata
import urllib.request

# last-good forum data, committed to the repo: GitHub Actions runners are
# sometimes served the forum's bot challenge, local builds rarely are —
# whenever a forum scrape fails, the previous successful result is used
FCACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "forum_cache.json")


def load_fcache():
    try:
        with open(FCACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_fcache(fc):
    try:
        with open(FCACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(fc, f, separators=(",", ":"))
    except Exception:
        pass

BASE = "https://www.kvbl.net/"
FORUM = "https://kvbl.boards.net"


def norm_name(n):
    """Accent/case-insensitive matching key ('Felício Nzola' == 'Felicio Nzola')."""
    n = unicodedata.normalize("NFKD", n)
    n = "".join(c for c in n if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", n).strip().lower()

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
    # boards.net serves a JS proof-of-work challenge to Python's TLS stack but
    # not to curl, so forum pages go through the curl binary (present on
    # Windows 10+ and on GitHub Actions runners)
    if "boards.net" in url:
        return fetch_curl(url, tries)
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


try:                       # browser-TLS impersonation; needed on CI runners
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None


def is_challenge(body):
    return "POW_CHALLENGE" in body or "challenge_nonce" in body


def fetch_curl(url, tries=3):
    """boards.net fetch: curl_cffi (Chrome TLS fingerprint) when installed,
    else the curl binary.  The proof-of-work challenge page counts as a
    failure so callers can fall back to cached forum data."""
    last = None
    for attempt in range(tries):
        try:
            if cffi_requests is not None:
                r = cffi_requests.get(url, impersonate="chrome", timeout=30)
                body = r.text
            else:
                # NOTE: no browser -A header — the forum challenges when the UA
                # claims a browser but the TLS handshake isn't one.  curl's
                # default identity passes; curl_cffi impersonates both at once.
                out = subprocess.run(
                    ["curl", "-sL", "--max-time", "30", url],
                    capture_output=True, timeout=45)
                if out.returncode != 0:
                    raise RuntimeError(f"curl rc={out.returncode}")
                body = out.stdout.decode("utf-8", errors="replace")
            if body and not is_challenge(body):
                return body
            last = RuntimeError("forum served bot challenge")
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
# Forum (ProBoards)
# ────────────────────────────────────────────────────────────
def forum_threads(board_path, pages=1):
    """Thread list for a board, in page order (newest activity first).
    Returns [{url, title, date}] — date is the nearest timestamp after the
    link in the HTML (the row's last-post time), best-effort."""
    out, seen = [], set()
    for pg in range(1, pages + 1):
        try:
            page = fetch(FORUM + board_path + (f"?page={pg}" if pg > 1 else ""))
        except Exception:
            break
        before = len(out)
        for m in re.finditer(r'href="(/thread/(\d+)/[^"]*)"[^>]*>(.*?)</a>', page):
            href, tid, title = m.group(1), m.group(2), strip_tags(m.group(3))
            if tid in seen or not title:
                continue
            seen.add(tid)
            tail = page[m.end():m.end() + 3000]
            dm = re.search(r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w* \d+, \d{4})', tail)
            out.append({"url": FORUM + href, "title": title,
                        "date": dm.group(1) if dm else ""})
        if len(out) == before:      # past the last page
            break
    return out


def thread_posts(thread_url):
    """Post bodies (tag-stripped text) of a thread's first page, with a
    best-effort timestamp for each."""
    page = fetch(thread_url)
    out = []
    for m in re.finditer(r'<div[^>]*class="[^"]*message[^"]*"[^>]*>(.*?)</div>', page, re.S):
        body = re.sub(r"\s+", " ", strip_tags(m.group(1))).strip()
        if not body:
            continue
        head = page[max(0, m.start() - 4000):m.start()]
        dm = re.findall(r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w* \d+, \d{4})', head)
        out.append({"date": dm[-1] if dm else "", "text": body})
    return out


def scrape_forum_transactions(limit=15):
    """Official transactions = threads on the KVBL Transactions board.
    Each trade thread's first post carries the asset lists ('X receives: …'),
    kept raw here and evaluated after metrics are computed."""
    out = []
    for t in forum_threads("/board/8/kvbl-transactions"):
        if t["title"].lower() in ("extensions",):     # pinned reference thread
            continue
        item = {"date": t["date"], "text": t["title"], "url": t["url"], "kind": "trade"}
        try:
            posts = thread_posts(t["url"])
            if posts:
                item["body"] = posts[0]["text"][:1500]
        except Exception:
            pass
        out.append(item)
        if len(out) >= limit:
            break
    return out


MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


def date_key(d):
    m = re.match(r"([A-Za-z]{3})\w* (\d+), (\d{4})", d or "")
    return (int(m.group(3)), MONTHS.get(m.group(1), 0), int(m.group(2))) if m else (0, 0, 0)


def merge_by_date(a, b, limit=15):
    return sorted(a + b, key=lambda t: date_key(t.get("date")), reverse=True)[:limit]


def scrape_extensions(limit=8):
    """Latest posts in the pinned Extensions thread."""
    posts = thread_posts(FORUM + "/thread/3328/extensions")
    out = [{"date": p["date"], "text": p["text"][:300], "kind": "ext",
            "url": FORUM + "/thread/3328/extensions"}
           for p in posts if len(p["text"]) > 20]
    return out[-limit:][::-1]


def scrape_fa_results(year):
    """The board-12 results thread for a given FA year.  Returns meta + the
    full text of every phase post (RFA offers, RFA matching, UFA bid 1/2)."""
    hit = None
    for t in forum_threads("/board/12/kvbl-free-agency-results"):
        m = re.search(r"(\d{4})", t["title"])
        if m and int(m.group(1)) == year:
            hit = t
            break
    if not hit:
        return None
    phases = [p["text"] for p in thread_posts(hit["url"])]
    return {"year": year, "url": hit["url"], "title": hit["title"], "phases": phases}


def scrape_dc_threads():
    """Depth-chart board: map team nickname -> that team's DC thread url.
    Thread titles are freeform ('Nuggets DC', 'OKC Thunder Depth Chart'), so
    the lookup key includes both title and url slug; the board spans pages."""
    out = {}
    for t in forum_threads("/board/10/kvbl-depth-charts", pages=3):
        out[(t["title"] + " " + t["url"].rsplit("/", 1)[-1]).lower()] = t["url"]
    return out


def scrape_available():
    """kvbl.net/Available.htm — unsigned players who can be signed right now.
    Same ratings-table format as the FA preview page."""
    rows = page_rows(fetch(BASE + "Available.htm"))
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


def latest_thread(board_path, pattern):
    """Newest thread on a board whose title matches pattern (highest year wins)."""
    best = None
    for t in forum_threads(board_path):
        m = re.search(pattern, t["title"], re.I)
        if m:
            yr = int(m.group(1))
            if best is None or yr > best[0]:
                best = (yr, t)
    return best  # (year, thread) or None


def sheet_links(thread_url):
    """All Google-Sheets links in a thread (all pages), with doc id + gid."""
    links, seen = [], set()
    for pg in range(1, 6):
        try:
            page = fetch(f"{thread_url}?page={pg}")
        except Exception:
            break
        found = re.findall(r'href="(https?://docs\.google\.com/spreadsheets/[^"]+)"', page)
        if pg > 1 and not found:
            break
        for url in found:
            im = re.search(r"/d/([A-Za-z0-9_-]+)", url)
            gm = re.search(r"[#&?]gid=(\d+)", url)
            if im and im.group(1) not in seen:
                seen.add(im.group(1))
                links.append((im.group(1), gm.group(1) if gm else None))
        if "?page=" not in page or f"page={pg+1}" not in page:
            break
    return links


def fetch_sheet_by_id(sid, gid=None):
    url = f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv"
    if gid is not None:
        url += f"&gid={gid}"
    return list(csv.reader(io.StringIO(fetch(url))))


def parse_fa_rows(rows):
    """FA-sheet rows → player dicts; None if the sheet isn't an FA ratings list."""
    if not rows:
        return None
    header = [c.strip().lower() for c in rows[0]]
    if "player" not in header or "2ga" not in header:
        return None
    idx = {c: i for i, c in enumerate(header)}
    out = []
    for row in rows[1:]:
        if len(row) < 3 or not row[idx["player"]].strip():
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
    return out if out else None


def discover_fa(log=print):
    """Find the newest 'YYYY KVBL Free Agency' thread on the general board and
    load its RFA/UFA sheets.  UFA sheets carry a 'yrs' (service years) column;
    that's how the two are told apart.  Returns (meta, rfa, ufa) with None
    lists when discovery fails (caller falls back to configured sheet ids)."""
    hit = latest_thread("/board/9/kvbl-general", r"(\d{4})\s+KVBL\s+Free\s+Agency")
    if not hit:
        return None, None, None
    year, th = hit
    meta = {"year": year, "title": th["title"], "url": th["url"],
            "complete": "complete" in th["title"].lower()}
    rfa = ufa = None
    for sid, gid in sheet_links(th["url"]):
        try:
            players = parse_fa_rows(fetch_sheet_by_id(sid, gid))
        except Exception as e:
            log(f"  [WARN] FA sheet {sid[:8]}…: {e}")
            continue
        if not players:
            continue
        if any("yrs" in p for p in players):
            ufa = ufa or players
        else:
            rfa = rfa or players
    return meta, rfa, ufa


def discover_draft(log=print):
    """Find the newest 'YYYY KVBL Draft' thread and any grades sheet inside it."""
    hit = latest_thread("/board/9/kvbl-general", r"(\d{4})\s+KVBL\s+Draft")
    if not hit:
        return None, None
    year, th = hit
    meta = {"year": year, "title": th["title"], "url": th["url"],
            "complete": "complete" in th["title"].lower()}
    for sid, gid in sheet_links(th["url"]):
        try:
            rows = fetch_sheet_by_id(sid, gid)
            header = [c.strip().lower() for c in rows[0]] if rows else []
            if "name" in header and "grade" in header:
                return meta, parse_draft_rows(rows)
        except Exception as e:
            log(f"  [WARN] draft sheet {sid[:8]}…: {e}")
    return meta, None


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
    return parse_draft_rows(fetch_sheet("draft"))


def parse_draft_rows(rows):
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
                    ("finances", scrape_finances),
                    ("fapreview", scrape_fapreview),
                    ("eligibility", scrape_eligibility)]:
        try:
            log(f"{key}...")
            data[key] = fn()
        except Exception as e:
            log(f"  [WARN] {key}: {e}")
            data[key] = [] if key != "eligibility" else {}

    fc = load_fcache()

    # transactions: forum board (cached), then extensions, then site-page fallback
    try:
        log("forum transactions...")
        tx = scrape_forum_transactions()
        if not tx:
            raise ValueError("empty")
        try:
            tx = merge_by_date(tx, scrape_extensions())
        except Exception as e:
            log(f"  [WARN] extensions: {e}")
        data["transactions"] = fc["transactions"] = tx
    except Exception as e:
        log(f"  [WARN] forum transactions ({e}), using {'cache' if fc.get('transactions') else 'site page'}")
        if fc.get("transactions"):
            data["transactions"] = fc["transactions"]
        else:
            try:
                data["transactions"] = scrape_transactions()
            except Exception as e2:
                log(f"  [WARN] transactions: {e2}")
                data["transactions"] = []

    try:
        log("available...")
        data["available"] = scrape_available()
    except Exception as e:
        log(f"  [WARN] available: {e}")
        data["available"] = []

    try:
        log("dc threads...")
        data["dc_threads"] = fc["dc_threads"] = scrape_dc_threads() or fc.get("dc_threads", {})
    except Exception as e:
        log(f"  [WARN] dc threads ({e}), using cache")
        data["dc_threads"] = fc.get("dc_threads", {})

    # FA pools: discover the newest FA thread's sheets; cache; configured ids last
    try:
        log("FA thread discovery...")
        meta, rfa, ufa = discover_fa(log)
    except Exception as e:
        log(f"  [WARN] FA discovery: {e}")
        meta = rfa = ufa = None
    data["fa_meta"] = fc["fa_meta"] = meta or fc.get("fa_meta") or {}
    if meta:
        fc["fa_meta"] = meta
    for key, found in (("rfa", rfa), ("ufa", ufa)):
        if found:
            data[key] = fc[key] = found
            log(f"  {key}: {len(found)} from thread")
        elif fc.get(key):
            data[key] = fc[key]
            log(f"  {key}: {len(data[key])} from cache")
        else:
            try:
                data[key] = scrape_fa_sheet(key)
                log(f"  {key}: {len(data[key])} from configured sheet")
            except Exception as e:
                log(f"  [WARN] {key}: {e}")
                data[key] = []

    # FA results thread (board 12): phase posts for the current FA year
    data["fa_results"] = None
    if data["fa_meta"].get("year"):
        try:
            log("FA results thread...")
            data["fa_results"] = fc["fa_results"] = (
                scrape_fa_results(data["fa_meta"]["year"]) or fc.get("fa_results"))
        except Exception as e:
            log(f"  [WARN] fa results ({e}), using cache")
            data["fa_results"] = fc.get("fa_results")

    # draft board: discover the newest draft thread's grades sheet; cache; configured last
    try:
        log("draft thread discovery...")
        dmeta, dlist = discover_draft(log)
    except Exception as e:
        log(f"  [WARN] draft discovery: {e}")
        dmeta = dlist = None
    data["draft_meta"] = fc["draft_meta"] = dmeta or fc.get("draft_meta") or {}
    if dlist:
        data["draft"] = fc["draft"] = dlist
        log(f"  draft: {len(dlist)} from thread")
    elif fc.get("draft"):
        data["draft"] = fc["draft"]
        log(f"  draft: {len(data['draft'])} from cache")
    else:
        try:
            data["draft"] = scrape_draft_sheet()
            log(f"  draft: {len(data['draft'])} from configured sheet")
        except Exception as e:
            log(f"  [WARN] draft: {e}")
            data["draft"] = []

    save_fcache(fc)
    return data
