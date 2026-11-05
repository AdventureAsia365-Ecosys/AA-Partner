// web/lib/i18nAddOns.ts
// AA-1036 (R3) — the EN / VI strings of the CRM's Settings > Add-ons page (a CRM owner asks Adventure Asia for an add-on
// without a Content studio membership), spread into lib/i18n.ts (every key exists in both languages: tests/i18n.test.ts).
// The wording follows the studio's Account › Add-ons page (app/(studio)/portal/_components/account.messages.ts).

export const addOnsEn = {
  "addons.title": "Add-ons",
  "addons.subtitle":
    "Add the Trip planner or the CRM when your agency needs them: the agency owner sends a request and Adventure Asia switches it on.",
  "addons.link": "Add-ons: see and request the Trip planner",
  "addons.ownerOnly": "Only the agency owner can see and request add-ons. Ask your owner to send the request.",
  "addons.crm": "CRM",
  "addons.crmAbout": "Leads, clients, proposals, bookings and payments for your agency, with AI drafting.",
  "addons.planner": "Trip planner",
  "addons.plannerAbout": "A map-first trip planner on your own site, fed by your Content tours.",
  "addons.state.active": "On",
  "addons.state.requested": "Requested",
  "addons.state.available": "Not added",
  "addons.activeNote": "Your agency has this add-on.",
  "addons.plannerActiveNote": "Your agency has the Trip planner. Adventure Asia sets it up on your site with you.",
  "addons.requestedNote": "Requested on {date}. Adventure Asia will reply here.",
  "addons.yourNote": "Your note",
  "addons.declined": "Your request of {date} was declined.",
  "addons.approved": "Approved on {date}.",
  "addons.approvedSettingUp": "Approved on {date}. Adventure Asia is switching it on.",
  "addons.approvedSetupFailed":
    "Approved on {date}, but the setup did not finish, so the add-on is not on yet. Adventure Asia will finish it.",
  "addons.reason": "Reason",
  "addons.noteLabel": "Note to Adventure Asia (optional)",
  "addons.notePlaceholder": "For example: how many people would use it, or when you would like to start.",
  "addons.request": "Request {name}",
  "addons.requesting": "Sending…",
  "addons.sent": "Request for {name} sent. Adventure Asia will reply here.",
  "addons.err.ownerOnly": "Only the agency owner can request an add-on.",
  "addons.err.alreadyOn": "This add-on is already on for your agency.",
  "addons.err.notInService": "Your account cannot request add-ons right now. Contact Adventure Asia.",
  "addons.err.tooMany": "Too many requests today. Try again tomorrow.",
  "addons.err.send": "The request could not be sent. Try again.",
  "addons.err.load": "Your add-ons cannot be shown right now. Try again in a few minutes.",
  "addons.err.noAccount":
    "Your agency has no Adventure Asia account for add-ons yet. Contact your Adventure Asia account manager.",
  "addons.notInService": "Your account is not in service, so add-ons cannot be requested now. Contact Adventure Asia.",
} as const;

export const addOnsVi: Record<keyof typeof addOnsEn, string> = {
  "addons.title": "Tiện ích bổ sung",
  "addons.subtitle":
    "Hãy thêm Trip planner hoặc CRM khi đại lý cần: chủ đại lý gửi yêu cầu và Adventure Asia sẽ bật cho bạn.",
  "addons.link": "Tiện ích bổ sung: xem và yêu cầu Trip planner",
  "addons.ownerOnly": "Chỉ chủ đại lý mới xem và yêu cầu được tiện ích. Hãy nhờ chủ đại lý gửi yêu cầu.",
  "addons.crm": "CRM",
  "addons.crmAbout": "Khách tiềm năng, khách hàng, đề xuất, đặt chỗ và thanh toán của đại lý, có AI hỗ trợ soạn thảo.",
  "addons.planner": "Trip planner",
  "addons.plannerAbout": "Công cụ lên kế hoạch chuyến đi theo bản đồ trên website của bạn, dùng các tour Content của bạn.",
  "addons.state.active": "Đang bật",
  "addons.state.requested": "Đã yêu cầu",
  "addons.state.available": "Chưa thêm",
  "addons.activeNote": "Đại lý của bạn đã có tiện ích này.",
  "addons.plannerActiveNote": "Đại lý của bạn đã có Trip planner. Adventure Asia sẽ cùng bạn cài đặt trên website.",
  "addons.requestedNote": "Đã yêu cầu ngày {date}. Adventure Asia sẽ phản hồi tại đây.",
  "addons.yourNote": "Ghi chú của bạn",
  "addons.declined": "Yêu cầu ngày {date} của bạn đã bị từ chối.",
  "addons.approved": "Đã duyệt ngày {date}.",
  "addons.approvedSettingUp": "Đã duyệt ngày {date}. Adventure Asia đang bật tiện ích cho bạn.",
  "addons.approvedSetupFailed":
    "Đã duyệt ngày {date}, nhưng việc thiết lập chưa xong nên tiện ích chưa được bật. Adventure Asia sẽ hoàn tất.",
  "addons.reason": "Lý do",
  "addons.noteLabel": "Ghi chú gửi Adventure Asia (không bắt buộc)",
  "addons.notePlaceholder": "Ví dụ: bao nhiêu người sẽ dùng, hoặc khi nào bạn muốn bắt đầu.",
  "addons.request": "Yêu cầu {name}",
  "addons.requesting": "Đang gửi…",
  "addons.sent": "Đã gửi yêu cầu {name}. Adventure Asia sẽ phản hồi tại đây.",
  "addons.err.ownerOnly": "Chỉ chủ đại lý mới yêu cầu được tiện ích.",
  "addons.err.alreadyOn": "Tiện ích này đã được bật cho đại lý của bạn.",
  "addons.err.notInService": "Tài khoản của bạn hiện không yêu cầu được tiện ích. Hãy liên hệ Adventure Asia.",
  "addons.err.tooMany": "Hôm nay đã có quá nhiều yêu cầu. Hãy thử lại vào ngày mai.",
  "addons.err.send": "Chưa gửi được yêu cầu. Hãy thử lại.",
  "addons.err.load": "Hiện chưa hiển thị được các tiện ích. Hãy thử lại sau vài phút.",
  "addons.err.noAccount":
    "Đại lý của bạn chưa có tài khoản Adventure Asia cho tiện ích bổ sung. Hãy liên hệ người phụ trách tài khoản tại Adventure Asia.",
  "addons.notInService": "Tài khoản của bạn hiện không hoạt động nên chưa yêu cầu được tiện ích. Hãy liên hệ Adventure Asia.",
};
