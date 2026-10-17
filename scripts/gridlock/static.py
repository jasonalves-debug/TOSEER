"""Weekly GRIDLOCK data: things that change slowly or need big downloads.

TTC + GO schedules, fire stations, schools, apartment towers, construction sites, places of worship,
neighbourhood profiles, postal-area points (to place 311 requests), and the holiday / school calendar.
"""

import csv
import io
import json
import math
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from datetime import date, timedelta

from .common import (SCARB_FSA, Status, ckan_resource, clip_line, datastore, datastore_rows, get, inbox, load, metres,
                     now_utc, point_of, save, simplify, stamp, TORONTO)

OUT = "gridlock_static.json"


# ───────────────────────── fire stations ─────────────────────────
def fire_stations():
    out = []
    for row in datastore("fire-station-locations"):
        p = point_of(row)
        if not p or not inbox(*p):
            continue
        sid = str(row.get("STATION") or row.get("STATION_NO") or row.get("NAME") or "")
        digits = "".join(ch for ch in sid if ch.isdigit())
        out.append({"id": digits or sid, "name": row.get("NAME") or sid, "lat": round(p[0], 6), "lng": round(p[1], 6)})
    return out


# ───────────────────────── GTFS helpers ─────────────────────────
def gtfs_zip(url):
    z = zipfile.ZipFile(io.BytesIO(get(url, timeout=300, raw=True)))

    def rows(name):
        with z.open(name) as f:
            yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
    return z, rows


def secs(hms):
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + s


# ───────────────────────── TTC: bus routes, frequencies, subway stations ─────────────────────────
def ttc():
    url = ckan_resource("ttc-routes-and-schedules", lambda r: (r.get("format") or "").lower() == "zip")["url"]
    z, rows = gtfs_zip(url)
    routes = {r["route_id"]: r for r in rows("routes.txt")}
    bus = {rid for rid, r in routes.items() if r.get("route_type") == "3"}
    subway = {rid for rid, r in routes.items() if r.get("route_type") == "1"}
    svc_kind = defaultdict(set)
    if "calendar.txt" in z.namelist():
        for r in rows("calendar.txt"):
            if r["monday"] == "1" or r["wednesday"] == "1":
                svc_kind[r["service_id"]].add("wd")
            if r["saturday"] == "1":
                svc_kind[r["service_id"]].add("sa")
            if r["sunday"] == "1":
                svc_kind[r["service_id"]].add("su")
    trips = {t["trip_id"]: (t["route_id"], t.get("direction_id", "0"), t.get("shape_id"), t["service_id"])
             for t in rows("trips.txt") if t["route_id"] in bus or t["route_id"] in subway}
    first, subway_stops = {}, set()
    allstops = {s["stop_id"]: s for s in rows("stops.txt")}
    here = {sid: (float(s["stop_lat"]), float(s["stop_lon"])) for sid, s in allstops.items()
            if inbox(float(s["stop_lat"]), float(s["stop_lon"]), 0.01)}
    timed = defaultdict(list)  # weekday bus trips: (seq, stop, departure s) at Scarborough stops → scheduled bus speeds
    for st in rows("stop_times.txt"):
        tr = trips.get(st["trip_id"])
        if not tr:
            continue
        if tr[0] in subway:
            subway_stops.add(st["stop_id"])
            continue
        seq = int(st["stop_sequence"])
        if st["trip_id"] not in first or seq < first[st["trip_id"]][0]:
            first[st["trip_id"]] = (seq, st["departure_time"])
        if st["stop_id"] in here and "wd" in svc_kind.get(tr[3], {"wd"}) and st["departure_time"]:
            timed[st["trip_id"]].append((seq, st["stop_id"], secs(st["departure_time"])))
    stations = {}
    for sid in subway_stops:
        s = allstops.get(sid)
        if s:
            base = re.split(r"\s+station\b", s["stop_name"], flags=re.I)[0].strip().title()
            stations[sid] = [base, round(float(s["stop_lat"]), 6), round(float(s["stop_lon"]), 6)]
    sched = bus_sched(timed, here)
    shapes = defaultdict(list)
    for s in rows("shapes.txt"):
        shapes[s["shape_id"]].append((int(s["shape_pt_sequence"]), float(s["shape_pt_lon"]), float(s["shape_pt_lat"])))
    use, tph = Counter(), defaultdict(lambda: {"wd": [0] * 28, "sa": [0] * 28, "su": [0] * 28})
    for tid, (rid, d, shp, svc) in trips.items():
        if rid not in bus:
            continue
        use[(rid, d, shp)] += 1
        if tid in first:
            hr = min(int(first[tid][1].split(":")[0]), 27)
            for k in svc_kind.get(svc, {"wd"}):
                tph[(rid, d)][k][hr] += 1
    best = {}
    for (rid, d, shp), n in use.items():
        if (rid, d) not in best or n > use[(rid, d, best[(rid, d)])]:
            best[(rid, d)] = shp
    out = []
    for (rid, d), shp in best.items():
        pts = [[x, y] for _, x, y in sorted(shapes.get(shp, []))]
        inside = sum(inbox(y, x) for x, y in pts)
        if not pts or inside < 0.25 * len(pts):
            continue
        h = tph[(rid, d)]
        for k in h:  # fold after-midnight hours (24–27) onto 0–3
            h[k] = [h[k][i] + (h[k][i + 24] if i < 4 else 0) for i in range(24)]
        r = routes[rid]
        out.append({"rid": rid, "route": r.get("route_short_name"), "name": (r.get("route_long_name") or "").title(), "dir": d,
                    "line": [[round(x, 6), round(y, 6)] for x, y in simplify(clip_line(pts))], "tph": h})
    return {"routes": out, "subway": stations, "bsched": sched}


