#!/usr/bin/env python3
"""Turn an article into a podcast episode on bren.place's private listen feed.

    python3 scripts/listen/add_episode.py --url https://example.com/post
    python3 scripts/listen/add_episode.py --text "..." --title "Some title" [--url ...]
    python3 scripts/listen/add_episode.py --feed-only      # rebuild feed.xml + episodes.json

Given a URL, the article text is fetched (Substack posts via Substack's own
API, everything else via trafilatura). Given --text (e.g. a paywalled post you
selected in your own browser), that text is used as-is and --url is only the
"source" link. The text is read aloud with edge-tts, or Piper if that fails.

Each episode is two committed files in <FEED_DIR>/episodes/: the MP3 and a
small .json with its metadata. feed.xml and episodes.json are NOT committed;
the site deploy rebuilds them with --feed-only. Keeping one file pair per
episode means several articles sent at once never conflict with each other.
"""
import argparse
import asyncio
import hashlib
import html
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

ROOT = Path(__file__).resolve().parents[2]
FEED_DIR = ROOT / "listen-x11z2ucwncju"           # unlisted path = the "private" part
EP_DIR = FEED_DIR / "episodes"
SITE = "https://bren.place/" + FEED_DIR.name + "/"
VOICE = os.environ.get("LISTEN_VOICE", "en-US-AndrewMultilingualNeural")
RATE = os.environ.get("LISTEN_RATE", "+0%")         # e.g. "+15%" to read faster
ENGINE = os.environ.get("LISTEN_ENGINE", "auto")    # auto (Edge, then Piper) | edge | piper
EDGE_STALL = 45                                     # seconds without audio before giving up on Edge
PIPER_VOICE = os.environ.get("LISTEN_PIPER_VOICE", "en_US-lessac-high")
PIPER_SPEED = float(os.environ.get("LISTEN_PIPER_SPEED", "1.0"))  # <1 is faster, e.g. 0.87
PIPER_DIR = Path(os.environ.get("PIPER_VOICES", Path.home() / ".cache" / "piper-voices"))
KEEP_DAYS = 60
MAX_EPISODES = 100
MAX_CHARS = 200_000                                  # ~3.5 hours of audio; bail on junk pages
FETCH_TIMEOUT = 30                                   # seconds per HTTP request
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


class Fail(Exception):
    """An expected failure with a message worth showing on the run page."""


def die(msg):
    raise Fail(msg)


# ---------- fetching ----------

def http_get(url, accept="text/html,application/xhtml+xml,*/*;q=0.8"):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": accept, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
        return r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")


def strip_tracking(url):
    """Drop ?utm_source=… and friends; they change nothing but the cache key."""
    u = urlparse(url)
    keep = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
            if not re.match(r"(utm_|publication_id$|post_id$|isFreemail$|r$|triedRedirect$|fbclid$|gclid$)", k)]
    return urlunparse(u._replace(query=urlencode(keep), fragment=""))


def html_to_text(fragment):
    import trafilatura
    text = trafilatura.extract(f"<html><body><article>{fragment}</article></body></html>",
                               include_comments=False, include_tables=False,
                               include_images=False, include_links=False, favor_recall=True)
    return text or ""


def looks_like_substack(url, raw_url):
    host = urlparse(url).netloc
    return host.endswith(".substack.com") or "publication_id=" in raw_url or "/p/" in urlparse(url).path


def from_substack(url):
    """Substack blocks scraping from cloud IPs but its post API works (the site's
    essay updater uses it too). Returns (text, meta) or None if this isn't Substack."""
    u = urlparse(url)
    m = re.match(r"^/p/([^/]+)", u.path)
    if not m:
        return None
    api = f"{u.scheme}://{u.netloc}/api/v1/posts/{m.group(1)}"
    try:
        post = json.loads(http_get(api, accept="application/json"))
    except Exception as e:
        print(f"Substack API didn't answer ({e}); falling back to the page.")
        return None
    body = html_to_text(post.get("body_html") or "")
    if post.get("audience") == "only_paid" and len(body) < 3000:
        die("This is a paid Substack post, so only the preview is public. "
            "Open it in your browser, select the article text, and use the "
            "bookmarklet (it sends the selected text instead of the link).")
    authors = [b.get("name") for b in post.get("publishedBylines") or [] if b.get("name")]
    return body, {
        "title": post.get("title"),
        "author": ", ".join(authors) or None,
        "site": (post.get("publication") or {}).get("name") or u.netloc.removesuffix(".substack.com"),
    }


