/**
 * Toronto Live Emergency Map — backend proxy.
 *
 * Runs server-side on Cloudflare's network, so it is NOT subject to the
 * browser CORS restrictions that block toronto.ca / arcgis.com when called
 * directly from a webpage. Fetches TPS "Calls for Service" + TFS "Active
 * Incidents" on a cron schedule, geocodes fire intersections (cached
 * forever in KV — intersections don't move), merges both into one JSON
 * blob, and serves it instantly to the frontend with CORS open.
 *
 * Routes:
 *   GET /api/incidents   -> merged live JSON (what the frontend polls)
 *   GET /api/refresh      -> force an immediate refresh (rate-limited)
 *
 * Cron: runs scheduled() every 5 minutes (matches TFS's own refresh rate).
 */

const TFS_URL = 'https://www.toronto.ca/wp-content/uploads/2017/11/9775-actiefireincidents.html';
const NOMINATIM_UA = 'toronto-live-emergency-map/1.0 (personal project; contact via GitHub)';
const KV_DATA_KEY = 'live_data';
const KV_REFRESH_LOCK = 'refresh_lock';
const MAX_GEOCODES_PER_RUN = 8; // stay well under subrequest limits + Nominatim's 1 req/sec policy

function corsHeaders() {
  return {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, OPTIONS',
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
  };
}

function classify(typeStr) {
  const t = (typeStr || '').toLowerCase();
  if (/shoot|firearm|gun|stab|homicide|hostage/.test(t)) return 'critical';
  if (/robbery|assault|weapon|break.?and.?enter|abduct/.test(t)) return 'high';
  if (/theft|fraud|disturb|mischief|b\s?&\s?e/.test(t)) return 'medium';
  return 'low';
}

function fireSeverity(alarmLevel) {
  const n = parseInt(alarmLevel, 10);
  if (isNaN(n)) return 'medium';
  if (n >= 2) return 'critical';
  if (n === 1) return 'high';
  return 'medium';
}


/* ─────────────────────────── TPS (police) ─────────────────────────── */
// TPS's own dedicated ArcGIS Server — this is what the public "Calls for
// Service" map actually queries (confirmed against a long-running community
// scraper: github.com/gnomon-/toronto-police-CAD). Far more reliable than
// resolving through an arcgis.com Online item, which is one extra hop that
// can silently change or rate-limit.
const C4S_URL = 'https://c4s.torontopolice.on.ca/arcgis/rest/services/CADPublic/C4S/MapServer/0/query';

async function fetchPolice() {
  const q =
    `${C4S_URL}?f=json&returnGeometry=true&spatialRel=esriSpatialRelIntersects` +
    `&where=1%3D1&outFields=ATSCENE_TS,DGROUP,TYP_ENG,XSTREETS,OBJECTID,Shape` +
    `&outSR=4326&resultRecordCount=500`;

  const res = await fetch(q, { headers: { 'User-Agent': NOMINATIM_UA, Accept: 'application/json' } });
  const json = await res.json();
  if (json.error) throw new Error('TPS C4S error: ' + JSON.stringify(json.error));
  if (!json.features) throw new Error('TPS C4S returned no features field');

  return json.features
    .map((f) => {
      const a = f.attributes || {};
      const g = f.geometry || {};
      const type = a.TYP_ENG || 'Call for Service';
      let ts = a.ATSCENE_TS ? new Date(a.ATSCENE_TS) : new Date();
      if (isNaN(ts)) ts = new Date();
      return {
        id: 'tps_' + a.OBJECTID,
        source: 'police',
        lat: g.y,
        lng: g.x,
        type,
        severity: classify(type),
        division: a.DGROUP || '',
        location: a.XSTREETS || 'Toronto',
        ts: ts.toISOString(),
      };
    })
    .filter((d) => d.lat && d.lng);
}

/* ─────────────────────────── TFS (fire) ─────────────────────────── */

// HTMLRewriter must run on a real Response stream.
async function parseFireHtml(html) {
  const rows = [];
  let currentRow = null;

  const rewriter = new HTMLRewriter()
    .on('tr', {
      element() {
        currentRow = [];
        rows.push(currentRow);
      },
    })
    .on('tr td', {
      element() {
        if (currentRow) currentRow.push('');
      },
      text(t) {
        if (currentRow && currentRow.length) {
          currentRow[currentRow.length - 1] += t.text;
        }
      },
    });

  const transformed = rewriter.transform(new Response(html, { headers: { 'Content-Type': 'text/html' } }));
  await transformed.text(); // drain to run handlers

  // Drop header / empty rows, keep rows with the expected 8 columns
  return rows
    .map((r) => r.map((c) => c.trim()).filter((c) => c.length || true))
    .filter((r) => r.length >= 7 && !/Prime\s*Street/i.test(r[0] || ''));
}

