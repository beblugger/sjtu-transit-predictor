import test from 'node:test';
import assert from 'node:assert/strict';
import {nearbyEvents,alreadyReminded,reminderKey,readSettings,notificationURL} from '../deploy/reminders.mjs';
import {brightnessLabel} from '../deploy/presentation.mjs';

const now=Date.parse('2026-11-05T07:30:00Z');
const manifest={from_time:'2026-11-05T00:00:00Z',to_time:'2026-11-06T00:00:00Z'};
const region={type:'Polygon',coordinates:[[[0,0],[.01,0],[.01,.002],[0,.002],[0,0]]]};
const event={identifier:'123',target:'moon',name:'Satellite',observer:{latitude:0,longitude:0},minimum_time:new Date(now+10*60000).toISOString(),
  visibility:{geometry:region,chart:{longitude:0,latitude:0,east_m_per_degree:100000,north_m_per_degree:100000},
    reference_time:new Date(now+10*60000).toISOString(),sampled_boundary_tolerance_m:5,
    models:[{bounds_m:[0,0,1000,200],origin_m:[0,0],time_offset_s:0,time_gradient_s_per_m:[.01,0]}]}};

test('nearby reminder uses inclusive 100 m / 10 minute bounds and excludes past events',()=>{
  const edge={longitude:.005,latitude:.003};
  assert.equal(nearbyEvents([event],edge,manifest,now).length,1);
  assert.equal(nearbyEvents([event],{...edge,latitude:.003001},manifest,now).length,0);
  assert.equal(nearbyEvents([event],edge,manifest,now-1).length,0);
  assert.equal(nearbyEvents([event],edge,manifest,now+10*60000).length,1);
  assert.equal(nearbyEvents([event],edge,manifest,now+10*60000+1).length,0);
  assert.equal(nearbyEvents([event],null,manifest,now).length,0);
  assert.equal(nearbyEvents([event],edge,manifest,now+86400000).length,0);
  assert.equal(nearbyEvents([{...event,visibility:null}],edge,manifest,now).length,0);
  assert.equal(nearbyEvents([event],{longitude:1,latitude:1},manifest,now).length,0);
});
test('local time decides reminders inside the region, exported time is identified outside',()=>{
  assert.equal(nearbyEvents([event],{longitude:.005,latitude:.001},manifest,now).length,0); // local time is five seconds later
  const [inside]=nearbyEvents([event],{longitude:.005,latitude:.001},manifest,now+5000);
  assert.equal(inside.distance,0);assert.equal(inside.approximate,false);
  assert.equal(inside.instant,new Date(now+605000).toISOString());
  assert.equal(nearbyEvents([event],{longitude:.005,latitude:.003},manifest,now)[0].approximate,true);
});
test('orbit updates do not duplicate the same pass but later passes still notify',()=>{
  const sent={[reminderKey(event)]:now};
  assert.equal(alreadyReminded(sent,event,new Date(now+5*60000).toISOString()),true);
  assert.equal(alreadyReminded(sent,event,new Date(now+90*60000).toISOString()),false);
  assert.equal(alreadyReminded(sent,{...event,identifier:'456'},new Date(now).toISOString()),false);
  assert.equal(new URL(notificationURL(event,'https://test.local/predictor/')).pathname,'/predictor/');
});
test('update and notification switches persist separately and tolerate broken storage',()=>{
  assert.deepEqual(readSettings({getItem:()=>'{broken'}),{autoUpdate:true,nearbyNotifications:false,followDevice:false});
  assert.deepEqual(readSettings({getItem:()=>JSON.stringify({autoUpdate:false,nearbyNotifications:true,followDevice:false})}),{autoUpdate:false,nearbyNotifications:true,followDevice:false});
  assert.equal(readSettings({getItem(){throw Error();}}).nearbyNotifications,false);
});
test('brightness distinguishes a real magnitude, shadow and missing parameters',()=>{
  assert.equal(brightnessLabel({satellite_sunlit:false,satellite_magnitude:5.9}),'在地影中');
  assert.equal(brightnessLabel({satellite_sunlit:true,satellite_magnitude:5.93}),'约 5.9 等');
  assert.equal(brightnessLabel({satellite_sunlit:true,satellite_magnitude:0}),'约 0.0 等');
  assert.equal(brightnessLabel({satellite_sunlit:true,satellite_magnitude:-2.5}),'约 -2.5 等');
  assert.match(brightnessLabel({satellite_sunlit:true,satellite_magnitude:NaN}),/暂无星等估计/);
  assert.match(brightnessLabel({}),/待数据更新/);
});
