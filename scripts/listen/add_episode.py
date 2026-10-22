#!/usr/bin/env python3
"""Turn an article into a podcast episode on bren.place's private listen feed.

    python3 scripts/listen/add_episode.py --url https://example.com/post
    python3 scripts/listen/add_episode.py --text "..." --title "Some title" [--url ...]

Given a URL, the article text is extracted with trafilatura. Given --text
(e.g. a paywalled post you selected in your own browser), that text is used
as-is and --url only becomes the "source" link. The text is read aloud with
edge-tts, the MP3 lands in <FEED_DIR>/episodes/, and episodes.json + feed.xml
are rewritten. Episodes older than KEEP_DAYS (or beyond MAX_EPISODES) are
deleted so the repo stays small.
"""
import argparse
import asyncio
import hashlib
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
FEED_DIR = ROOT / "listen-x11z2ucwncju"           # unlisted path = the "private" part
SITE = "https://bren.place/" + FEED_DIR.name + "/"
STATE = FEED_DIR / "episodes.json"
VOICE = os.environ.get("LISTEN_VOICE", "en-US-AndrewMultilingualNeural")
RATE = os.environ.get("LISTEN_RATE", "+0%")         # e.g. "+15%" to read faster
KEEP_DAYS = 60
MAX_EPISODES = 100
MAX_CHARS = 200_000                                  # ~3.5 hours of audio; bail on junk pages
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"


# ---------- extraction ----------

def extract(url):
    import trafilatura
    from trafilatura.settings import use_config

    cfg = use_config()
    cfg.set("DEFAULT", "USER_AGENTS", UA)
    downloaded = trafilatura.fetch_url(url, config=cfg)
    if not downloaded:
        sys.exit(f"Couldn't download {url}")
    text = trafilatura.extract(
        downloaded, url=url, config=cfg,
        include_comments=False, include_tables=False, include_images=False,
        include_links=False, favor_precision=True,
    )
    if not text:
        sys.exit(f"Couldn't find article text at {url}")
    meta = trafilatura.extract_metadata(downloaded, default_url=url)
    return text, {
        "title": (meta.title if meta and meta.title else None),
        "author": (meta.author if meta and meta.author else None),
        "site": (meta.sitename if meta and meta.sitename else None),
    }


def clean(text):
    """Make extracted text pleasant to hear rather than read."""
    t = text.replace("\r", "")
    t = re.sub(r"https?://\S+", "", t)                   # bare links
    t = re.sub(r"\[\d+(?:[,–-]\d+)*\]", "", t)            # [1] [2-4] footnote markers
    # superscript-style footnotes ("word.3 Next"), but not decimals like "3.5"
    t = re.sub(r"(?:(?<=[a-z][.,;:!?])|(?<=[a-z\"”’)]))\d{1,2}(?=\s|$)", "", t)
    t = re.sub(r"^\s*[-*•]\s+", "", t, flags=re.M)       # list bullets
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" +([.,;:!?])", r"\1", t)                # "later ." left by removed markers
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", t) if p.strip()]
    out = []
    for p in paras:
        if re.fullmatch(r"(share|subscribe|leave a comment|share this post|upgrade to paid)\.?", p, re.I):
            continue
        if not re.search(r"[.!?:;\"”’)]$", p):             # headings & list items: add a pause
            p += "."
        out.append(p)
    return "\n\n".join(out)


# ---------- audio ----------

async def speak(text, path):
    import edge_tts
    await edge_tts.Communicate(text, VOICE, rate=RATE).save(str(path))


def duration_seconds(path):
    from mutagen.mp3 import MP3
    try:
        return int(MP3(path).info.length)
    except Exception:
        return 0


# ---------- feed ----------

def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return []


def prune(episodes):
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    keep, drop = [], []
    for i, ep in enumerate(sorted(episodes, key=lambda e: e["added"], reverse=True)):
        (keep if i < MAX_EPISODES and datetime.fromisoformat(ep["added"]) >= cutoff else drop).append(ep)
    for ep in drop:
        (FEED_DIR / "episodes" / ep["file"]).unlink(missing_ok=True)
    return keep


