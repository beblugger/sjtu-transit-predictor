import test from 'node:test';
import assert from 'node:assert/strict';
import {gzipSync} from 'node:zlib';
import {createHash} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {checkedEvents, decodeColumnSnapshot} from '../deploy/data.mjs';
import {inflateBands} from '../deploy/bands.mjs';
import {regionContains, visibilityAt} from '../deploy/core.mjs';

const json = Buffer.from(JSON.stringify({events: [{name: '卫星'}]}));
const compressed = gzipSync(json);
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const manifest = {files: {'events.json': hash(json), 'events.json.gz': hash(compressed)}};
globalThis.location = new URL('https://example.test/site/');

test('events use verified gzip and fall back for older snapshots and browsers', async () => {
  const requests = [];
  globalThis.fetch = async url => {
    requests.push(url.pathname);
    return new Response(url.pathname.endsWith('.gz') ? compressed : json);
  };
  assert.deepEqual(await checkedEvents(manifest), {events: [{name: '卫星'}]});
  assert.deepEqual(requests.splice(0), ['/site/data/events.json.gz']);
  assert.deepEqual(await checkedEvents({files: {'events.json': hash(json)}}), {events: [{name: '卫星'}]});
  assert.deepEqual(requests.splice(0), ['/site/data/events.json']);
  const saved = globalThis.DecompressionStream;
  try {
    globalThis.DecompressionStream = undefined;
    assert.deepEqual(await checkedEvents(manifest), {events: [{name: '卫星'}]});
    assert.deepEqual(requests, ['/site/data/events.json']);
  } finally { globalThis.DecompressionStream = saved; }
});

test('compressed and uncompressed checksums must both match', async () => {
  globalThis.fetch = async () => new Response(compressed);
  await assert.rejects(checkedEvents({files: {...manifest.files, 'events.json.gz': '0'.repeat(64)}}), /校验不一致/);
  await assert.rejects(checkedEvents({files: {...manifest.files, 'events.json': '0'.repeat(64)}}), /校验不一致/);
});

test('early events are verified, and a failed early request is retried', async () => {
  const columnar = execFileSync('python3', ['-m', 'app.event_columns'], {input: json});
  const columnsManifest = {files: {...manifest.files, 'events.columns.json.gz': hash(columnar)}};
  let requests = 0;
  globalThis.fetch = async () => { requests++; return new Response(columnar); };
  assert.deepEqual(await checkedEvents(columnsManifest, Promise.resolve(columnar)), {events: [{name: '卫星'}]});
  assert.equal(requests, 0);
  await assert.rejects(checkedEvents({files: {...columnsManifest.files, 'events.columns.json.gz': '0'.repeat(64)}}, Promise.resolve(columnar)), /校验不一致/);
  assert.deepEqual(await checkedEvents(columnsManifest, Promise.resolve(null)), {events: [{name: '卫星'}]});
  assert.equal(requests, 1);
  assert.deepEqual(await checkedEvents(columnsManifest, Promise.reject(new Error('network failed'))), {events: [{name: '卫星'}]});
  assert.equal(requests, 2);
});

test('column encoding round trips nested values and does not share dictionary objects', async () => {
  const rows = Array.from({length: 100}, (_, i) => ({
    name: i % 2 ? '卫星' : 'ISS', value: i ? i / 7 : 0,
    nested: i % 5 ? {position: [121.3853762025436, 31.009198705811666], extra: null} : null,
    varying: i % 2 ? {first: 0} : {second: [1, 2]},
  }));
  for (const events of [rows, []]) {
    const payload = {catalog_sha256: 'abc', events};
    const bytes = execFileSync('python3', ['-m', 'app.event_columns'], {input: JSON.stringify(payload)});
    globalThis.fetch = async () => new Response(bytes);
    const result = await checkedEvents({files: {'events.columns.json.gz': hash(bytes)}});
    assert.deepEqual(result, payload);
    if (events.length) {
      result.events[1].nested.position[0] = 0;
      assert.equal(result.events[2].nested.position[0], 121.3853762025436);
    }
  }
});

