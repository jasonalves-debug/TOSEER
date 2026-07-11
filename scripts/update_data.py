#!/usr/bin/env python3
"""
Fetches Toronto Police "Calls for Service" (C4S) and Toronto Fire Active
Incidents, geocodes fire intersections (cached forever — they don't move),
and writes a merged JSON file for the frontend to poll.

Run by .github/workflows/update.yml on a schedule. No servers, no accounts
beyond GitHub. Everything here can be run locally too:

    pip install requests
    python scripts/update_data.py
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.tps.ca/",
}

NOMINATIM_UA = "toronto-live-emergency-map/1.0 (personal project; self-hosted)"  # Nominatim's usage policy wants an identifying UA, not a browser spoof

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "docs", "data")
OUTPUT_PATH = os.path.join(DATA_DIR, "incidents.json")
GEOCACHE_PATH = os.path.join(DATA_DIR, "geocode_cache.json")
MAX_NEW_GEOCODES = 15  # per run — stays well within Nominatim's 1 req/sec policy


def http_get_json(url, params=None, headers=None):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={**BROWSER_HEADERS, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {e.code} {e.reason} — response body: {body!r}") from None


def http_get_text(url):
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            return res.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {e.code} {e.reason} — response body: {body!r}") from None


# Mirrors TYPE_GLOSSARY in index.html — keep both in sync. Confirmed entries
# come from real TPS Communications training material (Service Procedure
# 04-42 / PRIME references); the rest are common, high-confidence CAD
# abbreviations. classify() uses this so severity matches against full words
# like "Assault" even when the raw code is the abbreviated "ASSLT".
TYPE_GLOSSARY = {
    "UNKTR": "Unknown Trouble", "IMPPE": "Impaired Person", "PDACC": "Property Damage Accident",
    "MISPE": "Missing Person", "EDP": "Emotionally Disturbed Person", "HAZ": "Hazard",
    "B&E": "Break and Enter", "BNE": "Break and Enter", "ASSLT": "Assault", "THEFT": "Theft",
    "ROBBERY": "Robbery", "DOM": "Domestic Dispute", "DOMAS": "Domestic Dispute",
    "MVC": "Motor Vehicle Collision", "PIACC": "Personal Injury Accident",
    "SUSP": "Suspicious Person/Activity", "SUSPER": "Suspicious Person", "SUSVEH": "Suspicious Vehicle",
    "NOISE": "Noise Complaint", "ALARM": "Alarm Activation", "TRESP": "Trespassing", "FRAUD": "Fraud",
    "MISCH": "Mischief", "WARRANT": "Warrant", "WELCHK": "Wellness Check", "WELL": "Wellness Check",
    "SHOTS": "Shots Fired", "WPN": "Weapons Call", "ABAND": "Abandoned Vehicle",
    "PARKING": "Parking Complaint", "DISP": "Dispute", "DISTRB": "Disturbance", "CO": "Carbon Monoxide Alarm",
    # unverified — suggested by another AI (Gemini), not independently confirmed
    # against any TPS source. A few (PDACC/UNKTR/MISCH/PIACC) overlapped with
    # what's already confirmed/inferred above, a mild positive signal, not proof.
    "FIR": "Fire / Fire Report", "SEEAM": "See Ambulance (medical assist request)",
    "ASS": "Assault", "ASSJU": "Assault (Just Occurred)", "ASSPR": "Assault (Priority / In Progress)",
    "DISPU": "Dispute", "DIS": "Disturbance", "MISVU": "Missing Person (Vulnerable)",
    "IMPDR": "Impaired Driver", "THEJU": "Theft (Just Occurred)", "THEPR": "Theft (Priority)",
    "THEVE": "Theft of Vehicle", "FRA": "Fraud", "ARR": "Arrest / Arrived at Scene",
    "DAM": "Damage to Property", "DAMPR": "Damage to Property (Priority)",
    "BREPR": "Break and Enter (Priority)", "ATTBR": "Attempt Break and Enter",
    "ANICO": "Animal Complaint", "LOSEL": "Lost/Found Property or Person", "FOUPR": "Found Property",
    "TRE": "Trespassing", "PERGU": "Person Guarding / Person Gone", "SOUGU": "Sound of Gunshots",
    "GASLE": "Gas Leak",
}


def plain_type(type_str):
    return TYPE_GLOSSARY.get((type_str or "").strip().upper(), type_str or "Call for Service")


def stable_id(prefix, *parts):
    """Hash of content fields (location/type/time), not a row number from the
    upstream service — TPS's feed rebuilds from scratch on every refresh, so
    its row IDs (OBJECTID) are not stable across polls for the same real
    incident. A content hash stays the same as long as the incident itself
    hasn't changed, which is what merge/dedup and the frontend's marker
    tracking actually need."""
    raw = "|".join(str(p) for p in parts)
    return f"{prefix}_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12]}"


def classify(type_str):
    # check both the raw code and its translated label — handles abbreviated
    # codes (ASSLT) and already-plain-English fire types (e.g. "Assault")
    t = f"{type_str or ''} {plain_type(type_str)}".lower()
    if re.search(r"shoot|shot|firearm|gun|stab|homicide|hostage", t):
        return "critical"
    if re.search(r"robbery|assault|weapon|break.?and.?enter|abduct", t):
        return "high"
    if re.search(r"theft|fraud|disturb|mischief|b\s?&\s?e", t):
        return "medium"
    return "low"


def fire_severity(alarm_level):
    try:
        n = int(alarm_level)
    except (TypeError, ValueError):
        return "medium"
    if n >= 2:
        return "critical"
    if n == 1:
        return "high"
    return "medium"


# ─────────────────────────── TPS (police) ───────────────────────────
# c4s.torontopolice.on.ca (the original source) now sits behind a Cloudflare
# bot challenge — confirmed by a "Just a moment..." response body, not a
# permissions error. Reliably automating past that would mean running a
# real browser to solve Cloudflare's JS challenge on every cron cycle,
# which is circumventing anti-bot protection rather than just fetching
# public data, so this script doesn't do that.
#
# TPS also publishes the same "Calls for Service" data through their own
# Esri ArcGIS Online org (torontops.maps.arcgis.com) — the official map at
# experience.arcgis.com/.../a22f5295933e48a5b0a4c90cd3c4cae1 queries this.
# Different infrastructure, no Cloudflare challenge observed.


def find_field(fields, patterns):
    names = [f["name"] for f in fields]
    for p in patterns:
        hit = next((n for n in names if p in n.upper()), None)
        if hit:
            return hit
    return None


# Toronto's rough bounding box — the actual ground-truth check. Service/org
# names turned out unreliable (a service legitimately named
# "Boulder_PD_Calls_For_Service" — almost certainly a leftover name from an
# Esri solution template TPS deployed under their own org — contained real
# Toronto coordinates; an org-id check would have wrongly rejected it). The
# coordinates themselves are what actually matters.
TORONTO_BBOX = (43.55, 43.95, -79.75, -79.0)  # lat_min, lat_max, lng_min, lng_max
SERVICE_URL_RE = re.compile(r'https://[a-zA-Z0-9.\-]+/[^"\\]*?/(?:FeatureServer|MapServer)/\d+')


def resolve_candidate_urls(item_id):
    """
    Item IDs on ArcGIS Online can point to a Feature Service directly, a Web
    Map, or a Web Mapping/Experience Builder App. Web Mapping App items often
    carry a `url` field too — but that's the human-facing app page
    (experience.arcgis.com/...), not a data endpoint, so it's only trusted
    for actual service item types. For app/map types, deep-scans the raw
    config text for EVERY service URL it contains (a complex app config can
    reference several layers — basemaps, other widgets, etc — not just the
    one we want), so the caller can try each and let real data decide which
    one is right, rather than trusting whichever URL happens to appear first.
    """
    item = http_get_json(f"https://www.arcgis.com/sharing/rest/content/items/{item_id}", {"f": "json"})
    item_type = item.get("type", "")
    title = item.get("title", "")

    if item.get("url") and item_type in ("Feature Service", "Map Service"):
        return [item["url"]], title

    raw = http_get_text(f"https://www.arcgis.com/sharing/rest/content/items/{item_id}/data?f=json")
    candidates = list(dict.fromkeys(m.group(0) for m in SERVICE_URL_RE.finditer(raw)))
    if not candidates:
        raise RuntimeError(f"Item '{title}' ({item_id}, type={item_type}) has no usable service "
                            f"URL found — first 300 chars of config: {raw[:300]}")
    return candidates, title


def fetch_and_validate_layer(service_url):
    """Fetches one candidate layer and returns parsed incidents only if the
    coordinates actually fall inside Toronto — otherwise returns None so the
    caller can move on to the next candidate instead of trusting a guess."""
    if re.search(r"/(FeatureServer|MapServer)/\d+/?$", service_url):
        layer_url = service_url.rstrip("/")
    else:
        svc = http_get_json(service_url, {"f": "json"})
        layers = svc.get("layers") or []
        if not layers:
            return None
        layer_url = f"{service_url.rstrip('/')}/{layers[0]['id']}"

    try:
        layer = http_get_json(layer_url, {"f": "json"})
    except Exception:
        return None
    fields = layer.get("fields", [])
    if not fields:
        return None  # not a point layer with attributes — e.g. a basemap tile service
    field_names = [f["name"] for f in fields]
    print(f"  [police] layer field names: {field_names}")

    type_field = find_field(fields, ["CALL_TYPE", "TYP_ENG", "OFFENCE", "CATEGORY", "TYPE"])
    div_field = find_field(fields, ["DIVISION", "DIV"])
    date_field = find_field(fields, ["DATE", "TIME", "OCC"])
    loc_field = find_field(fields, ["INTERSECTION", "XSTREET", "CROSS", "ADDRESS", "LOCATION",
                                     "NEIGHBOURHOOD", "NEIGHBORHOOD", "HOOD", "NBHD", "BLOCK", "GEO"])
    oid_field = layer.get("objectIdField", "OBJECTID")
    print(f"  [police] field mapping -> type:{type_field} div:{div_field} date:{date_field} loc:{loc_field}")

    params = {"where": "1=1", "outFields": "*", "outSR": "4326", "resultRecordCount": "500", "f": "json"}
    try:
        data = http_get_json(f"{layer_url}/query", params)
    except Exception:
        return None
    features = data.get("features")
    if not features:
        return None

    out, in_bbox = [], 0
    lat_min, lat_max, lng_min, lng_max = TORONTO_BBOX
    for i, f in enumerate(features):
        a = f.get("attributes", {})
        g = f.get("geometry", {})
        if i == 0:
            print(f"  [police] sample raw attributes: {a}")
        lat, lng = g.get("y"), g.get("x")
        if not lat or not lng:
            continue
        if lat_min <= lat <= lat_max and lng_min <= lng <= lng_max:
            in_bbox += 1
        type_str = (a.get(type_field) if type_field else None) or "Call for Service"
        ts_raw = a.get(date_field) if date_field else None
        ts = datetime.fromtimestamp(ts_raw / 1000, tz=timezone.utc) if isinstance(ts_raw, (int, float)) and ts_raw else datetime.now(timezone.utc)
        loc = (a.get(loc_field) if loc_field else "") or "Toronto"
        # ID is built from stable categorical/text fields only — NOT raw lat/lng
        # (server-side reprojection can introduce tiny float differences between
        # identical queries) and NOT the exact timestamp (uncertain whether this
        # field is a fixed "occurred at" time or a "last touched" value that
        # ticks on refresh). A coarse 15-minute time bucket still disambiguates
        # genuinely distinct incidents at the same intersection without being
        # sensitive to either kind of jitter.
        coarse_time = int(ts_raw // 900000) if isinstance(ts_raw, (int, float)) and ts_raw else 0
        out.append({
            "id": stable_id("tps", loc.lower(), type_str.lower(), coarse_time),
            "source": "police",
            "lat": lat, "lng": lng,
            "type": type_str,
            "type_plain": plain_type(type_str),
            "severity": classify(type_str),
            "division": (a.get(div_field) if div_field else "") or "",
            "location": loc,
            "ts": ts.isoformat(),
        })

    if not out or in_bbox / len(out) < 0.8:
        print(f"    candidate {layer_url} -> {in_bbox}/{len(out) if out else 0} in Toronto bbox, rejecting")
        return None
    print(f"  [police] {layer_url} -> {in_bbox}/{len(out)} points confirmed inside Toronto's bounding box")
    return out


def fetch_police():
    # Both direct ArcGIS Online item ids tried so far were wrong: b49d1583...
    # was TPS's logo image, 90b4acdbd... ("Calls_For_Service") turned out to
    # be Boulder, Colorado PD's data (0/500 points fell inside Toronto's bbox
    # — confirmed, not a guess). Trying the Experience Builder app TPS itself
    # links to as "TPS Calls for Service" — its config may reference several
    # layers, so every candidate found gets tried and validated against real
    # Toronto coordinates rather than trusting whichever comes first.
    candidates, title = resolve_candidate_urls("a22f5295933e48a5b0a4c90cd3c4cae1")
    print(f"  [police] '{title}' config references {len(candidates)} candidate service URL(s)")
    for url in candidates:
        result = fetch_and_validate_layer(url)
        if result is not None:
            return result
    raise RuntimeError(f"None of {len(candidates)} candidate service URLs from '{title}' "
                        f"contained data inside Toronto's bounding box")


# ─────────────────────────── TFS (fire) ───────────────────────────
# Toronto Fire actually publishes a machine-readable feed for this — no HTML
# table scraping needed. Confirmed against a real live pull:
# https://www.toronto.ca/data/fire/livecad.xml
#
# Quirks observed in real data, all handled below:
#  - prime_street carries a borough suffix after a comma, e.g. "BARTLEY DR, NY"
#  - MEDICAL calls have no street at all — prime_street is just an FSA postal
#    code ("M1S") and cross_streets is empty, for privacy reasons
#  - some events have a blank prime_street and the real intersection sits
#    entirely inside cross_streets ("HOBSON AVE / BERMONDSEY RD")
#  - units_disp can be missing entirely (units not yet assigned)

TFS_XML_URL = "https://www.toronto.ca/data/fire/livecad.xml"

try:
    from zoneinfo import ZoneInfo
    TORONTO_TZ = ZoneInfo("America/Toronto")  # TFS dispatch_time is naive *local* time, not UTC
except Exception:
    # tzdata missing on this system — fall back to a fixed EST offset (loses DST accuracy,
    # better than silently treating Toronto local time as UTC)
    from datetime import timedelta
    TORONTO_TZ = timezone(timedelta(hours=-5))

BOROUGH_NAMES = {
    "NY": "North York", "TT": "Toronto", "SC": "Scarborough",
    "ET": "Etobicoke", "EY": "East York", "YK": "York",
}
FSA_RE = re.compile(r"^[A-Z]\d[A-Z]$")


def fetch_fire_events():
    import xml.etree.ElementTree as ET
    xml_text = http_get_text(TFS_XML_URL)
    root = ET.fromstring(xml_text)
    events = []
    for ev in root.findall("event"):
        def get(tag):
            el = ev.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        events.append({
            "prime_street": get("prime_street"),
            "cross_streets": get("cross_streets"),
            "dispatch_time": get("dispatch_time"),
            "event_num": get("event_num"),
            "event_type": get("event_type"),
            "alarm_lev": get("alarm_lev"),
            "beat": get("beat"),
            "units_disp": get("units_disp"),
        })
    return events


def load_geocode_cache():
    if os.path.exists(GEOCACHE_PATH):
        with open(GEOCACHE_PATH) as f:
            return json.load(f)
    return {}


def save_geocode_cache(cache):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(GEOCACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


def geocode_query(cache, key, params, budget):
    """params is either {'q': '...'} for free-form, or structured fields
    like {'postalcode': 'M3M', 'country': 'Canada'} — Nominatim docs say
    structured queries are more targeted for cases like bare postal codes,
    though Canadian postcode coverage in OSM is documented as patchy
    (osm-search/Nominatim#1452), so even this isn't a precision guarantee."""
    if key in cache:
        return cache[key]
    if budget[0] <= 0:
        return None  # try again next run
    try:
        result = http_get_json(NOMINATIM_URL, {"format": "json", "limit": 1, **params},
                                headers={"User-Agent": NOMINATIM_UA})
    except Exception as e:
        print(f"  [geocode failed] {params}: {e}", file=sys.stderr)
        return None
    budget[0] -= 1
    time.sleep(1)  # respect Nominatim's 1 req/sec usage policy
    if not result:
        cache[key] = None  # remember the miss so we don't retry it every run forever
        return None
    geo = {"lat": float(result[0]["lat"]), "lng": float(result[0]["lon"])}
    cache[key] = geo
    return geo


def build_location(ev, budget, cache):
    """Returns (geo_or_None, human_readable_location, is_approximate)."""
    prime_raw = ev["prime_street"].strip()
    cross_raw = ev["cross_streets"].strip()
    cross_parts = [c.strip() for c in cross_raw.split("/") if c.strip()]

    # Case 1: MEDICAL / privacy-redacted — prime_street is an FSA code, no
    # streets at all. Structured query (postalcode=, not crammed into free
    # text) is the more targeted option Nominatim's docs recommend for this,
    # but Canadian postcode data in OSM is known-patchy — this is still only
    # ever a rough area, never a real location, hence is_approximate=True
    # and the frontend renders these as a soft area circle, not a pin.
    if FSA_RE.match(prime_raw) and not cross_parts:
        key = f"fsa2:{prime_raw}"  # bumped from fsa: — forces re-geocode of anything cached under the old, less accurate free-form method
        geo = geocode_query(cache, key, {"postalcode": prime_raw, "country": "Canada"}, budget)
        return geo, f"{prime_raw} (approximate area)", True

    # Split "STREET NAME, NY" -> street + borough
    if "," in prime_raw:
        street, _, borough_code = prime_raw.rpartition(",")
        street = street.strip()
        borough = BOROUGH_NAMES.get(borough_code.strip(), "")
    else:
        street, borough = prime_raw, ""

    # Case 2: blank prime_street, real intersection lives entirely in cross_streets
    if not street and len(cross_parts) >= 2:
        a, b = cross_parts[0], cross_parts[1]
        key = f"xx:{a.lower()}|{b.lower()}"
        query = f"{a} & {b}, Toronto, Ontario, Canada"
        geo = geocode_query(cache, key, {"q": query}, budget)
        return geo, f"{a} & {b}", False

    # Case 3: normal "street + first cross street" intersection
    if street and cross_parts:
        cross = cross_parts[0]
        key = f"st:{street.lower()}|{cross.lower()}|{borough.lower()}"
        loc_label = f"{borough} - " if borough else ""
        borough_part = f"{borough}, " if borough and borough != "Toronto" else ""
        query = f"{street} & {cross}, {borough_part}Toronto, Ontario, Canada"
        geo = geocode_query(cache, key, {"q": query}, budget)
        return geo, f"{loc_label}{street} & {cross}", False

    # Case 4: street with no cross street at all — geocode the street + borough, low precision
    if street:
        key = f"sb:{street.lower()}|{borough.lower()}"
        borough_part = f"{borough}, " if borough and borough != "Toronto" else ""
        query = f"{street}, {borough_part}Toronto, Ontario, Canada"
        geo = geocode_query(cache, key, {"q": query}, budget)
        return geo, f"{street}{', ' + borough if borough else ''}", True

    return None, "Toronto", True


def fetch_fire():
    cache = load_geocode_cache()
    budget = [MAX_NEW_GEOCODES]
    out = []
    for ev in fetch_fire_events():
        geo, location, approx = build_location(ev, budget, cache)
        if not geo:
            continue  # not geocoded yet — appears once cache fills in on a later run
        ts = ev["dispatch_time"]
        try:
            naive = datetime.fromisoformat(ts) if ts else datetime.now()
            ts_iso = naive.replace(tzinfo=TORONTO_TZ).astimezone(timezone.utc).isoformat()
        except ValueError:
            ts_iso = datetime.now(timezone.utc).isoformat()
        out.append({
            "id": f"tfs_{ev['event_num'] or f'{location}_{ts}'}",
            "source": "fire",
            "lat": geo["lat"],
            "lng": geo["lng"],
            "type": ev["event_type"] or "Fire Dispatch",
            "type_plain": plain_type(ev["event_type"] or "Fire Dispatch"),
            "severity": fire_severity(ev["alarm_lev"]),
            "division": ev["beat"] or "",
            "location": location,
            "approximate": approx,
            "units": ev["units_disp"] or "",
            "ts": ts_iso,
        })
    save_geocode_cache(cache)
    return out


# ─────────────────────────── Spatial intelligence ───────────────────────────
# All "intelligence" lives here in Python, not in the browser — the frontend
# only ever renders fields that are already computed and attached below.

def haversine_m(lat1, lng1, lat2, lng2):
    import math
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1, math.sqrt(a)))


