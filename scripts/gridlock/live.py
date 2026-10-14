"""Live GRIDLOCK data, rebuilt every 15 minutes.

Police + fire calls and City roadwork come from files TOSEER's earlier steps just wrote. Everything else is
fetched here: weather, Environment Canada warnings, Ontario 511, TTC service alerts and bus detours,
311 requests and utility-cut permits (those two only re-download when the City republishes them).
"""

import csv
import io
import json
import os
import re
import time
import zipfile
from datetime import date, timedelta

from .common import (CENTER, E, N, S, SCARB_FSA, W, Status, ckan_resources, clip_line, datastore_rows, decode_polyline, get, inbox,
                     iso_to_epoch, load, now_utc, pb, pb_text, save, simplify, stamp, TORONTO)

OUT, CACHE, STATIC = "gridlock_live.json", "gridlock_cache.json", "gridlock_static.json"
SCARB_DIVS = ("D41", "D42", "D43")
SCARB_WARDS = ("20", "21", "22", "23", "24", "25")


def today():
    return now_utc().astimezone(TORONTO).date()


# ───────────────────────── police + fire calls (TOSEER incidents.json) ─────────────────────────
POLICE_ACT = {"PDACC": "crash1", "PIACC": "crash2", "HAZ": "hazard", "IMPDR": "impaired", "SEEAM": "medical",
              "MISVU": "missing", "MISPE": "missing", "FIR": "fire_small", "ARR": "police_low"}


def police_act(code, sev):
    return POLICE_ACT.get(code) or {"critical": "police_crit", "high": "police_high", "medium": "police_med"}.get(sev, "police_low")


def fire_act(label):
    t = label.lower()
    if "trapped" in t:
        return "crash3"
    if "vehicle" in t:
        return "carfire" if "fire" in t else "crash2"
    if "collision" in t:
        return "crash2"
    if any(k in t for k in ("wires", "hydro", "transformer", "pole")):
        return "hydro"
    if any(k in t for k in ("gas", "hazmat", "spill", "odour", "odor")):
        return "gas"
    if any(k in t for k in ("alarm", "co detector", "carbon monoxide")):
        return "fire_alarm"
    if any(k in t for k in ("grass", "rubbish", "outdoor")):
        return "fire_small"
    if t.startswith("fire") or "smoke" in t or "explosion" in t:
        return "fire_struct"
    if any(k in t for k in ("rescue", "medical", "elevator", "water")):
        return "medical"
    return "fire_alarm"


def _title(s):
    return re.sub(r"[A-Za-z]+('[A-Za-z]+)?", lambda m: m.group(0).capitalize(), s.lower())


def calls(st):
    inc = load("incidents.json")
    if not inc:
        raise RuntimeError("incidents.json missing")
    out = []
    for i in inc.get("incidents", []):
        ts = iso_to_epoch(i.get("ts"))
        if not ts or not inbox(i.get("lat"), i.get("lng")):
            continue
        src = "tps" if i["source"] == "police" else "tfs"
        code, plain, sev = i.get("type", ""), i.get("type_plain") or "", i.get("severity", "low")
        label = (plain if plain and plain != code else "Police incident") if src == "tps" else (plain or "Fire call")
        loc = " - ".join(dict.fromkeys(p.strip() for p in _title((i.get("location") or "").replace("Scarborough - ", "")).split(" - ") if p.strip()))
        c = {"id": i["id"], "src": src, "code": code, "label": label, "act": police_act(code, sev) if src == "tps" else fire_act(label),
             "sev": sev, "lat": round(i["lat"], 6), "lng": round(i["lng"], 6), "ts": ts, "loc": loc, "div": i.get("division")}
        if src == "tfs" and i.get("units"):
            c["units"] = [u.strip() for u in str(i["units"]).split(",") if u.strip()][:6]
        out.append(c)
    out.sort(key=lambda c: c["ts"])
    kept = []  # the same event often arrives twice (two callers, re-dispatch): merge near-duplicates
    for c in out:
        if not any(k["src"] == c["src"] and c["ts"] - k["ts"] < 1800 and abs(k["lat"] - c["lat"]) < 0.0018 and abs(k["lng"] - c["lng"]) < 0.0025
                   and (k["code"] == c["code"] or (k["act"].startswith("crash") and c["act"].startswith("crash"))) for k in kept[-40:]):
            kept.append(c)
    ss = inc.get("source_status", {})
    st["tps"], st["tfs"] = f"{ss.get('police', 'unknown')} ({sum(c['src'] == 'tps' for c in kept)})", f"{ss.get('fire', 'unknown')} ({sum(c['src'] == 'tfs' for c in kept)})"
    return kept, inc.get("generated")


