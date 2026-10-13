"""
GRIDLOCK live feed — turns TOSEER's data (plus a few extra public sources) into one
small Scarborough-only file the GRIDLOCK traffic sim reads every time it launches.

Runs as an extra step in TOSEER's existing GitHub Action, AFTER update_data.py and
fetch_road_restrictions.py, so it reuses their fresh output instead of re-scraping.
Every outside source is optional: if one fails, its status says so and the rest still
ship. This step is marked continue-on-error in the workflow, so it can never block
TOSEER's own map from updating.

Outputs (docs/data/):
  gridlock_live.json    every run   (~15 min)  calls, roadwork, weather, alerts, 511, rates
  gridlock_static.json  weekly                 TTC bus routes + headways, fire stations
"""

import csv
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "docs", "data")
LIVE_PATH = os.path.join(DATA_DIR, "gridlock_live.json")
STATIC_PATH = os.path.join(DATA_DIR, "gridlock_static.json")
UA = "gridlock-feed/1.0 (personal project; github.com/jasonalves-debug/TOSEER)"
CKAN = "https://ckan0.cf.opendata.inter.prod-toronto.ca"

# Scarborough box (a little generous; the game crops to its own boundary)
W, S, E, N = -79.345, 43.665, -79.10, 43.87
CENTER = (43.775, -79.23)
SCARB_DIVS = ["D41", "D42", "D43"]
TORONTO = timezone(timedelta(hours=-4))  # replaced below with zoneinfo when available
try:
    from zoneinfo import ZoneInfo
    TORONTO = ZoneInfo("America/Toronto")
except Exception:
    pass


def inbox(lat, lng):
    return lat is not None and lng is not None and S <= lat <= N and W <= lng <= E


def get(url, params=None, timeout=25, raw=False):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        b = r.read()
    return b if raw else json.loads(b)


def load(name):
    p = os.path.join(DATA_DIR, name)
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def iso_to_epoch(s):
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


# ───────────────────────── calls: what each real call makes happen in the sim ─────────────────────────
# "act" is a behaviour key the game understands. Police calls keep their own description;
# codes TOSEER couldn't decode show as "Police incident".
POLICE_ACT = {
    "PDACC": "crash1", "PIACC": "crash2",
    "HAZ": "hazard", "IMPDR": "impaired", "SEEAM": "medical", "MISVU": "missing", "MISPE": "missing",
    "FIR": "fire_small", "ARR": "police_low",
}


def police_act(code, sev):
    if code in POLICE_ACT:
        return POLICE_ACT[code]
    return {"critical": "police_crit", "high": "police_high", "medium": "police_med"}.get(sev, "police_low")


def fire_act(label):
    t = label.lower()
    if "trapped" in t:
        return "crash3"
    if "vehicle" in t and ("fire" in t):
        return "carfire"
    if "vehicle" in t or "collision" in t or "mva" in t:
        return "crash2"
    if "wires" in t or "hydro" in t or "transformer" in t or "pole" in t:
        return "hydro"
    if "gas" in t or "hazmat" in t or "spill" in t or "odour" in t or "odor" in t:
        return "gas"
    if "alarm" in t or "co detector" in t or "carbon monoxide" in t:
        return "fire_alarm"
    if "grass" in t or "rubbish" in t or "outdoor" in t:
        return "fire_small"
    if t.startswith("fire") or "smoke" in t or "explosion" in t:
        return "fire_struct"
    if "rescue" in t or "medical" in t or "elevator" in t or "water" in t:
        return "medical"
    return "fire_alarm"


