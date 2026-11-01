// web/lib/i18nTripClosed.ts
// AA-957 S262 round 3 (design v3 C12 §2.7) — the words of a trip that is cancelled or being cancelled, on every card of
// the trip page (nothing new is booked, quoted, shared, signed or sent; messages stay open for the refund and the
// wrap-up), the Booking details row of Traveller info, the lead source codes the API writes (the full list is
// api/app/lead_sources.py; tests/closedTripRound3.test.ts reads it and checks each has a label in both languages), the
// A-12 amount of a cancelled commission. Spread into lib/i18n.ts (every key exists in both languages).

export const tripClosedEn = {
  "trip.closedPlanning": "This trip is cancelled: its dates, bookings and proposal no longer change.",
  "trip.closedPending": "This trip is being cancelled: it is closed until AA confirms, and cannot be deleted meanwhile.",
  "ledger.closed": "This trip is cancelled: no new booking, AA item or confirmation. Cancel what is still booked with each supplier.",
  "docs.closed": "This trip is cancelled: new files stay with the agency and are not shown to the traveller.",
  "messages.closed": "This trip is cancelled. You can still write to the client about the refund and the wrap-up; automated messages are stopped.",
  "esign.closed": "This trip is cancelled: no signature is asked any more.",
  "esign.notNeeded": "Not needed",
  "proposal.portalClosed": "This trip is cancelled: travellers send no more details and sign nothing.",
  "proposal.detailsNotNeeded": "Details not needed",
  "proposal.waiverNotNeeded": "Waiver not needed",
  "builder.closed": "This trip is cancelled: its proposal versions are kept as they were sent.",
  "info.bookingOpen": "Travellers send these from their own page: see Stage 3 under Proposal.",
  "info.bookingClosed": "This trip is cancelled: no booking details are needed.",
  "info.bookingDone": "This trip is over: what travellers sent from their own page stays under Stage 3 in Proposal.",
  "commissions.col.wasExpected": "Was expected",
  "source.agent_created": "Added by the agency",
  "source.companion": "Added as a companion",
  "source.import": "Imported",
  "source.synthetic_seed": "Sample data",
  "source.website_form": "Website form",
} as const;

export const tripClosedVi: Record<keyof typeof tripClosedEn, string> = {
  "trip.closedPlanning": "Chuyến này đã huỷ: ngày đi, dịch vụ đã đặt và báo giá không còn thay đổi.",
  "trip.closedPending": "Chuyến này đang được huỷ: chuyến bị khoá cho tới khi AA xác nhận và chưa thể xoá trong lúc này.",
  "ledger.closed": "Chuyến này đã huỷ: không thêm dịch vụ, hạng mục AA hay xác nhận mới. Hãy huỷ với từng nhà cung cấp những gì còn đang đặt.",
  "docs.closed": "Chuyến này đã huỷ: tệp mới chỉ lưu ở đại lý, không hiện cho khách.",
  "messages.closed": "Chuyến này đã huỷ. Bạn vẫn nhắn được cho khách về việc hoàn tiền và kết thúc chuyến; tin nhắn tự động đã dừng.",
  "esign.closed": "Chuyến này đã huỷ: không cần ký thêm.",
  "esign.notNeeded": "Không cần",
  "proposal.portalClosed": "Chuyến này đã huỷ: khách không cần gửi thêm thông tin hay ký giấy tờ.",
  "proposal.detailsNotNeeded": "Không cần thông tin",
  "proposal.waiverNotNeeded": "Không cần ký cam kết",
  "builder.closed": "Chuyến này đã huỷ: các phiên bản báo giá được giữ nguyên như đã gửi.",
  "info.bookingOpen": "Khách gửi các thông tin này từ trang riêng của mình: xem Giai đoạn 3 trong phần Báo giá.",
  "info.bookingClosed": "Chuyến này đã huỷ: không cần chi tiết đặt chỗ.",
  "info.bookingDone": "Chuyến đã kết thúc: những gì khách gửi từ trang riêng của mình vẫn nằm trong Giai đoạn 3 ở phần Báo giá.",
  "commissions.col.wasExpected": "Dự kiến trước khi huỷ",
  "source.agent_created": "Do đại lý thêm",
  "source.companion": "Thêm làm bạn đồng hành",
  "source.import": "Nhập từ tệp",
  "source.synthetic_seed": "Dữ liệu mẫu",
  "source.website_form": "Biểu mẫu trên website",
};
