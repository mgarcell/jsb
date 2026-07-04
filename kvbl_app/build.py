"""
KVBL app builder.

  python build.py            scrape everything, compute metrics, write ../docs/
  python build.py --cached   reuse last scrape (docs/data.json) and just re-render

Output:
  docs/index.html   the app (template.html with data embedded)
  docs/data.json    raw data snapshot, for debugging / reuse
"""

import json
import os
import sys
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scrape
import metrics

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(os.path.dirname(HERE), "docs")


def main():
    os.makedirs(DOCS, exist_ok=True)
    data_path = os.path.join(DOCS, "data.json")

    if "--cached" in sys.argv and os.path.exists(data_path):
        print("Using cached data.json")
        with open(data_path, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = scrape.scrape_all()
        data = metrics.compute_all(data)
        data["built"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))

    with open(os.path.join(HERE, "template.html"), encoding="utf-8") as f:
        tpl = f.read()

    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    html = tpl.replace("/*__DATA__*/null", payload)

    out = os.path.join(DOCS, "index.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {out}  ({len(html)//1024} KB)")


if __name__ == "__main__":
    main()