def build_calls(status):
    try:
        inc = load("incidents.json")
    except Exception as e:
        status["tps"] = status["tfs"] = f"down: {e}"
        return [], None
    out = []
    for i in inc.get("incidents", []):
        if not inbox(i.get("lat"), i.get("lng")):
            continue
        ts = iso_to_epoch(i.get("ts", ""))
        if not ts:
            continue
        src = "tps" if i["source"] == "police" else "tfs"
        code, plain, sev = i.get("type", ""), i.get("type_plain") or "", i.get("severity", "low")
        if src == "tps":
            label = plain if plain and plain != code else "Police incident"
            act = police_act(code, sev)
        else:
            label = plain or "Fire call"
            act = fire_act(label)
        c = {"id": i["id"], "src": src, "code": code, "label": label, "act": act, "sev": sev,
             "lat": round(i["lat"], 6), "lng": round(i["lng"], 6), "ts": ts,
             "loc": (i.get("location") or "").replace("Scarborough - ", "").title(),
             "div": i.get("division")}
        if src == "tfs" and i.get("units"):
            c["units"] = [u.strip() for u in str(i["units"]).split(",") if u.strip()][:6]
        out.append(c)
    ss = inc.get("source_status", {})
    status["tps"] = ss.get("police", "unknown")
    status["tfs"] = ss.get("fire", "unknown")
    out.sort(key=lambda c: c["ts"])
    # the same event often comes in twice (two callers, re-dispatch): merge near-duplicates
    kept = []
    for c in out:
        dup = False
        for k in reversed(kept[-40:]):
            if c["ts"] - k["ts"] > 1800:
                break
            same_kind = k["code"] == c["code"] or (k["act"].startswith("crash") and c["act"].startswith("crash"))
            if k["src"] == c["src"] and same_kind and abs(k["lat"] - c["lat"]) < 0.0018 and abs(k["lng"] - c["lng"]) < 0.0025:
                dup = True
                break
        if not dup:
            kept.append(c)
    for c in kept:  # "Lewiston Rd - Lewiston Rd" → "Lewiston Rd"
        parts = [p.strip() for p in c["loc"].split(" - ")]
        if len(parts) == 2 and parts[0].lower() == parts[1].lower():
            c["loc"] = parts[0]
    return kept, inc.get("generated")


# ───────────────────────── City road restrictions (already fetched by TOSEER) ─────────────────────────
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def build_roads(status):
    try:
        rr = load("road_restrictions.json")
    except Exception as e:
        status["roads"] = f"down: {e}"
        return []
    now_ms = time.time() * 1000
    out = []
    for r in rr.get("restrictions", []):
        raw = r.get("raw") or {}
        if not inbox(r.get("lat"), r.get("lng")):
            continue
        try:
            end = int(raw.get("endTime") or 0)
            start = int(raw.get("startTime") or 0)
        except ValueError:
            continue
        if end and end < now_ms:
            continue
        if start and start > now_ms + 7 * 86400000:  # more than a week out: irrelevant
            continue
        line = []
        gp = raw.get("geoPolyline")
        if gp and gp != "None":
            try:
                line = [[round(p[0], 6), round(p[1], 6)] for p in json.loads("[" + gp + "]")]
            except Exception:
                line = []
        if not line:
            line = [[r["lng"], r["lat"]]]
        sched = None
        if raw.get("workPeriod") != "Continuous":
            ev = raw.get("scheduleEveryday")
            sched = {}
            for k, d in enumerate(DAYS):
                v = raw.get("schedule" + d)
                if (not v or v == "None") and ev and ev != "None":
                    v = ev
                if v and v != "None":
                    sched[k] = v
        out.append({
            "id": raw.get("id") or r.get("id"), "road": raw.get("road"), "from": raw.get("fromRoad"), "to": raw.get("toRoad"),
            "cls": raw.get("roadClass"), "line": line[:40], "both": raw.get("directionsAffected") == "BOTH_DIRECTIONS",
            "full": raw.get("type") == "ROAD_CLOSED", "impact": raw.get("maxImpact"), "start": start // 1000, "end": end // 1000,
            "sched": sched, "why": (r.get("reason") or raw.get("description") or "").replace("Toronto-TMC: ", "")[:90],
            "who": raw.get("contractor") if raw.get("contractor") not in (None, "None") else None,
        })
    status["roads"] = f"live ({len(out)})"
    return out