# ───────────────────────── City road restrictions (TOSEER road_restrictions.json) ─────────────────────────
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def roads(st):
    rr = load("road_restrictions.json")
    if not rr:
        raise RuntimeError("road_restrictions.json missing")
    if rr.get("status") != "live":
        st["roads"] = f"stale: City feed down, using list from {rr.get('generated', '?')[:16]}Z" if rr.get("restrictions") else f"down: {rr.get('status')}"
    now_ms, out = time.time() * 1000, []
    for r in rr.get("restrictions", []):
        raw = r.get("raw") or {}
        if not inbox(r.get("lat"), r.get("lng")):
            continue
        try:
            start, end = int(raw.get("startTime") or 0), int(raw.get("endTime") or 0)
        except ValueError:
            continue
        if (end and end < now_ms) or (start and start > now_ms + 7 * 86400000):
            continue
        line = []
        if raw.get("geoPolyline") not in (None, "None", ""):
            try:
                line = [[round(p[0], 6), round(p[1], 6)] for p in json.loads("[" + raw["geoPolyline"] + "]")]
            except ValueError:
                pass
        sched = None
        if raw.get("workPeriod") != "Continuous":
            every = raw.get("scheduleEveryday")
            sched = {k: v for k, v in ((k, raw.get("schedule" + d) if raw.get("schedule" + d) not in (None, "None") else every) for k, d in enumerate(DAYS)) if v and v != "None"}
        out.append({"id": raw.get("id") or r.get("id"), "road": raw.get("road"), "from": raw.get("fromRoad"), "to": raw.get("toRoad"),
                    "line": (line or [[r["lng"], r["lat"]]])[:40], "both": raw.get("directionsAffected") == "BOTH_DIRECTIONS",
                    "full": raw.get("type") == "ROAD_CLOSED", "impact": raw.get("maxImpact"), "start": start // 1000, "end": end // 1000, "sched": sched,
                    "why": (r.get("reason") or raw.get("description") or "").replace("Toronto-TMC: ", "")[:90],
                    "who": raw.get("contractor") if raw.get("contractor") not in (None, "None") else None})
    return out


