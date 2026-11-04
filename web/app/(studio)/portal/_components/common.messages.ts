// web/app/(studio)/portal/_components/common.messages.ts
// AA-1036 (R3) — small studio parts that share one catalogue: skeleton labels, the CTA review notice, the "served"
// and "newer master" badges, the tour-version status words.
import { defineMessages } from "@/app/_kit";

export const COMMON_MESSAGES = defineMessages(
  {
    loading: "Loading",
    loadingDashboard: "Loading dashboard",
    ctaNotice:
      "Only mention reviews or ratings if they are real and you can show them publicly. AI-written copy never adds reviews for you.",
    ctaMentions: "Mentions:",

    // GettingStarted
    gsTitle: (done: number, total: number) => `Get started · ${done} of ${total}`,
    gsHide: "Hide getting started",
    gsBrandTitle: "Set up your brand voice",
    gsBrandSub: "Tell us how you sound so every rewrite matches it.",
    gsBrandCta: "Open Brand Identity",
    gsTourTitle: "Rewrite your first tour",
    gsTourSub: "Pick a published tour and make it yours.",
    gsTourCta: "Browse tours",
    gsPostTitle: "Write your first social post",
    gsPostSub: "Turn a tour into a ready-to-publish post.",
    gsPostCta: "Open Social Content",
    gsWpTitle: "Connect WordPress",
    gsWpSub: "Publish blog posts to your site in one click.",
    gsWpCta: "Connect",

    // ConnectionSummary
    connManage: "Manage",
    connCheck: "Check connection",

    // LiveWriter
    lwWriting: "Writing",
    lwStarting: "Starting…",

    // MasterNewerNotice
    rewrite: "Rewrite",
    starting: "Starting…",

    // SeoHealthBar
    seoHealth: "SEO Health",
    seoReady: "Content ready to use",
    seoTitleMissing: "SEO title missing",
    seoTitleLong: (n: number) => `Title too long (${n}/60 chars)`,
    seoTitleOk: (n: number) => `Title OK (${n}/60 chars)`,
    seoMetaMissing: "Meta description missing",
    seoMetaLong: (n: number) => `Description too long (${n}/160 chars)`,
    seoMetaShort: (n: number) => `Description too short — aim for 80–160 chars (${n})`,
    seoMetaOk: (n: number) => `Description OK (${n}/160 chars)`,
    seoHighlightsOk: (n: number) => `${n} highlights ✓`,
    seoHighlightsFew: (n: number) => `Need at least 3 highlights (have ${n})`,
    seoSummaryOk: "Summary complete",
    seoSummaryShort: "Summary too short or missing",

    // ServedBadge
    servedSince: (date: string) => ` since ${date}`,
    servedDiscontinued: (since: string) =>
      `Adventure Asia no longer serves this tour${since}. What you made for it stays here; new rewrites, posts and publishing are closed.`,
    servedPaused: (since: string) => `Adventure Asia has paused this tour${since}. New work on it is closed until it is back.`,
    publishClosedRetired: "Publishing closed: tour no longer served",
    publishClosedSuperseded: "A new version of this tour is on the way: publish once it is out",
    publishClosedPaused: "Publishing paused while Adventure Asia has paused this tour",
    afterUnpublishRetired: "The piece stays in My Content. Adventure Asia no longer serves this tour, so it cannot be published again.",
    afterUnpublishSuperseded:
      "The piece stays in My Content and goes back to Ready to Publish; you can publish it again once the new version of this tour is out.",
    afterUnpublishPaused:
      "The piece stays in My Content and goes back to Ready to Publish. Adventure Asia has paused this tour for now, so you can publish it again once the tour is back.",
    afterUnpublish: "The piece stays in My Content and goes back to Ready to Publish, so you can publish it again.",
    badgeDiscontinued: "AA no longer serves this tour",
    badgePaused: "Paused",

    // workStatus
    wsQueued: "Queued",
    wsWriting: "Writing",
    wsChecking: "Checking",
    wsDone: "Ready",
    wsFailed: "Writing failed",
    actReview: "Ready to review",
    actApproved: "Approved",
    actRejected: "Rejected",
    actEdited: "Edited",
    loadingStatus: "Loading status",

    // UsageSparkline
    sparkAria: (label: string, total: string) => `${label}: ${total} this month`,
    sparkPerDay: (label: string) => `${label} per day`,
    sparkPeak: (n: string) => `peak ${n}`,
    sparkToday: (n: string) => `today · ${n}`,
  },
  {
    loading: "Đang tải",
    loadingDashboard: "Đang tải trang tổng quan",
    ctaNotice:
      "Chỉ nhắc tới đánh giá hoặc xếp hạng khi chúng có thật và bạn có thể công khai. Nội dung do AI viết không bao giờ tự thêm đánh giá cho bạn.",
    ctaMentions: "Có nhắc tới:",

    gsTitle: (done: number, total: number) => `Bắt đầu · ${done}/${total}`,
    gsHide: "Ẩn phần bắt đầu",
    gsBrandTitle: "Thiết lập giọng văn thương hiệu",
    gsBrandSub: "Cho chúng tôi biết phong cách của bạn để mọi bản viết lại đều đúng giọng.",
    gsBrandCta: "Mở Nhận diện thương hiệu",
    gsTourTitle: "Viết lại tour đầu tiên",
    gsTourSub: "Chọn một tour đã đăng và biến nó thành của bạn.",
    gsTourCta: "Xem tour",
    gsPostTitle: "Viết bài mạng xã hội đầu tiên",
    gsPostSub: "Biến một tour thành bài đăng sẵn sàng xuất bản.",
    gsPostCta: "Mở Bài mạng xã hội",
    gsWpTitle: "Kết nối WordPress",
    gsWpSub: "Đăng bài blog lên website của bạn chỉ với một cú bấm.",
    gsWpCta: "Kết nối",

    connManage: "Quản lý",
    connCheck: "Kiểm tra kết nối",

    lwWriting: "Đang viết",
    lwStarting: "Đang bắt đầu…",

    rewrite: "Viết lại",
    starting: "Đang bắt đầu…",

    seoHealth: "Sức khoẻ SEO",
    seoReady: "Nội dung sẵn sàng sử dụng",
    seoTitleMissing: "Thiếu tiêu đề SEO",
    seoTitleLong: (n: number) => `Tiêu đề quá dài (${n}/60 ký tự)`,
    seoTitleOk: (n: number) => `Tiêu đề ổn (${n}/60 ký tự)`,
    seoMetaMissing: "Thiếu mô tả meta",
    seoMetaLong: (n: number) => `Mô tả quá dài (${n}/160 ký tự)`,
    seoMetaShort: (n: number) => `Mô tả quá ngắn — nên 80–160 ký tự (${n})`,
    seoMetaOk: (n: number) => `Mô tả ổn (${n}/160 ký tự)`,
    seoHighlightsOk: (n: number) => `${n} điểm nổi bật ✓`,
    seoHighlightsFew: (n: number) => `Cần ít nhất 3 điểm nổi bật (hiện có ${n})`,
    seoSummaryOk: "Tóm tắt đầy đủ",
    seoSummaryShort: "Tóm tắt quá ngắn hoặc còn thiếu",

    servedSince: (date: string) => ` từ ${date}`,
    servedDiscontinued: (since: string) =>
      `Adventure Asia đã ngừng cung cấp tour này${since}. Những gì bạn đã làm cho tour vẫn được giữ ở đây; không thể viết lại, viết bài hay đăng mới.`,
    servedPaused: (since: string) => `Adventure Asia đã tạm dừng tour này${since}. Chưa thể làm thêm cho tour cho tới khi tour mở lại.`,
    publishClosedRetired: "Đã đóng đăng bài: tour không còn được cung cấp",
    publishClosedSuperseded: "Phiên bản mới của tour sắp ra mắt: hãy đăng khi bản mới có",
    publishClosedPaused: "Tạm dừng đăng bài vì Adventure Asia đang tạm dừng tour này",
    afterUnpublishRetired: "Bài viết vẫn ở Nội dung của tôi. Adventure Asia không còn cung cấp tour này nên bài không thể đăng lại.",
    afterUnpublishSuperseded:
      "Bài viết vẫn ở Nội dung của tôi và trở về trạng thái Sẵn sàng đăng; bạn có thể đăng lại khi phiên bản mới của tour ra mắt.",
    afterUnpublishPaused:
      "Bài viết vẫn ở Nội dung của tôi và trở về trạng thái Sẵn sàng đăng. Adventure Asia đang tạm dừng tour này, bạn có thể đăng lại khi tour mở lại.",
    afterUnpublish: "Bài viết vẫn ở Nội dung của tôi và trở về trạng thái Sẵn sàng đăng, bạn có thể đăng lại bất cứ lúc nào.",
    badgeDiscontinued: "AA đã ngừng tour này",
    badgePaused: "Tạm dừng",

    wsQueued: "Đang chờ",
    wsWriting: "Đang viết",
    wsChecking: "Đang kiểm tra",
    wsDone: "Sẵn sàng",
    wsFailed: "Viết không thành công",
    actReview: "Chờ bạn duyệt",
    actApproved: "Đã duyệt",
    actRejected: "Bị từ chối",
    actEdited: "Đã chỉnh sửa",
    loadingStatus: "Đang tải trạng thái",

    sparkAria: (label: string, total: string) => `${label}: ${total} trong tháng này`,
    sparkPerDay: (label: string) => `${label} mỗi ngày`,
    sparkPeak: (n: string) => `cao nhất ${n}`,
    sparkToday: (n: string) => `hôm nay · ${n}`,
  },
);
