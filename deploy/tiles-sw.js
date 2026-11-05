// Only cache low-resolution tiles actually requested by the current viewport.
const CACHE='transit-exterior-z14-v1',MAX_TILES=64,MAX_BYTES=12*1024*1024;
let writes=Promise.resolve();
self.addEventListener('install',()=>self.skipWaiting());
self.addEventListener('activate',event=>event.waitUntil(self.clients.claim()));
self.addEventListener('fetch',event=>{
  const u=new URL(event.request.url);
  if(event.request.method!=='GET'||u.origin!=='https://tile.openstreetmap.org'||!/^\/14\/\d+\/\d+\.png$/.test(u.pathname))return;
  event.respondWith(tile(event.request));
});
async function tile(request){
  const cache=await caches.open(CACHE),cached=await cache.match(request);
  if(cached&&Number(cached.headers.get('x-transit-expires'))>Date.now())return cached;
  try{
    const response=await fetch(request);
    const control=response.headers.get('cache-control')||'';
    if(!response.ok||response.type==='opaque'||/no-store|no-cache/i.test(control))return response;
    const body=await response.clone().arrayBuffer();
    if(body.byteLength>1024*1024)return response;
    const maxAge=control.match(/(?:^|,)\s*max-age=(\d+)/i),date=Date.parse(response.headers.get('date'));
    const age=Math.max(Number(response.headers.get('age')||0),Number.isFinite(date)?(Date.now()-date)/1000:0);
    const expires=response.headers.get('expires');
    let ttl=maxAge?Math.max(0,Number(maxAge[1])-age)*1000:expires?Math.max(0,Date.parse(expires)-Date.now()):7*86400000;
    if(expires&&Number.isFinite(Date.parse(expires)))ttl=Math.min(ttl,Math.max(0,Date.parse(expires)-Date.now()));
    if(!Number.isFinite(ttl)||ttl<=0)return response;
    const headers=new Headers(response.headers);
    headers.set('x-transit-expires',String(Date.now()+ttl));headers.set('x-transit-bytes',String(body.byteLength));
    const stored=new Response(body,{status:response.status,statusText:response.statusText,headers});
    const write=writes.then(async()=>{
      await cache.delete(request);await cache.put(request,stored);
      const keys=await cache.keys();let bytes=0;
      for(const key of keys){const value=await cache.match(key);bytes+=Number(value?.headers.get('x-transit-bytes')||0);}
      while(keys.length>MAX_TILES||bytes>MAX_BYTES){const key=keys.shift(),value=await cache.match(key);bytes-=Number(value?.headers.get('x-transit-bytes')||0);await cache.delete(key);}
    });
    writes=write.catch(()=>{});await writes;
    return response;
  }catch(error){if(cached&&!/must-revalidate/i.test(cached.headers.get('cache-control')||''))return cached;throw error;}
}
