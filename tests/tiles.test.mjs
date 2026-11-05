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
  const context=vm.createContext({URL,Request,Response,Headers,Date,Promise,caches:{open:async()=>cache},
    self:{addEventListener:(name,fn)=>listeners[name]=fn,skipWaiting(){},clients:{claim(){}}},
    fetch:async()=>{stats.fetches++;return new Response(new Uint8Array(size),{headers:{'cache-control':control,'content-type':'image/png'}});}});
  vm.runInContext(source,context);
  return {context,values,listeners,stats,tile:url=>{context.request=new Request(url,{mode:'cors'});return vm.runInContext('tile(request)',context);}};
}
test('coarse tiles are reused; scientific data and high zoom tiles are never intercepted',async()=>{
  const r=runtime(),url='https://tile.openstreetmap.org/14/1/1.png';
  await r.tile(url);await r.tile(url);assert.equal(r.stats.fetches,1);
  for(const url of ['https://example.test/data/events.json','https://tile.openstreetmap.org/18/1/1.png']){
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
