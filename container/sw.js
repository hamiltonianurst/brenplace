const C='container-202611262345';
const PRE=['./','vendor/three.min.js','vendor/OrbitControls.js','manifest.webmanifest','icon-192.png','icon-512.png'];
self.addEventListener('install',e=>{e.waitUntil(caches.open(C).then(c=>c.addAll(PRE)).then(()=>self.skipWaiting()))});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k.startsWith('container-')&&k!==C).map(k=>caches.delete(k)))).then(()=>self.clients.claim()))});
self.addEventListener('fetch',e=>{
  const r=e.request;if(r.method!=='GET')return;
  const u=new URL(r.url);
  if(r.mode==='navigate'&&u.origin===location.origin){
    e.respondWith((async()=>{
      const c=await caches.open(C);
      const net=fetch(r).then(res=>{if(res.ok)c.put('./',res.clone());return res});
      const cached=await c.match('./');
      if(!cached)return net;
      const slow=new Promise(ok=>setTimeout(()=>ok(cached),3500));
      return Promise.race([net.catch(()=>cached),slow]);
    })());
    return;
  }
  e.respondWith(caches.match(r).then(hit=>hit||fetch(r).then(res=>{
    if(res.ok||res.type==='opaque'){const cp=res.clone();caches.open(C).then(c=>c.put(r,cp))}
    return res;
  })));
});
