// web/app/(studio)/legal/privacy/privacy.messages.ts
// AA-886 — the DRAFT privacy notice of the Partner Portal. Every factual sentence describes what the platform does
// today (sources in docs/implementation-notes/AA-886-s245.md); open legal points are marked [to be confirmed]. S262
// (v0.3): the AI provider list in §5 and §6 follows the AI Use Register (root docs/architecture/ai-use-register.md,
// ADR 0009); AA-820. S264 (register v1.2, CIS migration 344): the writing fallback, Claude through the Anthropic API,
// then an OpenAI model as a last resort.
// AA-1036 (R3): moved out of page.tsx as a structured document (../../_legal/legalDoc.ts) in English and Vietnamese.
// English applies; the Vietnamese text is a faithful translation for reading (the page says so).
import { SUPPORT_EMAIL } from "@/app/_kit/core/brand";
import type { Lang } from "@/app/_kit/core/i18n";
import { COMPANY_PRIVACY_URL, TERMS_PATH } from "@/lib/studio/legal";
import type { LegalDoc } from "../../_legal/legalDoc";

const MAIL = { a: SUPPORT_EMAIL, href: `mailto:${SUPPORT_EMAIL}` };

const EN: LegalDoc = {
  metaTitle: "Privacy notice (draft) — Adventure Asia Partner Portal",
  title: "Privacy notice",
  intro: [
    "This notice explains what personal data the Adventure Asia Partner Portal collects about partner businesses and the people who use it, why, and who receives it. Adventure Asia's company-wide policy is at ",
    { a: "adventure.asia/privacy-policy", href: COMPANY_PRIVACY_URL },
    ".",
  ],
  sections: [
    { id: "who", title: "1. Who we are", blocks: [{ p: [
      "The Partner Portal is run by Adventure Asia. The legal entity responsible for your data (the data controller) and its registered address are [to be confirmed]. Privacy questions and requests: ",
      MAIL, ".",
    ] }] },
    { id: "collect", title: "2. What we collect", blocks: [{ ul: [
      [{ b: "Partner account." }, " Your business name, plan and account status, and the API key you sign in with. We check the key on our server; the browser then holds a sign-in session."],
      [{ b: "Brand settings and content." }, " The brand voice and settings you enter, the tour rewrites and social or blog pieces generated for you, and your edits."],
      [{ b: "Channel connections." }, " The details you give us to publish to your own channels (for example a WordPress site or a Facebook Page)."],
      [{ b: "Activity records." }, " Each AI model call (provider, model, cost, time, partner and job), each publish and unpublish (piece, channel, post id, status, time, and whether your account or an Adventure Asia admin acted), and an admin log of changes to partner accounts."],
      [{ b: "Technical data." }, " IP address, browser type and request logs, processed by our hosting providers and our security systems."],
    ] }] },
    { id: "cookies", title: "3. Cookies", blocks: [{ p: [
      "The portal uses only sign-in cookies, which scripts on the page cannot read. They hold your session and expire after 24 hours or when you sign out. We do not use advertising or analytics cookies.",
    ] }] },
    { id: "use", title: "4. How we use your data", blocks: [
      { ul: [
        ["To run your account and produce the content you ask for."],
        ["To publish or unpublish a piece on your channels when you tell us to."],
        ["To support you, keep the service secure, and enforce the partner terms."],
        ["To measure our own costs and service quality."],
      ] },
      { p: ["The legal basis for each use is [to be confirmed]."] },
    ] },
    { id: "ai", title: "5. AI processing", blocks: [{ p: [
      "Content is generated and checked by AI models. Claude (Anthropic), through Amazon Bedrock, writes it; OpenAI models, also through Amazon Bedrock, review, score and check the drafts. When those models are unavailable, the review and check steps may fall back to other models on Amazon Bedrock (Amazon Nova, Meta Llama). If Amazon Bedrock is unavailable, the Anthropic API or the OpenAI API, used directly and with a spending cap, may be used as a fallback; those calls go from Amazon Web Services to Anthropic or OpenAI, on accounts Adventure Asia holds in its own company name. For writing, that means Claude through the Anthropic API first and, as a last resort, an OpenAI model through the OpenAI API. For some partners, a decision-checking service (Jev, by TypeSafe) also answers yes-or-no checks about the drafts. These providers receive tour content, your brand settings and the instructions for each piece. Our policy is not to send personal data to these models and not to store any in our AI call records, so please do not enter personal data about other people in brand settings or content.",
    ] }] },
    { id: "share", title: "6. Who receives your data", blocks: [
      { ul: [
        [{ b: "Adventure Asia staff" }, ", for support and moderation (they can suspend an account or unpublish a post that Adventure Asia published for you)."],
        [{ b: "Amazon Web Services" }, " — hosting, database, Amazon Bedrock."],
        [{ b: "Vercel" }, " — hosts and delivers the portal pages."],
        [{ b: "AI providers" }, " — Anthropic and OpenAI (through Amazon Bedrock, and directly only as a fallback), and Jev (TypeSafe) for some partners, as described above."],
        [{ b: "Your channels" }, " (for example WordPress or Meta), when you publish to them."],
      ] },
      { p: ["We do not sell personal data."] },
    ] },
    { id: "transfers", title: "7. Where your data is stored", blocks: [{ p: [
      "Our database and servers are in the United States (Amazon Web Services). The safeguards for transfers outside your country are [to be confirmed].",
    ] }] },
    { id: "retention", title: "8. How long we keep it", blocks: [{ ul: [
      ["AI model call records: 180 days."],
      ["Security logs: 365 days."],
      ["Publish records and the admin log: a period will be set before production [to be confirmed]."],
      ["Account data and content: while your account is active, then [to be confirmed]."],
    ] }] },
    { id: "rights", title: "9. Your rights", blocks: [{ p: [
      "You can ask to see, correct or delete personal data we hold about you. Write to ", MAIL,
      ". Response times and your right to complain to a data protection authority are [to be confirmed].",
    ] }] },
    { id: "processor", title: "10. Data about your own customers", blocks: [{ p: [
      "If you put personal data of other people into the portal, you are responsible for having the right to do so. A data processing agreement between you and Adventure Asia is [to be confirmed].",
    ] }] },
    { id: "changes", title: "11. Changes to this notice", blocks: [{ p: [
      "We will publish changes on this page with a new date. See also the ", { a: "partner terms", href: TERMS_PATH }, ".",
    ] }] },
  ],
};