# ───────────────────────── history → expected calls per hour (Watch / Shift realism) ─────────────────────────
def build_rates(status):
    try:
        h = load("history_stats.json")
    except Exception as e:
        status["rates"] = f"down: {e}"
        return None
    rates = {}
    for src, keys in (("police", SCARB_DIVS), ("fire", None)):
        groups = h.get(src, {})
        days = [set() for _ in range(7)]  # distinct dates seen per weekday = how many weeks of history
        for g in groups.values():
            for k, b in g.items():
                try:
                    days[int(k.split("_")[0])].update(b.keys())
                except ValueError:
                    pass
        tot = [0.0] * 168
        for gname, g in groups.items():
            if keys is not None and gname not in keys:
                continue
            if keys is None and not gname.startswith("2"):  # TFS 2xx stations are Scarborough
                continue
            for k, b in g.items():
                try:
                    dow, hr = (int(x) for x in k.split("_"))
                except ValueError:
                    continue
                tot[dow * 24 + hr] += sum(b.values())
        nw = [max(1, len(d)) for d in days]
        weeks = min(nw)
        rates[src] = [round(v / nw[i // 24], 2) for i, v in enumerate(tot)]
    status["rates"] = f"live ({weeks}+ weeks)"
    return rates


# ───────────────────────── weather (Open-Meteo, no key) + Environment Canada alerts ─────────────────────────
def build_weather(status):
    try:
        d = get("https://api.open-meteo.com/v1/forecast", {
            "latitude": CENTER[0], "longitude": CENTER[1], "timezone": "America/Toronto",
            "current": "temperature_2m,precipitation,rain,showers,snowfall,weather_code,cloud_cover,wind_speed_10m,wind_gusts_10m,visibility,is_day",
            "hourly": "precipitation_probability,precipitation,weather_code", "forecast_hours": 6})
        c = d.get("current", {})
        wx = {k: c.get(k) for k in ("time", "temperature_2m", "precipitation", "rain", "showers", "snowfall", "weather_code",
                                    "cloud_cover", "wind_speed_10m", "wind_gusts_10m", "visibility", "is_day")}
        hh = d.get("hourly", {})
        wx["next"] = [{"t": t, "p": p, "code": w} for t, p, w in zip(hh.get("time", []), hh.get("precipitation", []), hh.get("weather_code", []))]
        status["weather"] = "live"
        return wx
    except Exception as e:
        status["weather"] = f"down: {e}"
        return None


def build_alerts(status):
    try:
        d = get("https://api.weather.gc.ca/collections/weather-alerts/items", {"f": "json", "bbox": f"{W},{S},{E},{N}", "limit": 50})
        feats = d.get("features", [])
        if feats:
            print("EC alert sample keys:", sorted(feats[0].get("properties", {}).keys()))
        out, seen = [], set()
        for f in feats:
            p = f.get("properties", {})
            name = p.get("alert_name_en") or p.get("alert_short_name_en") or p.get("headline") or p.get("event") or p.get("name")
            if not name or name in seen:
                continue
            seen.add(name)
            out.append({"name": name, "type": p.get("alert_type") or p.get("severity"), "expires": p.get("expiration_datetime") or p.get("expires"),
                        "text": (p.get("alert_text_en") or p.get("description") or "")[:240]})
        status["alerts"] = f"live ({len(out)})"
        return out
    except Exception as e:
        status["alerts"] = f"down: {e}"
        return []


# ───────────────────────── Ontario 511 (401 / DVP closures). Needs a free key: repo secret ONTARIO511_KEY ─────────────────────────
def build_511(status):
    key = os.environ.get("ONTARIO511_KEY", "").strip()
    try:
        params = {"format": "json", "lang": "en"}
        if key:
            params["key"] = key
        d = get("https://511on.ca/api/v2/get/event", params)
    except Exception as e:
        status["511"] = f"down: {e}" + ("" if key else " (no ONTARIO511_KEY secret set)")
        return []
    out = []
    for ev in d if isinstance(d, list) else []:
        lat, lng = ev.get("Latitude"), ev.get("Longitude")
        if not inbox(lat, lng):
            continue
        out.append({"id": ev.get("ID"), "road": ev.get("RoadwayName"), "dir": ev.get("DirectionOfTravel"), "lat": lat, "lng": lng,
                    "lat2": ev.get("LatitudeSecondary"), "lng2": ev.get("LongitudeSecondary"), "type": ev.get("EventType"),
                    "sub": ev.get("EventSubType"), "full": bool(ev.get("IsFullClosure")), "lanes": ev.get("LanesAffected"),
                    "start": ev.get("StartDate"), "end": ev.get("PlannedEndDate"), "desc": (ev.get("Description") or "")[:200]})
    status["511"] = f"live ({len(out)})"
    return out


# ───────────────────────── weekly static: TTC bus routes + fire stations ─────────────────────────
def ckan_resource(slug, want):
    pkg = get(f"{CKAN}/api/3/action/package_show", {"id": slug})
    res = pkg["result"]["resources"]
    for fmt in want:
        for r in res:
            if (r.get("format") or "").lower() == fmt and r.get("url"):
                return r["url"]
    raise RuntimeError(f"{slug}: no resource in {want}; have {[r.get('format') for r in res]}")


def build_stations(status):
    try:
        url = ckan_resource("fire-station-locations", ["geojson", "json"])
        d = get(url, timeout=60)
        out = []
        for f in d.get("features", []):
            p = f.get("properties", {})
            g = f.get("geometry") or {}
            co = g.get("coordinates")
            if g.get("type") == "MultiPoint":
                co = co[0]
            if not co:
                continue
            lng, lat = co[0], co[1]
            if not inbox(lat, lng):
                continue
            sid = str(p.get("STATION") or p.get("STATION_NO") or p.get("NAME") or "").strip()
            digits = "".join(ch for ch in sid if ch.isdigit())
            out.append({"id": digits or sid, "name": p.get("NAME") or sid, "lat": round(lat, 6), "lng": round(lng, 6)})
        if not out:
            print("fire station sample props:", d.get("features", [{}])[0].get("properties"))
        status["stations"] = f"live ({len(out)})"
        return out
    except Exception as e:
        status["stations"] = f"down: {e}"
        return []


def simplify(pts, tol=0.00018):
    """Douglas–Peucker on lng/lat (~15 m)."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]
        bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        L = (dx * dx + dy * dy) or 1e-12
        best, bi = 0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L))
            d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d > best:
                best, bi = d, i
        if bi >= 0 and best > tol * tol:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(pts, keep) if k]


def build_ttc(status):
    try:
        url = ckan_resource("ttc-routes-and-schedules", ["zip"])
        z = zipfile.ZipFile(io.BytesIO(get(url, timeout=180, raw=True)))

        def rows(name):
            with z.open(name) as f:
                yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))

        routes = {r["route_id"]: r for r in rows("routes.txt") if r.get("route_type") == "3"}
        # which service_ids run on weekdays / Saturday / Sunday
        svc_kind = {}
        if "calendar.txt" in z.namelist():
            for r in rows("calendar.txt"):
                if r["monday"] == "1" or r["wednesday"] == "1":
                    svc_kind.setdefault(r["service_id"], set()).add("wd")
                if r["saturday"] == "1":
                    svc_kind.setdefault(r["service_id"], set()).add("sa")
                if r["sunday"] == "1":
                    svc_kind.setdefault(r["service_id"], set()).add("su")
        trips = {}
        for t in rows("trips.txt"):
            if t["route_id"] in routes:
                trips[t["trip_id"]] = (t["route_id"], t.get("direction_id", "0"), t.get("shape_id"), t["service_id"])
        # first departure of each trip
        first = {}
        for st in rows("stop_times.txt"):
            tid = st["trip_id"]
            if tid not in trips:
                continue
            seq = int(st["stop_sequence"])
            if tid not in first or seq < first[tid][0]:
                first[tid] = (seq, st["departure_time"])
        # shapes
        shapes = defaultdict(list)
        for s in rows("shapes.txt"):
            shapes[s["shape_id"]].append((int(s["shape_pt_sequence"]), float(s["shape_pt_lon"]), float(s["shape_pt_lat"])))
        # most-used shape per route+direction, and trips per hour per day-kind
        use = Counter()
        tph = defaultdict(lambda: {"wd": [0] * 28, "sa": [0] * 28, "su": [0] * 28})
        for tid, (rid, d, shp, svc) in trips.items():
            use[(rid, d, shp)] += 1
            if tid in first:
                hr = int(first[tid][1].split(":")[0])
                for k in svc_kind.get(svc, {"wd"}):
                    tph[(rid, d)][k][min(hr, 27)] += 1
        best = {}
        for (rid, d, shp), n in use.items():
            if (rid, d) not in best or n > use[(rid, d, best[(rid, d)])]:
                best[(rid, d)] = shp
        out = []
        for (rid, d), shp in best.items():
            pts = [(lng, lat) for _, lng, lat in sorted(shapes.get(shp, []))]
            inside = [inbox(lat, lng) for lng, lat in pts]
            if not pts or sum(inside) < 0.25 * len(pts):
                continue
            # keep from first to last point inside the box
            i0 = inside.index(True)
            i1 = len(inside) - 1 - inside[::-1].index(True)
            seg = [[round(x, 6), round(y, 6)] for x, y in simplify(pts[i0:i1 + 1])]
            h = tph[(rid, d)]
            for k in h:  # fold 24–27h (after-midnight trips) back onto 0–3h
                for x in range(24, 28):
                    h[k][x - 24] += h[k][x]
                h[k] = h[k][:24]
            r = routes[rid]
            out.append({"route": r.get("route_short_name"), "name": (r.get("route_long_name") or "").title(), "dir": d,
                        "color": r.get("route_color") or "DA251D", "line": seg, "tph": h})
        status["ttc"] = f"live ({len(out)} route-directions)"
        return out
    except Exception as e:
        status["ttc"] = f"down: {e}"
        return []


def main():
    status = {}
    calls, inc_generated = build_calls(status)
    live = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "incidents_generated": inc_generated,
        "calls": calls,
        "roads": build_roads(status),
        "rates": build_rates(status),
        "weather": build_weather(status),
        "alerts": build_alerts(status),
        "hwy": build_511(status),
        "status": status,
    }
    with open(LIVE_PATH, "w", encoding="utf-8") as f:
        json.dump(live, f, separators=(",", ":"))
    print("gridlock_live.json:", os.path.getsize(LIVE_PATH), "bytes", json.dumps(status))

    # weekly static
    stale = True
    try:
        st = load("gridlock_static.json")
        age = time.time() - iso_to_epoch(st.get("generated", ""))
        stale = age > 6.5 * 86400 or not st.get("ttc") or not st.get("stations")
        stale = stale and age > 3600  # don't hammer if a source is down: retry hourly at most
    except Exception:
        st = None
    if stale or "--static" in sys.argv:
        s2 = {}
        new = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "stations": build_stations(s2), "ttc": build_ttc(s2), "status": s2}
        if st:  # keep last good copy of anything that failed this time
            for k in ("stations", "ttc"):
                if not new[k] and st.get(k):
                    new[k] = st[k]
        with open(STATIC_PATH, "w", encoding="utf-8") as f:
            json.dump(new, f, separators=(",", ":"))
        print("gridlock_static.json:", os.path.getsize(STATIC_PATH), "bytes", json.dumps(s2))


if __name__ == "__main__":
    main()
