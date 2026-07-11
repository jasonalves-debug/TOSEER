"""
Fetches Toronto's Road Restrictions & Closures dataset (open.toronto.ca).

Resolves the actual resource URL dynamically via CKAN's package_show API
rather than hardcoding a resource ID — IDs on this portal have changed
before (the dataset's own docs mention a 2017 migration to a new RESTful
service), so hardcoding one is exactly the kind of guess that quietly
breaks later. This costs one extra request per run.

Schema is NOT independently confirmed yet (my fetch tool couldn't reach
this endpoint directly to inspect field names in advance — same as every
other source in this project, the first real run against real data is
what actually confirms it). This script prints the raw first record so
that's visible immediately rather than assumed.
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
NOMINATIM_UA = "toronto-live-emergency-map/1.0 (personal project; self-hosted)"

CKAN_BASE = "https://ckan0.cf.opendata.inter.prod-toronto.ca"
DATASET_SLUG = "road-restrictions"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "docs", "data")
OUTPUT_PATH = os.path.join(DATA_DIR, "road_restrictions.json")
GEOCACHE_PATH = os.path.join(DATA_DIR, "road_geocode_cache.json")
MAX_NEW_GEOCODES = 15  # per run, same rate-limit discipline as the fire intersection geocoding


def http_get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=15) as res:
        return res.read()


def http_get_json(url, params=None, headers=None):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    return json.loads(http_get(url, headers))


def resolve_resource_url():
    """Finds the JSON resource for this dataset by asking CKAN what's
    actually there right now, instead of trusting a hardcoded resource ID."""
    pkg = http_get_json(f"{CKAN_BASE}/api/3/action/package_show", {"id": DATASET_SLUG})
    if not pkg.get("success"):
        raise RuntimeError(f"package_show failed: {pkg}")
    resources = pkg["result"].get("resources", [])
    if not resources:
        raise RuntimeError("road-restrictions package has no resources listed")

    json_resources = [r for r in resources if (r.get("format") or "").upper() == "JSON"]
    candidates = json_resources or resources  # fall back to whatever's there if none marked JSON
    # prefer one whose name/url suggests it's the live data feed, not documentation
    candidates.sort(key=lambda r: 0 if "restriction" in (r.get("name") or "").lower() else 1)
    chosen = candidates[0]
    print(f"  [road] resolved resource: '{chosen.get('name')}' ({chosen.get('format')}) -> {chosen.get('url')}")
    return chosen["url"]


def load_geocache():
    if os.path.exists(GEOCACHE_PATH):
        try:
            with open(GEOCACHE_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_geocache(cache):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(GEOCACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


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
        print(f"  [road geocode failed] {query}: {e}", file=sys.stderr)
        return None
    budget[0] -= 1
    time.sleep(1)
    if not results:
        cache[key] = None
        return None
    geo = {"lat": float(results[0]["lat"]), "lng": float(results[0]["lon"])}
    cache[key] = geo
    return geo


def extract_point(record):
    """Handles whatever shape the geometry/coordinates turn out to be:
    a direct lat/lng pair, a GeoJSON-style geometry (point or line — line
    midpoint used for a marker position), or falls through to None so the
    caller can try geocoding street names instead."""
    for lat_key, lng_key in [("latitude", "longitude"), ("lat", "lng"), ("lat", "lon"), ("y", "x")]:
        lat_candidates = [k for k in record if k.lower() == lat_key]
        lng_candidates = [k for k in record if k.lower() == lng_key]
        if lat_candidates and lng_candidates:
            try:
                lat, lng = float(record[lat_candidates[0]]), float(record[lng_candidates[0]])
                if lat and lng:
                    return lat, lng
            except (TypeError, ValueError):
                pass

    geom = record.get("geometry") or record.get("geom")
    if isinstance(geom, dict):
        coords = geom.get("coordinates")
        gtype = geom.get("type", "")
        try:
            if gtype == "Point" and len(coords) == 2:
                return coords[1], coords[0]
            if gtype in ("LineString",) and coords:
                mid = coords[len(coords) // 2]
                return mid[1], mid[0]
            if gtype in ("MultiLineString",) and coords and coords[0]:
                mid = coords[0][len(coords[0]) // 2]
                return mid[1], mid[0]
        except (TypeError, IndexError):
            pass
    return None


def build_location_text(record):
    """Best-effort human-readable location. 'name' (e.g. 'Bayview Bloor Dvp
    S Ramp before Don Valley Parkway S') is the richest single field this
    dataset actually provides — confirmed against a real sample record, not
    guessed. Falls back to assembling road/fromRoad/toRoad if name is ever
    missing on some record."""
    if record.get("name"):
        return str(record["name"])
    field_groups = [
        ["road", "streetName", "roadway"],
        ["fromRoad", "fromStreet"],
        ["toRoad", "toStreet"],
    ]
    parts = []
    for group in field_groups:
        for key in group:
            if record.get(key):
                parts.append(str(record[key]))
                break
    return " ".join(parts).strip() or None


def main():
    status = "error"
    out = []
    try:
        url = resolve_resource_url()
        raw = http_get(url)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            # The live feed apparently contains bare backslashes (e.g. in a
            # file path) that aren't valid JSON escapes. Escape any
            # backslash that isn't already part of a legitimate JSON escape
            # sequence (\", \\, \/, \b, \f, \n, \r, \t, \uXXXX) and retry
            # once, rather than failing outright on real, reachable data.
            print(f"  [road] strict JSON parse failed ({e}) — retrying with escape sanitization", file=sys.stderr)
            text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            sanitized = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', text)
            data = json.loads(sanitized)
        # response might be a bare list, {"result": [...]}, {"features": [...]},
        # CKAN datastore's actual shape ({"result": {"records": [...]}}), or
        # this dataset's own specific wrapper ({"Closure": [...]}).
        if isinstance(data, list):
            records = data
        else:
            candidate = data.get("result") or data.get("features") or data.get("value") or data.get("Closure") or []
            if isinstance(candidate, dict):
                records = candidate.get("records") or candidate.get("features") or []
            else:
                records = candidate
            if not records:
                # generic fallback: whichever top-level key holds a non-empty
                # list is almost certainly the real data, whatever it's called
                for k, v in data.items():
                    if isinstance(v, list) and v:
                        print(f"  [road] using auto-detected key '{k}' (no known key name matched)")
                        records = v
                        break

        if records and isinstance(records[0], dict) and "properties" in records[0]:
            records = [{**r["properties"], "geometry": r.get("geometry")} for r in records]  # GeoJSON FeatureCollection shape

        if records:
            print(f"  [road] sample raw record: {records[0]}")
        elif isinstance(data, dict):
            # still nothing — show the truth instead of silently reporting
            # zero records with no way to diagnose why
            print(f"  [road] no records found — top-level keys were: {list(data.keys())}")
            for k in data.keys():
                v = data[k]
                shape = f"dict with keys {list(v.keys())}" if isinstance(v, dict) else f"list of {len(v)}" if isinstance(v, list) else type(v).__name__
                print(f"  [road]   data['{k}'] is a {shape}")

        cache = load_geocache()
        budget = [MAX_NEW_GEOCODES]
        skipped_expired = 0
        for i, r in enumerate(records):
            if r.get("expired") in (1, "1", True):
                skipped_expired += 1
                continue
            point = extract_point(r)
            location = build_location_text(r) or "Toronto"
            approximate = False
            if not point and location != "Toronto":
                geo = geocode(f"{location}, Toronto, Ontario, Canada", cache, budget)
                if geo:
                    point = (geo["lat"], geo["lng"])
                    approximate = True
            if not point:
                continue
            lat, lng = point
            out.append({
                "id": f"road_{r.get('id') or r.get('_id') or r.get('OBJECTID') or i}",
                "lat": lat, "lng": lng,
                "location": location,
                "reason": r.get("description") or r.get("type") or r.get("workEventType") or "Road restriction",
                "status": r.get("currImpact") or r.get("maxImpact") or "",
                "district": r.get("district") or "",
                "approximate": approximate,
                "raw": {k: v for k, v in r.items() if k != "geometry"},  # keep everything for later frontend use
            })
        save_geocache(cache)
        status = "live"
        print(f"[road] {len(out)} road restrictions/closures mapped ({skipped_expired} expired entries skipped)")
    except Exception as e:
        status = f"error: {e}"
        print(f"[road] FAILED: {e}", file=sys.stderr)

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump({
            "generated": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "restrictions": out,
        }, f, indent=2)
    print(f"Wrote {len(out)} road restrictions to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
