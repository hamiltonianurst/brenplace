#!/usr/bin/env python3
"""Refresh the "Recent essays" section of index.html from Substack.

Replaces everything between the ESSAYS:START and ESSAYS:END comments.
Free posts get their subtitle; paid posts get a faded teaser and a subscribe
button. If Substack can't be reached, the page is left as it was.
"""
import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime

PUB = "https://hamiltonianurst.substack.com"
COUNT = 3              # how many essays to show
PAGE = "index.html"
HEADERS = {"User-Agent": "Mozilla/5.0 (bren.place essay updater)"}
MARKERS = re.compile(r"(<!-- ESSAYS:START.*?-->\n)(.*?)(\s*<!-- ESSAYS:END -->)", re.S)


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def from_archive_api():
    """Substack's (unofficial) archive API. It says which posts are paid."""
    data = json.loads(fetch(f"{PUB}/api/v1/archive?sort=new&limit=12"))
    posts = []
    for p in data:
        if p.get("type") not in (None, "newsletter"):   # skip podcasts, threads
            continue
        posts.append({
            "title": p["title"],
            "url": p["canonical_url"],
            "date": datetime.fromisoformat(p["post_date"].replace("Z", "+00:00")),
            "blurb": p.get("subtitle") or p.get("description") or "",
            "paid": p.get("audience") == "only_paid",
            "teaser": p.get("truncated_body_text") or "",
        })
    return posts[:COUNT]


def from_rss():
    """Fallback: the official RSS feed (can't tell paid from free)."""
    root = ET.fromstring(fetch(f"{PUB}/feed"))
    posts = []
    for item in root.iter("item"):
        posts.append({
            "title": item.findtext("title", ""),
            "url": item.findtext("link", ""),
            "date": parsedate_to_datetime(item.findtext("pubDate")),
            "blurb": item.findtext("description", ""),
            "paid": False,
            "teaser": "",
        })
    return posts[:COUNT]


def render(p):
    e = html.escape
    d = p["date"]
    tag = ' <span class="tag">Paid</span>' if p["paid"] else ""
    lines = [
        '    <article class="essay paid">' if p["paid"] else '    <article class="essay">',
        f'      <time datetime="{d:%Y-%m-%d}">{d:%B} {d.day}, {d.year}</time>',
        f'      <h3><a href="{e(p["url"])}">{e(p["title"])}</a>{tag}</h3>',
    ]
    if p["paid"]:
        lines.append(f'      <div class="teaser"><p>{e(p["teaser"] or p["blurb"])}</p></div>')
        lines.append(f'      <a class="subscribe" href="{PUB}/subscribe">Subscribe to read the rest</a>')
    elif p["blurb"]:
        lines.append(f'      <p>{e(p["blurb"])}</p>')
    lines.append("    </article>")
    return "\n".join(lines)


def main():
    posts = []
    for source in (from_archive_api, from_rss):
        try:
            posts = source()
            if posts:
                break
        except Exception as err:
            print(f"warning: {source.__name__} failed: {err}", file=sys.stderr)
    if not posts:
        print("No posts fetched; leaving the page unchanged.")
        return

    page = open(PAGE, encoding="utf-8").read()
    if not MARKERS.search(page):
        sys.exit(f"ESSAYS:START / ESSAYS:END markers not found in {PAGE}")
    body = "\n".join(render(p) for p in posts)
    new = MARKERS.sub(lambda m: m.group(1) + body + m.group(3), page, count=1)
    if new != page:
        open(PAGE, "w", encoding="utf-8").write(new)
        print(f"Updated {PAGE} with {len(posts)} essays.")
    else:
        print("Essays already up to date.")


if __name__ == "__main__":
    main()
