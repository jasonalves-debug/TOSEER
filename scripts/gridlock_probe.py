"""Temporary: records schemas/samples of candidate GRIDLOCK sources. Deleted after use."""
import io, json, os, urllib.parse, urllib.request, zipfile, csv, collections
CKAN = "https://ckan0.cf.opendata.inter.prod-toronto.ca"
UA = "gridlock-feed/1.0 (github.com/jasonalves-debug/TOSEER)"
out = {}
def get(url, params=None, timeout=90, raw=False):
    if params: url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout) as r: b = r.read()
    return b if raw else json.loads(b)
def res_url(slug, pred):
    for r in get(f"{CKAN}/api/3/action/package_show", {"id": slug})["result"]["resources"]:
        if pred(r): return r
def safe(name, f):
    try: out[name] = f()
    except Exception as e: out[name] = "error: " + repr(e)[:300]
def p311():
    r = res_url("311-service-requests-customer-initiated", lambda r: r["name"].endswith("2026"))
    z = zipfile.ZipFile(io.BytesIO(get(r["url"], raw=True, timeout=180)))
    name = z.namelist()[0]; rows = csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8-sig", errors="replace"))
    cols = rows.fieldnames; c = collections.Counter(); st = collections.Counter(); sample = []; last = ""
    for row in rows:
        fsa = (row.get("First 3 Chars of Postal Code") or "")
        if not fsa.startswith("M1"): continue
        c[row.get("Service Request Type")] += 1; st[row.get("Status")] += 1; last = max(last, row.get("Creation Date") or "")
        if len(sample) < 6 and row.get("Intersection Street 1"): sample.append(row)
    return {"file": name, "cols": cols, "types": c.most_common(90), "status": st.most_common(10), "last": last, "sample": sample}
def detour():
    d = json.loads(get("https://gtfsrt.ttc.ca/trips/detour", {"format": "text"}, raw=True))
    ents = d.get("entity", [])
    o = {"n": len(ents), "keys": sorted({k for e in ents for k in e.keys()})}
    for e in ents[:2]:
        e2 = json.loads(json.dumps(e))
        if "tripModifications" in e2: e2["tripModifications"]["selectedTrips"] = [{"tripIds": s.get("tripIds", [])[:2], **{k: v for k, v in s.items() if k != "tripIds"}} for s in e2["tripModifications"].get("selectedTrips", [])[:2]]
        o.setdefault("samples", []).append(json.dumps(e2)[:2500])
    shapes = [e for e in ents if "shape" in e][:1]
    o["shape_sample"] = json.dumps(shapes)[:800]
    o["ids"] = [e.get("id") for e in ents][:60]
    return o
def schools():
    r = res_url("school-locations-all-types", lambda r: r.get("datastore_active"))
    d = get(f"{CKAN}/api/3/action/datastore_search", {"resource_id": r["id"], "limit": 2000})["result"]["records"]
    sc = [x for x in d if (x.get("POSTAL_CODE") or "").startswith("M1")]
    return {"n_scarb": len(sc), "levels": collections.Counter(x.get("SCHOOL_LEVEL") for x in sc).most_common(), "types": collections.Counter(x.get("SCHOOL_TYPE_DESC") for x in sc).most_common(), "boards": collections.Counter(x.get("BOARD_NAME") for x in sc).most_common()}
def gogtfs():
    z = zipfile.ZipFile(io.BytesIO(get("https://assets.metrolinx.com/raw/upload/Documents/Metrolinx/Open%20Data/GO-GTFS.zip", timeout=180, raw=True)))
    rd = lambda n: csv.DictReader(io.TextIOWrapper(z.open(n), encoding="utf-8-sig"))
    trips = [t for t in rd("trips.txt") if t["route_id"].endswith(("-ST", "-LE"))]
    cd = list(rd("calendar_dates.txt"))
    return {"trip_cols": list(trips[0].keys()), "trip_sample": trips[:2], "n_trips": len(trips), "cd_sample": cd[:3], "cd_dates": sorted({c["date"] for c in cd})[:3] + sorted({c["date"] for c in cd})[-3:],
            "st_head": next(rd("stop_times.txt")), "shape_head": next(rd("shapes.txt")), "shapes_for": collections.Counter(t["shape_id"] for t in trips).most_common(6)}
def profiles():
    r = res_url("neighbourhood-profiles", lambda r: r["format"].upper() == "XLSX" and "2021" in r["name"])
    z = zipfile.ZipFile(io.BytesIO(get(r["url"], raw=True))); return {"files": z.namelist()[:20], "size": r.get("size")}
safe("s311", p311); safe("detour", detour); safe("schools", schools); safe("go", gogtfs); safe("profiles", profiles)
json.dump(out, open(os.path.join(os.path.dirname(__file__), "..", "docs", "data", "gridlock_probe.json"), "w"), indent=1)
