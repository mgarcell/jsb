"""
KVBL app builder.

  python build.py            scrape everything, compute metrics, write ../docs/
  python build.py --cached   reuse last scrape (kvbl_app/.cache.json), re-render only

Output:
  docs/index.html   the app — league data embedded as base64 so player names
                    are not plain-text searchable on GitHub / search engines
  docs/robots.txt   tells search engines not to index the app
"""

import base64
import json
import os
import sys
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scrape
import metrics

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(os.path.dirname(HERE), "docs")
CACHE = os.path.join(HERE, ".cache.json")
HISTORY = os.path.join(HERE, "ratings_history.json")


def update_history(data):
    """Append a ratings snapshot per player WHENEVER THEIR RATINGS CHANGE
    (they only change at season rollovers, so the file grows one entry per
    player per season).  This is the raw material for a future aging /
    progression model — the app itself only gets a tiny 'what changed last'
    diff per player, so current-season views stay uncluttered."""
    try:
        with open(HISTORY, encoding="utf-8") as f:
            hist = json.load(f)
    except Exception:
        hist = {}

    today = datetime.date.today().isoformat()
    season = data.get("league", {}).get("season", 0)
    everyone = ([p for td in data["teams"].values() for p in td["players"]]
                + data.get("fa_pool", []))
    changed = 0
    for p in everyone:
        r = p.get("r")
        if not r:
            continue
        key = scrape.norm_name(p["name"])
        entries = hist.setdefault(key, [])
        if entries and entries[-1]["r"] == r:
            pass                                  # unchanged — no churn
        else:
            entries.append({"d": today, "season": season, "name": p["name"],
                            "team": p.get("team", ""), "pos": p.get("pos", ""),
                            "age": p.get("age", 0), "r": r})
            changed += 1
        if len(entries) >= 2:
            prev, last = entries[-2], entries[-1]
            diff = {k: v - prev["r"].get(k, 0)
                    for k, v in last["r"].items() if v != prev["r"].get(k, 0)}
            if diff:
                p["rchg"] = {"d": prev["d"], "diff": diff}

    with open(HISTORY, "w", encoding="utf-8") as f:
        json.dump(hist, f, separators=(",", ":"))
    print(f"History: {len(hist)} players tracked, {changed} new snapshots")


def main():
    os.makedirs(DOCS, exist_ok=True)

    if "--cached" in sys.argv and os.path.exists(CACHE):
        print("Using cached scrape")
        with open(CACHE, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = scrape.scrape_all()
        data = metrics.compute_all(data)
        update_history(data)
        data["built"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))

    with open(os.path.join(HERE, "template.html"), encoding="utf-8") as f:
        tpl = f.read()

    payload = base64.b64encode(
        json.dumps(data, separators=(",", ":")).encode("utf-8")).decode("ascii")
    html = tpl.replace("/*__DATA_B64__*/", payload)

    out = os.path.join(DOCS, "index.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(DOCS, "robots.txt"), "w") as f:
        f.write("User-agent: *\nDisallow: /\n")
    # remove the old plain-text snapshot if it's still around
    old = os.path.join(DOCS, "data.json")
    if os.path.exists(old):
        os.remove(old)
    print(f"Wrote {out}  ({len(html)//1024} KB)")


if __name__ == "__main__":
    main()
