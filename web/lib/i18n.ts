// web/lib/i18n.ts
// AA-935 — EN/VI (card CRM-07 "EN/VI"; design v3 §2.3 "Language and dates"). Two levels:
//
//   - the date locale (en-AU, en-GB, en-US, vi-VN; lib/dates.ts): how dates are written and read;
//   - the UI language (en, vi): derived from it (vi-VN -> vi, every en-* -> en).
//
// Resolution order: the person's own choice (cookie aa_crm_locale, set on Settings) -> the agency's default
// (crm.tenant.default_locale, crm-004) -> the browser's Accept-Language -> en-AU. Strings are plain objects, no
// library; every key exists in both languages (a unit test checks it). Edge-safe.

import { DATE_LOCALES, isDateLocale, type DateLocale } from "./dates";

export type Lang = "en" | "vi";
export const LOCALE_COOKIE = "aa_crm_locale";
export const DEFAULT_LOCALE: DateLocale = "en-AU";

export function langOf(locale: DateLocale): Lang {
  return locale === "vi-VN" ? "vi" : "en";
}

/** Best date locale for an Accept-Language header, or null. */
export function fromAcceptLanguage(header: string | null | undefined): DateLocale | null {
  if (!header) return null;
  const tags = header
    .split(",")
    .map((part) => {
      const [tag, ...params] = part.trim().split(";");
      const q = params.map((p) => p.trim()).find((p) => p.startsWith("q="));
      return { tag: tag.trim().toLowerCase(), q: q ? Number(q.slice(2)) || 0 : 1 };
    })
    .filter((t) => t.tag && t.q > 0)
    .sort((x, y) => y.q - x.q);
  for (const { tag } of tags) {
    const exact = DATE_LOCALES.find((l) => l.toLowerCase() === tag);
    if (exact) return exact;
    if (tag === "vi" || tag.startsWith("vi-")) return "vi-VN";
    if (tag === "en-us") return "en-US";
    if (tag === "en-gb" || tag === "en-ie") return "en-GB";
    if (tag === "en" || tag.startsWith("en-")) return "en-AU";
  }
  return null;
}

export function resolveLocale(input: {
  cookie?: string | null;
  agencyDefault?: string | null;
  acceptLanguage?: string | null;
}): DateLocale {
  if (isDateLocale(input.cookie)) return input.cookie;
  if (isDateLocale(input.agencyDefault)) return input.agencyDefault;
  return fromAcceptLanguage(input.acceptLanguage) ?? DEFAULT_LOCALE;
}

