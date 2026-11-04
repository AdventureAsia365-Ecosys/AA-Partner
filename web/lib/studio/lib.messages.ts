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
