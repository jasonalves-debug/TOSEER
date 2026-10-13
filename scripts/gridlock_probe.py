"""Temporary: records schemas/samples of candidate GRIDLOCK sources. Deleted after use."""
import io, json, os, urllib.parse, urllib.request, zipfile, csv
CKAN = "https://ckan0.cf.opendata.inter.prod-toronto.ca"
UA = "gridlock-feed/1.0 (github.com/jasonalves-debug/TOSEER)"
out = {}
def get(url, params=None, timeout=60, raw=False, data=None):
    if params: url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA}, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r: b = r.read()
    return b if raw else json.loads(b)
def probe_ckan(slug):
    o = {}
    try:
        res = get(f"{CKAN}/api/3/action/package_show", {"id": slug})["result"]["resources"]
        o["resources"] = [{k: r.get(k) for k in ("id", "name", "format", "datastore_active", "size", "last_modified")} for r in res]
        ds = [r for r in res if r.get("datastore_active")]
        if ds:
            d = get(f"{CKAN}/api/3/action/datastore_search", {"resource_id": ds[0]["id"], "limit": 2})["result"]
            o["fields"] = [f["id"] for f in d["fields"]]; o["total"] = d.get("total"); o["sample"] = [{k: str(v)[:160] for k, v in rec.items()} for rec in d["records"]]
    except Exception as e:
        o["error"] = str(e)[:300]
    return o
for slug in ["school-locations-all-types", "311-service-requests-customer-initiated", "apartment-building-registration", "apartment-building-evaluation",
             "building-permits-active-permits", "development-applications", "utility-cut-permits", "places-of-worship", "neighbourhood-profiles",
             "neighbourhoods", "address-points-municipal-toronto-one-address-repository", "traffic-signals-tabular", "school-zones"]:
    out[slug] = probe_ckan(slug)
# SQL support (311 Scarborough, recent)
try:
    rid = next(r["id"] for r in out["311-service-requests-customer-initiated"]["resources"] if r.get("datastore_active"))
    q = f'SELECT "Service Request Type", COUNT(*) AS n FROM "{rid}" WHERE "First 3 Chars of Postal Code" LIKE \'M1%\' GROUP BY 1 ORDER BY n DESC LIMIT 60'
    out["sql_311_types"] = get(f"{CKAN}/api/3/action/datastore_search_sql", {"sql": q})["result"]["records"]
except Exception as e:
    out["sql_311_types"] = "error: " + str(e)[:300]
# TTC GTFS-RT
for path in ["alerts/all", "alerts/subway", "trips/detour"]:
    try:
        t = get("https://gtfsrt.ttc.ca/" + path, {"format": "text"}, raw=True).decode("utf-8", "replace")
        out["ttc_" + path] = {"len": len(t), "head": t[:2500]}
    except Exception as e:
        out["ttc_" + path] = "error: " + str(e)[:200]
try:
    b = get("https://gtfsrt.ttc.ca/alerts/all", {"format": "binary"}, raw=True); out["ttc_alerts_binary_len"] = len(b)
except Exception as e:
    out["ttc_alerts_binary_len"] = "error: " + str(e)[:200]
# GO GTFS
try:
    z = zipfile.ZipFile(io.BytesIO(get("https://assets.metrolinx.com/raw/upload/Documents/Metrolinx/Open%20Data/GO-GTFS.zip", timeout=180, raw=True)))
    o = {"files": z.namelist()}
    rows = list(csv.DictReader(io.TextIOWrapper(z.open("routes.txt"), encoding="utf-8-sig")))
    o["routes"] = [{k: r.get(k) for k in ("route_id", "route_short_name", "route_long_name", "route_type")} for r in rows if r.get("route_type") == "2"][:20]
    st = list(csv.DictReader(io.TextIOWrapper(z.open("stops.txt"), encoding="utf-8-sig")))
    o["stops_sample"] = [s for s in st if any(k in (s.get("stop_name") or "") for k in ("Kennedy", "Agincourt", "Milliken", "Scarborough", "Guildwood", "Eglinton", "Rouge"))][:20]
    o["has_shapes"] = "shapes.txt" in z.namelist()
    out["go_gtfs"] = o
except Exception as e:
    out["go_gtfs"] = "error: " + str(e)[:300]
# Overpass level crossings
try:
    q = '[out:json][timeout:60];(node["railway"="level_crossing"](43.67,-79.345,43.87,-79.10);node["railway"="crossing"](43.67,-79.345,43.87,-79.10););out body;'
    d = get("https://overpass-api.de/api/interpreter", data=urllib.parse.urlencode({"data": q}).encode(), timeout=90)
    out["overpass_crossings"] = {"n": len(d.get("elements", [])), "sample": d.get("elements", [])[:12]}
except Exception as e:
    out["overpass_crossings"] = "error: " + str(e)[:300]
try:
    out["holidays"] = get("https://canada-holidays.ca/api/v1/provinces/ON", {"year": 2026})["province"]["holidays"][:20]
except Exception as e:
    out["holidays"] = "error: " + str(e)[:200]
json.dump(out, open(os.path.join(os.path.dirname(__file__), "..", "docs", "data", "gridlock_probe.json"), "w"), indent=1)
print("probe done")