def attach_relationships(incidents, max_related=6, max_distance_m=600, max_hours=3):
    """
    'Relationship intelligence': for each incident, find other recent/nearby
    incidents that might be part of the same pattern (e.g. a multi-car
    collision generating several calls, or a string of related disturbance
    calls). Pure spatial+temporal proximity — not a claim of actual causal
    connection, just "worth a human glancing at together." Computed once
    here so the frontend just draws lines between coordinates it's given.
    """
    parsed = []
    for d in incidents:
        try:
            parsed.append((d, datetime.fromisoformat(d["ts"])))
        except (ValueError, KeyError):
            parsed.append((d, None))

    for d, ts in parsed:
        if ts is None:
            d["related"] = []
            continue
        candidates = []
        for e, ets in parsed:
            if e is d or ets is None:
                continue
            hours_apart = abs((ts - ets).total_seconds()) / 3600
            if hours_apart > max_hours:
                continue
            dist = haversine_m(d["lat"], d["lng"], e["lat"], e["lng"])
            if dist > max_distance_m:
                continue
            candidates.append({"id": e["id"], "distance_m": round(dist), "minutes_apart": round(hours_apart * 60)})
        candidates.sort(key=lambda c: (c["distance_m"], c["minutes_apart"]))
        d["related"] = candidates[:max_related]


