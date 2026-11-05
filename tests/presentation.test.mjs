import test from 'node:test';
import assert from 'node:assert/strict';
import {weatherCondition,visibleDetailEntries,dataNotices,brightnessLabel} from '../deploy/presentation.mjs';
import {filterEvents} from '../deploy/core.mjs';
const weather={deterministic:{hourly:{times:['2026-11-05T00:00:00Z','2026-11-05T01:00:00Z','2026-11-05T02:00:00Z'],cloud_cover_pct:[5,95,60],weather_code:[0,3,2],precipitation_mm:[0,0,0]}}};
test('sky conditions are independent of composite scores and do not extrapolate unavailable weather',()=>{
  assert.equal(weatherCondition(weather,'2026-11-05T00:00:00Z').kind,'clear');
  assert.equal(weatherCondition(weather,'2026-11-05T01:00:00Z').kind,'overcast');
  assert.equal(weatherCondition(weather,'2026-11-05T02:00:00Z').kind,'overcast');
  assert.equal(weatherCondition(weather,'2026-11-05T03:00:00Z').kind,'unavailable');
  assert.equal(weatherCondition(null,'2026-11-05T00:00:00Z').kind,'unavailable');
  const events=[{name:'a',identifier:'1',minimum_time:'2026-11-05T00:00:00Z',altitude_deg:30,object_angular_diameter_arcsec:null,weather:{decision:'suppressed'}},
    {name:'b',identifier:'2',minimum_time:'2026-11-05T01:00:00Z',altitude_deg:30,object_angular_diameter_arcsec:null,weather:{decision:'announced'}}];
  const f={search:'',from:Date.parse('2026-11-05T00:00:00Z'),to:Date.parse('2026-11-05T03:00:00Z'),target:'all',minAltitude:15,minDiameter:0,keepUnknown:true,weather:'clear',sort:'time'};
  assert.deepEqual(filterEvents(events,f,null,weather),[events[0]]);
  assert.deepEqual(filterEvents(events,{...f,weather:'overcast'},null,weather),[events[1]]);
});
test('unknown detail values disappear while real zero measurements remain',()=>{
  assert.deepEqual(visibleDetailEntries([['a','未知'],['b','未知 — 5 m'],['c',null],['d','0.00 m'],['e','未设置位置'],['f','unknown']]),[['d','0.00 m']]);
  assert.equal(visibleDetailEntries([['亮度',brightnessLabel({satellite_sunlit:true})]]).length,1);
});
test('confirmed backend failure has priority; inability to check never claims shutdown',()=>{
  const manifest={from_time:'2026-11-05T00:00:00Z',to_time:'2026-11-06T00:00:00Z',weather_status:{},warnings:[]};
  const selected={reason_codes:['object_size_unknown_centerline_only','orbit_epoch_old'],weather:{reasons:[]}};
  const args={manifest,selected,now:Date.parse('2026-11-05T12:00:00Z')};
  const disabled=dataNotices({...args,backend:{state:'disabled_manually'}});
  assert.equal(disabled[0].level,'critical');assert.match(disabled[0].text,/已停用/);
  assert.equal(disabled.at(-1).level,'info');
  assert.ok(dataNotices({...args,backend:{state:'missing'}}).some(x=>x.level==='critical'));
  assert.ok(dataNotices({...args,backend:{state:'active',failure:'failure'}}).some(x=>x.level==='critical'));
  assert.equal(dataNotices({...args,backend:{state:'unknown'}}).some(x=>x.level==='critical'),false);
  assert.equal(dataNotices({...args,now:Date.parse('2026-11-07T00:00:00Z')})[0].level,'critical');
});
