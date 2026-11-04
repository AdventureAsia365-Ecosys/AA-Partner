// web/lib/studio/lib.messages.ts
// AA-1036 (R3) — the text the studio's pure helpers (lib/studio/*.ts) put on screen, in English and Vietnamese (kit
// i18n). Each helper takes the UI language ("en" by default) and reads its catalogue here, so the English stays the
// one the unit tests pin and Vietnamese appears only when the person picked it.
import { defineMessages } from "@/app/_kit/core/i18n";

/** lib/studio/masterNotice.ts */
export const MASTER_MESSAGES = defineMessages(
  {
    label: "Updated by Adventure Asia",
    notice: (on: string) =>
      `Adventure Asia updated this tour${on ? ` on ${on}` : ""}, after your version. Your text was not changed. Rewrite it if you want the new details.`,
  },
  {
    label: "Adventure Asia đã cập nhật",
    notice: (on: string) =>
      `Adventure Asia đã cập nhật tour này${on ? ` vào ${on}` : ""}, sau phiên bản của bạn. Nội dung của bạn không bị thay đổi. Hãy viết lại nếu bạn muốn dùng thông tin mới.`,
  },
);

/** lib/studio/account.ts */
export const ACCOUNT_LIB_MESSAGES = defineMessages(
  {
    actorYou: "You",
    actorAa: "Adventure Asia",
    actorSystem: "Automatic",
    fmtText: "Plain text",
    fmtHtml: "HTML file",
    fmtMarkdown: "Markdown",
    fmtDocx: "Word document",
    fmtPdf: "PDF",
    fmtJson: "JSON file",
    fmtCsv: "CSV file",
    fmtXlsx: "Excel file",
    healthNotConnected: "Not connected",
    healthHealthy: "Working",
    healthFailing: "Needs attention",
    healthUntested: "Not tested yet",
  },
  {
    actorYou: "Bạn",
    actorAa: "Adventure Asia",
    actorSystem: "Tự động",
    fmtText: "Văn bản thường",
    fmtHtml: "Tệp HTML",
    fmtMarkdown: "Markdown",
    fmtDocx: "Tài liệu Word",
    fmtPdf: "PDF",
    fmtJson: "Tệp JSON",
    fmtCsv: "Tệp CSV",
    fmtXlsx: "Tệp Excel",
    healthNotConnected: "Chưa kết nối",
    healthHealthy: "Đang hoạt động",
    healthFailing: "Cần kiểm tra",
    healthUntested: "Chưa thử kết nối",
  },
);

/** lib/studio/members.ts */
export const MEMBERS_LIB_MESSAGES = defineMessages(
  {
    roleOwner: "Owner",
    roleEditor: "Editor",
    roleViewer: "Viewer",
    hintOwner: "Manages the workspace, brand and connections",
    hintEditor: "Writes, edits and publishes content",
    hintViewer: "Reads content and reports",
    statusActive: "Active",
    statusInvited: "Invitation sent",
    invited: "Invited (not signed in yet)",
    member: (ref: string) => `Member ${ref}`,
    none: "No members yet",
    active: (n: number) => `${n} active member${n === 1 ? "" : "s"}`,
    pending: (n: number) => `${n} invitation${n === 1 ? "" : "s"} pending`,
  },
  {
    roleOwner: "Chủ sở hữu",
    roleEditor: "Biên tập viên",
    roleViewer: "Người xem",
    hintOwner: "Quản lý không gian làm việc, thương hiệu và các kết nối",
    hintEditor: "Viết, chỉnh sửa và đăng nội dung",
    hintViewer: "Xem nội dung và báo cáo",
    statusActive: "Đang hoạt động",
    statusInvited: "Đã gửi lời mời",
    invited: "Đã mời (chưa đăng nhập)",
    member: (ref: string) => `Thành viên ${ref}`,
    none: "Chưa có thành viên",
    active: (n: number) => `${n} thành viên đang hoạt động`,
    pending: (n: number) => `${n} lời mời đang chờ`,
  },
);

