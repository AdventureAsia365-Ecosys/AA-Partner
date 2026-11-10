// web/app/(studio)/_legal/legal.messages.ts
// AA-1036 (R3) — the frame of the studio's draft legal pages (draft banner, version line, footer links) in English and
// Vietnamese. The documents themselves are ../legal/*/<name>.messages.ts.
import { defineMessages } from "@/app/_kit/core/i18n";

export const LEGAL_MESSAGES = defineMessages(
  {
    draftLabel: "Draft — pending legal review",
    draftBody:
      "This is a working draft. Adventure Asia's legal counsel has not reviewed or approved it, and it may change before the Partner Portal leaves its development phase. Parts marked [to be confirmed] are still open. Questions:",
    translationNote: "",
    logoAlt: "Adventure Asia",
    versionLine: (version: string, updated: string) => `Adventure Asia Partner Portal · ${version} · last updated ${updated}`,
    version: "Draft v0.6",
    updated: "10 November 2026",
    nav: "Legal",
    privacy: "Privacy notice",
    terms: "Partner terms",
    login: "Partner login",
  },
  {
    draftLabel: "Bản nháp — đang chờ rà soát pháp lý",
    draftBody:
      "Đây là bản nháp đang soạn. Bộ phận pháp lý của Adventure Asia chưa rà soát hay phê duyệt, và nội dung có thể thay đổi trước khi Cổng đối tác kết thúc giai đoạn phát triển. Các phần ghi [cần xác nhận] vẫn đang bỏ ngỏ. Câu hỏi xin gửi về:",
    translationNote:
      "Bản tiếng Việt này là bản dịch để tham khảo. Nếu có khác biệt, bản tiếng Anh được áp dụng.",
    logoAlt: "Adventure Asia",
    versionLine: (version: string, updated: string) => `Cổng đối tác Adventure Asia · ${version} · cập nhật lần cuối ${updated}`,
    version: "Bản nháp v0.6",
    updated: "10 tháng 11 năm 2026",
    nav: "Pháp lý",
    privacy: "Thông báo quyền riêng tư",
    terms: "Điều khoản đối tác",
    login: "Đăng nhập đối tác",
  },
);