def hms(sec):
    return f"{sec // 3600:d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def write_feed(episodes):
    esc = html.escape
    items = []
    for ep in episodes:
        desc = f'<p>Read aloud from <a href="{esc(ep["url"])}">{esc(ep["url"])}</a></p>' if ep.get("url") else ""
        items.append(f"""    <item>
      <title>{esc(ep["title"])}</title>
      <guid isPermaLink="false">{esc(ep["id"])}</guid>
      <pubDate>{format_datetime(datetime.fromisoformat(ep["added"]))}</pubDate>
      {f'<link>{esc(ep["url"])}</link>' if ep.get("url") else ""}
      <description><![CDATA[{desc}]]></description>
      <itunes:author>{esc(ep.get("byline") or "bren.place")}</itunes:author>
      <itunes:duration>{hms(ep.get("seconds", 0))}</itunes:duration>
      <enclosure url="{SITE}episodes/{ep["file"]}" length="{ep["bytes"]}" type="audio/mpeg"/>
    </item>""")
    now = format_datetime(datetime.now(timezone.utc))
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>Brendan's read-later</title>
    <link>{SITE}</link>
    <atom:link href="{SITE}feed.xml" rel="self" type="application/rss+xml"/>
    <description>Articles saved for listening. Private feed.</description>
    <language>en-us</language>
    <lastBuildDate>{now}</lastBuildDate>
    <itunes:author>bren.place</itunes:author>
    <itunes:image href="{SITE}cover.png"/>
    <itunes:explicit>false</itunes:explicit>
    <itunes:block>Yes</itunes:block>
    <itunes:category text="Society &amp; Culture"/>
{chr(10).join(items)}
  </channel>
</rss>
"""
    (FEED_DIR / "feed.xml").write_text(feed)


# ---------- main ----------

def slug(s, n=50):
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:n].rstrip("-") or "article"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="")
    ap.add_argument("--text", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--feed-only", action="store_true", help="just rebuild feed.xml")
    a = ap.parse_args()

    (FEED_DIR / "episodes").mkdir(parents=True, exist_ok=True)
    episodes = load_state()

    if not a.feed_only:
        url = a.url.strip()
        if a.text.strip():
            body, meta = a.text, {"title": None, "author": None, "site": None}
        elif url:
            body, meta = extract(url)
        else:
            sys.exit("Need --url or --text")

        title = (a.title.strip() or meta["title"]
                 or (urlparse(url).netloc if url else "Untitled article"))
        site = meta["site"] or (urlparse(url).netloc.removeprefix("www.") if url else "")
        byline = ", ".join(x for x in [meta["author"], site] if x)

        body = clean(body)
        if len(body) < 200:
            sys.exit("Extracted text is suspiciously short; is the page paywalled? "
                     "Try selecting the text and sending that instead.")
        if len(body) > MAX_CHARS:
            sys.exit(f"Text is {len(body):,} characters; refusing (limit {MAX_CHARS:,}).")

        intro = title.rstrip(".") + "."
        if meta["author"]:
            intro += f" By {meta['author']}."
        if site:
            intro += f" From {site}."

        added = datetime.now(timezone.utc).replace(microsecond=0)
        ep_id = hashlib.sha1(f"{url}|{title}|{added.isoformat()}".encode()).hexdigest()[:12]
        fname = f"{added:%Y%m%d}-{slug(title)}-{ep_id[:4]}.mp3"
        out = FEED_DIR / "episodes" / fname

        print(f"Speaking {len(body):,} chars as '{title}' with {VOICE}…")
        try:
            asyncio.run(speak(intro + "\n\n" + body, out))
        except BaseException:
            out.unlink(missing_ok=True)                  # don't leave a half-written MP3
            raise

        episodes.append({
            "id": ep_id, "title": title, "url": url, "byline": byline,
            "added": added.isoformat(), "file": fname,
            "bytes": out.stat().st_size, "seconds": duration_seconds(out),
        })
        print(f"Wrote {fname} ({out.stat().st_size / 1e6:.1f} MB, {hms(episodes[-1]['seconds'])})")

    episodes = prune(episodes)
    STATE.write_text(json.dumps(episodes, indent=1) + "\n")
    write_feed(episodes)
    print(f"Feed has {len(episodes)} episode(s): {SITE}feed.xml")


if __name__ == "__main__":
    main()
