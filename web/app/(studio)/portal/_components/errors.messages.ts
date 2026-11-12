// web/app/(studio)/portal/_components/errors.messages.ts
// AA-1036 (R3) — the sentences of the errors the studio's own API helpers raise (reviewListApi.ts, catalogApi.ts,
// marketplaceApi.ts, the kit's api.ts). A helper throws a StudioError carrying one of these keys and the HTTP
// status (studioError.ts); the component shows it in the UI language through plainError(e, fallback, lang).
// A sentence the API wrote itself (its `detail`) is shown as it is.
import { defineMessages } from "@/app/_kit";

export const ERROR_MESSAGES = defineMessages(
  {
    requestFailed: (status: number) => `Request failed (${status})`,
    saveFailed: (status: number) => `Save failed (${status})`,
    publishFailed: (status: number) => `Publish failed (${status})`,
    postNotThrough: "The post didn't go through — try again in a moment.",
    // AA-1088 (M3): the piece needs a final read before it goes live (CIS 409 content_needs_final_check)
    needsFinalCheckOwner:
      "Read this piece through once more before it goes live: we could not confirm every detail in it against the trip (dates, places, what is included). If it reads right, choose Publish anyway on the Publish page.",
    needsFinalCheckEditor:
      "Read this piece through once more before it goes live: we could not confirm every detail in it against the trip (dates, places, what is included). An account owner can then publish it.",
    exportFailed: (status: number) => `Export failed (${status})`,
    ctaMissing: "Add a call to action first: open it from Create and write it there.",
    rewriteStartFailed: (status: number) => `Could not start the rewrite (${status})`,
    retryFailed: (status: number) => `Retry failed (${status})`,
    rewriteBusy: "A rewrite of this tour is already in progress.",
    rewriteRequestFailed: (status: number) => `Rewrite request failed (${status})`,
    unknown: "Something went wrong — try again.",
  },
  {
    requestFailed: (status: number) => `Yêu cầu không thành công (${status})`,
    saveFailed: (status: number) => `Không lưu được (${status})`,
    publishFailed: (status: number) => `Đăng bài không thành công (${status})`,
    postNotThrough: "Bài chưa đăng được — vui lòng thử lại sau giây lát.",
    needsFinalCheckOwner:
      "Hãy đọc lại bài này một lần trước khi đăng: chúng tôi chưa xác nhận được mọi chi tiết trong bài so với tour (ngày, địa điểm, dịch vụ bao gồm). Nếu bài đã đúng, chọn Vẫn đăng ở trang Đăng bài.",
    needsFinalCheckEditor:
      "Hãy đọc lại bài này một lần trước khi đăng: chúng tôi chưa xác nhận được mọi chi tiết trong bài so với tour (ngày, địa điểm, dịch vụ bao gồm). Sau đó chủ tài khoản có thể đăng bài.",
    exportFailed: (status: number) => `Xuất không thành công (${status})`,
    ctaMissing: "Hãy thêm lời kêu gọi hành động trước: mở bài từ mục Sáng tạo và viết ở đó.",
    rewriteStartFailed: (status: number) => `Không bắt đầu viết lại được (${status})`,
    retryFailed: (status: number) => `Thử lại không thành công (${status})`,
    rewriteBusy: "Tour này đang được viết lại.",
    rewriteRequestFailed: (status: number) => `Yêu cầu viết lại không thành công (${status})`,
    unknown: "Đã có lỗi — vui lòng thử lại.",
  },
);
