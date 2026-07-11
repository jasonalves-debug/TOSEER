# Live Incident Map — Architecture Brief

I'm running a live crime map in this project using a specific architecture I want replicated here, replacing whatever "gtaupdate" currently does. Read this, then compare it against gtaupdate's actual approach and tell me the concrete diffs before changing anything.

## Core principle: Backend-First / Dumb Frontend
All intelligence is computed in Python. The frontend only renders pre-computed fields — no client-side geocoding, clustering, or anomaly logic. If gtaupdate does any of that in JS, that's the first thing to fix.

## Pipeline shape (three components)
1. **`update_data.py`** — stdlib-only Python. Fetches from source APIs, geocodes, computes relationships, writes a single JSON file. Run on a schedule (cron / Task Scheduler / GitHub Actions), not triggered by the frontend.
2. **`serve.py`** — trivial static file server, or GitHub Pages if deploying that way.
3. **`index.html`** — single self-contained file, no build step, no CDN dependency (inline any libraries like MapLibre GL — CDN blocking is a real failure mode).

## Specific patterns worth stealing

**24-hour rolling retention via merge-and-dedup**: each run merges fresh fetches into what's already on disk (fresh overwrites stale duplicates by ID), then drops anything past the retention window. This is necessary because upstream APIs often only expose a narrow "currently active" window, not true history — the pipeline itself is what remembers.

**Stable content-hash IDs**: incidents get an ID derived from content (not an upstream row number), so re-fetches of the same event dedupe correctly across runs.

**Geographic bbox validation on data source**: whatever API/endpoint gtaupdate hits, verify returned points actually fall inside Toronto's bounding box before trusting the source. Wrong-endpoint bugs (pointing at a different city or a decommissioned feed) are silent and nasty otherwise.

**Geocoding with a budget + fallback ladder**: cap new geocode calls per run (respect Nominatim's 1 req/sec policy if using it), cache aggressively, and when a full intersection match fails, retry with just the primary street name before giving up — mark the result `is_approximate: true` so the frontend can render it differently (soft circle vs. precise pin).

**Relationship intelligence**: pre-compute nearby-incident pairs (e.g. within 600m / 3hrs) server-side, output as a `related` array per incident, so the frontend just draws lines — no client-side spatial math.

**Anomaly detection against historical baseline**: keep a rolling per-source/per-division/per-hour-bucket history file, flag current activity that's a meaningful multiple above baseline.

**source_status accuracy**: never mark a source `"live"` just because the fetch didn't throw — a source that returns 0 usable results due to a real failure (not a genuinely quiet period) should report `"error: ..."`. Distinguish "genuinely zero events" from "fetch succeeded but geocoding/parsing silently dropped everything."

## Process lessons (the expensive ones)
- **One source of truth for paths.** A relative-path bug (`DATA_DIR` built with an extra `..`) caused hours of "data updates but map doesn't change" confusion. Local dev server root and deployed (GitHub Pages `/docs`) root need to agree on one path convention, or you maintain two configs deliberately.
- **Don't guess at intermittent failures — add one diagnostic line, run it, look at the actual output before changing code.** Several bugs here (fire silently marked "live" with 0 results, geocode misses) were fixed by adding a single print statement first, not by theorizing.
- **Feature discipline**: features that were built then deliberately removed for being fragile/low-value: social feed integration, traffic camera feeds, audio feed integration. Resist scope creep from "interesting but off-thesis" data sources.

## Data sources (Toronto)

**Police — Toronto Police Service (TPS) "Calls for Service"**
Served via ArcGIS Online FeatureServer, not a documented public API — the item ID was found by digging through TPS's Experience Builder app config (regex-scanning the app's JS for service URLs), since there's no clean published endpoint list. Known-good endpoint as of this build:
```
https://services.arcgis.com/S9th0jAJ7bqgIRjw/arcgis/rest/services/C4S_Public_NoGO/FeatureServer/0
```
Query it like any ArcGIS REST FeatureServer (`/query?where=1=1&outFields=*&f=json`, etc.). This can silently change — TPS could redeploy an updated Experience Builder app with a different item ID. That's why the bbox-validation step in the main brief exists: confirm returned lat/lng actually fall inside Toronto before trusting the response.

Fields returned: `OBJECTID, OCCURRENCE_TIME, DIVISION, LATITUDE, LONGITUDE, CALL_TYPE_CODE, CALL_TYPE, CROSS_STREETS, OCCURRENCE_TIME_AGOL`.

**Fire — Toronto Fire Services (TFS)**
Official public XML feed, much more reliable than the police source since it's a documented endpoint rather than reverse-engineered:
```
https://www.toronto.ca/data/fire/livecad.xml
```
This feed does NOT include lat/lng — only text fields like `prime_street` and `cross_streets` (sometimes an FSA/postal code instead of a street, for privacy-redacted medical calls). Requires geocoding as a separate step (Nominatim was used here) to get coordinates.

**Geocoding**
Nominatim (OpenStreetMap) public instance — free, but rate-limited to ~1 req/sec and coverage of Canadian postal codes is documented as patchy. That's the origin of the geocode-budget-per-run and fallback-ladder patterns above.

If gtaupdate is hitting a different city's police/fire data, the TPS/TFS specifics above obviously don't transfer — but the *method* of finding a police feed (checking if the department publishes an ArcGIS Open Data / Experience Builder app and extracting the service URL) and validating it (bbox check) generalizes to most North American municipal police departments.
None of this implies gtaupdate's actual data source, city, or domain is wrong — this brief is about the *pipeline shape*, not the specific APIs. Tell me what gtaupdate does differently before assuming it should be replaced wholesale.
