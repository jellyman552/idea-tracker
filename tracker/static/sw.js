// Minimal service worker: makes the app installable. It always goes to the
// network (no offline cache), so you never see stale project data.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
