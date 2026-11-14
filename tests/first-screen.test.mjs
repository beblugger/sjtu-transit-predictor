import test, {afterEach} from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {loadFirstScreen} from '../deploy/first-screen.mjs';
import {eventKey} from '../deploy/core.mjs';

const hash = value => createHash('sha256').update(value).digest('hex');
const original = {fetch: globalThis.fetch, location: globalThis.location, initial: globalThis.__firstScreenData};
let fixtureCount = 0;
afterEach(() => {
  globalThis.fetch = original.fetch;
  globalThis.location = original.location;
  if (original.initial === undefined) delete globalThis.__firstScreenData;
  else globalThis.__firstScreenData = original.initial;
});

function fixture() {
  const start = Date.parse('2026-11-14T00:00:00Z');
  const instant = offset => new Date(start + offset).toISOString();
  const source = hash(`full scientific events ${++fixtureCount}`);
  const manifest = {schema_version: 1, generated_at: instant(0), from_time: instant(0), to_time: instant(3600000),
    catalog_sha256: hash('orbits'), event_count: 104, files: {'events.json': source}};
  const events = [1000, 2000, 3000].map((offset, index) => ({identifier: `90071992547409931234${index}`, target: 'sun', name: '示例卫星',
    minimum_time: instant(offset), start_time: instant(offset - 100), end_time: instant(offset + 100),
    observer: {latitude: 31.02807123456789, longitude: 121.42839123456789},
    weather: {decision: 'announced'}, altitude_deg: 15.0000001, duration_ms: 200, range_km: 500,
    visibility: {chart: {longitude: 121.42839123456789, latitude: 31.02807123456789,
      east_m_per_degree: 100000, north_m_per_degree: 110000}, domain_m: [-100, -100, 100, 100],
      reference_time: instant(offset), bands: [{bounds_m: [-100, -100, 100, 100], normal: [0, 1], offset_m: 0,
        half_width_m: 20, origin_m: [0, 0], time_offset_s: 0, time_gradient_s_per_m: [0.01, 0]}]}}));
  const campus = {type: 'FeatureCollection', features: [{type: 'Feature', properties: {},
    geometry: {type: 'Polygon', coordinates: [[[121, 31], [122, 31], [122, 32], [121, 31]]]}}]};
  const packets = [0, 1000].map((from, index) => ({format: 'first-screen-v1', source_sha256: source,
    valid_from: instant(from), valid_until: instant(index ? 2000 : 1000), default_event_count: 3 - index,
    manifest, payload: {generated_at: manifest.generated_at, catalog_sha256: manifest.catalog_sha256,
      event_count: 3 - index, events: events.slice(index)}, campus,
    weather: {deterministic: {hourly: {times: [instant(0)], cloud_cover_pct: [0], precipitation_mm: [0], weather_code: [0]}}}}));
  const bytes = packets.map(packet => Buffer.from(JSON.stringify(packet)));
  const index = {format: 'first-screen-index-v1', source_sha256: source, generated_at: manifest.generated_at,
    from_time: manifest.from_time, to_time: manifest.to_time,
    packets: packets.map((packet, i) => ({valid_from: packet.valid_from, valid_until: packet.valid_until,
      filename: `first-screen-${source.slice(0, 12)}-${String(i).padStart(5, '0')}.json`, sha256: hash(bytes[i]),
      count: packet.default_event_count}))};
  globalThis.location = new URL('https://example.test/site/');
  globalThis.__firstScreenData = {index, packet: packets[0]};
  return {start, index, packets, bytes};
}

test('embedded first screen needs no data requests and preserves full manifest and event identities', async () => {
  const {start, packets} = fixture();
  globalThis.fetch = () => { throw new Error('first screen must not request the complete snapshot'); };
  const result = await loadFirstScreen({now: start});
  assert.equal(result.manifest.event_count, 104);
  assert.equal(result.default_event_count, 3);
  assert.equal(result.payload.events.length, 3);
  assert.deepEqual(result.payload.events.map(eventKey), packets[0].payload.events.map(eventKey));
  assert.equal(result.payload.events[0].altitude_deg, 15.0000001);
  assert.equal(result.payload.events[0].visibility.geometry.type, 'MultiPolygon');
  assert.equal(packets[0].payload.events[0].visibility.geometry, undefined, 'decoding does not mutate the embedded source');
});

test('event expiry picks one verified small packet using an exact half-open interval', async () => {
  const {start, index, packets, bytes} = fixture();
  const requests = [];
  globalThis.fetch = async url => {
    requests.push(url);
    return new Response(bytes[1]);
  };
  const result = await loadFirstScreen({now: start + 1000});
  assert.equal(requests.length, 1);
  assert.equal(requests[0].pathname, '/site/data/' + index.packets[1].filename);
  assert.equal(requests[0].searchParams.get('v'), index.packets[1].sha256);
  assert.deepEqual(result.payload.events.map(eventKey), packets[1].payload.events.map(eventKey));
  assert.equal(result.default_event_count, 2);
  const again = await loadFirstScreen({now: start + 1000});
  again.payload.events[0].observer.latitude = 0;
  assert.notEqual((await loadFirstScreen({now: start + 1000})).payload.events[0].observer.latitude, 0);
  assert.equal(await loadFirstScreen({now: start + 2000}), null, 'no packet is reused after its validity ends');
  assert.equal(requests.length, 1);
});

