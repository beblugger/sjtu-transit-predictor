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
const filterNow = Date.parse('2026-11-03T00:00:00Z');
const applyFilters = (events, f, location = null, weather = null, now = filterNow) =>
  filterEvents(events, f, location, weather, now);

test('UTC and Beijing conversion retains the same instant, including day rollover', () => {
  const time = '2026-11-04T20:12:33.123Z';
  assert.equal(formatTime(time, 'Asia/Shanghai', true), '2026-11-05 04:12:33.123');
  for (const zone of ['UTC', 'Asia/Shanghai']) assert.equal(inputInstant(inputTime(time, zone), zone), Date.parse(time) - 123);
  assert.ok(Number.isNaN(inputInstant('', 'UTC')));
});
test('time range is half open and old orbital epochs are retained', () => {
  const events = [event(1, '2026-11-04T00:00:00Z'), event(2, '2026-11-05T00:00:00Z')];
  assert.deepEqual(applyFilters(events, filters).map(e => e.identifier), ['1']);
});

test('future-only filtering excludes events whose minimum has already occurred', () => {
  const now = Date.parse('2026-11-04T00:30:00Z');
  const events = [event(1, '2026-11-04T00:29:59Z'), event(2, '2026-11-04T00:30:00Z'), event(3, '2026-11-04T00:30:01Z')];
  assert.deepEqual(applyFilters(events, filters, null, null, now).map(e => e.identifier), ['3']);
  assert.deepEqual(applyFilters(events, filters).map(e => e.identifier), ['1', '2', '3']);
});
test('unknown size is distinct from zero and does not disappear unless requested', () => {
  const events = [event(1, '2026-11-04T01:00:00Z'), event(2, '2026-11-04T02:00:00Z', 0), event(3, '2026-11-04T03:00:00Z', 5)];
  assert.equal(number(null), '未知'); assert.equal(number(0), '0.00');
  assert.deepEqual(applyFilters(events, {...filters, minDiameter: 1}).map(e => e.identifier), ['1', '3']);
  assert.deepEqual(applyFilters(events, {...filters, minDiameter: 1, keepUnknown: false}).map(e => e.identifier), ['3']);
});
test('altitude and weather filters are explicit', () => {
  const events = [event(1, '2026-11-04T00:00:00Z', 5, 10), event(2, '2026-11-04T01:00:00Z', 5, 20)];
  assert.deepEqual(applyFilters(events, filters).map(e => e.identifier), ['2']);
  assert.equal(applyFilters(events, {...filters, weather: 'announced'}).length, 0);
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
  const equivalent={...e,physical_size_m:null,angular_diameter_method:'rcs_sphere',equivalent_diameter_m:10,
    object_angular_radius_arcsec:1,object_angular_diameter_arcsec:2,satellite_rcs_m2:78.54,angular_size_confidence:'low'};
  const output=eventsCSV([equivalent]).split('\r\n'),columns=output[0].slice(1).split(',').map(x=>x.slice(1,-1));
  // Parse the final columns only; the satellite name deliberately contains commas.
  const values=output[1].split(',').slice(-6).map(x=>x.slice(1,-1));
  assert.deepEqual(values,['1','rcs_sphere','10','78.54','','low']);
  assert.ok(columns.includes('rcs_equivalent_diameter_m'));
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
  assert.throws(() => validateSnapshot(m, {...p, events: [{...e, start_time: undefined, end_time: undefined}]}, campus));
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
  assert.equal(applyFilters([e],{...filters,insideOnly:true},{longitude:.005,latitude:.003}).length,0);
  const location={longitude:.005,latitude:.003};
  assert.equal(applyFilters([e],{...filters,maxDistance:99},location).length,0);
  assert.equal(applyFilters([e],{...filters,maxDistance:101},location).length,1);
  assert.equal(applyFilters([e],{...filters,maxDistance:0},{longitude:.009,latitude:.001}).length,1);
  assert.equal(applyFilters([e],{...filters,maxDistance:0}).length,1);
  assert.equal(applyFilters([e],{...filters,maxDistance:null},location).length,1);
  assert.equal(applyFilters([e],{...filters,maxDistance:10000},{longitude:1,latitude:1}).length,0);
  assert.equal(applyFilters([event(2,'2026-11-04T00:00:00Z')],{...filters,maxDistance:1000},location).length,0);
});