const en = {
  "app.name": "Adventure Asia Partner",
  "app.short": "AA Partner",
  "nav.home": "Home",
  "nav.group.crm": "CRM",
  "nav.group.money": "Money",
  "nav.group.grow": "Grow",
  "nav.group.studio": "Content studio",
  "nav.pipeline": "Pipeline",
  "nav.clients": "Clients",
  "nav.inbox": "Inbox",
  "nav.tasks": "Tasks",
  "nav.payments": "Payments",
  "nav.bookings": "Bookings",
  "nav.commissions": "Commissions",
  "nav.reports": "Reports",
  "nav.marketing": "Marketing",
  "nav.suppliers": "Suppliers",
  "nav.library": "Library",
  "nav.automations": "Automations",
  "nav.studio": "Open Content studio",
  "nav.settings": "Settings",
  "nav.help": "Help & what's new",
  "shell.menu": "Menu",
  "shell.closeMenu": "Close menu",
  "shell.switchAgency": "Switch agency",
  "shell.signOut": "Sign out",
  "shell.search": "Search clients, trips, bookings",
  "shell.searchSoon": "Search arrives with the first client records",
  "shell.theme": "Theme",
  "shell.language": "Language and dates",
  "shell.synthetic": "Test agency",
  "role.owner": "Owner",
  "role.agent": "Agent",
  "role.assistant": "Assistant",
  "home.title": "Welcome",
  "home.subtitle": "Your agency's CRM. The first pieces are in place; pipeline, clients and messages follow.",
  "home.checklist": "Getting started",
  "home.check.signin": "Sign in to the CRM",
  "home.check.agency": "Open your agency",
  "home.check.inquiry": "Receive your first inquiry",
  "home.check.studio": "Publish a tour from Content studio",
  "home.needs": "Needs you",
  "home.needsEmpty": "Nothing needs you yet. New inquiries, holds and payments will show here.",
  "section.pipeline.title": "No trips yet",
  "section.pipeline.body": "Inquiries from your website form land here as cards on the board.",
  "section.clients.title": "No clients yet",
  "section.clients.body": "Every traveller who sends an inquiry becomes a client record you control.",
  "section.inbox.title": "Inbox is empty",
  "section.inbox.body": "E-mail, WhatsApp and SMS from your clients, with suggested replies, will appear here.",
  "section.tasks.title": "No tasks",
  "section.tasks.body": "Follow-ups, deadlines and reminders for your trips will be listed here.",
  "section.payments.title": "No payments yet",
  "section.payments.body": "Deposits, schedules and reminders for each trip will be tracked here.",
  "section.bookings.title": "No bookings yet",
  "section.bookings.body": "Adventure Asia lines and your own suppliers' bookings, across all trips.",
  "section.commissions.title": "No commissions yet",
  "section.commissions.body": "What you earn per trip and per supplier.",
  "section.reports.title": "No reports yet",
  "section.reports.body": "Pipeline, conversion and revenue reports once trips are in.",
  "section.marketing.title": "No campaigns yet",
  "section.marketing.body": "Ready-to-travel-again lists, special dates and campaigns to past clients.",
  "section.suppliers.title": "Suppliers",
  "section.suppliers.body": "Adventure Asia is built in. Add the hotels, guides and transport you book yourself.",
  "section.library.title": "Library is empty",
  "section.library.body": "Itinerary items, task lists, message templates, forms and travel packs.",
  "section.automations.title": "No automations yet",
  "section.automations.body": "Rules that send, remind and assign for you.",
  "section.help.title": "Help & what's new",
  "section.help.body": "Guides and release notes will be collected here.",
  "section.forbidden.title": "Not available for your role",
  "section.forbidden.body": "Ask an owner of the agency if you need this page.",
  "settings.title": "Settings",
  "settings.language": "Language and dates",
  "settings.languageHelp": "Choose how dates are written. Dates are always shown with the month spelled out.",
  "settings.save": "Save",
  "settings.saved": "Saved",
  "settings.tryDate": "Try a date",
  "settings.tryDateHelp": "Type a date the way you usually write it.",
  "settings.readAs": "Read as",
  "settings.invalidDate": "Not a date",
  "settings.agency": "Agency",
  "settings.role": "Your role",
  "settings.more": "Team, channels, payments, forms, AI, data and privacy arrive with the next cards.",
  "select.title": "Choose an agency",
  "select.subtitle": "You belong to more than one agency. Pick the one to open.",
  "select.none": "Your account is not a member of any agency yet",
  "select.noneHelp": "Ask the agency owner to invite you, or contact Adventure Asia support.",
  "select.open": "Open",
  "select.locked": "Locked",
  "error.api": "The CRM service is not answering right now. Try again in a minute.",
  "error.retry": "Try again",
  "error.session": "The CRM service did not accept your sign-in. Sign in again, or contact Adventure Asia support if this keeps happening.",
  "auth.signedOut": "You are signed out",
  "auth.signedOutHelp": "Thanks for using the Adventure Asia Partner CRM.",
  "auth.signIn": "Sign in",
  "auth.unavailable": "Sign-in is not configured on this server yet.",
  "auth.failed": "Sign-in did not complete. Please try again.",
  "health.title": "Service health",
  "health.web": "Web",
  "health.api": "API",
  "health.ready": "API ready",
  "health.ok": "OK",
  "health.down": "Not answering",
  "health.notReady": "Not ready",
  "client.title": "Adventure Asia",
  "client.body": "This page is not available yet.",
} as const;

