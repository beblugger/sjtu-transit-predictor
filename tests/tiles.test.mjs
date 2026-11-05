import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../deploy/tiles-sw.js',import.meta.url),'utf8');
function runtime(control='public, max-age=3600',size=16){
  const values=new Map(),listeners={},stats={fetches:0};
  const cache={match:async key=>values.get(typeof key==='string'?key:key.url)?.clone(),
    delete:async key=>values.delete(typeof key==='string'?key:key.url),
    put:async(key,value)=>values.set(key.url,value.clone()),keys:async()=>[...values.keys()].map(url=>new Request(url))};
  const context=vm.createContext({URL,Request,Response,Headers,Date,Promise,AbortSignal,caches:{open:async()=>cache},
    self:{registration:{scope:'https://example.test/'},addEventListener:(name,fn)=>listeners[name]=fn,skipWaiting(){},clients:{claim(){}}},
    fetch:async()=>{stats.fetches++;return new Response(new Uint8Array(size),{headers:{'cache-control':control,'content-type':'image/png'}});}});
  vm.runInContext(source,context);
  return {context,values,listeners,stats,tile:url=>{context.request=new Request(url,{mode:'cors'});return vm.runInContext('tile(request)',context);}};
}
test('viewport tiles are reused; scientific data and unsupported zooms are never intercepted',async()=>{
  const r=runtime(),url='https://tile.openstreetmap.org/14/1/1.png';
  await r.tile(url);await r.tile(url);assert.equal(r.stats.fetches,1);
  for(const zoom of [14,16,19]){
    let response;r.listeners.fetch({request:new Request(`https://tile.openstreetmap.org/${zoom}/1/1.png`),respondWith(value){response=value;}});
    assert.ok(response);await response;
  }
  for(const url of ['https://example.test/data/events.json','https://tile.openstreetmap.org/20/1/1.png','https://map.sjtu.edu.cn/geoserver/jmap/wms']){
    let intercepted=false;r.listeners.fetch({request:new Request(url),respondWith(){intercepted=true;}});assert.equal(intercepted,false);
  }
});
test('parallel writes keep the tile and byte limits',async()=>{
  const r=runtime('max-age=3600',200000);
  r.context.requests=Array.from({length:70},(_,i)=>new Request(`https://tile.openstreetmap.org/14/${i}/1.png`,{mode:'cors'}));
  await vm.runInContext('Promise.all(requests.map(tile))',r.context);
  assert.ok(r.values.size<=64);
  assert.ok([...r.values.values()].reduce((sum,v)=>sum+Number(v.headers.get('x-transit-bytes')),0)<=12*1024*1024);
});
test('no-store tile responses are never retained',async()=>{
  const r=runtime('no-store');await r.tile('https://tile.openstreetmap.org/14/1/1.png');assert.equal(r.values.size,0);
});
test('application files prefer the network and fall back offline without caching scientific files',async()=>{
  const r=runtime();
  r.context.request=new Request('https://example.test/app.mjs?v=first');
  await vm.runInContext('appFile(request)',r.context);
  assert.ok(r.values.has('https://example.test/app.mjs'));
  await vm.runInContext('appFile(request)',r.context);
  assert.equal(r.stats.fetches,2);
  r.context.fetch=async()=>{throw new Error('offline');};
  assert.ok(await vm.runInContext('appFile(request)',r.context));
  let intercepted=false;
  r.listeners.fetch({request:new Request('https://example.test/data/manifest.json'),respondWith(){intercepted=true;}});
  assert.equal(intercepted,false);
});
test('notification clicks focus the event within the app scope and reject external URLs',async()=>{
  const r=runtime(),navigations=[];
  r.context.self.clients.matchAll=async()=>[{url:'https://example.test/',navigate:async url=>navigations.push(url),focus:async()=>navigations.push('focused')}];
  let done;
  const click=url=>r.listeners.notificationclick({notification:{data:{url},close(){}},waitUntil:promise=>done=promise});
  click('https://example.test/#event=123');await done;
  assert.deepEqual(navigations,['https://example.test/#event=123','focused']);
  click('https://other.test/');await done;
  assert.equal(navigations.length,2);
});