def from_page(url):
    import trafilatura
    try:
        downloaded = http_get(url)
    except Exception as e:
        die(f"Couldn't download {url} ({e}). The site may block automated readers; "
            "select the article text and send it with the bookmarklet instead.")
    text = trafilatura.extract(downloaded, url=url, include_comments=False,
                               include_tables=False, include_images=False,
                               include_links=False, favor_precision=True)
    if not text:
        die(f"Couldn't find article text at {url}. Try selecting the text and sending that instead.")
    meta = trafilatura.extract_metadata(downloaded, default_url=url)
    return text, {
        "title": getattr(meta, "title", None),
        "author": getattr(meta, "author", None),
        "site": getattr(meta, "sitename", None),
    }


def extract(raw_url):
    url = strip_tracking(raw_url)
    if looks_like_substack(url, raw_url):
        got = from_substack(url)
        if got:
            return url, *got
    return url, *from_page(url)


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
        if re.fullmatch(r"(share|subscribe|leave a comment|share this post|upgrade to paid"
                        r"|thanks for reading.*subscribe.*)\.?", p, re.I):
            continue
        if not re.search(r"[.!?:;\"”’)]$", p):             # headings & list items: add a pause
            p += "."
        out.append(p)
    return "\n\n".join(out)


# ---------- audio ----------

def speak(text, path):
    """Microsoft's Edge voices sound best but are an unofficial service that
    sometimes stalls or breaks. If it does, fall back to Piper, which runs
    entirely on the GitHub runner and can't be blocked."""
    if ENGINE in ("auto", "edge"):
        # Run Edge in a child process with a hard time limit. Edge normally reads
        # ~1,000 characters in a few seconds, but it sometimes trickles audio so
        # slowly that a short post takes 7 minutes; this budget catches that, and
        # killing a process works even if edge-tts blocks its own event loop.
        budget = 60 + len(text) // 40          # 1k chars -> 85 s, 40k chars -> ~18 min
        if ENGINE == "edge":
            budget *= 4
        import subprocess
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
            tf.write(text)
        try:
            subprocess.run([sys.executable, __file__, "--edge-child", tf.name, str(path)],
                           check=True, timeout=budget)
            Path(tf.name).unlink(missing_ok=True)
            return "edge"
        except Exception as e:  # noqa: BLE001 - any Edge problem means "use Piper"
            if isinstance(e, subprocess.TimeoutExpired):
                e = f"too slow, gave up after {budget} s"
            elif isinstance(e, subprocess.CalledProcessError):
                e = "failed (see the log above)"
            path.unlink(missing_ok=True)
            Path(tf.name).unlink(missing_ok=True)
            if ENGINE == "edge":
                die(f"Edge text-to-speech {e}")
            print(f"::warning title=Used backup voice::Edge text-to-speech {e}; "
                  f"using Piper instead." if os.environ.get("GITHUB_ACTIONS")
                  else f"Edge TTS {e}; using Piper.", flush=True)
    speak_piper(text, path)
    return "piper"


async def speak_edge(text, path):
    import edge_tts
    # Give up if no audio arrives for EDGE_STALL seconds. A total time limit
    # would have to be huge for long essays; a stall limit catches hangs fast.
    stream = edge_tts.Communicate(text, VOICE, rate=RATE).stream().__aiter__()
    got_audio = False
    with open(path, "wb") as f:
        while True:
            try:
                chunk = await asyncio.wait_for(stream.__anext__(), EDGE_STALL)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                raise RuntimeError(f"no audio for {EDGE_STALL}s") from None
            if chunk["type"] == "audio":
                f.write(chunk["data"])
                got_audio = True
    if not got_audio:
        raise RuntimeError("no audio received")


def speak_piper(text, path):
    import subprocess
    import wave
    import lameenc
    from piper import PiperVoice, SynthesisConfig

    model = PIPER_DIR / f"{PIPER_VOICE}.onnx"
    if not model.exists():
        PIPER_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, "-m", "piper.download_voices", PIPER_VOICE,
                        "--download-dir", str(PIPER_DIR)], check=True, timeout=600)
    voice = PiperVoice.load(model)
    wav_path = path.with_suffix(".wav")
    try:
        cfg = SynthesisConfig(length_scale=PIPER_SPEED)
        with wave.open(str(wav_path), "wb") as wf:
            voice.synthesize_wav(text, wf, syn_config=cfg)
        with wave.open(str(wav_path), "rb") as wf:
            enc = lameenc.Encoder()
            enc.set_bit_rate(64)
            enc.set_in_sample_rate(wf.getframerate())
            enc.set_channels(wf.getnchannels())
            enc.set_quality(2)
            mp3 = enc.encode(wf.readframes(wf.getnframes())) + enc.flush()
        path.write_bytes(mp3)
    finally:
        wav_path.unlink(missing_ok=True)


