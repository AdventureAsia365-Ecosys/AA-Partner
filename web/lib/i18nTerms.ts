// web/lib/i18nTerms.ts
// AA-1033 — the EN / VI strings of the CRM terms of use page (/legal/terms, lib/legal.ts) and its links (shell footer, Settings > AI),
// spread into lib/i18n.ts (every key exists in both languages: tests/i18n.test.ts).
//
// Draft: today the page holds the "AI-generated content" section only, modelled on the Content studio terms §3
// (portal/app/terms/page.tsx). It states what the CRM does (drafts are suggestions, nothing is sent without a person,
// the F3 scrubber replaces the personal details it knows for the trip and those it detects before text reaches the
// model, best effort, so it never promises that every detail is caught: CONTEXT.md "Settings > AI" / "Draft"), that AI
// can make mistakes, that a draft is for information, and that the confirmed quote and the booking terms are what binds. It
// never claims to exclude liability; the final legal wording is reviewed by counsel later.
//
// AA-820 (S266): the section also summarises the AI Schedule v1.0 (root docs/legal/tenant-ai-schedule.md): who runs the
// models (Amazon Bedrock, Adventure Asia's own accounts), the providers' usage policies (links from lib/aiTerms.ts, shown
// after "terms.ai.policies"), the prohibited uses, the high-risk advice rule for ancillaries (insurance, visas,
// financing: root ADR 0019 b6, CONTEXT.md "AI boundary for high-risk advice") and which text binds.

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
    "Drafts are written by AI models of third-party providers, such as Anthropic's Claude, on Amazon Bedrock (Amazon Web Services), called through Adventure Asia's AI gateway on accounts Adventure Asia holds in its own company name. Your agency gets no account or key with these providers. Before text reaches the model, the CRM replaces with placeholders the personal details it knows for the trip's clients and travellers (names, e-mail addresses, phone numbers, passport numbers) and the details it can detect. It may not catch every detail, so do not add personal data a draft does not need.",
  "terms.ai.policies":
    "These providers' usage policies apply to your agency as a user of a service built on their models. Follow them, as well as these terms:",
  "terms.ai.misuse":
    "Do not use AI drafts to give false or misleading information, in particular about visas and entry rules, health, safety, prices or availability.",
  "terms.ai.prohibited":
    "Do not use AI drafts for fake reviews or testimonials, invented personas, hidden sponsorship, spam, or to build profiles of people without their knowledge.",
  "terms.ai.highRisk":
    "Insurance, visas, financing and other high-risk matters: do not use an AI draft to advise a specific client on them (for example which insurance to buy, whether they need a visa, or an instalment plan) unless a qualified person in your agency checks the advice first and the client is told that AI was used. General travel information is not advice.",
  "terms.ai.responsible": "Your agency reviews what it sends and is responsible for it.",
  "terms.ai.off": "Adventure Asia may switch AI drafts off for an agency that breaks these rules.",
  "terms.ai.schedule":
    "This section summarises the AI Schedule v1.0 attached to your agency's agreement with Adventure Asia. If they differ, the AI Schedule applies.",
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
    "Bản nháp được viết bởi các mô hình AI của nhà cung cấp bên thứ ba, ví dụ Claude của Anthropic, trên Amazon Bedrock (Amazon Web Services), gọi qua cổng AI của Adventure Asia bằng các tài khoản đứng tên công ty Adventure Asia. Đại lý của bạn không nhận tài khoản hay khóa nào của các nhà cung cấp này. Trước khi văn bản đến mô hình, CRM thay bằng ký hiệu những thông tin cá nhân mà CRM biết về khách và người đi trong chuyến (tên, e-mail, số điện thoại, số hộ chiếu) và những thông tin CRM phát hiện được. CRM có thể không bắt được mọi thông tin, vì vậy đừng thêm dữ liệu cá nhân mà bản nháp không cần.",
  "terms.ai.policies":
    "Chính sách sử dụng của các nhà cung cấp này áp dụng cho đại lý của bạn với tư cách người dùng một dịch vụ xây trên mô hình của họ. Hãy tuân theo các chính sách đó, cùng với các điều khoản này:",
  "terms.ai.misuse":
    "Không dùng bản nháp AI để đưa thông tin sai hoặc gây hiểu lầm, nhất là về visa và quy định nhập cảnh, sức khỏe, an toàn, giá hoặc tình trạng còn chỗ.",
  "terms.ai.prohibited":
    "Không dùng bản nháp AI để tạo đánh giá hoặc lời chứng thực giả, nhân vật bịa đặt, che giấu tài trợ, thư rác, hoặc để lập hồ sơ về một người mà họ không biết.",
  "terms.ai.highRisk":
    "Bảo hiểm, visa, trả góp và các vấn đề rủi ro cao khác: không dùng bản nháp AI để tư vấn cho một khách cụ thể (ví dụ nên mua bảo hiểm nào, khách có cần visa không, hay một kế hoạch trả góp) trừ khi một người có chuyên môn trong đại lý kiểm tra lời tư vấn trước và khách được báo rằng có dùng AI. Thông tin du lịch chung không phải là tư vấn.",
  "terms.ai.responsible": "Đại lý của bạn xem lại những gì mình gửi và chịu trách nhiệm về nội dung đó.",
  "terms.ai.off": "Adventure Asia có thể tắt bản nháp AI của đại lý vi phạm các quy định này.",
  "terms.ai.schedule":
    "Mục này tóm tắt AI Schedule v1.0 đính kèm thỏa thuận giữa đại lý của bạn và Adventure Asia. Nếu hai văn bản khác nhau, AI Schedule được áp dụng.",
  "terms.more": "Các điều khoản sử dụng khác của CRM đang được soạn và sẽ được thêm vào trang này.",
};

/** The bullets of the terms' AI section, in order (rendered by app/(crm)/legal/terms/page.tsx; the policy links follow "terms.ai.policies"). */
export const AI_TERMS = [
  "terms.ai.drafts",
  "terms.ai.information",
  "terms.ai.check",
  "terms.ai.binding",
  "terms.ai.models",
  "terms.ai.policies",
  "terms.ai.misuse",
  "terms.ai.prohibited",
  "terms.ai.highRisk",
  "terms.ai.responsible",
  "terms.ai.off",
  "terms.ai.schedule",
] as const satisfies readonly (keyof typeof termsEn)[];
