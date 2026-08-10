"""
Daily AI brief builder — Road to Million.

Reads RSS feeds, picks the stories that matter, and asks Claude to draft a
short brief in the site's voice. Writes the draft into daily/posts.json.
It does NOT publish: the GitHub Action opens a pull request for review.

Run locally with:  python scripts/build_brief.py
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
from anthropic import Anthropic

# ── Settings you can change ────────────────────────────────────────────
LOOKBACK_HOURS = 30      # how far back to look for new stories
MAX_STORIES = 6          # how many make the brief
MIN_STORIES = 3          # below this, skip the day entirely
SEEN_MEMORY = 600        # how many old links to remember

# Add or remove freely. Anything with an RSS feed works.
FEEDS = [
    ("OpenAI",           "https://openai.com/news/rss.xml"),
    ("Google DeepMind",  "https://deepmind.google/blog/rss.xml"),
    ("Hugging Face",     "https://huggingface.co/blog/feed.xml"),
    ("TechCrunch AI",    "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("VentureBeat AI",   "https://venturebeat.com/category/ai/feed/"),
    ("Ars Technica AI",  "https://arstechnica.com/ai/feed/"),
    ("The Verge AI",     "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("MIT Tech Review",  "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
    ("Simon Willison",   "https://simonwillison.net/atom/everything/"),
    ("Import AI",        "https://importai.substack.com/feed"),
    ("Hacker News",      "https://hnrss.org/frontpage?points=250"),
]

# The editorial angle. This is the part that makes the blog yours —
# rewrite it in your own words once you find your voice.
ANGLE = """
The writer is a mechanical engineer in Perth, Australia, teaching himself to
build software while working a day job. He writes for two kinds of readers:
other engineers and tradespeople wondering what AI means for their work, and
beginners learning to build things with AI who have limited time.

Voice rules:
- Plain, direct, unhurried. No hype words: no "game-changing", "revolutionary",
  "unleash", "the future of". No exclamation marks.
- Practical over speculative. What can a person actually do with this today?
- Honest about uncertainty. If something is a press release, say so.
- Occasionally connects a story to hands-on engineering or to learning to code,
  but only when the link is real. Never force it.
- Never claims the writer has personally tested something.
"""

ROOT = Path(__file__).resolve().parent.parent
POSTS = ROOT / "daily" / "posts.json"
SEEN = ROOT / "daily" / "seen.json"


def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def clean(text, limit=600):
    """Strip HTML tags out of feed summaries."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def gather():
    """Pull recent items from every feed."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    seen = set(load(SEEN, []))
    items = []

    for source, url in FEEDS:
        try:
            parsed = feedparser.parse(url)
        except Exception as err:
            print(f"  ! {source}: {err}", file=sys.stderr)
            continue

        if parsed.bozo and not parsed.entries:
            print(f"  ! {source}: no entries", file=sys.stderr)
            continue

        for entry in parsed.entries[:15]:
            link = entry.get("link")
            if not link or link in seen:
                continue

            stamp = entry.get("published_parsed") or entry.get("updated_parsed")
            if stamp:
                published = datetime(*stamp[:6], tzinfo=timezone.utc)
                if published < cutoff:
                    continue

            items.append({
                "source": source,
                "title": entry.get("title", "").strip(),
                "link": link,
                "blurb": clean(entry.get("summary", "")),
            })

        print(f"  · {source}: ok")

    return items


def ask_claude(client, items):
    """One call: pick the stories that matter, and write the brief."""
    catalogue = "\n\n".join(
        f"[{i}] {it['source']} — {it['title']}\n{it['blurb']}"
        for i, it in enumerate(items)
    )

    prompt = f"""You are drafting today's AI brief for a small blog.

{ANGLE}

Here are {len(items)} stories published in the last day:

{catalogue}

Choose the {MAX_STORIES} that genuinely matter to those readers. Skip funding
rounds without a product, opinion pieces, and pure speculation. Prefer things a
reader could use, or that change what is possible.

COPYRIGHT: write every summary entirely in your own words. Do not quote the
source articles. Do not copy their sentence structure.

Reply with ONLY a JSON object, no markdown fences, no preamble:

{{
  "intro": "One or two sentences framing the day. Concrete, not throat-clearing.",
  "items": [
    {{
      "index": <the [number] of the story>,
      "headline": "Your own headline, under 10 words",
      "summary": "2-3 sentences, your own words",
      "why": "One sentence: why this matters to an engineer or a beginner"
    }}
  ]
}}"""

    reply = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )

    text = "".join(b.text for b in reply.content if b.type == "text")
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    return json.loads(text)


def main():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY is not set.")

    print("Reading feeds...")
    items = gather()
    print(f"Found {len(items)} new stories.")

    if len(items) < MIN_STORIES:
        print("Too few new stories. Skipping today.")
        return

    print("Asking Claude to draft the brief...")
    draft = ask_claude(Anthropic(api_key=key), items)

    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    entries = []
    for picked in draft["items"]:
        src = items[picked["index"]]
        entries.append({
            "headline": picked["headline"],
            "summary": picked["summary"],
            "why": picked["why"],
            "source": src["source"],
            "link": src["link"],
        })

    posts = load(POSTS, [])
    posts = [p for p in posts if p["date"] != today]          # replace a re-run
    posts.insert(0, {"date": today, "intro": draft["intro"], "items": entries})
    POSTS.write_text(json.dumps(posts[:120], indent=2, ensure_ascii=False), encoding="utf-8")

    seen = [e["link"] for e in entries] + load(SEEN, [])
    SEEN.write_text(json.dumps(seen[:SEEN_MEMORY], indent=0), encoding="utf-8")

    print(f"Drafted {len(entries)} stories for {today}.")


if __name__ == "__main__":
    main()