def duration_seconds(path):
    from mutagen.mp3 import MP3
    try:
        return int(MP3(path).info.length)
    except Exception:
        return 0


# ---------- feed ----------

def load_episodes():
    eps = []
    for meta in EP_DIR.glob("*.json"):
        try:
            eps.append(json.loads(meta.read_text()))
        except Exception:
            print(f"Skipping unreadable {meta.name}")
    return sorted(eps, key=lambda e: e["added"], reverse=True)


def prune():
    """Delete episodes past KEEP_DAYS / MAX_EPISODES. Run when adding, so the
    deletions get committed."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    for i, ep in enumerate(load_episodes()):
        if i >= MAX_EPISODES or datetime.fromisoformat(ep["added"]) < cutoff:
            (EP_DIR / ep["file"]).unlink(missing_ok=True)
            (EP_DIR / (Path(ep["file"]).stem + ".json")).unlink(missing_ok=True)


def hms(sec):
    return f"{sec // 3600:d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def write_feed():
    episodes = [e for e in load_episodes() if (EP_DIR / e["file"]).exists()]
    (FEED_DIR / "episodes.json").write_text(json.dumps(episodes, indent=1) + "\n")
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
    print(f"Feed has {len(episodes)} episode(s): {SITE}feed.xml")


# ---------- main ----------

def slug(s, n=50):
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:n].rstrip("-") or "article"


def add(raw_url, text, title_override):
    raw_url = raw_url.strip()
    if text.strip():
        url, body, meta = strip_tracking(raw_url) if raw_url else "", text, {}
    elif raw_url:
        url, body, meta = extract(raw_url)
    else:
        die("Nothing to read: no URL or text was sent.")

    title = (title_override.strip() or meta.get("title")
             or (urlparse(url).netloc if url else "Untitled article"))
    site = meta.get("site") or (urlparse(url).netloc.removeprefix("www.") if url else "")
    byline = ", ".join(x for x in [meta.get("author"), site] if x)

    body = clean(body)
    if len(body) < 200:
        die("The extracted text is suspiciously short (paywall or a page that loads with "
            "JavaScript?). Select the article text and send it with the bookmarklet instead.")
    if len(body) > MAX_CHARS:
        die(f"Text is {len(body):,} characters; refusing (limit {MAX_CHARS:,}).")

    intro = title.rstrip(".") + "."
    if meta.get("author"):
        intro += f" By {meta['author']}."
    if site:
        intro += f" From {site}."

    added = datetime.now(timezone.utc).replace(microsecond=0)
    ep_id = hashlib.sha1(f"{url}|{title}|{added.isoformat()}".encode()).hexdigest()[:12]
    stem = f"{added:%Y%m%d}-{slug(title)}-{ep_id[:4]}"
    out = EP_DIR / f"{stem}.mp3"

    print(f"Speaking {len(body):,} chars as '{title}'…", flush=True)
    try:
        engine = speak(intro + "\n\n" + body, out)
    except BaseException:
        out.unlink(missing_ok=True)                      # don't leave a half-written MP3
        raise

    ep = {"id": ep_id, "title": title, "url": url, "byline": byline, "engine": engine,
          "added": added.isoformat(), "file": out.name,
          "bytes": out.stat().st_size, "seconds": duration_seconds(out)}
    (EP_DIR / f"{stem}.json").write_text(json.dumps(ep, indent=1) + "\n")
    print(f"Wrote {out.name} with {engine} ({ep['bytes'] / 1e6:.1f} MB, {hms(ep['seconds'])})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="")
    ap.add_argument("--text", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--feed-only", action="store_true", help="just rebuild feed.xml + episodes.json")
    ap.add_argument("--edge-child", nargs=2, metavar=("TEXTFILE", "MP3"), help=argparse.SUPPRESS)
    a = ap.parse_args()

    if a.edge_child:                                   # internal: see speak()
        try:
            asyncio.run(speak_edge(Path(a.edge_child[0]).read_text(), Path(a.edge_child[1])))
        except Exception as e:                         # parent reports it; no error annotation
            print(f"Edge TTS: {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(1)
        return

    EP_DIR.mkdir(parents=True, exist_ok=True)
    if not a.feed_only:
        add(a.url, a.text, a.title)
        prune()
    write_feed()


if __name__ == "__main__":
    try:
        main()
    except Fail as e:
        # "::error::" puts the message on the run's summary page, not just in the log.
        print(f"::error title=Couldn't make the episode::{e}" if os.environ.get("GITHUB_ACTIONS") else str(e))
        sys.exit(1)
    except Exception as e:
        print(f"::error title=Unexpected error::{type(e).__name__}: {e}" if os.environ.get("GITHUB_ACTIONS") else "")
        raise
