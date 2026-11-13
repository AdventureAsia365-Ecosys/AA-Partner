// web/lib/i18nTourRetired.ts
// AA-1093 part 1 — the EN / VI words of an AA tour Adventure Asia no longer offers: the trip page card, the pipeline
// filter and card badge, the builder option badge and the task kind. Spread into lib/i18n.ts (every key exists in both
// languages: tests/i18n.test.ts).

export const tourRetiredEn = {
  "tourRetired.title": "AA tour discontinued",
  "tourRetired.badge": "AA tour discontinued",
  "tourRetired.filter": "AA tour discontinued",
  "tourRetired.help": "Adventure Asia no longer offers this tour. Nothing was changed: your proposal options, holds and bookings stay as they are. Offer the client another tour, or check a booking with Adventure Asia.",
  "tourRetired.since": "Since {date}",
  "tourRetired.unknownTour": "An AA tour",
  "tourRetired.item.open_option": "Proposal options: {n}",
  "tourRetired.item.held_line": "AA lines on hold: {n}",
  "tourRetired.item.quoted_line": "Quoted AA lines: {n}",
  "tourRetired.item.confirmed_line": "Booked AA lines (for information): {n}",
  "task.aa_tour_discontinued": "Adventure Asia no longer offers an AA tour of this trip: offer another tour or check the booking",
};

export const tourRetiredVi: Record<keyof typeof tourRetiredEn, string> = {
  "tourRetired.title": "Tour AA ngừng bán",
  "tourRetired.badge": "Tour AA ngừng bán",
  "tourRetired.filter": "Tour AA ngừng bán",
  "tourRetired.help": "Adventure Asia không còn bán tour này. Không có gì bị thay đổi: các lựa chọn trong đề xuất, giữ chỗ và đặt chỗ vẫn giữ nguyên. Hãy đề xuất cho khách một tour khác, hoặc kiểm tra đặt chỗ với Adventure Asia.",
  "tourRetired.since": "Từ {date}",
  "tourRetired.unknownTour": "Một tour AA",
  "tourRetired.item.open_option": "Lựa chọn trong đề xuất: {n}",
  "tourRetired.item.held_line": "Dịch vụ AA đang giữ chỗ: {n}",
  "tourRetired.item.quoted_line": "Dịch vụ AA đã báo giá: {n}",
  "tourRetired.item.confirmed_line": "Dịch vụ AA đã đặt (để biết): {n}",
  "task.aa_tour_discontinued": "Adventure Asia không còn bán một tour AA của chuyến này: đề xuất tour khác hoặc kiểm tra đặt chỗ",
};