# ─────────────────────────── Long-term history & anomaly detection ───────────────────────────
# This is deliberately a SEPARATE file from incidents.json. incidents.json is
# pruned to a 24h operational window by design; a real baseline ("is this
# normal for a Tuesday at 9pm") needs weeks of history, so this file
# accumulates forever (bounded — see MAX_HISTORY_DAYS) independent of the
# 24h window. Bucketed by (source, division, day-of-week, hour) — division
# is used as the area unit since it's already a real, stable field in both
# TPS and TFS data, not something invented for this.
HISTORY_PATH = os.path.join(DATA_DIR, "history_stats.json")
MAX_HISTORY_DAYS = 120           # bound file growth — ~4 months of baseline is plenty
MIN_SAMPLE_DAYS = 3              # don't call anything an "anomaly" off fewer than 3 observed occurrences
MIN_ABSOLUTE_COUNT = 3           # don't flag "1 incident vs baseline 0.2" as a dramatic spike — just noise
ANOMALY_RATIO = 2.0              # current must be at least this many times the baseline average
ANOMALY_WINDOW_HOURS = 3         # "current activity" = incidents in the last N hours


def load_history():
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_history(history):
    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=2)


def bucket_key(dow, hour):
    return f"{dow}_{hour}"


def record_history(newly_seen, history):
    """newly_seen must be incidents not present in the previous run's output
    — counting the same incident on every run while it sits in the 24h
    window would massively inflate the baseline. Each real occurrence is
    counted exactly once, on the run where it first appeared."""
    for d in newly_seen:
        try:
            ts_local = datetime.fromisoformat(d["ts"]).astimezone(TORONTO_TZ)
        except (ValueError, KeyError):
            continue
        source = d.get("source", "unknown")
        division = d.get("division") or "UNKNOWN"
        date_str = ts_local.strftime("%Y-%m-%d")
        key = bucket_key(ts_local.weekday(), ts_local.hour)

        bucket = history.setdefault(source, {}).setdefault(division, {}).setdefault(key, {})
        bucket[date_str] = bucket.get(date_str, 0) + 1

    # prune dates older than MAX_HISTORY_DAYS so this file doesn't grow forever
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MAX_HISTORY_DAYS)).strftime("%Y-%m-%d")
    for source_buckets in history.values():
        for division_buckets in source_buckets.values():
            for key, dates in division_buckets.items():
                for d in [d for d in dates if d < cutoff]:
                    del dates[d]

    return history


