// web/lib/i18nBuilderMap.ts
// AA-948 (card CRM-20) — the EN / VI words of the proposal builder's map (components/proposals/BuilderMap.tsx): its
// empty states and the days without a place. Spread into lib/i18n.ts (every key exists in both languages:
// tests/i18n.test.ts).

export const builderMapEn = {
  "builderMap.title": "Map",
  "builderMap.label": "Map of the trip days",
  "builderMap.noToken.title": "Map not set up yet",
  "builderMap.noToken.body": "The trip map appears here once the map service is set up for this workspace. Everything else in the builder works without it.",
  "builderMap.empty.title": "Nothing to show on the map yet",
  "builderMap.empty.body": "Add an AA tour to a day: its places appear here, numbered by day.",
  "builderMap.failed": "The map could not load. Reload the page to try again; the builder works without it.",
  "builderMap.unplaced": "Days without a place on the map: {days}",
  "builderMap.stop": "Day {n}: {places}",
  "builderMap.approximate": "approximate area",
};

export const builderMapVi: Record<keyof typeof builderMapEn, string> = {
  "builderMap.title": "Bản đồ",
  "builderMap.label": "Bản đồ các ngày của chuyến đi",
  "builderMap.noToken.title": "Chưa thiết lập bản đồ",
  "builderMap.noToken.body": "Bản đồ chuyến đi sẽ hiện ở đây khi dịch vụ bản đồ được thiết lập cho không gian làm việc này. Mọi phần khác của trình soạn vẫn dùng được.",
  "builderMap.empty.title": "Chưa có gì để hiện trên bản đồ",
  "builderMap.empty.body": "Thêm một tour AA vào một ngày: các địa điểm của tour sẽ hiện ở đây, đánh số theo ngày.",
  "builderMap.failed": "Không tải được bản đồ. Tải lại trang để thử lại; trình soạn vẫn dùng được khi không có bản đồ.",
  "builderMap.unplaced": "Các ngày chưa có địa điểm trên bản đồ: {days}",
  "builderMap.stop": "Ngày {n}: {places}",
  "builderMap.approximate": "khu vực ước lượng",
};
