// web/app/(studio)/legal/partner-terms/terms.messages.ts
// AA-886 — the DRAFT partner terms of the Partner Portal; open legal points are marked [to be confirmed]. §3 (AI)
// names the providers (AI Use Register, ADR 0009) and lists the providers' usage policies from lib/aiTerms.ts (one copy,
// shared with the CRM terms). S271 (register v1.11, route control version 6 "prefer_anthropic_api"): Claude is called on
// the Anthropic API first and on Amazon Bedrock second, so the direct APIs are named as providers that may process the
// content, not as a fallback; the order is AA's setting and may change (Draft v0.5). S271 review: Claude models also
// check content (FAQ checks, audit and extraction fallbacks), and AA may change the ORDER without notice while adding or
// replacing a provider follows the DPA notice (root docs/legal/dpa.md 6.2).
// AA-1062 (Draft v0.6, register v1.13): since CIS migration 383 (route control version 7) no OpenAI model writes; only
// Claude models write, and a writing step with no Claude model available waits and is retried (queue_retry). The
// "last resort" writer sentence is gone; tests/studio/studioLegalProviders.test.ts keeps it out.
// AA-1036 (R3): moved out of page.tsx as a structured document (../../_legal/legalDoc.ts) in English and Vietnamese.
// English applies; the Vietnamese text is a faithful translation for reading (the page says so).
import { SUPPORT_EMAIL } from "@/app/_kit/core/brand";
import type { Lang } from "@/app/_kit/core/i18n";
import { AI_PROVIDER_POLICIES, AI_SCHEDULE_VERSION } from "@/lib/aiTerms";
import { PRIVACY_PATH } from "@/lib/studio/legal";
import type { Inline, LegalDoc } from "../../_legal/legalDoc";

const MAIL = { a: SUPPORT_EMAIL, href: `mailto:${SUPPORT_EMAIL}` };

/** "the A, the B and the C" with each policy linked (`and` / `the` in the document's language; `the` may be ""). */
function policyList(and: string, the: string): Inline[] {
  const comma = the ? `, ${the} ` : ", ";
  const last = the ? ` ${and} ${the} ` : ` ${and} `;
  return AI_PROVIDER_POLICIES.flatMap((policy, i): Inline[] => [
    ...(i === 0 ? [] : [i === AI_PROVIDER_POLICIES.length - 1 ? last : comma]),
    { a: policy.name, href: policy.href },
  ]);
}

