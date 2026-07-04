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


def main():
    os.makedirs(DOCS, exist_ok=True)

    if "--cached" in sys.argv and os.path.exists(CACHE):
        print("Using cached scrape")
        with open(CACHE, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = scrape.scrape_all()
        data = metrics.compute_all(data)
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