test('missing inline data and shared event links leave the complete snapshot to the application', async () => {
  const {start} = fixture();
  globalThis.fetch = () => { throw new Error('unexpected request'); };
  globalThis.location.hash = new URLSearchParams({event: 'exact event key'}).toString();
  assert.equal(await loadFirstScreen({now: start}), null);
  globalThis.location.hash = '';
  delete globalThis.__firstScreenData;
  assert.equal(await loadFirstScreen({now: start}), null);
});

test('sharing a displayed first-page event does not force later small-packet reads into a complete download', async () => {
  const {start, packets, bytes} = fixture();
  await loadFirstScreen({now: start});
  globalThis.location.hash = new URLSearchParams({event: eventKey(packets[0].payload.events[1])}).toString();
  let requests = 0;
  globalThis.fetch = async () => { requests++; return new Response(bytes[1]); };
  const next = await loadFirstScreen({now: start + 1000});
  assert.equal(next.default_event_count, 2);
  assert.equal(requests, 1);
});

test('stale packet checksums, wrong scientific versions and malformed events cannot become a first screen', async () => {
  let data = fixture();
  globalThis.__firstScreenData.packet = null;
  globalThis.fetch = async () => new Response(Buffer.from('stale packet'));
  await assert.rejects(loadFirstScreen({now: data.start + 1000}), /校验不一致/);
  data = fixture();
  globalThis.__firstScreenData.packet.manifest.files['events.json'] = '0'.repeat(64);
  await assert.rejects(loadFirstScreen({now: data.start}), /格式或版本无效/);
  data = fixture();
  globalThis.__firstScreenData.packet.payload.events[0].minimum_time = 'invalid';
  await assert.rejects(loadFirstScreen({now: data.start}), /无效的关键字段/);
  data = fixture();
  globalThis.__firstScreenData.packet.default_event_count = 999;
  await assert.rejects(loadFirstScreen({now: data.start}), /格式或版本无效/);
});

test('refresh checks the current manifest and index and reads only the selected small packet', async () => {
  fixture();
  const {start, index, packets, bytes} = fixture();
  const current = structuredClone(globalThis.__firstScreenData);
  delete globalThis.__firstScreenData;
  const indexBytes = Buffer.from(JSON.stringify(index));
  const manifest = {...packets[0].manifest, files: {...packets[0].manifest.files, 'first-screen.json': hash(indexBytes)}};
  const requests = [];
  globalThis.fetch = async url => {
    requests.push(url.pathname);
    if (url.pathname.endsWith('/manifest.json')) return Response.json(manifest);
    if (url.pathname.endsWith('/first-screen.json')) return new Response(indexBytes);
    if (url.pathname.endsWith('/' + index.packets[1].filename)) return new Response(bytes[1]);
    throw new Error('unexpected full data request');
  };
  const result = await loadFirstScreen({refresh: true, now: start + 1000});
  assert.equal(result.default_event_count, 2);
  assert.deepEqual(requests, ['/site/data/manifest.json', '/site/data/first-screen.json', '/site/data/' + index.packets[1].filename]);
  assert.equal((await loadFirstScreen({now: start + 1000})).valid_until, current.index.packets[1].valid_until);
  assert.equal(requests.length, 3, 'later reads use the verified refreshed index and cached packet');
});

test('Python first-screen export is consumed unchanged through event expiry and the final empty page', async () => {
  const {start, packets} = fixture();
  const source = packets[0];
  const payload = {...source.payload, from_time: source.manifest.from_time, to_time: source.manifest.to_time,
    events: source.payload.events.map(event => ({...event, visibility: null}))};
  const files = JSON.parse(execFileSync('python3', ['-m', 'app.first_screen'], {
    input: JSON.stringify({payload, weather: source.weather, campus: source.campus, manifest: source.manifest}),
    encoding: 'utf8',
  }));
  const index = JSON.parse(files['first-screen.json']);
  globalThis.__firstScreenData = {index, packet: JSON.parse(files[index.packets[0].filename])};
  const requests = [];
  globalThis.fetch = async url => {
    requests.push(url.pathname);
    return new Response(files[url.pathname.split('/').at(-1)]);
  };
  assert.deepEqual((await loadFirstScreen({now: start})).payload.events.map(eventKey), payload.events.map(eventKey));
  assert.equal(requests.length, 0);
  assert.deepEqual((await loadFirstScreen({now: start + 1000})).payload.events.map(eventKey), payload.events.slice(1).map(eventKey));
  const empty = await loadFirstScreen({now: start + 3000});
  assert.deepEqual(empty.payload.events, []);
  assert.equal(empty.default_event_count, 0);
  assert.equal(empty.manifest.event_count, 104);
  assert.equal(requests.length, 2);
  assert.ok(requests.every(path => /\/first-screen-[a-f0-9]{12}-\d{5}\.json$/.test(path)));
});
