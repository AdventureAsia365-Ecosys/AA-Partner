// web/app/(studio)/portal-access/access.messages.ts
// AA-1036 (R3) — the studio access pages (agency chooser, no access) in English and Vietnamese.
import { defineMessages } from "@/app/_kit";

export const ACCESS_MESSAGES = defineMessages(
  {
    logoAlt: "Adventure Asia",
    signOut: "Sign out",
    support: "Questions? Contact Adventure Asia:",
    unavailableTitle: "Content studio unavailable",
    noAccessTitle: "No Content studio access",
    unavailableBody: "The Content studio could not check your access right now. Try again in a minute.",
    noAccessBody:
      "This account has no access to a Content studio yet. If you expected one, ask Adventure Asia for an invitation, then sign in to AA Partner with the e-mail address the invitation was sent to.",
    tryAgain: "Try again",
    chooseTitle: "Choose a Content studio",
    chooseError: "That agency could not be opened. Choose again.",
    unreachable: "The Content studio is not reachable right now. Try again in a minute.",
    roleOwner: "Owner",
    roleEditor: "Editor",
    roleViewer: "Viewer",
    open: "Open",
    goToPartner: "Go to AA Partner",
  },
  {
    logoAlt: "Adventure Asia",
    signOut: "Đăng xuất",
    support: "Cần hỗ trợ? Liên hệ Adventure Asia:",
    unavailableTitle: "Content studio tạm thời không mở được",
    noAccessTitle: "Chưa có quyền vào Content studio",
    unavailableBody: "Content studio chưa kiểm tra được quyền truy cập của bạn lúc này. Vui lòng thử lại sau một phút.",
    noAccessBody:
      "Tài khoản này chưa được cấp quyền vào Content studio nào. Nếu bạn đang chờ quyền truy cập, hãy nhờ Adventure Asia gửi lời mời, rồi đăng nhập AA Partner bằng đúng địa chỉ e-mail đã nhận lời mời.",
    tryAgain: "Thử lại",
    chooseTitle: "Chọn Content studio",
    chooseError: "Không mở được đại lý đó. Vui lòng chọn lại.",
    unreachable: "Hiện chưa kết nối được tới Content studio. Vui lòng thử lại sau một phút.",
    roleOwner: "Chủ sở hữu",
    roleEditor: "Biên tập viên",
    roleViewer: "Người xem",
    open: "Mở",
    goToPartner: "Về AA Partner",
  },
);