const EN: LegalDoc = {
  metaTitle: "Partner terms (draft) — Adventure Asia Partner Portal",
  title: "Partner terms",
  intro: [
    "These terms apply to partner businesses that use the Adventure Asia Partner Portal and its API. They will form part of, or sit next to, the partner agreement you sign with Adventure Asia [relationship to be confirmed].",
  ],
  sections: [
    { id: "service", title: "1. The service", blocks: [{ p: [
      "The portal gives you Adventure Asia catalog tours rewritten in your brand voice, social and blog pieces about those tours, and tools to export them or publish them to your own channels. The operating legal entity is [to be confirmed].",
    ] }] },
    { id: "account", title: "2. Your account and API key", blocks: [{ ul: [
      ["Keep your API key secret. Activity under your key is treated as yours."],
      ["Tell us at once at ", MAIL, " if you think the key is known to someone else, so we can revoke it."],
    ] }] },
    { id: "ai", title: "3. AI-generated content and acceptable use", blocks: [{ ul: [
      ["Content is generated with AI and can contain mistakes. The portal shows this at review, publish and export: “This content was generated with AI. Review it before publishing; you are responsible for what you publish.”"],
      ["The content is written and checked with AI models of third-party providers, and may be processed by any of them. Anthropic's Claude models write it and may also check it: Adventure Asia calls Claude through the Anthropic API first and through Amazon Bedrock (Amazon Web Services) second. OpenAI models review, score and check it, through Amazon Bedrock or the OpenAI API, and other models on Amazon Bedrock (Amazon Nova, Meta Llama) may also review and check it. Only Claude models write it: if no Claude model is available, the writing step waits and is tried again later. The Anthropic API and the OpenAI API are used directly, each with a spending cap. Adventure Asia may change the order in which the providers listed here are used without notice. Adding or replacing a provider follows the advance notice set out in the data processing agreement between you and Adventure Asia."],
      ["Every AI feature runs on accounts that Adventure Asia holds in its own company name with these providers. You do not get an account or a key with any of them, and you must not try to reach them through the portal other than through its AI features."],
      [
        "These providers' usage policies apply to you as a user of a service built on their models, whichever way Adventure Asia reaches the model. You must follow them, including the ",
        ...policyList("and", "the"),
        ", as well as these terms. The updated Anthropic Usage Policy applies from 12 November 2026.",
      ],
      { text: ["You must not use the portal or its content for:"], ul: [
        ["fake reviews, ratings or testimonials, or quotes and stories presented as coming from customers who did not give them;"],
        ["invented personas, sockpuppet accounts, or content attributed to a real person who did not write or approve it;"],
        ["astroturfing, or networks of websites or accounts that look independent but are not;"],
        ["hiding sponsorship or a commercial relationship, or removing a disclosure the law or a channel requires;"],
        ["seeding search engines or AI systems with content that misrepresents its origin, authorship or independence;"],
        ["spam, or posting meant to get around the publishing limit;"],
        ["false or misleading information, in particular about visas and entry rules, health, safety, prices or availability;"],
        ["advice to a specific person on insurance, finance, visas or other high-risk matters, unless a qualified person reviews it before it is delivered and the person receiving it is told that AI was used (general travel information, such as which visa rules exist, is not advice);"],
        ["getting around Adventure Asia's content checks, limits or other safeguards;"],
        ["any automation or AI agent you run through the API that does one of the above: you are responsible for it."],
      ] },
      ["If the law or a channel's rules require content to be labelled as AI-generated, you add that label."],
      ["If you use AI content to talk directly with your customers, for example in a chat or in automated replies, tell them they are talking to AI, at least at the start of each conversation."],
      ["You review each piece before it is published, and you are its publisher. Telling Adventure Asia to publish a piece, in the portal or through the API, confirms that you have reviewed it."],
      ["Adventure Asia may hold, rewrite or decline to generate or publish content that breaks these rules. Unpublishing and suspension are covered in sections 4 and 5."],
      ["Your plan may include a monthly AI allowance. When it is used up, AI features may pause until the next month or until Adventure Asia raises it; nothing extra is charged for AI use."],
      ["Adventure Asia keeps records of AI use (model, provider, step, time and cost, not the text itself) and of publishing, to run and secure the service and to show that these rules are followed."],
      [`This section summarises the ${AI_SCHEDULE_VERSION} attached to your partner agreement. If they differ, the AI Schedule applies.`],
    ] }] },
    { id: "publishing", title: "4. Publishing to your channels", blocks: [{ ul: [
      ["Adventure Asia publishes a piece to your channels only when you tell it to publish that piece, using the access you connected. The post appears under your name."],
      ["There is a daily limit on the number of posts Adventure Asia publishes for you."],
      ["You can unpublish your posts. Adventure Asia can also unpublish a post it published for you, for example when it breaks these terms."],
    ] }] },
    { id: "suspension", title: "5. Suspension and ending the service", blocks: [{ p: [
      "Adventure Asia may suspend an account, which stops its API access and publishing at once, for example to protect the service or when these terms are broken. Notice periods, termination and what happens to your content afterwards are [to be confirmed].",
    ] }] },
    { id: "ownership", title: "6. Content rights", blocks: [{ p: [
      "Who owns the generated content and the licence to the catalog tours are [to be confirmed].",
    ] }] },
    { id: "fees", title: "7. Plans and fees", blocks: [{ p: ["Plans, fees and payment terms are [to be confirmed]."] }] },
    { id: "availability", title: "8. Availability", blocks: [{ p: [
      "The portal is under active development; features can change and the service may be unavailable at times. Service levels and support hours are [to be confirmed].",
    ] }] },
    { id: "liability", title: "9. Liability and governing law", blocks: [{ p: [
      "Limits of liability, the governing law and the competent courts are [to be confirmed].",
    ] }] },
    { id: "contact", title: "10. Contact", blocks: [{ p: [
      "Questions: ", MAIL, ". How we handle personal data is described in the ", { a: "privacy notice", href: PRIVACY_PATH }, ".",
    ] }] },
  ],
};