# ───────────────────────── call history → calls per hour (Watch mode realism) ─────────────────────────
def rates(st):
    h = load("history_stats.json")
    if not h:
        raise RuntimeError("history_stats.json missing")
    out, weeks = {}, 0
    for src, keep in (("police", lambda g: g in SCARB_DIVS), ("fire", lambda g: g.startswith("2"))):
        groups = h.get(src, {})
        days = [set() for _ in range(7)]
        for g in groups.values():
            for k, b in g.items():
                if "_" in k:
                    days[int(k.split("_")[0])].update(b.keys())
        tot = [0.0] * 168
        for gname, g in groups.items():
            if keep(gname):
                for k, b in g.items():
                    if "_" in k:
                        dow, hr = (int(x) for x in k.split("_"))
                        tot[dow * 24 + hr] += sum(b.values())
        nw = [max(1, len(d)) for d in days]
        weeks = min(nw)
        out[src] = [round(v / nw[i // 24], 2) for i, v in enumerate(tot)]
    st["rates"] = f"live ({weeks}+ weeks of history)"
    return out


# ───────────────────────── weather + warnings ─────────────────────────
def weather():
    d = get("https://api.open-meteo.com/v1/forecast", {
        "latitude": CENTER[0], "longitude": CENTER[1], "timezone": "America/Toronto",
        "current": "temperature_2m,precipitation,rain,showers,snowfall,weather_code,cloud_cover,wind_speed_10m,wind_gusts_10m,visibility,is_day"},
        timeout=25)
    c = d.get("current", {})
    return {k: c.get(k) for k in ("time", "temperature_2m", "precipitation", "rain", "showers", "snowfall", "weather_code", "cloud_cover",
                                  "wind_speed_10m", "wind_gusts_10m", "visibility", "is_day")}


def warnings():
    d = get("https://api.weather.gc.ca/collections/weather-alerts/items", {"f": "json", "bbox": f"{W},{S},{E},{N}", "limit": 50}, timeout=25)
    out, seen = [], set()
    for f in d.get("features", []):
        p = f.get("properties", {})
        name = p.get("alert_name_en") or p.get("alert_short_name_en") or p.get("headline") or p.get("event")
        if name and name not in seen:
            seen.add(name)
            out.append({"name": name, "type": p.get("alert_type") or p.get("severity"), "expires": p.get("expiration_datetime") or p.get("expires"),
                        "text": (p.get("alert_text_en") or p.get("description") or "")[:240]})
    return out


# ───────────────────────── Ontario 511 (needs repo secret ONTARIO511_KEY) ─────────────────────────
def ontario511(st):
    key = os.environ.get("ONTARIO511_KEY", "").strip()
    if not key:
        st["511"] = "off: add a free Ontario 511 key as repo secret ONTARIO511_KEY"
        return []
    d = get("https://511on.ca/api/v2/get/event", {"format": "json", "lang": "en", "key": key}, timeout=25)
    return [{"id": v.get("ID"), "road": v.get("RoadwayName"), "dir": v.get("DirectionOfTravel"), "lat": v.get("Latitude"), "lng": v.get("Longitude"),
             "type": v.get("EventType"), "sub": v.get("EventSubType"), "full": bool(v.get("IsFullClosure")), "lanes": v.get("LanesAffected"),
             "start": v.get("StartDate"), "end": v.get("PlannedEndDate"), "desc": (v.get("Description") or "")[:200]}
            for v in (d if isinstance(d, list) else []) if inbox(v.get("Latitude"), v.get("Longitude"))]


# ───────────────────────── TTC: service alerts (GTFS-RT) → subway shuttles, route notices ─────────────────────────
EFFECT = {1: "NO_SERVICE", 2: "REDUCED_SERVICE", 3: "SIGNIFICANT_DELAYS", 4: "DETOUR", 5: "ADDITIONAL_SERVICE", 6: "MODIFIED_SERVICE",
          7: "OTHER_EFFECT", 8: "UNKNOWN_EFFECT", 9: "STOP_MOVED", 10: "NO_EFFECT", 11: "ACCESSIBILITY_ISSUE"}
BETWEEN = re.compile(r"between ([A-Z][\w.' -]+?) and ([A-Z][\w.' -]+?) stations?", re.I)


def ttc_alerts(static):
    routes = {r.get("rid"): r["route"] for r in static.get("ttc", [])} if static else {}
    subway = static.get("subway", {}) if static else {}
    by_name = {}
    for sid, (name, lat, lng) in subway.items():
        by_name.setdefault(name.lower(), (name, lat, lng))
    now = time.time()
    feed = pb(get("https://gtfsrt.ttc.ca/alerts/all", {"format": "binary"}, raw=True, timeout=30))
    out = []
    for ent in feed.get(2, []):
        e = pb(ent)
        if 5 not in e:
            continue
        a = pb(e[5][0])
        periods = [(pb(p).get(1, [0])[0], pb(p).get(2, [0])[0]) for p in a.get(1, [])]
        if periods and not any((s or 0) - 6 * 3600 <= now <= (t or 4e9) for s, t in periods):
            continue  # neither active nor starting within six hours
        rids, stops = set(), set()
        for sel in a.get(5, []):
            s = pb(sel)
            if 2 in s:
                rids.add(s[2][0].decode())
            if 5 in s:
                stops.add(s[5][0].decode())
        header = pb_text(a[10][0]) if 10 in a else ""
        effect = EFFECT.get(a.get(7, [8])[0], "UNKNOWN_EFFECT")
        here_stops = [subway[x] for x in stops if x in subway and inbox(subway[x][1], subway[x][2])]
        bus_routes = sorted({routes[r] for r in rids if r in routes})
        if not here_stops and not bus_routes and not re.search(r"kennedy|warden|victoria park|scarborough", header, re.I):
            continue
        alert = {"id": e.get(1, [b""])[0].decode(), "effect": effect, "header": header[:300], "routes": bus_routes,
                 "lines": sorted(r for r in rids if r in ("1", "2", "4", "5", "6")), "periods": periods[:4]}
        m = BETWEEN.search(header)
        if m and alert["lines"] and effect in ("NO_SERVICE", "REDUCED_SERVICE", "MODIFIED_SERVICE"):
            a_st, b_st = by_name.get(m.group(1).strip().lower()), by_name.get(m.group(2).strip().lower())
            if a_st and b_st:
                alert["shuttle"] = {"from": a_st, "to": b_st}
        out.append(alert)
    return out


DETOUR_ID = re.compile(r":(\d)_\d_\d+_([^_]+)_(.+?)(_shape)?$")


def ttc_detours(static):
    ours = {(r["route"], r["dir"]) for r in static.get("ttc", [])} if static else set()
    d = get("https://gtfsrt.ttc.ca/trips/detour", {"format": "text"}, timeout=30)
    shapes = {e["id"]: e["shape"]["encodedPolyline"] for e in d.get("entity", []) if "shape" in e}
    ymd = today().strftime("%Y%m%d")
    out, seen = [], set()
    for e in d.get("entity", []):
        tm = e.get("tripModifications")
        if not tm or ymd not in tm.get("serviceDates", [ymd]):
            continue
        m = DETOUR_ID.search(e["id"])
        if not m:
            continue
        direction, route, headsign = m.group(1), m.group(2).replace(" Bus", ""), m.group(3)
        if (route, direction) not in ours or (route, direction) in seen:
            continue
        sid = next((s.get("shapeId") for s in tm.get("selectedTrips", []) if s.get("shapeId")), None)
        if sid not in shapes:
            continue
        line = clip_line(decode_polyline(shapes[sid]))
        if len(line) >= 2:
            seen.add((route, direction))
            out.append({"route": route, "dir": direction, "headsign": headsign, "line": [[round(x, 6), round(y, 6)] for x, y in simplify(line)]})
    return out


# ───────────────────────── 311 requests (City republishes the year file every few days) ─────────────────────────
ACT_311 = {"Road Pothole / Road Damage": "pothole", "Road - Sinking": "sinkhole", "Catch Basin - Blocked / Flooding": "ponding",
           "Tree Emergency Clean-Up": "tree", "Clean up Debris on Road": "debris", "Watermain-Possible Break": "watermain"}
FRESH_311 = {"tree": 3, "debris": 3, "watermain": 4}  # days these stay on the road; potholes stay until fixed


def requests_311(cache, st):
    year = str(today().year)
    res = next(r for r in ckan_resources("311-service-requests-customer-initiated") if r["name"].strip().endswith(year))
    key = res.get("last_modified") or res.get("id")
    if cache.get("311", {}).get("stamp") != key:
        z = zipfile.ZipFile(io.BytesIO(get(res["url"], raw=True, timeout=240)))
        rows = csv.DictReader(io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8-sig", errors="replace"))
        items, counter, newest = [], {}, ""
        for row in rows:
            fsa, typ, status = row.get("First 3 Chars of Postal Code") or "", row.get("Service Request Type") or "", row.get("Status") or ""
            created = (row.get("Creation Date") or "")[:10]
            newest = max(newest, created)
            if fsa not in SCARB_FSA or typ not in ACT_311 or status not in ("New", "In Progress"):
                continue
            n = counter[(created, typ, fsa)] = counter.get((created, typ, fsa), 0) + 1
            items.append({"id": f"{created}|{ACT_311[typ]}|{fsa}|{n}", "act": ACT_311[typ], "type": typ, "fsa": fsa, "date": created, "status": status})
        cache["311"] = {"stamp": key, "data": {"items": items, "newest": newest}}
    data = cache["311"]["data"]
    newest = date.fromisoformat(data["newest"]) if data.get("newest") else today()
    keep = [i for i in data["items"] if i["act"] not in FRESH_311 or (newest - date.fromisoformat(i["date"])).days <= FRESH_311[i["act"]]]
    st["311"] = f"live ({len(keep)} open road requests, City data to {data.get('newest')})"
    return {"items": keep[-600:], "newest": data.get("newest")}


# ───────────────────────── utility cut permits (Hydro, Enbridge, Bell, Rogers, Toronto Water digs) ─────────────────────────
DESC = re.compile(r"^\s*(?:(\d+[A-Z]?)\s+)?(.+?)\s*\(\s*between\s+(.+?)\s+and\s+(.+?)\s*\)", re.I)


def utility_cuts(cache, st):
    res = ckan_resources("utility-cut-permits")
    ds = next(r for r in res if r.get("datastore_active") in (True, "True") and r["name"].strip() == "Utility Cut Permits")
    key = "v2|" + max((r.get("last_modified") or "") for r in res if r["name"].startswith("Utility Cut Permits."))  # bump vN when parsing changes
    t = today()
    if cache.get("cuts", {}).get("stamp") != key or cache["cuts"].get("day") != t.isoformat():
        items = []
        for r in datastore_rows(ds["id"], {"CITY_WARD": list(SCARB_WARDS)}):
            try:
                a, b = date.fromisoformat(r["PROPOSED_FROM_DATE"][:10]), date.fromisoformat(r["PROPOSED_TO_DATE"][:10])
            except (KeyError, TypeError, ValueError):
                continue
            if not (a <= t + timedelta(1) and b >= t) or "CANCEL" in (r.get("PERMIT_STATUS") or "").upper():
                continue
            m = DESC.match(r.get("DISPLAY_DESC") or "")
            if not m:
                continue
            items.append({"id": r.get("PERMIT_NUMBER"), "who": (r.get("CLIENT_NAME") or "").strip(), "what": (r.get("INSTALLATION_TYPE_DESC") or "").strip(),
                          "num": m.group(1), "street": _title(re.sub(r"\[.*?\]|,", " ", m.group(2)).strip()), "a": _title(m.group(3)), "b": _title(m.group(4)),
                          "from": a.isoformat(), "to": b.isoformat()})
        cache["cuts"] = {"stamp": key, "day": t.isoformat(), "data": items}
    return cache["cuts"]["data"]


# ───────────────────────── build ─────────────────────────
def build():
    st = Status()
    static = load(STATIC, {}) or {}
    cache = load(CACHE, {}) or {}
    got = st.run("tps", calls, st) or ([], None)
    out = {"generated": stamp(), "incidents_generated": got[1], "calls": got[0]}
    out["roads"] = st.run("roads", roads, st)
    out["rates"] = st.run("rates", rates, st)
    out["weather"] = st.run("weather", weather)
    out["alerts"] = st.run("alerts", warnings)
    out["hwy"] = st.run("511", ontario511, st)
    out["ttc_alerts"] = st.run("ttc_alerts", ttc_alerts, static)
    out["ttc_detours"] = st.run("ttc_detours", ttc_detours, static)
    out["s311"] = st.run("311", requests_311, cache, st)
    out["cuts"] = st.run("cuts", utility_cuts, cache, st)
    out["status"] = dict(st)
    size = save(OUT, out)
    save(CACHE, cache)
    print(OUT, size, "bytes")
    for k, v in st.items():
        print(f"  {k}: {v}")