test('visibility band seams do not become observation boundaries', async () => {
  const {visibilityAt} = await import('../deploy/core.mjs');
  const rectangle = (left, bottom, right, top) => [[[left,bottom],[right,bottom],[right,top],[left,top],[left,bottom]]];
  const e = {...event(1, '2026-11-04T00:00:00Z'), visibility: {
    chart: {longitude:0, latitude:0, east_m_per_degree:1000, north_m_per_degree:1000},
    geometry: {type:'MultiPolygon', coordinates:[rectangle(-.2,-.1,0,.1), rectangle(0,-.1,.2,.1)]},
    calculation_domain: {type:'Polygon', coordinates:rectangle(-1,-1,1,1)},
    reference_time:'2026-11-04T00:00:00Z', sampled_boundary_tolerance_m:5,
    bands: [], models: [{bounds_m:[-200,-100,200,100], origin_m:[0,0], time_offset_s:0, time_gradient_s_per_m:[.01,0]}],
  }};
  for (const longitude of [-.001, 0, .001]) {
    const local = visibilityAt(e, {longitude, latitude:0});
    assert.equal(local.inside, true);
    assert.equal(local.distance_m, 0);
    assert.ok(Math.abs(local.boundary_distance_m - 100) < 1e-6);
    assert.equal(local.near_boundary, false);
  }
  const outside = visibilityAt(e, {longitude:0, latitude:.15});
  assert.equal(outside.inside, false);
  assert.ok(Math.abs(outside.distance_m - 50) < 1e-6);
});

test('overlay window uses two chronological neighbors on each side without backfill', async () => {
  const {overlayEvents,nearestEvent}=await import('../deploy/map.mjs');
  const rows=Array.from({length:8},(_,i)=>({...event(i%3,`2026-11-05T0${i}:00:00Z`),visibility:{}}));
  assert.deepEqual(overlayEvents([...rows].reverse(),rows[3]),rows.slice(1,6));
  assert.deepEqual(overlayEvents(rows,rows[0]),rows.slice(0,3));
  assert.deepEqual(overlayEvents(rows,rows[7]),rows.slice(5));
  assert.deepEqual(overlayEvents(rows,null),[]);
  assert.equal(nearestEvent(rows,Date.parse('2026-11-05T03:10:00Z')),rows[3]);
  assert.equal(nearestEvent([],0),null);
  // A pagination boundary must not change the centered map window.
  const many=Array.from({length:55},(_,i)=>({...event(i,new Date(i*60000).toISOString()),visibility:{}}));
  assert.deepEqual(overlayEvents(many,many[49]),many.slice(47,52));
});
test('WGS84 geometry uses Web Mercator without GCJ offset', async () => {
  const {worldPixel,mapView}=await import('../deploy/map.mjs');
  assert.deepEqual(worldPixel([0,0],0),[128,128]);
  assert.ok(worldPixel([0,45],0)[1]<128);
  const visibility={calculation_domain:{type:'Polygon',coordinates:[[[121.39,31],[121.47,31],[121.47,31.05],[121.39,31.05],[121.39,31]]]}};
  const view=mapView({},visibility);
  assert.ok(view.project([121.43,31.025]).every(Number.isFinite));
});

test('zoom stays inside the prediction domain', async () => {
  const {mapView,worldPixel}=await import('../deploy/map.mjs');
  const domain={type:'Polygon',coordinates:[[[121.39,31],[121.47,31],[121.47,31.05],[121.39,31.05],[121.39,31]]]};
  const base=mapView({}, {calculation_domain:domain});
  const view=mapView({}, {calculation_domain:domain},16,[-1e9,1e9]);
  const corners=domain.coordinates[0].map(p=>worldPixel(p,14));
  assert.ok(view.cx-480/view.scale>=Math.min(...corners.map(p=>p[0]))-1e-7);
  assert.ok(view.cy+280/view.scale<=Math.max(...corners.map(p=>p[1]))+1e-7);
  assert.equal(view.scale/base.scale,16);

});

