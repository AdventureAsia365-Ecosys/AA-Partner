// web/lib/i18nTripCodes.ts
// AA-957 (S262 live evidence) — the EN / VI words of codes the trip page showed raw: the task kinds and stage-history
// reasons the API writes without a label so far (the full list is api/app/task_kinds.py; tests/tripCodes.test.ts
// checks every code there has a label in both languages), the AA incident's departure (shown by the agency's own
// booking line, never the Booking id), and the "show all" of the AA ground operations activity (AA-956). Spread into
// lib/i18n.ts (every key exists in both languages: tests/i18n.test.ts).

export const tripCodesEn = {
  "task.aa_confirm_failed": "AA could not confirm the held places: check the AA bookings",
  "task.aa_hold_expired": "An AA hold ran out: hold the places again",
  "task.aa_incident": "AA reported an incident on a departure: check on the travellers",
  "task.aa_line_declined": "AA declined a booking: offer the client another option",
  "task.aa_lines_failed": "The AA bookings of the accepted proposal could not be made: try again",
  "task.collect_deposit": "Collect the deposit",
  "task.proposal_change_request": "The client asked for changes to the proposal",
  "task.proposal_follow_up": "Follow up: the client viewed the proposal but chose nothing yet",
  "task.proposal_hold_problem": "A proposal option the client needs is sold out: update the proposal",
  "task.stage3_due_t21": "Booking details are still missing 3 weeks before departure",
  "reason.deposit_paid": "Deposit paid",
  "reason.import": "Imported",
  "duty.incident.departureOn": "{title}, from {date}",
  "ops.activityAll": "Show all {n}",
  "ops.activityFewer": "Show fewer",
  "ops.activityOlder": "Older entries are in the access log.",
  "ops.activityOlderLink": "Open the access log",
};

export const tripCodesVi: Record<keyof typeof tripCodesEn, string> = {
  "task.aa_confirm_failed": "AA không xác nhận được các chỗ đang giữ: kiểm tra các đặt chỗ AA",
  "task.aa_hold_expired": "Một lượt giữ chỗ của AA đã hết hạn: giữ chỗ lại",
  "task.aa_incident": "AA báo sự cố ở một lịch khởi hành: kiểm tra tình hình khách",
  "task.aa_line_declined": "AA từ chối một đặt chỗ: đề xuất cho khách lựa chọn khác",
  "task.aa_lines_failed": "Không tạo được các đặt chỗ AA của đề xuất đã chấp nhận: thử lại",
  "task.collect_deposit": "Thu tiền đặt cọc",
  "task.proposal_change_request": "Khách yêu cầu thay đổi đề xuất",
  "task.proposal_follow_up": "Nhắc khách: khách đã xem đề xuất nhưng chưa chọn gì",
  "task.proposal_hold_problem": "Một lựa chọn khách cần trong đề xuất đã hết chỗ: cập nhật đề xuất",
  "task.stage3_due_t21": "Còn thiếu chi tiết đặt chỗ, 3 tuần trước ngày đi",
  "reason.deposit_paid": "Đã nhận tiền cọc",
  "reason.import": "Đã nhập",
  "duty.incident.departureOn": "{title}, từ {date}",
  "ops.activityAll": "Xem tất cả {n}",
  "ops.activityFewer": "Thu gọn",
  "ops.activityOlder": "Các mục cũ hơn có trong nhật ký truy cập.",
  "ops.activityOlderLink": "Mở nhật ký truy cập",
};
