const SHELL_CACHE = "patronaj-shell-v3";
const SHELL = ["/", "/assets/style.css", "/assets/app.js", "/example.json"];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(SHELL_CACHE).then(cache => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil((async () => {
    const cachesToDelete = (await caches.keys()).filter(key => key.startsWith("patronaj-shell-") && key !== SHELL_CACHE);
    await Promise.all(cachesToDelete.map(key => caches.delete(key)));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin || !SHELL.includes(url.pathname)) return;
  event.respondWith((async () => {
    const cache = await caches.open(SHELL_CACHE);
    return (await cache.match(url.pathname)) ?? fetch(request);
  })());
});
