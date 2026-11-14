// web/lib/i18nTripSource.ts
// AA-1089 (Odoo study rank 48) — the EN / VI words of the trip header's "AA tour" line: the AA tour and departure the
// trip came from (GET /trips/{id} `aa_source`). Spread into lib/i18n.ts (every key exists in both languages:
// tests/i18n.test.ts).

export const tripSourceEn = {
  "tripSource.label": "AA tour",
  "tripSource.unknownTour": "An AA tour",
  "tripSource.departs": "Departs {date}",
  "tripSource.bookedOnline": "Booked online",
  "tripSource.page": "Tour page",
  "tripSource.pageHint": "Opens the public tour page in a new tab",
};

export const tripSourceVi: Record<keyof typeof tripSourceEn, string> = {
  "tripSource.label": "Tour AA",
  "tripSource.unknownTour": "Một tour AA",
  "tripSource.departs": "Khởi hành {date}",
  "tripSource.bookedOnline": "Đặt trực tuyến",
  "tripSource.page": "Trang tour",
  "tripSource.pageHint": "Mở trang tour công khai trong thẻ mới",
};