/** lib/studio/tours.ts */
export const TOURS_LIB_MESSAGES = defineMessages(
  {
    days: (n: number) => `${n} day${n === 1 ? "" : "s"}`,
    nights: (n: number) => `${n} night${n === 1 ? "" : "s"}`,
    priceNote: (pax: number) => `Suggested price for ${pax} traveller${pax === 1 ? "" : "s"}. Your advisor confirms the final price.`,
    durAll: "Any length",
    dur13: "1–3 days",
    dur47: "4–7 days",
    dur8: "8+ days",
    actAdventure: "Adventure sports",
    actCulture: "Culture & heritage",
    actFood: "Food & cooking",
    actLocal: "Local life",
    actTrekking: "Trekking & hiking",
    actWater: "On the water",
    actWellness: "Wellness & relaxation",
    actWildlife: "Wildlife & nature",
    intEasy: "Easy",
    intModerate: "Moderate",
    intChallenging: "Challenging",
    intStrenuous: "Strenuous",
    addedAll: "Added any time",
    added14: "Added in the last 14 days",
    added30: "Added in the last 30 days",
    added90: "Added in the last 90 days",
    rate: (price: string) => ` at ${price} each`,
    quotaNoPlan: "No plan attached yet: rewrites are not billed.",
    quotaUnlimited: "Your plan has no rewrite limit.",
    quotaUsedUpOne: (rate: string) => `Your plan's rewrites are used up: this rewrite is billed as overage${rate}.`,
    quotaUsedUpMany: (n: number, rate: string) => `Your plan's rewrites are used up: these ${n} rewrites are billed as overage${rate}.`,
    quotaPartly: (left: number, extra: number, n: number, rate: string) =>
      `${left} rewrite${left === 1 ? "" : "s"} left this period: ${extra} of these ${n} ${extra === 1 ? "is" : "are"} billed as overage${rate}.`,
    quotaLeft: (left: number) => `${left} rewrite${left === 1 ? "" : "s"} left this period.`,
  },
  {
    days: (n: number) => `${n} ngày`,
    nights: (n: number) => `${n} đêm`,
    priceNote: (pax: number) => `Giá đề xuất cho ${pax} khách. Tư vấn viên sẽ xác nhận giá cuối cùng.`,
    durAll: "Mọi độ dài",
    dur13: "1–3 ngày",
    dur47: "4–7 ngày",
    dur8: "Từ 8 ngày",
    actAdventure: "Thể thao mạo hiểm",
    actCulture: "Văn hoá & di sản",
    actFood: "Ẩm thực & nấu ăn",
    actLocal: "Đời sống địa phương",
    actTrekking: "Trekking & đi bộ đường dài",
    actWater: "Trên mặt nước",
    actWellness: "Chăm sóc sức khoẻ & thư giãn",
    actWildlife: "Thiên nhiên & động vật hoang dã",
    intEasy: "Nhẹ nhàng",
    intModerate: "Vừa phải",
    intChallenging: "Thử thách",
    intStrenuous: "Rất vất vả",
    addedAll: "Thêm vào bất kỳ lúc nào",
    added14: "Thêm trong 14 ngày qua",
    added30: "Thêm trong 30 ngày qua",
    added90: "Thêm trong 90 ngày qua",
    rate: (price: string) => `, ${price} mỗi lượt`,
    quotaNoPlan: "Chưa gắn gói dịch vụ: các lượt viết lại không bị tính phí.",
    quotaUnlimited: "Gói của bạn không giới hạn lượt viết lại.",
    quotaUsedUpOne: (rate: string) => `Gói của bạn đã hết lượt viết lại: lượt này được tính phí vượt hạn mức${rate}.`,
    quotaUsedUpMany: (n: number, rate: string) => `Gói của bạn đã hết lượt viết lại: ${n} lượt này được tính phí vượt hạn mức${rate}.`,
    quotaPartly: (left: number, extra: number, n: number, rate: string) =>
      `Còn ${left} lượt viết lại trong kỳ này: ${extra} trong ${n} lượt sẽ được tính phí vượt hạn mức${rate}.`,
    quotaLeft: (left: number) => `Còn ${left} lượt viết lại trong kỳ này.`,
  },
);