def baseline_for(history, source, division, dow, hour):
    """Returns (average_per_occurrence, days_observed) for this bucket, or (None, 0) if unknown."""
    bucket = history.get(source, {}).get(division, {}).get(bucket_key(dow, hour), {})
    if not bucket:
        return None, 0
    return sum(bucket.values()) / len(bucket), len(bucket)


def detect_anomalies(incidents, history):
    """Compares current rolling activity per (source, division) against the
    historical baseline for the same day-of-week/hour. Stays silent — not
    fabricating a number — wherever there isn't enough history yet."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=ANOMALY_WINDOW_HOURS)
    now_local = now.astimezone(TORONTO_TZ)

    recent_by_group = {}
    for d in incidents:
        try:
            ts = datetime.fromisoformat(d["ts"])
        except (ValueError, KeyError):
            continue
        if ts < cutoff:
            continue
        group = (d.get("source", "unknown"), d.get("division") or "UNKNOWN")
        recent_by_group.setdefault(group, []).append(d)

    anomalies = []
    for (source, division), group_incidents in recent_by_group.items():
        current_count = len(group_incidents)
        if current_count < MIN_ABSOLUTE_COUNT:
            continue
        avg, days = baseline_for(history, source, division, now_local.weekday(), now_local.hour)
        if avg is None or days < MIN_SAMPLE_DAYS:
            continue  # not enough history yet — say nothing rather than guess
        if avg <= 0 or current_count / avg < ANOMALY_RATIO:
            continue
        anomalies.append({
            "source": source, "division": division,
            "current_count": current_count, "baseline_avg": round(avg, 1),
            "ratio": round(current_count / avg, 1), "sample_days": days,
            "window_hours": ANOMALY_WINDOW_HOURS,
            "example_ids": [d["id"] for d in group_incidents[:5]],
        })
    anomalies.sort(key=lambda a: -a["ratio"])
    return anomalies


RETENTION_HOURS = 24


def load_previous_incidents():
    if not os.path.exists(OUTPUT_PATH):
        return []
    try:
        with open(OUTPUT_PATH) as f:
            return json.load(f).get("incidents", [])
    except (json.JSONDecodeError, OSError):
        return []


def merge_and_retain(previous, fresh, hours=RETENTION_HOURS):
    """
    Both upstream sources are windowed in ways that don't match what we want:
    TPS's own feed only retains ~4hrs, and TFS only ever shows *currently
    active* incidents (a resolved fire vanishes from their feed entirely).
    Neither alone gives a true rolling 12-hour history, so this script is the
    thing that remembers — merge fresh results into what was already on disk
    (fresh data wins for any id seen again), then drop anything older than
    the retention window.
    """
    by_id = {d["id"]: d for d in previous}
    by_id.update({d["id"]: d for d in fresh})  # fresh overwrites stale duplicates

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    kept = []
    for d in by_id.values():
        try:
            ts = datetime.fromisoformat(d["ts"])
        except (ValueError, KeyError):
            continue
        if ts >= cutoff:
            kept.append(d)
    return sorted(kept, key=lambda d: d["ts"], reverse=True)


# ─────────────────────────── Main ───────────────────────────

def main():
    status = {"police": "error", "fire": "error"}
    police, fire = [], []

    try:
        police = fetch_police()
        status["police"] = "live"
        print(f"[police] {len(police)} incidents")
    except Exception as e:
        status["police"] = f"error: {e}"
        print(f"[police] FAILED: {e}", file=sys.stderr)

    try:
        fire = fetch_fire()
        status["fire"] = "live"
        print(f"[fire] {len(fire)} incidents")
    except Exception as e:
        status["fire"] = f"error: {e}"
        print(f"[fire] FAILED: {e}", file=sys.stderr)

    previous = load_previous_incidents()
    previous_ids = {p["id"] for p in previous}
    fresh_this_run = police + fire
    newly_seen = [d for d in fresh_this_run if d["id"] not in previous_ids]

    incidents = merge_and_retain(previous, fresh_this_run)
    print(f"[retention] {len(previous)} on disk + {len(fresh_this_run)} fresh -> "
          f"{len(incidents)} kept within {RETENTION_HOURS}h window "
          f"({len(newly_seen)} genuinely new since last run)")

    attach_relationships(incidents)

    history = load_history()
    record_history(newly_seen, history)
    save_history(history)
    anomalies = detect_anomalies(incidents, history)
    if anomalies:
        print(f"[anomalies] {len(anomalies)} elevated-activity area(s): "
              + "; ".join(f"{a['source']}/{a['division']} {a['ratio']}x" for a in anomalies))
    else:
        print("[anomalies] none flagged (either normal activity, or not enough history yet)")

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "source_status": status,
        "retention_hours": RETENTION_HOURS,
        "incidents": incidents,
        "anomalies": anomalies,
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"Wrote {len(incidents)} incidents to {OUTPUT_PATH}")
    print(f"source_status: {status}")

    # Fail the Action loudly if BOTH sources are down — makes broken runs visible
    if status["police"] != "live" and status["fire"] != "live":
        sys.exit(1)


if __name__ == "__main__":
    main()
