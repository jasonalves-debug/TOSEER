"""Shared helpers: HTTP, CKAN (Toronto open data), geometry, GTFS-RT protobuf decoding, status reporting."""

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DATA_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "docs", "data"))
UA = "gridlock-feed/2.0 (personal project; github.com/jasonalves-debug/TOSEER)"
CKAN = "https://ckan0.cf.opendata.inter.prod-toronto.ca"
TORONTO = ZoneInfo("America/Toronto")

# Scarborough, a little generous; the game crops to its own boundary
W, S, E, N = -79.345, 43.665, -79.10, 43.87
CENTER = (43.775, -79.23)
SCARB_FSA = ("M1B", "M1C", "M1E", "M1G", "M1H", "M1J", "M1K", "M1L", "M1M", "M1N", "M1P", "M1R", "M1S", "M1T", "M1V", "M1W", "M1X")


def inbox(lat, lng, pad=0.0):
    return lat is not None and lng is not None and S - pad <= lat <= N + pad and W - pad <= lng <= E + pad


def now_utc():
    return datetime.now(timezone.utc)


def stamp():
    return now_utc().isoformat(timespec="seconds")


def iso_to_epoch(s):
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


# ───────────────────────── HTTP ─────────────────────────
def get(url, params=None, timeout=60, raw=False, data=None, tries=2):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept": "application/json, */*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
            return body if raw else json.loads(body.decode("utf-8-sig"))
        except Exception as e:  # noqa: BLE001 — retried, then re-raised
            last = e
            time.sleep(2 + attempt * 3)
    raise last


# ───────────────────────── CKAN ─────────────────────────
def ckan_resources(slug):
    return get(f"{CKAN}/api/3/action/package_show", {"id": slug})["result"]["resources"]


def ckan_resource(slug, pred):
    for r in ckan_resources(slug):
        if pred(r):
            return r
    raise RuntimeError(f"{slug}: no matching resource")


def datastore_rows(resource_id, filters=None, page=32000, limit=None):
    """All rows of a CKAN datastore resource (server-side filters, paged)."""
    rows, offset = [], 0
    while True:
        params = {"resource_id": resource_id, "limit": page, "offset": offset}
        if filters:
            params["filters"] = json.dumps(filters)
        res = get(f"{CKAN}/api/3/action/datastore_search", params, timeout=180)["result"]
        batch = res["records"]
        rows += batch
        offset += len(batch)
        if not batch or offset >= res.get("total", 0) or (limit and offset >= limit):
            return rows


def datastore(slug, filters=None, pred=None):
    r = ckan_resource(slug, pred or (lambda r: r.get("datastore_active") in (True, "True")))
    return datastore_rows(r["id"], filters)


def point_of(row):
    """(lat, lng) from a CKAN row with a GeoJSON 'geometry' string or LATITUDE/LONGITUDE columns."""
    g = row.get("geometry")
    if g:
        g = json.loads(g) if isinstance(g, str) else g
        c = g.get("coordinates")
        if g.get("type") == "MultiPoint":
            c = c[0]
        if c:
            return float(c[1]), float(c[0])
    try:
        return float(row["LATITUDE"]), float(row["LONGITUDE"])
    except (KeyError, TypeError, ValueError):
        return None


# ───────────────────────── files ─────────────────────────
def path(name):
    return os.path.join(DATA_DIR, name)


def load(name, default=None):
    try:
        with open(path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(name, obj):
    with open(path(name), "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
    return os.path.getsize(path(name))


# ───────────────────────── status ─────────────────────────
class Status(dict):
    """Per-source status strings shown in the game's Live data panel ("live (42)" / "down: …")."""

    def run(self, name, fn, *args):
        try:
            out = fn(*args)
            n = len(out) if hasattr(out, "__len__") and not isinstance(out, dict) else None
            self.setdefault(name, f"live ({n})" if n is not None else "live")
            return out
        except Exception as e:  # noqa: BLE001 — one source failing must not stop the rest
            self[name] = f"down: {type(e).__name__}: {e}"[:300]
            return None


# ───────────────────────── geometry ─────────────────────────
def simplify(pts, tol=0.00012):
    """Douglas–Peucker on [lng, lat] points (tol in degrees, ~10 m)."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = pts[a], pts[b]
        dx, dy = bx - ax, by - ay
        L = (dx * dx + dy * dy) or 1e-12
        best, bi = 0.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
            d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d > best:
                best, bi = d, i
        if bi >= 0 and best > tol * tol:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(pts, keep) if k]


def clip_line(pts, pad=0.0):
    """Longest run of [lng, lat] points inside the box (with one point of overhang each side)."""
    best, cur = [], []
    for i, (x, y) in enumerate(pts):
        if inbox(y, x, pad):
            if not cur and i:
                cur.append(pts[i - 1])
            cur.append([x, y])
        elif cur:
            cur.append([x, y])
            best, cur = (cur if len(cur) > len(best) else best), []
    return cur if len(cur) > len(best) else best


def decode_polyline(s, precision=5):
    """Google encoded polyline → [[lng, lat], ...]."""
    pts, i, lat, lng, f = [], 0, 0, 0, 10 ** precision
    while i < len(s):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            d = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += d
            else:
                lng += d
        pts.append([lng / f, lat / f])
    return pts


def metres(lat1, lng1, lat2, lng2):
    kx = 111320.0 * 0.7217  # cos(43.77°)
    return ((lng2 - lng1) * kx) ** 2 + ((lat2 - lat1) * 111320.0) ** 2


# ───────────────────────── protobuf (just enough for GTFS-Realtime) ─────────────────────────
def _varint(b, i):
    shift = v = 0
    while True:
        c = b[i]
        i += 1
        v |= (c & 0x7F) << shift
        if c < 0x80:
            return v, i
        shift += 7


def pb(buf):
    """Decode one protobuf message into {field_number: [values]} (varints as int, length-delimited as bytes)."""
    out, i = {}, 0
    while i < len(buf):
        key, i = _varint(buf, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(buf, i)
        elif wt == 2:
            n, i = _varint(buf, i)
            v, i = buf[i:i + n], i + n
        elif wt == 1:
            v, i = buf[i:i + 8], i + 8
        elif wt == 5:
            v, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f"unsupported wire type {wt}")
        out.setdefault(f, []).append(v)
    return out


def pb_text(translated):
    """GTFS-RT TranslatedString → English text."""
    best = ""
    for t in pb(translated).get(1, []):
        tr = pb(t)
        text = tr.get(1, [b""])[0].decode("utf-8", "replace")
        lang = tr.get(2, [b"en"])[0].decode()
        if lang.startswith("en") or not best:
            best = text
    return best