def _metres(a, b):
    dy = (b[0] - a[0]) * 111320
    dx = (b[1] - a[1]) * 111320 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


def bus_sched(timed, here):
    """Scheduled weekday bus speed (stops included) on ~600 m stretches between stops, by time of day.
    The TTC timetable already allows for normal rush-hour traffic, so a bus well behind this pace is stuck in something
    unusual. Output: [latA, lngA, latB, lngB, [m/s before 7, 7-9:30, 9:30-15, 15-19, after 19]] (0 = no trips)."""
    def period(t):
        h = (t / 3600) % 24
        return 0 if 5 <= h < 7 else 1 if h < 9.5 and h >= 7 else 2 if 9.5 <= h < 15 else 3 if 15 <= h < 19 else 4
    agg = defaultdict(lambda: [[] for _ in range(5)])
    for seq in timed.values():
        seq.sort()
        i = 0
        while i < len(seq) - 1:
            a, d, j = seq[i], 0.0, i
            while j < len(seq) - 1 and d < 600:
                if seq[j + 1][0] - seq[j][0] > 3:  # left the area and came back: don't bridge the gap
                    break
                d += _metres(here[seq[j][1]], here[seq[j + 1][1]])
                j += 1
            b = seq[j]
            if j == i:
                i += 1
                continue
            dt = b[2] - a[2]
            if d >= 400 and dt > 0 and 1 < d / dt < 25:
                agg[(a[1], b[1])][period(a[2])].append(d / dt)
            i = j
    out = []
    for (sa, sb), per in agg.items():
        med = [round(sorted(v)[len(v) // 2], 1) if v else 0 for v in per]
        if any(med):
            (la, ga), (lb, gb) = here[sa], here[sb]
            out.append([round(la, 5), round(ga, 5), round(lb, 5), round(gb, 5), med])
    return out


# ───────────────────────── GO trains (Stouffville + Lakeshore East) and level crossings ─────────────────────────
GO_GTFS = "https://assets.metrolinx.com/raw/upload/Documents/Metrolinx/Open%20Data/GO-GTFS.zip"


def _cum(line):
    out, acc = [0.0], 0.0
    for (x0, y0), (x1, y1) in zip(line, line[1:]):
        acc += metres(y0, x0, y1, x1) ** 0.5
        out.append(round(acc, 1))
    return out


def _project(line, cum, lat, lng):
    """(distance along line in m, offset in m) of the closest point."""
    best = (1e18, 0.0)
    kx = 111320.0 * 0.7217
    for i, ((x0, y0), (x1, y1)) in enumerate(zip(line, line[1:])):
        ax, ay, bx, by, px, py = x0 * kx, y0 * 111320.0, x1 * kx, y1 * 111320.0, lng * kx, lat * 111320.0
        vx, vy = bx - ax, by - ay
        L2 = vx * vx + vy * vy or 1e-9
        t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / L2))
        d2 = (px - ax - t * vx) ** 2 + (py - ay - t * vy) ** 2
        if d2 < best[0]:
            best = (d2, cum[i] + t * (L2 ** 0.5))
    return best[1], best[0] ** 0.5


def go_trains(days=8):
    z, rows = gtfs_zip(GO_GTFS)
    lines = {r["route_id"] for r in rows("routes.txt") if r.get("route_short_name") in ("ST", "LE")}
    today = now_utc().astimezone(TORONTO).date()
    want = {(today + timedelta(d)).strftime("%Y%m%d") for d in range(-1, days)}
    svc_dates = defaultdict(set)
    for c in rows("calendar_dates.txt"):
        if c.get("exception_type") == "1" and c["date"] in want:
            svc_dates[c["service_id"]].add(c["date"])
    trips = {t["trip_id"]: t for t in rows("trips.txt") if t["route_id"] in lines and t["service_id"] in svc_dates}
    stops = {s["stop_id"]: (float(s["stop_lat"]), float(s["stop_lon"]), s["stop_name"]) for s in rows("stops.txt")}
    times = defaultdict(list)
    for st in rows("stop_times.txt"):
        if st["trip_id"] in trips:
            times[st["trip_id"]].append((int(st["stop_sequence"]), st["stop_id"], secs(st["arrival_time"]), secs(st["departure_time"])))
    shape_ids = {t["shape_id"] for t in trips.values()}
    raw = defaultdict(list)
    for s in rows("shapes.txt"):
        if s["shape_id"] in shape_ids:
            raw[s["shape_id"]].append((int(s["shape_pt_sequence"]), float(s["shape_pt_lon"]), float(s["shape_pt_lat"])))
    shapes, index, same = [], {}, {}
    for sid, pts in raw.items():
        line = [[round(x, 6), round(y, 6)] for x, y in simplify(clip_line([[x, y] for _, x, y in sorted(pts)], pad=0.05), tol=0.00005)]
        if len(line) < 2:
            continue
        key = (tuple(line[0]), tuple(line[-1]), len(line))  # many shape_ids are the same track
        if key not in same:
            same[key] = len(shapes)
            shapes.append({"line": line, "cum": _cum(line)})
        index[sid] = same[key]
    by_date = defaultdict(list)
    for tid, t in trips.items():
        si = index.get(t["shape_id"])
        if si is None:
            continue
        sh = shapes[si]
        pts = []
        for _, stop, arr, dep in sorted(times[tid]):
            lat, lng, _name = stops[stop]
            d, off = _project(sh["line"], sh["cum"], lat, lng)
            if off < 400 and 0 < d < sh["cum"][-1]:
                pts.append([arr, round(d)])
                if dep != arr:  # dwell at the platform
                    pts.append([dep, round(d)])
        if len(pts) >= 2:
            for dt in svc_dates[t["service_id"]]:
                by_date[dt].append([si, pts])
    for v in by_date.values():
        v.sort(key=lambda tr: tr[1][0][0])
    return {"shapes": shapes, "trips": dict(by_date)}


OVERPASS = ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter", "https://maps.mail.ru/osm/tools/overpass/api/interpreter")


def level_crossings(go):
    q = '[out:json][timeout:90];node["railway"="level_crossing"](43.665,-79.345,43.87,-79.10);out;'
    d, last = None, None
    for server in OVERPASS:  # the public Overpass servers are often busy: try the mirrors in turn
        try:
            d = get(server, data=("data=" + q).encode(), timeout=120, tries=1)
            break
        except Exception as e:  # noqa: BLE001
            last = e
    if d is None:
        raise last
    out = []
    for n in d.get("elements", []):
        for si, sh in enumerate(go["shapes"]):
            dist, off = _project(sh["line"], sh["cum"], n["lat"], n["lon"])
            if off < 25:
                out.append({"lat": round(n["lat"], 6), "lng": round(n["lon"], 6), "shape": si, "d": round(dist)})
    # one entry per physical crossing per shape
    seen, uniq = set(), []
    for c in out:
        key = (c["shape"], round(c["d"] / 30))
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    return uniq


# ───────────────────────── schools ─────────────────────────
SECONDARY = re.compile(r"collegiate|secondary|high school|\bc\.?\s?i\.?\b|\bs\.?s\.?\b|institute|academy", re.I)


def schools():
    out = []
    for row in datastore("school-locations-all-types"):
        p = point_of(row)
        postal = (row.get("POSTAL_CODE") or "")[:3]
        if not p or not inbox(*p) or postal not in SCARB_FSA:
            continue
        typ = row.get("SCHOOL_TYPE_DESC") or ""
        if typ == "College":
            continue
        name = (row.get("NAME") or "").title()
        kind = "private" if typ == "Private" else "sec" if SECONDARY.search(name) else "elem"
        out.append({"name": name, "kind": kind, "board": row.get("BOARD_NAME") or typ, "lat": round(p[0], 6), "lng": round(p[1], 6), "fsa": postal})
    return out


# ───────────────────────── apartment towers (City apartment-building evaluations: storeys, units, location) ─────────────────────────
def towers():
    latest = {}
    for row in datastore("apartment-building-evaluation", pred=lambda r: r.get("datastore_active") in (True, "True") and "current" in r["name"].lower()):
        try:
            lat, lng = float(row["LATITUDE"]), float(row["LONGITUDE"])
            storeys, units = int(float(row["CONFIRMED STOREYS"])), int(float(row["CONFIRMED UNITS"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not inbox(lat, lng):
            continue
        year = row.get("YEAR EVALUATED") or ""
        rsn = row.get("RSN")
        if rsn not in latest or year > latest[rsn][0]:
            latest[rsn] = (year, {"lat": round(lat, 6), "lng": round(lng, 6), "storeys": storeys, "units": units,
                                  "addr": (row.get("SITE ADDRESS") or "").title(), "built": row.get("YEAR BUILT")})
    return [v for _, v in latest.values()]


# ───────────────────────── major construction sites (active building permits + address points) ─────────────────────────
CLOSED = {"Closed", "Cancelled", "Revoked", "Refused", "Application Withdrawn", "Abandoned", "Permit Revoked", "Work Not Started (Revoked)"}


def _clip(text, n):
    """First n characters, cut at a word boundary, with an ellipsis when shortened."""
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0].rstrip(",;:-") + "…"


def construction():
    rows = datastore("building-permits-active-permits", filters={"POSTAL": list(SCARB_FSA)})
    sites = {}
    for r in rows:
        if (r.get("WORK") or "") != "New Building" or (r.get("STATUS") or "") in CLOSED or (r.get("COMPLETED_DATE") or "None") != "None":
            continue
        try:
            units = int(float(r.get("DWELLING_UNITS_CREATED") or 0))
            cost = float(r.get("EST_CONST_COST") or 0)
        except ValueError:
            continue
        if units < 20 and cost < 5_000_000:
            continue
        gid = str(r.get("GEO_ID") or "")
        if gid and (gid not in sites or cost > sites[gid]["cost"]):
            sites[gid] = {"addr": f"{r.get('STREET_NUM')} {r.get('STREET_NAME')} {r.get('STREET_TYPE')}".title().strip(), "units": units, "cost": cost,
                          "use": (r.get("PROPOSED_USE") or "").title(), "desc": _clip(r.get("DESCRIPTION") or "", 160), "status": r.get("STATUS")}
    addr = ckan_resource("address-points-municipal-toronto-one-address-repository", lambda r: r.get("datastore_active") in (True, "True"))["id"]
    ids = [int(g) for g in sites if g.isdigit()]
    for i in range(0, len(ids), 80):
        for row in datastore_rows(addr, {"ADDRESS_POINT_ID": ids[i:i + 80]}):
            p = point_of(row)
            s = sites.get(str(row.get("ADDRESS_POINT_ID")))
            if p and s:
                s["lat"], s["lng"] = round(p[0], 6), round(p[1], 6)
    return [s for s in sites.values() if "lat" in s and inbox(s["lat"], s["lng"])]


# ───────────────────────── places of worship ─────────────────────────
def worship():
    out = []
    for row in datastore("places-of-worship"):
        p = point_of(row)
        if not p or not inbox(*p):
            continue
        out.append({"name": (row.get("PLACE_NAME") or row.get("FTH_ORGANIZATION") or "").strip(), "faith": row.get("FTH_FAITH") or "",
                    "denom": row.get("FTH_DENOMINATION") or "", "lat": round(p[0], 6), "lng": round(p[1], 6), "fsa": (row.get("POSTAL_CODE") or "")[:3]})
    return out


# ───────────────────────── neighbourhoods: boundaries + 2021 census profile ─────────────────────────
def _xlsx_rows(blob):
    z = zipfile.ZipFile(io.BytesIO(blob))
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    shared = ["".join(t.text or "" for t in si.iter("{%s}t" % ns["m"])) for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns)]
    for row in ET.fromstring(z.read("xl/worksheets/sheet1.xml")).iter("{%s}row" % ns["m"]):
        cells = {}
        for c in row.findall("m:c", ns):
            col = re.match(r"[A-Z]+", c.get("r")).group(0)
            idx = 0
            for ch in col:
                idx = idx * 26 + ord(ch) - 64
            v = c.find("m:v", ns)
            if v is None:
                continue
            cells[idx - 1] = shared[int(v.text)] if c.get("t") == "s" else v.text
        if cells:
            yield [cells.get(i) for i in range(max(cells) + 1)]


PROFILE_ROWS = {
    "pop": r"^Total - Age groups of the population",
    "hh": r"^Total - Private households by household size",
    "commuters": r"^Total - Main mode of commuting",
    "car": r"^\s*Car, truck or van\s*$",
    "transit": r"^\s*Public transit\s*$",
    "walk": r"^\s*Walked\s*$",
    "bike": r"^\s*Bicycle\s*$",
}


def _norm(n):
    return re.sub(r"[^a-z]", "", re.sub(r"\(\d+\)", "", (n or "").lower()))


def neighbourhoods():
    polys = {}
    for row in datastore("neighbourhoods", pred=lambda r: r.get("datastore_active") in (True, "True") and "historical" not in r["name"].lower()):
        g = row.get("geometry")
        g = g if isinstance(g, dict) else json.loads(g)
        ring = g["coordinates"][0] if g["type"] == "Polygon" else max((p[0] for p in g["coordinates"]), key=len)
        cx = sum(p[0] for p in ring) / len(ring)
        cy = sum(p[1] for p in ring) / len(ring)
        if inbox(cy, cx):
            polys[_norm(row["AREA_NAME"])] = {"name": row["AREA_NAME"], "poly": [[round(x, 5), round(y, 5)] for x, y in simplify(ring, 0.0003)]}
    blob = get(ckan_resource("neighbourhood-profiles", lambda r: (r.get("format") or "").upper() == "XLSX" and "2021" in r["name"])["url"], raw=True, timeout=120)
    rows = list(_xlsx_rows(blob))
    header = next(r for r in rows if r and (r[0] or "").strip().lower().startswith("neighbourhood name"))
    cols = {i: _norm(n) for i, n in enumerate(header) if i and n}
    found = {}
    for r in rows:
        label = r[0] or ""
        for key, pat in PROFILE_ROWS.items():
            if key not in found and re.search(pat, label):
                found[key] = r
    for i, key in cols.items():
        if key not in polys:
            continue
        rec = polys[key]
        for k, r in found.items():
            try:
                rec[k] = int(float(r[i]))
            except (IndexError, TypeError, ValueError):
                pass
    return list(polys.values())


# ───────────────────────── postal areas → sample points (311 only publishes the first 3 postal characters) ─────────────────────────
def fsa_points(*sources):
    pts = defaultdict(list)
    for src in sources:
        for item in src or []:
            if item.get("fsa") in SCARB_FSA and len(pts[item["fsa"]]) < 40:
                pts[item["fsa"]].append([item["lat"], item["lng"]])
    return dict(pts)


# ───────────────────────── calendar: Ontario holidays + TDSB school year ─────────────────────────
# TDSB elementary calendar 2026–27 (tdsb.on.ca key dates). Update each summer when the board publishes the next year.
SCHOOL_YEARS = [{
    "year": "2026-27", "first": "2026-09-08", "last": "2027-06-29",
    "breaks": [["2026-12-21", "2027-01-01"], ["2027-03-15", "2027-03-19"]],
    "off": ["2026-09-02", "2026-09-03", "2026-11-20", "2027-01-15", "2027-02-12", "2027-06-04", "2027-06-30",
            "2026-10-12", "2026-12-30", "2026-12-31", "2027-02-15", "2027-03-26", "2027-03-29", "2027-05-24"],
}]


def calendar():
    year = now_utc().astimezone(TORONTO).year
    hols = []
    for y in (year, year + 1):
        for h in get("https://canada-holidays.ca/api/v1/provinces/ON", {"year": y})["province"]["holidays"]:
            hols.append({"date": h.get("observedDate") or h["date"], "name": h["nameEn"]})
    days = set()
    for sy in SCHOOL_YEARS:
        days.update(sy["off"])
        for a, b in sy["breaks"]:
            d, end = date.fromisoformat(a), date.fromisoformat(b)
            while d <= end:
                days.add(d.isoformat())
                d += timedelta(1)
    return {"holidays": hols, "school": [{"year": sy["year"], "first": sy["first"], "last": sy["last"]} for sy in SCHOOL_YEARS], "noschool": sorted(days)}


# ───────────────────────── build ─────────────────────────
def cameras():
    """Traffic cameras: City of Toronto intersection cameras (live still, refreshed every few minutes) + Ontario 511 highway cameras."""
    out = []
    for r in datastore("traffic-cameras"):
        p = point_of(r)
        if p and inbox(*p) and r.get("IMAGEURL"):
            out.append({"src": "city", "name": f"{_title(r.get('MAINROAD'))} & {_title(r.get('CROSSROAD'))}", "lat": round(p[0], 5), "lng": round(p[1], 5),
                        "views": [["Live", r["IMAGEURL"].replace("http://", "https://")]]})
    key = os.environ.get("ONTARIO511_KEY", "").strip()
    if key:
        for c in get("https://511on.ca/api/v2/get/cameras", {"format": "json", "lang": "en", "key": key}, timeout=60):
            views = [[v.get("Description") or "View", v["Url"]] for v in c.get("Views") or [] if v.get("Status") == "Enabled" and v.get("Url")]
            if views and inbox(c.get("Latitude"), c.get("Longitude")):
                out.append({"src": "511", "name": c.get("Location") or c.get("Roadway") or "Highway camera", "lat": round(c["Latitude"], 5), "lng": round(c["Longitude"], 5), "views": views})
    return out


def _title(s):
    """'SHEPPARD AVE E' → 'Sheppard Ave E'."""
    return " ".join(w if len(w) == 1 else w.capitalize() for w in (s or "").split())


def build():
    st = Status()
    prev = load(OUT, {}) or {}
    out = {"generated": stamp()}
    out["stations"] = st.run("stations", fire_stations)
    t = st.run("ttc", ttc)
    if t:
        out["ttc"], out["subway"], out["bsched"] = t["routes"], t["subway"], t.get("bsched") or []
        st["bsched"] = f"live ({len(out['bsched'])} scheduled bus-speed stretches)"
        st["ttc"] = f"live ({len(t['routes'])} route-directions, {len(t['subway'])} subway stops)"
    go = st.run("go", go_trains)
    if go:
        out["go"] = go
        st["go"] = f"live ({len(go['shapes'])} lines, {sum(len(v) for v in go['trips'].values())} trips over {len(go['trips'])} days)"
        out["crossings"] = st.run("crossings", level_crossings, go)
    out["schools"] = st.run("schools", schools)
    out["towers"] = st.run("towers", towers)
    out["sites"] = st.run("sites", construction)
    out["worship"] = st.run("worship", worship)
    out["hoods"] = st.run("hoods", neighbourhoods)
    out["calendar"] = st.run("calendar", calendar)
    out["cameras"] = st.run("cameras", cameras)
    # anything that failed this week: keep last week's copy so the game never loses a layer
    for k in ("stations", "ttc", "subway", "bsched", "go", "crossings", "schools", "towers", "sites", "worship", "hoods", "calendar"):
        if not out.get(k) and prev.get(k):
            out[k] = prev[k]
            st[k] = st.get(k, "") + " (kept last good copy)"
    out["fsa"] = fsa_points(out.get("schools"), out.get("worship"))
    out["status"] = dict(st)
    size = save(OUT, out)
    print(OUT, size, "bytes")
    for k, v in st.items():
        print(f"  {k}: {v}")
