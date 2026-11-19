// web/lib/i18nAgencyUi.ts
// AA-1129 (S283 UI audit, agency app P1 / P2): Home's "Needs you" panel, the names of team members in menus and filters
// ("lan.nguyen · Agent", or "Agent 2" for a member with no invitation address, never an id), the Unmatched inbox
// rows (sender, subject, preview, message count). A counted message has its singular in `<key>.one` (plural()).

export const agencyUiEn = {
  "trip.memberNamed": "{name} · {role}",
  "trip.memberSeq": "{role} {n}",
  "home.needs.tasksOverdue": "{n} overdue tasks",
  "home.needs.tasksOverdue.one": "{n} overdue task",
  "home.needs.tasksToday": "{n} tasks due today",
  "home.needs.tasksToday.one": "{n} task due today",
  "home.needs.waiting": "{n} trips waiting on you",
  "home.needs.waiting.one": "{n} trip waiting on you",
  "home.needs.latePayments": "{n} late payments",
  "home.needs.latePayments.one": "{n} late payment",
  "home.needs.none": "Nothing needs you right now. New inquiries, overdue tasks and late payments show here.",
  "home.needs.mine": "Your tasks and trips; late payments are the agency's.",
  "inbox.messages": "{n} messages",
  "inbox.messages.one": "{n} message",
  "inbox.noSubject": "(no subject)",
} as const;

export const agencyUiVi: Record<keyof typeof agencyUiEn, string> = {
  "trip.memberNamed": "{name} · {role}",
  "trip.memberSeq": "{role} {n}",
  "home.needs.tasksOverdue": "{n} việc quá hạn",
  "home.needs.tasksOverdue.one": "{n} việc quá hạn",
  "home.needs.tasksToday": "{n} việc đến hạn hôm nay",
  "home.needs.tasksToday.one": "{n} việc đến hạn hôm nay",
  "home.needs.waiting": "{n} chuyến đang chờ bạn",
  "home.needs.waiting.one": "{n} chuyến đang chờ bạn",
  "home.needs.latePayments": "{n} khoản thanh toán trễ hạn",
  "home.needs.latePayments.one": "{n} khoản thanh toán trễ hạn",
  "home.needs.none": "Hiện chưa có việc cần bạn. Yêu cầu mới, việc quá hạn và thanh toán trễ sẽ hiện ở đây.",
  "home.needs.mine": "Việc và chuyến của bạn; thanh toán trễ là của cả đại lý.",
  "inbox.messages": "{n} tin nhắn",
  "inbox.messages.one": "{n} tin nhắn",
  "inbox.noSubject": "(không có tiêu đề)",
};
