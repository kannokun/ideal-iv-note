// 理想個体ノート Service Worker
// 方針: HTMLはネットワーク優先(オンラインで常に最新・オフラインでキャッシュ)。
//       公開データ(Supabase REST GET)・フォントは stale-while-revalidate。
//       認証(/auth/)と手持ち(user_collection)・全ての書込(POST/PATCH/DELETE)はSW非介入=常にネットワーク。
const CACHE = "iv-cache-2026-10-24j";
const SHELL = ["./", "./index.html"];

self.addEventListener("install", e => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL).catch(() => {})));
});

self.addEventListener("activate", e => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;                 // 書込系はネットワークのみ
  const url = new URL(req.url);

  // 認証・ユーザーデータはキャッシュしない(常に最新・プライベート)
  if (url.pathname.startsWith("/auth/") || url.pathname.includes("/rest/v1/user_collection")) return;

  // HTML(ナビゲーション)はネットワーク優先→失敗時キャッシュ(=オンラインは常に最新で陳腐化しない)
  if (req.mode === "navigate" || url.pathname.endsWith("/") || url.pathname.endsWith("index.html")) {
    e.respondWith(
      fetch(req).then(res => {
        const cp = res.clone();
        caches.open(CACHE).then(c => c.put("./index.html", cp));
        return res;
      }).catch(() => caches.match("./index.html"))
    );
    return;
  }

  // 公開データ(種族/フォーム/個体値)・フォント等: stale-while-revalidate
  e.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const cached = await cache.match(req);
    const net = fetch(req).then(res => {
      if (res && (res.ok || res.type === "opaque")) cache.put(req, res.clone());
      return res;
    }).catch(() => null);
    return cached || (await net) || new Response("", { status: 504 });
  })());
});
