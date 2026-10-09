/* Service worker: makes the dashboard installable as an app. Everything still comes
   from the network (the data is live and behind a login); when the NAS can't be
   reached, page loads get a short offline notice instead of the browser's error. */
"use strict";

const OFFLINE = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Runs</title><style>body{font:15px/1.5 system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;padding:16px;text-align:center;background:#f9f9f7;color:#0b0b0b}
@media (prefers-color-scheme:dark){body{background:#0d0d0d;color:#fff}}button{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid #8884;background:none;color:inherit}</style>
<div><h1 style="font-size:20px">Can't reach the dashboard</h1><p>Check that you're on the home network (or VPN), then try again.</p>
<button onclick="location.reload()">Retry</button></div>`;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (ev) => ev.waitUntil(self.clients.claim()));

self.addEventListener("fetch", (ev) => {
  if (ev.request.mode !== "navigate") return;
  ev.respondWith(fetch(ev.request).catch(() =>
    new Response(OFFLINE, { headers: { "Content-Type": "text/html; charset=utf-8" } })));
});
