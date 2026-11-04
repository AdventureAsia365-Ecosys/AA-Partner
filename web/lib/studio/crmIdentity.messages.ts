// web/lib/studio/crmIdentity.messages.ts
// AA-1036 (R3) — the "Signed in to AA Partner …" sentence of the studio (lib/studio/crmIdentity.ts signedInText) in
// English and Vietnamese. Role names match the CRM's (lib/i18n.ts role.*).
import { defineMessages } from "@/app/_kit/core/i18n";

export const IDENTITY_MESSAGES = defineMessages(
  {
    roleOwner: "Owner",
    roleAgent: "Agent",
    roleAssistant: "Assistant",
    noAgency: "Signed in to AA Partner, with no agency selected.",
    noAgencyAs: (email: string) => `Signed in to AA Partner as ${email}, with no agency selected.`,
    emailRole: (email: string, role: string, agency: string) => `Signed in to AA Partner as ${email} (${role} of ${agency}).`,
    emailFor: (email: string, agency: string) => `Signed in to AA Partner as ${email}, for ${agency}.`,
    role: (role: string, agency: string) => `Signed in to AA Partner as ${role} of ${agency}.`,
    for: (agency: string) => `Signed in to AA Partner for ${agency}.`,
  },
  {
    roleOwner: "Chủ đại lý",
    roleAgent: "Nhân viên tư vấn",
    roleAssistant: "Trợ lý",
    noAgency: "Đã đăng nhập AA Partner, chưa chọn đại lý.",
    noAgencyAs: (email: string) => `Đã đăng nhập AA Partner bằng ${email}, chưa chọn đại lý.`,
    emailRole: (email: string, role: string, agency: string) => `Đã đăng nhập AA Partner bằng ${email} (${role} của ${agency}).`,
    emailFor: (email: string, agency: string) => `Đã đăng nhập AA Partner bằng ${email}, cho ${agency}.`,
    role: (role: string, agency: string) => `Đã đăng nhập AA Partner với vai trò ${role} của ${agency}.`,
    for: (agency: string) => `Đã đăng nhập AA Partner cho ${agency}.`,
  },
);
