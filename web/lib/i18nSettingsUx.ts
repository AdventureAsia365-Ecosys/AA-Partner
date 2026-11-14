// web/lib/i18nSettingsUx.ts
// AA-1096 (S280 settings audit) — the EN / VI strings the settings pages gained: the per-form fields and quota of
// Settings > Forms, the refund approval threshold of Settings > Payments, a markup kept above AA's current limit, a
// stored price-check limit outside the range, and the refund limit amount an agent is held to. Spread into
// lib/i18n.ts (every key exists in both languages: tests/i18n.test.ts).

export const settingsUxEn = {
  "forms.error.invalid_origins":
    "Each website is https://host or https://host:port with no path (https://www.your-agency.com, not https://www.your-agency.com/contact), and a form takes at most 10.",
  "forms.error.invalid_privacy_notice_url": "The privacy link must start with https:// and contain no spaces.",
  "forms.error.invalid_owner": "Pick an active member of your agency.",
  "forms.error.other": "The form could not be saved. Check the values and try again.",
  "forms.privacyHelp": "Empty: the form shows your agency's privacy link from Settings > Connections.",
  "forms.owner": "New inquiries from this form go to",
  "forms.ownerNone": "No one (the inquiry waits unassigned)",
  "forms.saveDetails": "Save privacy link and owner",
  "forms.saved": "Saved.",
  "forms.quota": "Each form takes up to {n} submissions an hour from all visitors together; past that, new inquiries are refused until the next hour.",
  "forms.quotaOwn": "Adventure Asia set this limit for your agency.",
  "forms.quotaOff": "Your forms have no hourly limit on submissions.",
  "forms.quotaHelp": "If real inquiries are refused, contact Adventure Asia support to raise it.",
  "payments.unavailable": "This section could not be loaded. Reload the page in a minute.",
  "payments.threshold": "Refund approval limit ({currency})",
  "payments.thresholdHelp":
    "An agent may approve a refund, or lower a cancellation charge, up to this amount in your base currency; above it, only the owner can. The number is kept as it is when you change the base currency, so check it after a change.",
  "payments.thresholdInvalid": "Enter an amount from 0 to 10,000,000 with at most 2 decimals.",
  "markup.aboveLimit": "Above AA's current limit of {max} %: it stays until you change it.",
  "markup.errorKind": "{kind}: enter a percentage from 0 to {max} with at most 2 decimals, or leave it empty.",
  "onlineBooking.outsideRange": "Your saved limit ({value}) is outside the range AA allows now ({min} to {max}). It still applies until you change it.",
  "invoice.error.above_threshold_limit":
    "This refund is above your approval limit of {limit}. Ask the owner to approve it.",
  "cancelCharge.needsOwnerLimit":
    "You can lower the charge by up to {limit} (your refund approval limit). Lowering it more is the agency owner's decision.",
} as const;

export const settingsUxVi: Record<keyof typeof settingsUxEn, string> = {
  "forms.error.invalid_origins":
    "Mỗi website là https://tên-miền hoặc https://tên-miền:cổng, không có đường dẫn phía sau (https://www.dai-ly-cua-ban.vn, không phải https://www.dai-ly-cua-ban.vn/lien-he), và mỗi biểu mẫu tối đa 10 website.",
  "forms.error.invalid_privacy_notice_url": "Đường dẫn quyền riêng tư phải bắt đầu bằng https:// và không có khoảng trắng.",
  "forms.error.invalid_owner": "Hãy chọn một thành viên đang hoạt động của đại lý.",
  "forms.error.other": "Không lưu được biểu mẫu. Hãy kiểm tra lại các giá trị và thử lại.",
  "forms.privacyHelp": "Để trống: biểu mẫu hiện đường dẫn quyền riêng tư của đại lý ở Cài đặt > Kết nối.",
  "forms.owner": "Yêu cầu mới từ biểu mẫu này giao cho",
  "forms.ownerNone": "Không ai (yêu cầu chờ chưa giao)",
  "forms.saveDetails": "Lưu đường dẫn quyền riêng tư và người phụ trách",
  "forms.saved": "Đã lưu.",
  "forms.quota": "Mỗi biểu mẫu nhận tối đa {n} lượt gửi mỗi giờ, tính chung mọi khách; vượt mức này, yêu cầu mới bị từ chối tới giờ sau.",
  "forms.quotaOwn": "Adventure Asia đặt giới hạn này riêng cho đại lý của bạn.",
  "forms.quotaOff": "Biểu mẫu của bạn không giới hạn số lượt gửi mỗi giờ.",
  "forms.quotaHelp": "Nếu yêu cầu thật bị từ chối, hãy liên hệ bộ phận hỗ trợ của Adventure Asia để tăng giới hạn.",
  "payments.unavailable": "Không tải được phần này. Hãy tải lại trang sau một phút.",
  "payments.threshold": "Hạn mức duyệt hoàn tiền ({currency})",
  "payments.thresholdHelp":
    "Nhân viên được duyệt khoản hoàn tiền, hoặc giảm phí huỷ, tới số tiền này theo tiền tệ gốc; vượt mức này chỉ chủ đại lý duyệt được. Con số được giữ nguyên khi bạn đổi tiền tệ gốc, nên hãy kiểm tra lại sau khi đổi.",
  "payments.thresholdInvalid": "Nhập số tiền từ 0 đến 10.000.000, tối đa 2 chữ số thập phân.",
  "markup.aboveLimit": "Vượt giới hạn hiện tại của AA là {max} %: mức này giữ nguyên cho tới khi bạn đổi.",
  "markup.errorKind": "{kind}: nhập phần trăm từ 0 đến {max}, tối đa 2 chữ số thập phân, hoặc để trống.",
  "onlineBooking.outsideRange": "Giới hạn bạn đã lưu ({value}) nằm ngoài khoảng AA cho phép hiện nay ({min} đến {max}). Giới hạn này vẫn áp dụng cho tới khi bạn đổi.",
  "invoice.error.above_threshold_limit": "Khoản hoàn này vượt hạn mức duyệt {limit} của bạn. Hãy nhờ chủ doanh nghiệp duyệt.",
  "cancelCharge.needsOwnerLimit":
    "Bạn được giảm phí tối đa {limit} (hạn mức duyệt hoàn tiền của bạn). Giảm nhiều hơn là quyết định của chủ đại lý.",
};
