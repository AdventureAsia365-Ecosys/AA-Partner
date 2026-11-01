// web/lib/i18nTerms.ts
// AA-1033 — the EN / VI strings of the CRM terms of use page (/terms) and its links (shell footer, Settings > AI),
// spread into lib/i18n.ts (every key exists in both languages: tests/i18n.test.ts).
//
// Draft: today the page holds the "AI-generated content" section only, modelled on the Content studio terms §3
// (portal/app/terms/page.tsx). It states what the CRM does (drafts are suggestions, nothing is sent without a person,
// personal details are replaced before text reaches the model: CONTEXT.md "Settings > AI" / "Draft"), that AI can make
// mistakes, that a draft is for information, and that the confirmed quote and the booking terms are what binds. It
// never claims to exclude liability; the final legal wording is reviewed by counsel later.

export const termsEn = {
  "terms.title": "Terms of use",
  "terms.subtitle": "Draft, pending legal review. This version covers the use of AI in the CRM.",
  "terms.link": "Terms of use",
  "terms.ai.title": "AI-generated content",
  "terms.ai.drafts":
    "When your agency switches AI on (Settings > AI), the CRM can draft client replies and follow-ups with AI. AI drafts can contain mistakes.",
  "terms.ai.information":
    "A draft is for information only: a suggestion for your team. Nothing reaches a client until a person in your agency reviews it and sends it.",
  "terms.ai.check":
    "Before you send, check prices, dates, visa and entry rules, health and safety information against the confirmed quote and your booking terms. The CRM shows this reminder under every AI draft.",
  "terms.ai.binding":
    "What binds your agency and your client is the confirmed quote and the booking terms the client accepts, not an AI draft.",
  "terms.ai.models":
    "Drafts are written by AI models of third-party providers, called through Adventure Asia's AI gateway. Personal details (names, e-mail addresses, phone numbers, passport numbers) are replaced with placeholders before any text reaches the model.",
  "terms.ai.misuse":
    "Do not use AI drafts to give false or misleading information, in particular about visas and entry rules, health, safety, prices or availability.",
  "terms.ai.responsible": "Your agency reviews what it sends and is responsible for it.",
  "terms.ai.off": "Adventure Asia may switch AI drafts off for an agency that breaks these rules.",
  "terms.more": "The other terms of use of the CRM are being prepared and will be added to this page.",
} as const;

export const termsVi: Record<keyof typeof termsEn, string> = {
  "terms.title": "Điều khoản sử dụng",
  "terms.subtitle": "Bản nháp, đang chờ rà soát pháp lý. Phiên bản này nói về việc dùng AI trong CRM.",
  "terms.link": "Điều khoản sử dụng",
  "terms.ai.title": "Nội dung do AI tạo",
  "terms.ai.drafts":
    "Khi đại lý của bạn bật AI (Cài đặt > AI), CRM có thể dùng AI để soạn nháp câu trả lời và tin nhắn theo dõi cho khách. Bản nháp AI có thể có sai sót.",
  "terms.ai.information":
    "Bản nháp chỉ để tham khảo: một gợi ý cho đội của bạn. Không có gì đến khách cho đến khi một người trong đại lý xem lại và tự gửi.",
  "terms.ai.check":
    "Trước khi gửi, hãy đối chiếu giá, ngày, quy định visa và nhập cảnh, thông tin sức khỏe và an toàn với báo giá đã xác nhận và điều khoản đặt chỗ của bạn. CRM hiện lời nhắc này dưới mỗi bản nháp AI.",
  "terms.ai.binding":
    "Điều ràng buộc đại lý và khách của bạn là báo giá đã xác nhận và điều khoản đặt chỗ mà khách chấp nhận, không phải bản nháp AI.",
  "terms.ai.models":
    "Bản nháp được viết bởi các mô hình AI của nhà cung cấp bên thứ ba, gọi qua cổng AI của Adventure Asia. Thông tin cá nhân (tên, e-mail, số điện thoại, số hộ chiếu) được thay bằng ký hiệu trước khi văn bản đến mô hình.",
  "terms.ai.misuse":
    "Không dùng bản nháp AI để đưa thông tin sai hoặc gây hiểu lầm, nhất là về visa và quy định nhập cảnh, sức khỏe, an toàn, giá hoặc tình trạng còn chỗ.",
  "terms.ai.responsible": "Đại lý của bạn xem lại những gì mình gửi và chịu trách nhiệm về nội dung đó.",
  "terms.ai.off": "Adventure Asia có thể tắt bản nháp AI của đại lý vi phạm các quy định này.",
  "terms.more": "Các điều khoản sử dụng khác của CRM đang được soạn và sẽ được thêm vào trang này.",
};

/** The bullets of the terms' AI section, in order (rendered by app/(crm)/terms/page.tsx). */
export const AI_TERMS = [
  "terms.ai.drafts",
  "terms.ai.information",
  "terms.ai.check",
  "terms.ai.binding",
  "terms.ai.models",
  "terms.ai.misuse",
  "terms.ai.responsible",
  "terms.ai.off",
] as const satisfies readonly (keyof typeof termsEn)[];