async function geocodeIntersection(env, prime, cross) {
  const key = `geo:${prime.toLowerCase()}|${cross.toLowerCase()}`;
  const cached = await env.EMERGENCY_KV.get(key);
  if (cached) return JSON.parse(cached);

  const pending = await env.EMERGENCY_KV.get('geo_budget_used');
  const used = parseInt(pending || '0', 10);
  if (used >= MAX_GEOCODES_PER_RUN) return null; // try again next cron run

  const q = encodeURIComponent(`${prime} & ${cross}, Toronto, Ontario, Canada`);
  const url = `https://nominatim.openstreetmap.org/search?format=json&limit=1&q=${q}`;
  const res = await fetch(url, { headers: { 'User-Agent': NOMINATIM_UA } });
  const arr = await res.json();
  await env.EMERGENCY_KV.put('geo_budget_used', String(used + 1), { expirationTtl: 300 });

  if (!arr.length) {
    await env.EMERGENCY_KV.put(key, JSON.stringify(null), { expirationTtl: 86400 }); // retry tomorrow
    return null;
  }
  const result = { lat: parseFloat(arr[0].lat), lng: parseFloat(arr[0].lon) };
  await env.EMERGENCY_KV.put(key, JSON.stringify(result)); // permanent — intersections don't move
  return result;
}

async function fetchFire(env) {
  const rows = await parseFireHtml(await (await fetch(TFS_URL)).text());
  const out = [];
  for (const r of rows) {
    const [prime, cross, dispatchTime, incidentNumber, incidentType, alarmLevel, area, units] = r;
    if (!prime || !cross) continue;
    const geo = await geocodeIntersection(env, prime, cross);
    if (!geo) continue; // not yet geocoded — will appear once cache fills in on a later run
    out.push({
      id: 'tfs_' + (incidentNumber || `${prime}_${cross}_${dispatchTime}`),
      source: 'fire',
      lat: geo.lat,
      lng: geo.lng,
      type: incidentType || 'Fire Dispatch',
      severity: fireSeverity(alarmLevel),
      division: area || '',
      location: `${prime} & ${cross}`,
      units: units || '',
      ts: new Date().toISOString(), // TFS table has no date, only time-of-day
    });
  }
  return out;
}

/* ─────────────────────────── Orchestration ─────────────────────────── */

async function refreshData(env) {
  const status = { police: 'error', fire: 'error' };
  let police = [];
  let fire = [];

  try {
    police = await fetchPolice();
    status.police = 'live';
  } catch (e) {
    status.police = 'error: ' + e.message;
  }

  try {
    fire = await fetchFire(env);
    status.fire = 'live';
  } catch (e) {
    status.fire = 'error: ' + e.message;
  }

  const payload = {
    generated: new Date().toISOString(),
    source_status: status,
    incidents: [...police, ...fire].sort((a, b) => new Date(b.ts) - new Date(a.ts)),
  };

  await env.EMERGENCY_KV.put(KV_DATA_KEY, JSON.stringify(payload));
  return payload;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: corsHeaders() });
    }

    if (url.pathname === '/api/incidents') {
      const cached = await env.EMERGENCY_KV.get(KV_DATA_KEY);
      if (cached) {
        return new Response(cached, { headers: corsHeaders() });
      }
      // nothing cached yet (first run) — fetch synchronously once
      const payload = await refreshData(env);
      return new Response(JSON.stringify(payload), { headers: corsHeaders() });
    }

    if (url.pathname === '/api/refresh') {
      const lock = await env.EMERGENCY_KV.get(KV_REFRESH_LOCK);
      if (lock) {
        return new Response(JSON.stringify({ ok: false, message: 'refreshed recently, try again shortly' }), {
          headers: corsHeaders(),
        });
      }
      await env.EMERGENCY_KV.put(KV_REFRESH_LOCK, '1', { expirationTtl: 30 });
      const payload = await refreshData(env);
      return new Response(JSON.stringify({ ok: true, payload }), { headers: corsHeaders() });
    }

    return new Response(JSON.stringify({ ok: true, routes: ['/api/incidents', '/api/refresh'] }), {
      headers: corsHeaders(),
    });
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(refreshData(env));
  },
};
