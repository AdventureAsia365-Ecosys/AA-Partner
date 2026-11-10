// AA-1039 (option A) — "Put back online": the pages Adventure Asia moved to draft while their tour was off sale, in
// English and Vietnamese (Publish page section + the bell's buttons).
import { defineMessages } from "@/app/_kit";

export const PUT_BACK_MESSAGES = defineMessages(
  {
    title: "Pages to put back online",
    sub: "These WordPress pages were moved to draft while their tour was off sale. The tour is back on sale: put each page back online when you are ready. Nothing is published until you click.",
    putBack: "Put back online",
    puttingBack: "Putting back…",
    viewPage: "View page",
    tourLine: (tour: string) => `Tour: ${tour}`,
    published: "The page is online again.",
    alreadyOnline: "The page was already online on WordPress.",
    leftAsIs: (state: string) => `The page was changed on WordPress (it is ${state} now), so it was left as is.`,
    failed: "Could not put the page back online.",
    stateprivate: "private",
    statepending: "pending review",
    statefuture: "scheduled",
    statetrash: "in the trash",
    statemissing: "deleted",
    stateother: "changed",
  },
  {
    title: "Trang cần đưa lại lên mạng",
    sub: "Các trang WordPress này đã được chuyển về bản nháp khi tour tạm ngừng bán. Tour đã mở bán lại: hãy đưa từng trang lên mạng lại khi bạn sẵn sàng. Không có gì được đăng cho tới khi bạn bấm.",
    putBack: "Đưa lại lên mạng",
    puttingBack: "Đang đưa lên…",
    viewPage: "Xem trang",
    tourLine: (tour: string) => `Tour: ${tour}`,
    published: "Trang đã lên mạng lại.",
    alreadyOnline: "Trang đã đang hiển thị trên WordPress.",
    leftAsIs: (state: string) => `Trang đã được thay đổi trên WordPress (hiện là ${state}), nên được giữ nguyên.`,
    failed: "Không đưa được trang lên mạng lại.",
    stateprivate: "riêng tư",
    statepending: "chờ duyệt",
    statefuture: "đã lên lịch",
    statetrash: "trong thùng rác",
    statemissing: "đã bị xoá",
    stateother: "đã đổi",
  },
);