test('malformed column lengths and dictionary indexes are rejected', () => {
  const document = {format: 'event-columns-v1', metadata: {}, count: 2};
  assert.throws(() => decodeColumnSnapshot({...document, events: {v: [1]}}), /格式无效/);
  assert.throws(() => decodeColumnSnapshot({...document, events: {d: ['x'], i: [0, 1]}}), /格式无效/);
  assert.throws(() => decodeColumnSnapshot({...document, events: {o: {x: {v: [1, 2]}}, p: [true]}}), /格式无效/);
  for (const events of [[null, {}], [[], {}], [1, {}]]) {
    assert.throws(() => decodeColumnSnapshot({...document, events: {v: events}}), /格式无效/);
  }
});

test('legacy column snapshots retain full geometry and v2 numeric offsets remain readable', () => {
  const event = {identifier: '0001', visibility: {geometry: {type: 'MultiPolygon', coordinates: []}}};
  assert.deepEqual(decodeColumnSnapshot({format: 'event-columns-v1', metadata: {version: 1}, count: 1, events: {v: [event]}}),
    {version: 1, events: [event]});
  const result = decodeColumnSnapshot({format: 'event-columns-v2', metadata: {}, count: 1, events: {v: [{
    identifier: 123456, target_azimuth_deg: 3599, satellite_azimuth_offset: 3,
    target_altitude_deg: 150, satellite_altitude_offset: -1,
  }]}}).events[0];
  assert.deepEqual(result, {identifier: '123456', target_azimuth_deg: 359.9, azimuth_deg: 0.2,
    target_altitude_deg: 15, altitude_deg: 14.9});
  assert.throws(() => decodeColumnSnapshot({format: 'event-columns-v2', metadata: {}, count: 1,
    events: {v: [{identifier: Number.MAX_SAFE_INTEGER + 1}]}}), /格式无效/);
});

