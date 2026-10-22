// Minimal service worker so Chrome treats this page as installable (needed for
// the share-sheet entry). It doesn't cache anything; every request goes to the network.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