export type MessageKey = keyof typeof en;

const vi: Record<MessageKey, string> = {
  "app.name": "Adventure Asia Partner",
  "app.short": "AA Partner",
  "nav.home": "Trang chủ",
  "nav.group.crm": "CRM",
  "nav.group.money": "Tài chính",
  "nav.group.grow": "Phát triển",
  "nav.group.studio": "Content studio",
  "nav.pipeline": "Quy trình bán",
  "nav.clients": "Khách hàng",
  "nav.inbox": "Hộp thư",
  "nav.tasks": "Công việc",
  "nav.payments": "Thanh toán",
  "nav.bookings": "Đặt chỗ",
  "nav.commissions": "Hoa hồng",
  "nav.reports": "Báo cáo",
  "nav.marketing": "Tiếp thị",
  "nav.suppliers": "Nhà cung cấp",
  "nav.library": "Thư viện",
  "nav.automations": "Tự động hoá",
  "nav.studio": "Mở Content studio",
  "nav.settings": "Cài đặt",
  "nav.help": "Trợ giúp và tính năng mới",
  "shell.menu": "Menu",
  "shell.closeMenu": "Đóng menu",
  "shell.switchAgency": "Đổi đại lý",
  "shell.signOut": "Đăng xuất",
  "shell.search": "Tìm khách hàng, chuyến đi, đặt chỗ",
  "shell.searchSoon": "Tìm kiếm có khi có hồ sơ khách hàng đầu tiên",
  "shell.theme": "Giao diện",
  "shell.language": "Ngôn ngữ và ngày tháng",
  "shell.synthetic": "Đại lý thử nghiệm",
  "role.owner": "Chủ đại lý",
  "role.agent": "Nhân viên tư vấn",
  "role.assistant": "Trợ lý",
  "home.title": "Xin chào",
  "home.subtitle": "CRM của đại lý bạn. Phần nền đã sẵn sàng; quy trình bán, khách hàng và tin nhắn sẽ có tiếp theo.",
  "home.checklist": "Bắt đầu",
  "home.check.signin": "Đăng nhập CRM",
  "home.check.agency": "Mở đại lý của bạn",
  "home.check.inquiry": "Nhận yêu cầu đầu tiên",
  "home.check.studio": "Đăng một tour từ Content studio",
  "home.needs": "Cần bạn xử lý",
  "home.needsEmpty": "Chưa có việc cần bạn. Yêu cầu mới, giữ chỗ và thanh toán sẽ hiện ở đây.",
  "section.pipeline.title": "Chưa có chuyến đi",
  "section.pipeline.body": "Yêu cầu từ biểu mẫu trên website của bạn sẽ hiện thành thẻ trên bảng.",
  "section.clients.title": "Chưa có khách hàng",
  "section.clients.body": "Mỗi khách gửi yêu cầu sẽ thành một hồ sơ khách hàng do bạn quản lý.",
  "section.inbox.title": "Hộp thư trống",
  "section.inbox.body": "E-mail, WhatsApp và SMS từ khách, kèm gợi ý trả lời, sẽ hiện ở đây.",
  "section.tasks.title": "Chưa có công việc",
  "section.tasks.body": "Việc cần theo dõi, hạn chót và nhắc việc cho các chuyến đi sẽ có ở đây.",
  "section.payments.title": "Chưa có thanh toán",
  "section.payments.body": "Tiền cọc, lịch thanh toán và nhắc thanh toán của từng chuyến đi.",
  "section.bookings.title": "Chưa có đặt chỗ",
  "section.bookings.body": "Dịch vụ của Adventure Asia và của nhà cung cấp riêng, trên mọi chuyến đi.",
  "section.commissions.title": "Chưa có hoa hồng",
  "section.commissions.body": "Khoản bạn nhận theo từng chuyến đi và từng nhà cung cấp.",
  "section.reports.title": "Chưa có báo cáo",
  "section.reports.body": "Báo cáo quy trình bán, tỉ lệ chốt và doanh thu khi đã có chuyến đi.",
  "section.marketing.title": "Chưa có chiến dịch",
  "section.marketing.body": "Danh sách khách sẵn sàng đi tiếp, ngày đặc biệt và chiến dịch cho khách cũ.",
  "section.suppliers.title": "Nhà cung cấp",
  "section.suppliers.body": "Adventure Asia có sẵn. Thêm khách sạn, hướng dẫn viên và xe bạn tự đặt.",
  "section.library.title": "Thư viện trống",
  "section.library.body": "Hạng mục lịch trình, danh sách việc, mẫu tin nhắn, biểu mẫu và gói thông tin chuyến đi.",
  "section.automations.title": "Chưa có tự động hoá",
  "section.automations.body": "Quy tắc tự gửi, tự nhắc và tự giao việc thay bạn.",
  "section.help.title": "Trợ giúp và tính năng mới",
  "section.help.body": "Hướng dẫn và ghi chú phát hành sẽ được tập hợp ở đây.",
  "section.forbidden.title": "Vai trò của bạn không xem được trang này",
  "section.forbidden.body": "Hãy hỏi chủ đại lý nếu bạn cần trang này.",
  "settings.title": "Cài đặt",
  "settings.language": "Ngôn ngữ và ngày tháng",
  "settings.languageHelp": "Chọn cách viết ngày. Ngày luôn hiển thị với tên tháng.",
  "settings.save": "Lưu",
  "settings.saved": "Đã lưu",
  "settings.tryDate": "Thử nhập ngày",
  "settings.tryDateHelp": "Gõ ngày theo cách bạn thường viết.",
  "settings.readAs": "Hiểu là",
  "settings.invalidDate": "Không phải ngày hợp lệ",
  "settings.agency": "Đại lý",
  "settings.role": "Vai trò của bạn",
  "settings.more": "Nhóm, kênh liên lạc, thanh toán, biểu mẫu, AI, dữ liệu và quyền riêng tư sẽ có ở các phiên bản sau.",
  "select.title": "Chọn đại lý",
  "select.subtitle": "Bạn thuộc nhiều đại lý. Hãy chọn đại lý muốn mở.",
  "select.none": "Tài khoản của bạn chưa thuộc đại lý nào",
  "select.noneHelp": "Hãy nhờ chủ đại lý mời bạn, hoặc liên hệ bộ phận hỗ trợ của Adventure Asia.",
  "select.open": "Mở",
  "select.locked": "Đã khoá",
  "error.api": "Dịch vụ CRM chưa phản hồi. Vui lòng thử lại sau một phút.",
  "error.retry": "Thử lại",
  "error.session": "Dịch vụ CRM không chấp nhận phiên đăng nhập của bạn. Hãy đăng nhập lại, hoặc liên hệ bộ phận hỗ trợ của Adventure Asia nếu lỗi lặp lại.",
  "auth.signedOut": "Bạn đã đăng xuất",
  "auth.signedOutHelp": "Cảm ơn bạn đã dùng CRM Adventure Asia Partner.",
  "auth.signIn": "Đăng nhập",
  "auth.unavailable": "Máy chủ này chưa được cấu hình đăng nhập.",
  "auth.failed": "Đăng nhập chưa hoàn tất. Vui lòng thử lại.",
  "health.title": "Tình trạng dịch vụ",
  "health.web": "Web",
  "health.api": "API",
  "health.ready": "API sẵn sàng",
  "health.ok": "Ổn",
  "health.down": "Không phản hồi",
  "health.notReady": "Chưa sẵn sàng",
  "client.title": "Adventure Asia",
  "client.body": "Trang này chưa có.",
};

export const MESSAGES: Record<Lang, Record<MessageKey, string>> = { en, vi };

export function t(lang: Lang, key: MessageKey): string {
  return MESSAGES[lang][key] ?? MESSAGES.en[key];
}

export function dictionary(lang: Lang): Record<MessageKey, string> {
  return MESSAGES[lang];
}
