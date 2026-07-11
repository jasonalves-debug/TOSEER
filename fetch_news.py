"""
Fetches CityNews Toronto + CBC Toronto RSS, extracts a best-effort location
from each article's title/summary, geocodes it, and tags articles that
mention a politician from politicians.json.

Honest expectations, stated once here rather than buried:
  - News prose is much harder to geocode reliably than dispatch text.
    TFS/TPS feeds are short and templated ("Yonge St & Bloor St"); news
    articles are long-form and the location may be mentioned anywhere,
    vaguely, or not at all. Expect a moderate hit rate, and expect some
    articles to get skipped entirely (no location found) rather than
    placed on a guess.
  - Politician tagging is a NAME MATCH in the article text, not a claim
    that the person was physically present at that location. An article
    that quotes a politician commenting on an event still gets tagged and
    placed at the event's location — that's "this politician is
    associated with this story," not "this politician was here."
  - RSS reachability from a script (vs. a browser) is unverified as of
    writing this — my own fetch tool got blocked reaching these feeds
    directly (bot detection), which may or may not reflect how they treat
    other script clients. The real answer comes from running this for
    real. If both feeds fail every run, that's the first thing to check,
    not a code bug to keep chasing blind.
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
NOMINATIM_UA = "toronto-live-emergency-map/1.0 (personal project; self-hosted)"

FEEDS = [
    {"url": "https://toronto.citynews.ca/feed/", "source": "citynews"},
    {"url": "https://www.cbc.ca/webfeed/rss/rss-canada-toronto", "source": "cbc"},
]

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "docs", "data")
OUTPUT_PATH = os.path.join(DATA_DIR, "news.json")
GEOCACHE_PATH = os.path.join(DATA_DIR, "news_geocode_cache.json")
POLITICIANS_PATH = os.path.join(os.path.dirname(__file__), "politicians.json")

MAX_ARTICLES_KEPT = 100
RETENTION_HOURS = 48  # news stays relevant longer than a dispatch call
MAX_NEW_GEOCODES = 15

SUFFIXES_RE = (
    r"(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|Drive|Crt|Court|Cres|Crescent|"
    r"Lane|Way|Pkwy|Parkway|Terr|Terrace|Pl|Place|Gate|Gardens|Gdns|Pk|Park|"
    r"Plaza|Sq|Square|Expy|Expressway|Hwy|Highway)"
)
# intersection pattern: "Yonge and Bloor", "Yonge St. & Bloor St.", "near Yonge and Dundas"
INTERSECTION_RE = re.compile(
    rf"\b([A-Z][a-zA-Z'\.]+(?:\s[A-Z][a-zA-Z'\.]+)?(?:\s{SUFFIXES_RE})?)\s(?:and|&)\s([A-Z][a-zA-Z'\.]+(?:\s[A-Z][a-zA-Z'\.]+)?(?:\s{SUFFIXES_RE})?)\b"
)
SINGLE_STREET_RE = re.compile(rf"\b([A-Z][a-zA-Z']+(?:\s[A-Z][a-zA-Z']+)?\s{SUFFIXES_RE})\b")

# Coarse, well-known area fallback — deliberately only major, unambiguous
# districts (not fine-grained postal codes, which is the sparse-data trap
# we already hit once in this project). Always marked approximate=True.
AREA_COORDS = {
    "downtown toronto": (43.6532, -79.3832), "downtown": (43.6532, -79.3832),
    "scarborough": (43.7731, -79.2578), "etobicoke": (43.6205, -79.5132),
    "north york": (43.7615, -79.4111), "east york": (43.6913, -79.3287),
    "york": (43.6896, -79.4759), "the beaches": (43.6708, -79.2958),
    "the annex": (43.6677, -79.4042), "yorkville": (43.6709, -79.3933),
    "kensington market": (43.6547, -79.4005), "liberty village": (43.6373, -79.4211),
    "leslieville": (43.6631, -79.3287), "parkdale": (43.6394, -79.4374),
    "rexdale": (43.7276, -79.5638), "jane and finch": (43.7673, -79.5199),
    "regent park": (43.6598, -79.3639), "cabbagetown": (43.6672, -79.3667),
}


def http_get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=15) as res:
        return res.read()


def http_get_json(url, params=None, headers=None):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    return json.loads(http_get(url, headers))


def load_json_file(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


def save_json_file(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def geocode(query, cache, budget):
    key = query.upper()
    if key in cache:
        return cache[key]
    if budget[0] <= 0:
        return None
    try:
        results = http_get_json(NOMINATIM_URL, {
            "q": query, "format": "json", "limit": 1, "countrycodes": "ca",
            "viewbox": "-79.75,43.95,-79.0,43.4", "bounded": 1,
        }, headers={"User-Agent": NOMINATIM_UA})
    except Exception as e:
        print(f"  [news geocode failed] {query}: {e}", file=sys.stderr)
        return None
    budget[0] -= 1
    time.sleep(1)
    if not results:
        cache[key] = None
        return None
    geo = {"lat": float(results[0]["lat"]), "lng": float(results[0]["lon"])}
    cache[key] = geo
    return geo


def find_location(text, cache, budget):
    """Layered extraction: real intersection > single street > known area
    keyword. Returns (lat, lng, location_text, approximate) or None."""
    m = INTERSECTION_RE.search(text)
    if m:
        query = f"{m.group(1)} and {m.group(2)}, Toronto, Ontario, Canada"
        geo = geocode(query, cache, budget)
        if geo:
            return geo["lat"], geo["lng"], f"{m.group(1)} & {m.group(2)}", False

    m = SINGLE_STREET_RE.search(text)
    if m:
        query = f"{m.group(1)}, Toronto, Ontario, Canada"
        geo = geocode(query, cache, budget)
        if geo:
            return geo["lat"], geo["lng"], m.group(1), True  # single street only — approximate

    text_lower = text.lower()
    for area, (lat, lng) in AREA_COORDS.items():
        if area in text_lower:
            return lat, lng, area.title(), True

    return None


def find_politicians(text, politicians):
    text_lower = text.lower()
    hits = []
    for p in politicians:
        if p.get("name", "").startswith("REPLACE_WITH"):
            continue  # unfilled template entry — skip
        names_to_check = [p["name"]] + p.get("aliases", [])
        if any(n.lower() in text_lower for n in names_to_check if n):
            hits.append(p)
    return hits


def fetch_feed(feed_url, source):
    articles = []
    try:
        raw = http_get(feed_url)
    except Exception as e:
        print(f"  [{source}] FAILED: {e}", file=sys.stderr)
        return articles
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"  [{source}] FAILED to parse XML: {e}", file=sys.stderr)
        return articles

    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        desc_raw = item.findtext("description") or ""
        desc = re.sub(r"<[^>]+>", " ", desc_raw)
        desc = re.sub(r"\s+", " ", desc).strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        pubdate = item.findtext("pubDate") or ""
        try:
            ts = parsedate_to_datetime(pubdate).astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError):
            ts = datetime.now(timezone.utc).isoformat()

        articles.append({
            "id": f"news_{source}_{guid}",
            "source": source, "title": title, "summary": desc[:400],
            "link": link, "ts": ts,
        })
    print(f"  [{source}] {len(articles)} articles in feed")
    return articles


def main():
    status = {}
    politicians = load_json_file(POLITICIANS_PATH, [])
    unfilled = sum(1 for p in politicians if p.get("name", "").startswith("REPLACE_WITH"))
    if unfilled:
        print(f"  [politicians] {unfilled} template entr{'y' if unfilled==1 else 'ies'} in "
              f"politicians.json not filled in yet — those are skipped, not matched")

    all_articles = []
    for feed in FEEDS:
        arts = fetch_feed(feed["url"], feed["source"])
        status[feed["source"]] = "live" if arts else "error: 0 articles (see log above)"
        all_articles.extend(arts)

    geocache = load_json_file(GEOCACHE_PATH, {})
    budget = [MAX_NEW_GEOCODES]
    located, tagged = 0, 0

    for a in all_articles:
        text = f"{a['title']} {a['summary']}"
        loc = find_location(text, geocache, budget)
        if loc:
            a["lat"], a["lng"], a["location"], a["approximate"] = loc
            located += 1
        pols = find_politicians(text, politicians)
        if pols:
            tagged += 1
            a["politicians"] = [{"name": p["name"], "role": p["role"], "photo": p.get("photo")} for p in pols]
            # article has no located position of its own -> default to the
            # FIRST mentioned politician's default location (City Hall / Queen's Park / etc)
            if "lat" not in a:
                p = pols[0]
                a["lat"], a["lng"] = p["default_lat"], p["default_lng"]
                a["location"] = p["default_location"]
                a["approximate"] = True

    save_json_file(GEOCACHE_PATH, geocache)

    mappable = [a for a in all_articles if "lat" in a]
    mappable.sort(key=lambda a: a["ts"], reverse=True)
    mappable = mappable[:MAX_ARTICLES_KEPT]

    print(f"[news] {len(all_articles)} articles fetched, {located} geocoded, "
          f"{tagged} tagged with a politician, {len(mappable)} kept (mappable)")

    save_json_file(OUTPUT_PATH, {
        "generated": datetime.now(timezone.utc).isoformat(),
        "source_status": status,
        "articles": mappable,
    })
    print(f"Wrote {len(mappable)} mappable articles to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
