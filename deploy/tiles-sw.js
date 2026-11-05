// Cache only native-resolution tiles requested by the current viewport.
const CACHE='transit-exterior-v2',MAX_TILES=64,MAX_BYTES=12*1024*1024;
const APP_CACHE='transit-app-v1';
const APP_FILES=['./','index.html','reading.html','styles.css','app.mjs','core.mjs','map.mjs','presentation.mjs','reminders.mjs','manifest.webmanifest','assets/icon-192.png','assets/icon-512.png'];
let writes=Promise.resolve();
self.addEventListener('install',event=>event.waitUntil((async()=>{
  const cache=await caches.open(APP_CACHE);
  await cache.addAll(APP_FILES.map(path=>new Request(new URL(path,self.registration.scope),{cache:'reload'})));
  await self.skipWaiting();
})()));
self.addEventListener('activate',event=>event.waitUntil((async()=>{
  await caches.delete('transit-exterior-z14-v1');
  for(const key of await caches.keys())if(key.startsWith('transit-app-')&&key!==APP_CACHE)await caches.delete(key);
  await self.clients.claim();
})()));
self.addEventListener('fetch',event=>{
  const u=new URL(event.request.url);
  const path=u.href.split('?')[0];
  if(event.request.method==='GET'&&APP_FILES.some(file=>new URL(file,self.registration.scope).href===path)){
    event.respondWith(appFile(event.request));return;
  }
  if(event.request.method!=='GET'||u.origin!=='https://tile.openstreetmap.org'||!/^\/(?:1[4-9])\/\d+\/\d+\.png$/.test(u.pathname))return;
  event.respondWith(tile(event.request));
});
async function appFile(request){
  const cache=await caches.open(APP_CACHE),key=new Request(request.url.split('?')[0]);
  try{
    const response=await fetch(new Request(request,{cache:'no-cache',signal:AbortSignal.timeout(8000)}));
    if(!response.ok)throw new Error(`HTTP ${response.status}`);
    try{await cache.put(key,response.clone());}catch{}
    return response;
  }catch(error){const saved=await cache.match(key);if(saved)return saved;throw error;}
}
self.addEventListener('notificationclick',event=>{
  event.notification.close();
  event.waitUntil((async()=>{
    const url=new URL(event.notification.data?.url||'./',self.registration.scope);
    if(url.origin!==new URL(self.registration.scope).origin||!url.href.startsWith(self.registration.scope))return;
    const clients=await self.clients.matchAll({type:'window',includeUncontrolled:true});
    for(const client of clients){
      if(!client.url.startsWith(self.registration.scope))continue;
      if('navigate' in client)await client.navigate(url.href);
      await client.focus();return;
    }
    await self.clients.openWindow(url.href);
  })());
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
