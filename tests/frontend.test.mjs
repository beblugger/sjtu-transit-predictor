import test from 'node:test';
import assert from 'node:assert/strict';
import {filterEvents, formatTime, inputInstant, inputTime, distanceMeters, number, eventsCSV, validateSnapshot} from '../deploy/core.mjs';
const start = Date.parse('2026-11-04T00:00:00Z');
const event = (id, time, diameter = null, altitude = 30) => ({identifier: String(id), name: `Satellite ${id}`, target: 'moon',
  start_time: time, minimum_time: time, end_time: time, duration_ms: 300, altitude_deg: altitude,
  azimuth_deg: 120, range_km: 500, object_angular_diameter_arcsec: diameter, physical_size_m: null,
  observer: {latitude: 31.028, longitude: 121.428, elevation_m: 0}, orbit_epoch: '2026-11-03T00:00:00Z',
  orbit_age_at_event_hours: 24, weather: {decision: 'unavailable', score: 0}, reason_codes: []});
const filters = {from: start, to: start + 86400000, target: 'all', search: '', minAltitude: 15,
  minDiameter: 0, keepUnknown: true, weather: 'all', sort: 'time'};

test('UTC and Beijing conversion retains the same instant, including day rollover', () => {
  const time = '2026-11-04T20:12:33.123Z';
  assert.equal(formatTime(time, 'Asia/Shanghai', true), '2026-11-05 04:12:33.123');
  for (const zone of ['UTC', 'Asia/Shanghai']) assert.equal(inputInstant(inputTime(time, zone), zone), Date.parse(time) - 123);
  assert.ok(Number.isNaN(inputInstant('', 'UTC')));
});
test('time range is half open and old orbital epochs are retained', () => {
  const events = [event(1, '2026-11-04T00:00:00Z'), event(2, '2026-11-05T00:00:00Z')];
  assert.deepEqual(filterEvents(events, filters).map(e => e.identifier), ['1']);
});
test('unknown size is distinct from zero and does not disappear unless requested', () => {
  const events = [event(1, '2026-11-04T01:00:00Z'), event(2, '2026-11-04T02:00:00Z', 0), event(3, '2026-11-04T03:00:00Z', 5)];
  assert.equal(number(null), '未知'); assert.equal(number(0), '0.00');
  assert.deepEqual(filterEvents(events, {...filters, minDiameter: 1}).map(e => e.identifier), ['1', '3']);
  assert.deepEqual(filterEvents(events, {...filters, minDiameter: 1, keepUnknown: false}).map(e => e.identifier), ['3']);
});
test('altitude and weather filters are explicit', () => {
  const events = [event(1, '2026-11-04T00:00:00Z', 5, 10), event(2, '2026-11-04T01:00:00Z', 5, 20)];
  assert.deepEqual(filterEvents(events, filters).map(e => e.identifier), ['2']);
  assert.equal(filterEvents(events, {...filters, weather: 'announced'}).length, 0);
});
test('WGS84 distance has physical metre units and no GCJ shift', () => {
  assert.equal(distanceMeters({latitude: 0, longitude: 0}, {latitude: 0, longitude: 0}), 0);
  assert.ok(Math.abs(distanceMeters({latitude: 0, longitude: 0}, {latitude: 0, longitude: .001}) - 111.31949) < .001);
  assert.equal(distanceMeters(null, {}), null);
});
test('CSV carries units, six digit IDs and UTC instants, escapes text and leaves unknown values empty', () => {
  const e = event(100001, '2026-11-04T01:00:00Z'); e.name = '=test,"quoted"';
  const csv = eventsCSV([e]);
  assert.ok(csv.includes('minimum_time_utc')); assert.ok(csv.includes('angular_diameter_arcsec'));
  assert.ok(csv.includes('"100001"')); assert.ok(csv.includes('"\'=test,""quoted"""'));
  assert.ok(csv.includes('"0.3"')); assert.ok(csv.includes('"500","",""'));
  assert.ok(csv.endsWith('\r\n'));
});
test('mixed publications and malformed events cannot be silently presented', () => {
  const e = event(1, '2026-11-04T00:00:00Z');
  const m = {schema_version: 1, catalog_sha256: 'a', generated_at: '2026-11-04T00:00:00Z',
    from_time: '2026-11-04T00:00:00Z', to_time: '2026-11-05T00:00:00Z', event_count: 1};
  const p = {...m, events: [e]}, campus = {type: 'FeatureCollection', features: [{}]};
  validateSnapshot(m, p, campus);
  assert.throws(() => validateSnapshot(m, {...p, generated_at: 'other'}, campus));
  assert.throws(() => validateSnapshot(m, {...p, events: [{...e, observer: null}]}, campus));
  assert.throws(() => validateSnapshot(m, {...p, events: [{...e, range_km: 0}]}, campus));
  assert.throws(() => validateSnapshot(m, {...p, events: [{...e, minimum_time: '2026-11-05T00:00:00Z'}]}, campus));
});

test('inverse region membership, distance and local time use geometry instead of a radius around the preferred point', async () => {
  const {visibilityAt, regionContains} = await import('../deploy/core.mjs');
  const geometry = {type:'MultiPolygon',coordinates:[[[[0,0],[.01,0],[.01,.002],[0,.002],[0,0]]]]};
  assert.equal(regionContains(geometry,[.009,.001]),true);
  assert.equal(regionContains(geometry,[.005,.003]),false);
  const e={...event(1,'2026-11-04T00:00:00Z'),visibility:{geometry,chart:{longitude:0,latitude:0,east_m_per_degree:100000,north_m_per_degree:100000},
    reference_time:'2026-11-04T00:00:00Z',sampled_boundary_tolerance_m:5,models:[{bounds_m:[0,0,1000,200],origin_m:[0,0],time_offset_s:0,time_gradient_s_per_m:[.01,0]}]}};
  const inside=visibilityAt(e,{longitude:.009,latitude:.001});
  assert.equal(inside.inside,true);assert.equal(inside.distance_m,0);
  assert.equal(inside.minimum_time,'2026-11-04T00:00:09.000Z');
  const outside=visibilityAt(e,{longitude:.005,latitude:.003});
  assert.equal(outside.inside,false);assert.ok(Math.abs(outside.distance_m-100)<.001);
  assert.equal(outside.minimum_time,null);
  assert.equal(filterEvents([e],{...filters,insideOnly:true},{longitude:.005,latitude:.003}).length,0);
});