test('Python compact bands preserve identity, horizon clipping, distances and local times in JavaScript', async () => {
  const fixture = JSON.parse(execFileSync('python3', ['-c', `
import base64, copy, json, math
from shapely.geometry import box, mapping, Polygon
from shapely.ops import transform
from app.event_columns import encode_snapshot

chart = {'longitude': 121.42839123456789, 'latitude': 31.02807123456789,
         'east_m_per_degree': 100000.123456789, 'north_m_per_degree': 110000.987654321,
         'observer_elevation_m': 12.3456789}
def geo(x, y, z=None):
    return (chart['longitude'] + x / chart['east_m_per_degree'],
            chart['latitude'] + y / chart['north_m_per_degree'])
region = Polygon([(-100, -20), (100, -20), (60, 20), (-100, 20)])
reference = '2026-11-14T00:00:00.123456+00:00'
models = [{'bounds_m': [left, -100, right, 100], 'origin_m': [0, 0],
           'time_offset_s': 0.123456789, 'time_gradient_s_per_m': [0.0123456789, 0.0023456789],
           'cross_track_at_origin_rad': 0, 'cross_track_gradient_rad_per_m': [0, 0.0001],
           'contact_radius_rad': 0.002} for left, right in [(-100, 0), (0, 100)]]
visibility = {'chart': chart, 'geometry': mapping(transform(geo, region)),
              'calculation_domain': mapping(transform(geo, box(-100, -100, 100, 100))),
              'reference_time': reference, 'models': models, 'area_m2': region.area,
              'campus_area_m2': 1234.56789, 'sampled_boundary_tolerance_m': 5,
              'maximum_sampled_boundary_error_m': 0.123456789,
              'maximum_sampled_time_error_s': 0.00123456789,
              'contact_validation_count': 8, 'orbital_uncertainty_m': None}
event = {'identifier': '900719925474099312345', 'minimum_time': reference,
         'start_time': '2026-11-14T00:00:00.023456+00:00',
         'end_time': '2026-11-14T00:00:00.223456+00:00',
         'observer': {'latitude': 31.02807123456789, 'longitude': 121.42839123456789, 'elevation_m': 12.3456789},
         'altitude_deg': 14.9999999, 'azimuth_deg': 359.9999999,
         'target_altitude_deg': 15.0000001, 'target_azimuth_deg': 0.0000001,
         'weather': {'score': 0.123456789, 'threshold': 0.123456788, 'decision': 'clear'},
         'visibility': visibility}
fallback = copy.deepcopy(event)
fallback['identifier'] = '0001'
fallback['visibility']['calculation_domain'] = mapping(transform(geo, Polygon([(-100, -100), (100, -100), (0, 100)])))
payload = {'catalog_sha256': 'abc', 'events': [event, fallback]}
print(json.dumps({'payload': payload, 'gzip': base64.b64encode(encode_snapshot(payload)).decode()}))
  `], {encoding: 'utf8'}));
  const bytes = Buffer.from(fixture.gzip, 'base64');
  globalThis.fetch = async () => new Response(bytes);
  const result = await checkedEvents({files: {'events.columns.json.gz': hash(bytes)}});
  const source = fixture.payload.events[0], decoded = result.events[0];
  const {visibility: originalVisibility, ...originalFields} = source;
  const {visibility, ...fields} = decoded;
  assert.deepEqual(fields, originalFields);
  assert.deepEqual(visibility.chart, originalVisibility.chart);
  assert.equal(visibility.reference_time, originalVisibility.reference_time);
  assert.ok(visibility.bands.some(band => band.clip), 'the horizon is encoded as a clip');
  assert.equal(visibility.campus_area_m2, originalVisibility.campus_area_m2);
  const chart = visibility.chart;
  const geo = (x, y) => [chart.longitude + x / chart.east_m_per_degree, chart.latitude + y / chart.north_m_per_degree];
  for (const [x, y] of [[-75, 0], [-25, 10], [0, 0], [25, -10], [75, 0], [95, 10], [0, 40], [-100, 0]]) {
    const point = geo(x, y), location = {longitude: point[0], latitude: point[1]};
    assert.equal(regionContains(visibility.geometry, point), regionContains(originalVisibility.geometry, point));
    const expected = visibilityAt(source, location), actual = visibilityAt(decoded, location);
    assert.equal(actual.inside, expected.inside);
    assert.equal(actual.minimum_time, expected.minimum_time);
    assert.ok(Math.abs(actual.boundary_distance_m - expected.boundary_distance_m) < 1e-6,
      `boundary distance differs at ${x}, ${y}: ${actual.boundary_distance_m} vs ${expected.boundary_distance_m}`);
  }
  for (const line of visibility.boundaries.coordinates) for (const point of line) {
    const y = (point[1] - chart.latitude) * chart.north_m_per_degree;
    assert.ok(Math.abs(Math.abs(y) - 20) < 1e-6, 'horizon and tile edges are not contact boundaries');
  }
  assert.deepEqual(result.events[1], fixture.payload.events[1], 'unsupported domains keep the complete visibility');
});

test('malformed compact visibility fails before producing nonfinite geometry', () => {
  const visibility = {chart: {longitude: 0, latitude: 0, east_m_per_degree: 100000, north_m_per_degree: 100000},
    domain_m: [-100, -100, 100, 100], bands: [{bounds_m: [-100, -100, 100, 100], normal: [0, 1],
      offset_m: 0, half_width_m: 20, origin_m: [0, 0], time_offset_s: 0, time_gradient_s_per_m: [0.01, 0]}]};
  for (const alter of [v => {v.chart.east_m_per_degree = 0;}, v => {v.domain_m = [0, 0];},
    v => {v.bands[0].normal = [0, 0];}, v => {v.bands[0].clip = [1, 0, null];}]) {
    const value = structuredClone(visibility);
    alter(value);
    assert.throws(() => inflateBands({visibility: value}), /格式无效/);
    assert.equal(value.geometry, undefined);
  }
});
