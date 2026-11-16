// web/lib/i18nHelp.ts
// AA-970 (design v3 card CRM-37b: R42 What's new, R43 help pages) — the EN / VI strings of Help & what's new: the
// What's new feed's frame (the entries carry their own words, lib/whatsNew.ts), the preview panel, the help topics of
// the most-used CRM areas beyond getting started, and the links to the public data and sub-processor pages (also on
// Settings > Data and privacy). Spread into lib/i18n.ts (every key exists in both languages: tests/i18n.test.ts).

export const helpEn = {
  "help.whatsNew.title": "What's new",
  "help.whatsNew.subtitle": "New features in the agency app, newest first.",
  "help.whatsNew.previewBadge": "Preview",
  "help.whatsNew.open": "Open",
  "help.gettingStarted.title": "Getting started",
  "help.using.title": "Using the CRM",
  "help.u.pipeline.t": "The pipeline",
  "help.u.pipeline.b":
    "Every trip is a card on the Pipeline board, in stages from Inquiry to Completed (or Lost or Cancelled). Each card says who the next step is waiting on. Open a card for the trip itself: its travellers, messages and proposal. Inquiries from your website form arrive as new cards.",
  "help.u.clients.t": "Clients and their details",
  "help.u.clients.b":
    "Every traveller who sends an inquiry becomes a client record your agency controls. Lists show names only: contact details, identity documents and health open on Reveal, and each view is written to your agency's access log (Settings > Access log).",
  "help.u.proposals.t": "Proposals",
  "help.u.proposals.b":
    "Open a trip and its Proposal tab. Build a version with its lines, total, deposit and booking terms, then choose Send to the client; the client reads and accepts it on their own page. To change a proposal you have sent, make a New version: the old one is marked Replaced.",
  "help.u.inbox.t": "The inbox",
  "help.u.inbox.b":
    "Client mail forwarded to your agency lands on its trip. The Inbox holds only mail that could not be matched to a client or a trip: link it to a client, and the whole thread moves to that client.",
  "help.u.tasks.t": "Tasks and automations",
  "help.u.tasks.b":
    "Tasks lists everything to do across your trips, by due date. A task due relative to departure waits until the trip has dates. Automations add tasks for you when a trip enters a stage, on a date of the trip, or on a client's special date.",
  "help.data.title": "Your data",
  "help.data.body":
    "Where your agency's data is kept, how it is protected and which companies may receive it are on two public pages you can share with your clients. Your agency's own rules (trash, archive, exports, support access) are in Settings > Data and privacy.",
  "help.data.page": "How we handle your data",
  "help.data.subprocessors": "Sub-processors",
  "help.data.settings": "Settings > Data and privacy",
  "help.preview.title": "Preview",
  "help.preview.body":
    "Your agency is on preview: you see these features before every agency gets them. Adventure Asia switches preview on or off for your agency; ask the team if you want to leave it.",
  "data.publicPages": "Public pages you can share with clients:",
} as const;

export const helpVi: Record<keyof typeof helpEn, string> = {
  "help.whatsNew.title": "Tính năng mới",
  "help.whatsNew.subtitle": "Các tính năng mới của ứng dụng đại lý, mới nhất ở trên.",
  "help.whatsNew.previewBadge": "Xem trước",
  "help.whatsNew.open": "Mở",
  "help.gettingStarted.title": "Bắt đầu",
  "help.using.title": "Dùng CRM",
  "help.u.pipeline.t": "Quy trình bán",
  "help.u.pipeline.b":
    "Mỗi chuyến đi là một thẻ trên bảng Quy trình bán, đi qua các giai đoạn từ Yêu cầu mới tới Hoàn tất (hoặc Không chốt, Đã huỷ). Mỗi thẻ ghi bước tiếp theo đang chờ ai. Mở thẻ để vào chính chuyến đi: khách đi, tin nhắn và báo giá. Yêu cầu từ biểu mẫu trên website của bạn hiện thành thẻ mới.",
  "help.u.clients.t": "Khách hàng và thông tin của khách",
  "help.u.clients.b":
    "Mỗi khách gửi yêu cầu thành một hồ sơ khách hàng do đại lý quản lý. Danh sách chỉ hiện tên: thông tin liên hệ, giấy tờ tuỳ thân và sức khoẻ chỉ hiện khi bấm Xem, và mỗi lần xem được ghi vào nhật ký truy cập của đại lý (Settings > Access log).",
  "help.u.proposals.t": "Báo giá",
  "help.u.proposals.b":
    "Mở một chuyến đi và thẻ Báo giá. Lập một phiên bản với các dòng dịch vụ, tổng tiền, tiền cọc và điều khoản đặt chỗ, rồi chọn Gửi cho khách; khách đọc và chấp nhận trên trang riêng của mình. Muốn đổi báo giá đã gửi, hãy tạo Phiên bản mới: bản cũ được đánh dấu Đã thay.",
  "help.u.inbox.t": "Hộp thư đến",
  "help.u.inbox.b":
    "Thư của khách chuyển tiếp tới đại lý sẽ nằm trên chuyến đi của nó. Hộp thư đến chỉ giữ thư chưa ghép được với khách hay chuyến đi: gắn thư với một khách, cả chuỗi thư chuyển sang khách đó.",
  "help.u.tasks.t": "Công việc và tự động hoá",
  "help.u.tasks.b":
    "Trang Công việc liệt kê mọi việc cần làm trên các chuyến đi, theo hạn. Việc có hạn tính theo ngày khởi hành sẽ chờ tới khi chuyến đi có ngày. Tự động hoá thêm công việc giúp bạn khi chuyến đi sang một giai đoạn, vào một ngày của chuyến đi, hoặc vào ngày đặc biệt của khách.",
  "help.data.title": "Dữ liệu của bạn",
  "help.data.body":
    "Dữ liệu của đại lý được lưu ở đâu, được bảo vệ thế nào và những công ty nào có thể nhận dữ liệu đó có trên hai trang công khai, bạn có thể gửi cho khách. Quy tắc riêng của đại lý (thùng rác, lưu trữ, xuất dữ liệu, quyền hỗ trợ) nằm ở Settings > Data and privacy.",
  "help.data.page": "Cách chúng tôi xử lý dữ liệu của bạn",
  "help.data.subprocessors": "Bên xử lý phụ",
  "help.data.settings": "Settings > Data and privacy",
  "help.preview.title": "Xem trước",
  "help.preview.body":
    "Đại lý của bạn đang ở chế độ xem trước: bạn thấy các tính năng này trước khi mọi đại lý cùng có. Adventure Asia bật hoặc tắt chế độ xem trước cho đại lý; hãy báo đội nếu bạn muốn rời chế độ này.",
  "data.publicPages": "Trang công khai bạn có thể gửi cho khách:",
};