test('campus tiles use the source frontend WMS grid at native screen scale',async()=>{
  const {mapView,campusTiles}=await import('../deploy/map.mjs');
  const domain={type:'Polygon',coordinates:[[[121.39,31],[121.47,31],[121.47,31.05],[121.39,31.05],[121.39,31]]]};
  const base=mapView({}, {calculation_domain:domain},2.5),closer=mapView({}, {calculation_domain:domain},5);
  const tiles=campusTiles(base,1280),zoomed=campusTiles(closer,1280);
  assert.ok(tiles.length>0&&tiles.length<64);
  assert.ok(tiles.every(t=>t.size*1280/960>128&&t.size*1280/960<257));
  assert.equal(zoomed[0].zoom,tiles[0].zoom+1);
  for(const tile of tiles){
    const url=new URL(tile.url);
    assert.equal(url.origin,'https://map.sjtu.edu.cn');
    assert.equal(url.searchParams.get('srs'),'EPSG:3857');
    assert.equal(url.searchParams.get('width'),'256');
    assert.equal(url.searchParams.get('height'),'256');
    const [left,bottom,right,top]=url.searchParams.get('bbox').split(',').map(Number);
    assert.ok(right>left&&top>bottom);assert.ok(Math.abs((right-left)-(top-bottom))<1e-6);
  }
});


test('exterior tiles sharpen on zoom and resize while preserving geographic alignment',async()=>{
  const {mapView,exteriorTiles,worldPixel}=await import('../deploy/map.mjs');
  const domain={type:'Polygon',coordinates:[[[121.39,31],[121.47,31],[121.47,31.05],[121.39,31.05],[121.39,31]]]};
  const view=mapView({}, {calculation_domain:domain},2.5);
  const tiles=exteriorTiles(view,1280);
  assert.ok(tiles[0].zoom>14);
  assert.equal(exteriorTiles(mapView({}, {calculation_domain:domain},5),1280)[0].zoom,tiles[0].zoom+1);
  assert.equal(exteriorTiles(view,2560)[0].zoom,tiles[0].zoom+1);
  assert.ok(tiles.every(t=>t.size*1280/960<257));
  const [px,py]=worldPixel([121.43,31.03],tiles[0].zoom),tx=Math.floor(px/256),ty=Math.floor(py/256);
  const tile=tiles.find(t=>new URL(t.url).pathname.endsWith(`/${tx}/${ty}.png`));
  const expected=view.project([121.43,31.03]);
  assert.ok(Math.abs(tile.x+(px/256-tx)*(tile.size-.2)-expected[0])<1e-6);
  assert.ok(Math.abs(tile.y+(py/256-ty)*(tile.size-.2)-expected[1])<1e-6);
  assert.ok(Math.min(...tiles.map(t=>t.x))<=0&&Math.max(...tiles.map(t=>t.x+t.size))>=960);
  assert.ok(Math.min(...tiles.map(t=>t.y))<=0&&Math.max(...tiles.map(t=>t.y+t.size))>=560);
});

test('dense campus tiles increase pixel resolution without moving their bounds',async()=>{
  const {campusTiles,worldPixel}=await import('../deploy/map.mjs');
  const [cx,cy]=worldPixel([121.43,31.03],14),view={cx,cy,scale:4};
  const normal=campusTiles(view,1280),dense=campusTiles(view,1280,2),capped=campusTiles(view,1280,3);
  assert.ok(normal.length>0);
  assert.equal(normal.length,dense.length);
  dense.forEach((tile,i)=>{
    const url=new URL(tile.url);
    assert.equal(url.searchParams.get('width'),'512');
    assert.equal(url.searchParams.get('height'),'512');
    assert.equal(url.searchParams.get('bbox'),new URL(normal[i].url).searchParams.get('bbox'));
    assert.deepEqual([tile.x,tile.y,tile.size],[normal[i].x,normal[i].y,normal[i].size]);
    assert.equal(tile.url,capped[i].url);
  });
});
