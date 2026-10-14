// GRIDLOCK bus relay (Cloudflare Worker): live TTC bus positions for Scarborough.
// The TTC feed can't be read by a web page directly, so this fetches it, keeps only Scarborough,
// and hands it to the game as small JSON. The TTC is asked at most once every 20 seconds,
// however many games are open.
const FEEDS = ['https://bustime.ttc.ca/gtfsrt/vehicles', 'https://gtfsrt.ttc.ca/vehicles/position?format=binary'];
const BOX = { s: 43.665, w: -79.345, n: 43.87, e: -79.10 };
const FRESH_MS = 20000;
let cached = null, cachedAt = 0, pending = null;

export default {
  async fetch() {
    if (!cached || Date.now() - cachedAt > FRESH_MS) {
      pending = pending || refresh().finally(() => { pending = null; });
      try { await pending; } catch (e) { if (!cached) return reply(JSON.stringify({ error: String(e.message || e) }), 502); }
    }
    return reply(cached, 200);
  },
};

function reply(body, status) {
  return new Response(body, { status, headers: { 'content-type': 'application/json', 'access-control-allow-origin': '*', 'cache-control': 'max-age=15' } });
}

async function refresh() {
  let last = null;
  for (const url of FEEDS) {
    try {
      const r = await fetch(url, { headers: { 'user-agent': 'gridlock-relay (personal project)' } });
      if (!r.ok) throw new Error(url + ' HTTP ' + r.status);
      cached = JSON.stringify(decode(new Uint8Array(await r.arrayBuffer())));
      cachedAt = Date.now();
      return;
    } catch (e) { last = e; }
  }
  throw last;
}

// GTFS-Realtime VehiclePositions → { t: feed time, v: [[vehicle, route, lat, lng, bearing, time], ...] }
function decode(b) {
  const msg = pb(b, 0, b.length), head = msg[1] ? pb(b, ...msg[1][0]) : {}, v = [];
  const str = r => new TextDecoder().decode(b.subarray(r[0], r[1]));
  for (const ent of msg[2] || []) {
    const e = pb(b, ...ent); if (!e[4]) continue;
    const vp = pb(b, ...e[4][0]); if (!vp[2]) continue;
    const pos = pb(b, ...vp[2][0]), lat = pos[1][0], lng = pos[2][0];
    if (lat < BOX.s || lat > BOX.n || lng < BOX.w || lng > BOX.e) continue;
    const trip = vp[1] ? pb(b, ...vp[1][0]) : {}, veh = vp[8] ? pb(b, ...vp[8][0]) : {};
    v.push([veh[1] ? str(veh[1][0]) : str(e[1][0]), trip[5] ? str(trip[5][0]) : '', +lat.toFixed(5), +lng.toFixed(5), pos[3] ? Math.round(pos[3][0]) : null, vp[5] ? vp[5][0] : 0]);
  }
  return { t: head[3] ? head[3][0] : 0, v };
}

// minimal protobuf reader: {field: [values]}; length-delimited values are [start, end] byte ranges
function pb(b, s, e) {
  const o = {}; let i = s;
  const vi = () => { let v = 0, m = 1, c; do { c = b[i++]; v += (c & 127) * m; m *= 128; } while (c & 128); return v; };
  while (i < e) {
    const k = vi(), f = Math.floor(k / 8), w = k & 7; let val;
    if (w === 0) val = vi();
    else if (w === 2) { const n = vi(); val = [i, i + n]; i += n; }
    else if (w === 5) { val = new DataView(b.buffer, b.byteOffset + i, 4).getFloat32(0, true); i += 4; }
    else if (w === 1) { val = 0; i += 8; }
    else throw new Error('protobuf wire type ' + w);
    (o[f] = o[f] || []).push(val);
  }
  return o;
}
