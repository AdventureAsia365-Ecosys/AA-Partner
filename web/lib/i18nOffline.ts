// web/lib/i18nOffline.ts
// AA-954 (card CRM-24b, R16) — the EN / VI strings of the installable, offline traveller page (design v3 C-09, WF-8
// "ITINERARY (works offline)", §2.9 rule 7), kept in their own file and spread into lib/i18n.ts (every key exists in
// both languages: tests/i18n.test.ts). Every key starts with "client." so it reaches the client page
// (lib/clientMessages.ts).

export const offlineEn = {
  "client.offline.title": "You are offline",
  "client.offline.lastUpdated": "Saved on this device. Last updated {when}.",
  "client.offline.onlineOnly": "Documents, your details, signing and payments open only with a connection.",
  "client.offline.contactTitle": "Your travel agent",
  "client.offline.email": "E-mail {agency}",
  "client.offline.retry": "Try again",
  "client.offline.forget": "Remove from this device",
  "client.offline.forgotten": "Your itinerary was removed from this device.",
  "client.offline.nothing": "You are offline and this device has no saved itinerary. Open your trip page again when you are connected.",
  "client.offline.banner": "You are offline. Your itinerary stays on this device; everything else needs a connection.",
  "client.offline.kept": "Your itinerary also works offline on this device until you sign out.",
  "client.error.offline": "You are offline. Please try again when you are connected.",
  "client.install.button": "Install trip app",
  "client.install.help": "Keep your itinerary on your phone's home screen. It opens without a connection too.",
  "client.install.ios": "On iPhone or iPad: tap Share, then Add to Home Screen.",
  "client.install.done": "The trip app is installed on this device.",
  "client.manifest.name": "My trip",
  "client.manifest.description": "Your trip itinerary from your travel agent, also without a connection.",
} as const;

export const offlineVi: Record<keyof typeof offlineEn, string> = {
  "client.offline.title": "Bạn đang ngoại tuyến",
  "client.offline.lastUpdated": "Đã lưu trên thiết bị này. Cập nhật lần cuối {when}.",
  "client.offline.onlineOnly": "Tài liệu, thông tin cá nhân, ký và thanh toán chỉ mở được khi có kết nối.",
  "client.offline.contactTitle": "Đại lý du lịch của bạn",
  "client.offline.email": "Gửi e-mail cho {agency}",
  "client.offline.retry": "Thử lại",
  "client.offline.forget": "Xoá khỏi thiết bị này",
  "client.offline.forgotten": "Lịch trình đã được xoá khỏi thiết bị này.",
  "client.offline.nothing": "Bạn đang ngoại tuyến và thiết bị này chưa lưu lịch trình nào. Hãy mở lại trang chuyến đi khi có kết nối.",
  "client.offline.banner": "Bạn đang ngoại tuyến. Lịch trình vẫn có trên thiết bị này; các phần khác cần kết nối.",
  "client.offline.kept": "Lịch trình cũng xem được khi ngoại tuyến trên thiết bị này cho tới khi bạn đăng xuất.",
  "client.error.offline": "Bạn đang ngoại tuyến. Vui lòng thử lại khi có kết nối.",
  "client.install.button": "Cài ứng dụng chuyến đi",
  "client.install.help": "Đưa lịch trình ra màn hình chính của điện thoại. Mở được cả khi không có kết nối.",
  "client.install.ios": "Trên iPhone hoặc iPad: chạm Chia sẻ, rồi Thêm vào màn hình chính.",
  "client.install.done": "Ứng dụng chuyến đi đã được cài trên thiết bị này.",
  "client.manifest.name": "Chuyến đi của tôi",
  "client.manifest.description": "Lịch trình chuyến đi từ đại lý du lịch của bạn, xem được cả khi không có kết nối.",
};