const VI: LegalDoc = {
  metaTitle: "Thông báo quyền riêng tư (bản nháp) — Cổng đối tác Adventure Asia",
  title: "Thông báo quyền riêng tư",
  intro: [
    "Thông báo này giải thích Cổng đối tác Adventure Asia thu thập những dữ liệu cá nhân nào về doanh nghiệp đối tác và những người sử dụng cổng, vì sao, và ai nhận được dữ liệu đó. Chính sách chung của Adventure Asia có tại ",
    { a: "adventure.asia/privacy-policy", href: COMPANY_PRIVACY_URL },
    ".",
  ],
  sections: [
    { id: "who", title: "1. Chúng tôi là ai", blocks: [{ p: [
      "Cổng đối tác do Adventure Asia vận hành. Pháp nhân chịu trách nhiệm về dữ liệu của bạn (bên kiểm soát dữ liệu) và địa chỉ đăng ký của pháp nhân đó [cần xác nhận]. Câu hỏi và yêu cầu về quyền riêng tư: ",
      MAIL, ".",
    ] }] },
    { id: "collect", title: "2. Chúng tôi thu thập gì", blocks: [{ ul: [
      [{ b: "Tài khoản đối tác." }, " Tên doanh nghiệp, gói dịch vụ và trạng thái tài khoản, cùng khoá API bạn dùng để đăng nhập. Chúng tôi kiểm tra khoá trên máy chủ; sau đó trình duyệt giữ một phiên đăng nhập."],
      [{ b: "Cài đặt thương hiệu và nội dung." }, " Giọng văn và các cài đặt thương hiệu bạn nhập, các bản viết lại tour và bài mạng xã hội hoặc bài blog được tạo cho bạn, cùng những chỉnh sửa của bạn."],
      [{ b: "Kết nối kênh." }, " Thông tin bạn cung cấp để chúng tôi đăng bài lên các kênh của bạn (ví dụ một website WordPress hoặc một Trang Facebook)."],
      [{ b: "Bản ghi hoạt động." }, " Mỗi lần gọi mô hình AI (nhà cung cấp, mô hình, chi phí, thời gian, đối tác và tác vụ), mỗi lần đăng và gỡ bài (bài viết, kênh, mã bài đăng, trạng thái, thời gian, và người thực hiện là tài khoản của bạn hay quản trị viên Adventure Asia), cùng nhật ký quản trị về các thay đổi trên tài khoản đối tác."],
      [{ b: "Dữ liệu kỹ thuật." }, " Địa chỉ IP, loại trình duyệt và nhật ký truy cập, do các nhà cung cấp dịch vụ lưu trữ và hệ thống bảo mật của chúng tôi xử lý."],
    ] }] },
    { id: "cookies", title: "3. Cookie", blocks: [{ p: [
      "Cổng chỉ dùng cookie đăng nhập, loại cookie mà mã trên trang không đọc được. Cookie giữ phiên đăng nhập của bạn và hết hạn sau 24 giờ hoặc khi bạn đăng xuất. Chúng tôi không dùng cookie quảng cáo hay phân tích.",
    ] }] },
    { id: "use", title: "4. Chúng tôi sử dụng dữ liệu của bạn như thế nào", blocks: [
      { ul: [
        ["Để vận hành tài khoản của bạn và tạo nội dung bạn yêu cầu."],
        ["Để đăng hoặc gỡ một bài trên các kênh của bạn khi bạn yêu cầu."],
        ["Để hỗ trợ bạn, giữ an toàn cho dịch vụ và thực thi điều khoản đối tác."],
        ["Để đo lường chi phí và chất lượng dịch vụ của chính chúng tôi."],
      ] },
      { p: ["Cơ sở pháp lý cho từng mục đích sử dụng [cần xác nhận]."] },
    ] },
    { id: "ai", title: "5. Xử lý bằng AI", blocks: [{ p: [
      "Nội dung được tạo và kiểm tra bởi các mô hình AI. Claude (Anthropic), thông qua Amazon Bedrock, viết nội dung; các mô hình OpenAI, cũng thông qua Amazon Bedrock, rà soát, chấm điểm và kiểm tra bản nháp. Khi các mô hình này không khả dụng, bước rà soát và kiểm tra có thể chuyển sang mô hình khác trên Amazon Bedrock (Amazon Nova, Meta Llama). Nếu Amazon Bedrock không khả dụng, Anthropic API hoặc OpenAI API, được gọi trực tiếp và có giới hạn chi tiêu, có thể được dùng làm phương án dự phòng; các lệnh gọi đó đi từ Amazon Web Services tới Anthropic hoặc OpenAI, trên tài khoản Adventure Asia đứng tên công ty. Với bước viết, điều đó có nghĩa là trước hết dùng Claude qua Anthropic API và, khi không còn lựa chọn nào khác, một mô hình OpenAI qua OpenAI API. Với một số đối tác, một dịch vụ kiểm tra quyết định (Jev, của TypeSafe) cũng trả lời các câu hỏi có-hoặc-không về bản nháp. Các nhà cung cấp này nhận nội dung tour, cài đặt thương hiệu của bạn và hướng dẫn cho từng bài. Chính sách của chúng tôi là không gửi dữ liệu cá nhân tới các mô hình này và không lưu dữ liệu cá nhân trong bản ghi lệnh gọi AI, vì vậy vui lòng không nhập dữ liệu cá nhân của người khác vào cài đặt thương hiệu hay nội dung.",
    ] }] },
    { id: "share", title: "6. Ai nhận dữ liệu của bạn", blocks: [
      { ul: [
        [{ b: "Nhân viên Adventure Asia" }, ", để hỗ trợ và kiểm duyệt (họ có thể tạm khoá tài khoản hoặc gỡ một bài mà Adventure Asia đã đăng cho bạn)."],
        [{ b: "Amazon Web Services" }, " — lưu trữ, cơ sở dữ liệu, Amazon Bedrock."],
        [{ b: "Vercel" }, " — lưu trữ và phân phối các trang của cổng."],
        [{ b: "Nhà cung cấp AI" }, " — Anthropic và OpenAI (thông qua Amazon Bedrock, và chỉ gọi trực tiếp khi dự phòng), cùng Jev (TypeSafe) với một số đối tác, như mô tả ở trên."],
        [{ b: "Các kênh của bạn" }, " (ví dụ WordPress hoặc Meta), khi bạn đăng bài lên đó."],
      ] },
      { p: ["Chúng tôi không bán dữ liệu cá nhân."] },
    ] },
    { id: "transfers", title: "7. Dữ liệu của bạn được lưu ở đâu", blocks: [{ p: [
      "Cơ sở dữ liệu và máy chủ của chúng tôi đặt tại Hoa Kỳ (Amazon Web Services). Các biện pháp bảo vệ khi chuyển dữ liệu ra ngoài quốc gia của bạn [cần xác nhận].",
    ] }] },
    { id: "retention", title: "8. Chúng tôi lưu giữ trong bao lâu", blocks: [{ ul: [
      ["Bản ghi lệnh gọi mô hình AI: 180 ngày."],
      ["Nhật ký bảo mật: 365 ngày."],
      ["Bản ghi đăng bài và nhật ký quản trị: thời hạn sẽ được đặt trước khi chạy chính thức [cần xác nhận]."],
      ["Dữ liệu tài khoản và nội dung: trong thời gian tài khoản còn hoạt động, sau đó [cần xác nhận]."],
    ] }] },
    { id: "rights", title: "9. Quyền của bạn", blocks: [{ p: [
      "Bạn có thể yêu cầu xem, sửa hoặc xoá dữ liệu cá nhân chúng tôi giữ về bạn. Hãy gửi thư tới ", MAIL,
      ". Thời hạn phản hồi và quyền khiếu nại với cơ quan bảo vệ dữ liệu [cần xác nhận].",
    ] }] },
    { id: "processor", title: "10. Dữ liệu về khách hàng của chính bạn", blocks: [{ p: [
      "Nếu bạn đưa dữ liệu cá nhân của người khác vào cổng, bạn chịu trách nhiệm bảo đảm mình có quyền làm việc đó. Thoả thuận xử lý dữ liệu giữa bạn và Adventure Asia [cần xác nhận].",
    ] }] },
    { id: "changes", title: "11. Thay đổi thông báo này", blocks: [{ p: [
      "Chúng tôi sẽ đăng các thay đổi trên trang này kèm ngày mới. Xem thêm ", { a: "điều khoản đối tác", href: TERMS_PATH }, ".",
    ] }] },
  ],
};

export const PRIVACY_DOC: Readonly<Record<Lang, LegalDoc>> = { en: EN, vi: VI };
