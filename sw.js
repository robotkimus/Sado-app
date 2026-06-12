/* 사도될까 — 서비스 워커
   앱 화면 파일은 캐시해서 오프라인에서도 뜨게 하고,
   시세 데이터(API)와 트레이딩뷰는 항상 네트워크에서 가져온다. */
const CACHE = "sado-v1";
const SHELL = [
  "./",
  "./index.html",
  "./manifest.json",
  "./icon-192.png",
  "./icon-512.png",
  "./apple-touch-icon.png"
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // 시세 API·트레이딩뷰·프록시는 캐시하지 않음 (항상 최신 데이터)
  if (url.origin !== location.origin) return;
  // 같은 출처(앱 파일)는 캐시 우선, 실패 시 네트워크
  e.respondWith(
    caches.match(e.request).then((hit) => {
      const fetched = fetch(e.request)
        .then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(e.request, copy));
          }
          return res;
        })
        .catch(() => hit);
      return hit || fetched;
    })
  );
});
