// AA-1036 (R3) — the Account pages (Members, Activity, Billing, Settings) and the notifications bell in English and
// Vietnamese.
import { defineMessages } from "@/app/_kit";

export const MEMBERS_MESSAGES = defineMessages(
  {
    colMember: "Member",
    colRole: "Role",
    colStatus: "Status",
    colAdded: "Added",
    colChanged: "Last change",
    mailSubject: "Content Studio members: add, change or remove",
    title: "Members",
    sub: "Everyone in your Content Studio workspace and their role.",
    requestChange: "Request a change",
    howItWorks:
      "Adventure Asia adds, changes and removes members for you for now: use Request a change. A member who is removed is also signed out of the Partner CRM within a minute. Roles inside the Partner CRM are managed in the CRM.",
    noOwner: "Your workspace has no active owner. Ask Adventure Asia to restore one before you make account changes.",
    loadError: "Could not load the members",
    emptyTitle: "No members yet",
    emptySub: "Members appear here once Adventure Asia adds your team to the workspace.",
  },
  {
    colMember: "Thành viên",
    colRole: "Vai trò",
    colStatus: "Trạng thái",
    colAdded: "Ngày thêm",
    colChanged: "Thay đổi gần nhất",
    mailSubject: "Content Studio members: add, change or remove",
    title: "Thành viên",
    sub: "Mọi người trong không gian Content Studio của bạn và vai trò của họ.",
    requestChange: "Yêu cầu thay đổi",
    howItWorks:
      "Hiện tại Adventure Asia sẽ thêm, thay đổi và xoá thành viên giúp bạn: hãy dùng Yêu cầu thay đổi. Thành viên bị xoá cũng sẽ bị đăng xuất khỏi Partner CRM trong vòng một phút. Vai trò trong Partner CRM được quản lý ngay trong CRM.",
    noOwner: "Không gian làm việc của bạn chưa có chủ sở hữu đang hoạt động. Hãy nhờ Adventure Asia khôi phục trước khi thay đổi tài khoản.",
    loadError: "Không tải được danh sách thành viên",
    emptyTitle: "Chưa có thành viên",
    emptySub: "Thành viên sẽ xuất hiện ở đây khi Adventure Asia thêm đội ngũ của bạn vào không gian làm việc.",
  },
);

export const BELL_MESSAGES = defineMessages(
  {
    approved: "was approved",
    rejected: "was rejected",
    failed: "rewrite didn't finish — retry it in My Tours",
    writing: "is being rewritten",
    done: "rewrite is ready to review",
    edited: "was edited",
    updated: "was updated",
    ariaNew: (n: number) => `Notifications, ${n} new`,
    title: "Notifications",
    viewAll: "View all →",
    caughtUp: "You’re all caught up.",
    aTour: "A tour",
  },
  {
    approved: "đã được duyệt",
    rejected: "đã bị từ chối",
    failed: "chưa viết lại xong — hãy thử lại trong Tour của tôi",
    writing: "đang được viết lại",
    done: "đã viết lại xong, chờ bạn duyệt",
    edited: "đã được chỉnh sửa",
    updated: "đã được cập nhật",
    ariaNew: (n: number) => `Thông báo, ${n} mới`,
    title: "Thông báo",
    viewAll: "Xem tất cả →",
    caughtUp: "Bạn đã xem hết thông báo.",
    aTour: "Một tour",
  },
);