const VI: LegalDoc = {
  metaTitle: "Điều khoản đối tác (bản nháp) — Cổng đối tác Adventure Asia",
  title: "Điều khoản đối tác",
  intro: [
    "Các điều khoản này áp dụng cho doanh nghiệp đối tác sử dụng Cổng đối tác Adventure Asia và API của cổng. Điều khoản sẽ là một phần của, hoặc đi kèm, hợp đồng đối tác bạn ký với Adventure Asia [quan hệ giữa hai văn bản cần xác nhận].",
  ],
  sections: [
    { id: "service", title: "1. Dịch vụ", blocks: [{ p: [
      "Cổng cung cấp cho bạn các tour trong danh mục của Adventure Asia được viết lại theo giọng văn thương hiệu của bạn, các bài mạng xã hội và bài blog về những tour đó, cùng công cụ để xuất nội dung hoặc đăng lên các kênh của bạn. Pháp nhân vận hành [cần xác nhận].",
    ] }] },
    { id: "account", title: "2. Tài khoản và khoá API của bạn", blocks: [{ ul: [
      ["Giữ bí mật khoá API của bạn. Mọi hoạt động dưới khoá của bạn được coi là của bạn."],
      ["Báo ngay cho chúng tôi qua ", MAIL, " nếu bạn nghĩ người khác đã biết khoá, để chúng tôi thu hồi khoá."],
    ] }] },
    { id: "ai", title: "3. Nội dung do AI tạo và quy định sử dụng", blocks: [{ ul: [
      ["Nội dung được tạo bằng AI và có thể có sai sót. Cổng hiển thị điều này khi duyệt, đăng và xuất: “Nội dung này được tạo bằng AI. Hãy kiểm tra trước khi đăng; bạn chịu trách nhiệm về những gì mình đăng.”"],
      ["Nội dung được viết và kiểm tra bằng các mô hình AI của nhà cung cấp bên thứ ba, và có thể được xử lý bởi bất kỳ nhà cung cấp nào trong số đó. Các mô hình Claude của Anthropic viết nội dung và cũng có thể kiểm tra nội dung: Adventure Asia gọi Claude qua Anthropic API trước và qua Amazon Bedrock (Amazon Web Services) sau. Các mô hình OpenAI rà soát, chấm điểm và kiểm tra nội dung, qua Amazon Bedrock hoặc OpenAI API, và các mô hình khác trên Amazon Bedrock (Amazon Nova, Meta Llama) cũng có thể rà soát và kiểm tra. Chỉ các mô hình Claude viết nội dung: nếu không có mô hình Claude nào khả dụng, bước viết sẽ chờ và được thử lại sau. Anthropic API và OpenAI API được gọi trực tiếp, mỗi API có giới hạn chi tiêu. Adventure Asia có thể thay đổi thứ tự sử dụng các nhà cung cấp nêu ở đây mà không cần thông báo. Việc thêm hoặc thay thế một nhà cung cấp phải được thông báo trước theo thoả thuận xử lý dữ liệu giữa bạn và Adventure Asia."],
      ["Mọi tính năng AI chạy trên tài khoản mà Adventure Asia đứng tên công ty với các nhà cung cấp này. Bạn không được cấp tài khoản hay khoá của bất kỳ nhà cung cấp nào, và không được tìm cách truy cập họ thông qua cổng ngoài các tính năng AI của cổng."],
      [
        "Chính sách sử dụng của các nhà cung cấp này áp dụng cho bạn với tư cách người dùng một dịch vụ xây dựng trên mô hình của họ, bất kể Adventure Asia truy cập mô hình theo cách nào. Bạn phải tuân thủ các chính sách đó, gồm ",
        ...policyList("và", ""),
        ", cũng như các điều khoản này. Chính sách sử dụng cập nhật của Anthropic áp dụng từ ngày 12 tháng 11 năm 2026.",
      ],
      { text: ["Bạn không được dùng cổng hoặc nội dung của cổng cho:"], ul: [
        ["đánh giá, xếp hạng hay lời chứng thực giả, hoặc trích dẫn và câu chuyện được trình bày như của khách hàng nhưng khách hàng đó không đưa ra;"],
        ["nhân vật bịa đặt, tài khoản giả mạo, hoặc nội dung gán cho một người thật không viết hay không đồng ý;"],
        ["tạo dư luận giả, hoặc mạng lưới website hay tài khoản trông như độc lập nhưng thực chất không phải;"],
        ["che giấu tài trợ hoặc quan hệ thương mại, hoặc gỡ bỏ phần công bố mà luật hay quy định của kênh yêu cầu;"],
        ["đưa vào công cụ tìm kiếm hoặc hệ thống AI nội dung trình bày sai về nguồn gốc, tác giả hay tính độc lập của nội dung;"],
        ["spam, hoặc đăng bài nhằm lách giới hạn đăng bài;"],
        ["thông tin sai hoặc gây hiểu nhầm, đặc biệt về visa và quy định nhập cảnh, sức khoẻ, an toàn, giá hoặc tình trạng còn chỗ;"],
        ["tư vấn cho một người cụ thể về bảo hiểm, tài chính, visa hoặc các vấn đề rủi ro cao khác, trừ khi người có chuyên môn kiểm tra trước khi gửi và người nhận được thông báo rằng có sử dụng AI (thông tin du lịch chung, ví dụ có những quy định visa nào, không phải là tư vấn);"],
        ["lách các bước kiểm tra nội dung, giới hạn hay biện pháp bảo vệ khác của Adventure Asia;"],
        ["bất kỳ quy trình tự động hay tác tử AI nào bạn chạy qua API làm một trong các việc trên: bạn chịu trách nhiệm về nó."],
      ] },
      ["Nếu luật hoặc quy định của một kênh yêu cầu ghi nhãn nội dung do AI tạo, bạn có trách nhiệm thêm nhãn đó."],
      ["Nếu bạn dùng nội dung AI để trò chuyện trực tiếp với khách hàng, ví dụ trong khung chat hoặc tin nhắn trả lời tự động, hãy cho họ biết họ đang trò chuyện với AI, ít nhất ở đầu mỗi cuộc trò chuyện."],
      ["Bạn kiểm tra từng bài trước khi đăng, và bạn là bên đăng bài. Việc yêu cầu Adventure Asia đăng một bài, trên cổng hoặc qua API, xác nhận rằng bạn đã kiểm tra bài đó."],
      ["Adventure Asia có thể giữ lại, viết lại hoặc từ chối tạo hay đăng nội dung vi phạm các quy định này. Việc gỡ bài và tạm khoá được nêu ở mục 4 và 5."],
      ["Gói của bạn có thể bao gồm hạn mức AI hằng tháng. Khi dùng hết, các tính năng AI có thể tạm dừng tới tháng sau hoặc tới khi Adventure Asia nâng hạn mức; việc dùng AI không phát sinh thêm phí."],
      ["Adventure Asia lưu bản ghi về việc sử dụng AI (mô hình, nhà cung cấp, bước, thời gian và chi phí, không lưu chính văn bản) và về việc đăng bài, để vận hành, bảo vệ dịch vụ và chứng minh các quy định này được tuân thủ."],
      [`Mục này tóm tắt ${AI_SCHEDULE_VERSION} đính kèm hợp đồng đối tác của bạn. Nếu có khác biệt, Phụ lục AI được áp dụng.`],
    ] }] },
    { id: "publishing", title: "4. Đăng bài lên các kênh của bạn", blocks: [{ ul: [
      ["Adventure Asia chỉ đăng một bài lên các kênh của bạn khi bạn yêu cầu đăng bài đó, bằng quyền truy cập bạn đã kết nối. Bài đăng xuất hiện dưới tên của bạn."],
      ["Có giới hạn hằng ngày về số bài Adventure Asia đăng cho bạn."],
      ["Bạn có thể gỡ bài của mình. Adventure Asia cũng có thể gỡ một bài đã đăng cho bạn, ví dụ khi bài vi phạm các điều khoản này."],
    ] }] },
    { id: "suspension", title: "5. Tạm khoá và chấm dứt dịch vụ", blocks: [{ p: [
      "Adventure Asia có thể tạm khoá một tài khoản, việc này dừng ngay quyền truy cập API và việc đăng bài của tài khoản, ví dụ để bảo vệ dịch vụ hoặc khi các điều khoản này bị vi phạm. Thời hạn thông báo, việc chấm dứt và những gì xảy ra với nội dung của bạn sau đó [cần xác nhận].",
    ] }] },
    { id: "ownership", title: "6. Quyền đối với nội dung", blocks: [{ p: [
      "Ai sở hữu nội dung được tạo và giấy phép sử dụng các tour trong danh mục [cần xác nhận].",
    ] }] },
    { id: "fees", title: "7. Gói dịch vụ và phí", blocks: [{ p: ["Các gói dịch vụ, phí và điều khoản thanh toán [cần xác nhận]."] }] },
    { id: "availability", title: "8. Tính sẵn sàng", blocks: [{ p: [
      "Cổng đang được phát triển tích cực; các tính năng có thể thay đổi và dịch vụ có lúc không khả dụng. Mức dịch vụ và giờ hỗ trợ [cần xác nhận].",
    ] }] },
    { id: "liability", title: "9. Trách nhiệm pháp lý và luật áp dụng", blocks: [{ p: [
      "Giới hạn trách nhiệm, luật áp dụng và toà án có thẩm quyền [cần xác nhận].",
    ] }] },
    { id: "contact", title: "10. Liên hệ", blocks: [{ p: [
      "Câu hỏi: ", MAIL, ". Cách chúng tôi xử lý dữ liệu cá nhân được mô tả trong ", { a: "thông báo quyền riêng tư", href: PRIVACY_PATH }, ".",
    ] }] },
  ],
};

export const TERMS_DOC: Readonly<Record<Lang, LegalDoc>> = { en: EN, vi: VI };
