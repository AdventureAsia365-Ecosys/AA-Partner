// web/app/(studio)/portal/help/facebook/facebookGuide.messages.ts
// AA-1036 (R3) — the "Connect your Facebook Page" guide in English and Vietnamese, as structured text the page renders
// (runs of plain, bold, code, links; numbered and bulleted lists; notes). Meta's own button and menu names stay in
// English in both languages: Meta's developer tools show them in English.
import { defineMessages, type Lang } from "@/app/_kit/core/i18n";

export const META = {
  apps: "https://developers.facebook.com/apps",
  explorer: "https://developers.facebook.com/tools/explorer/",
  debugger: "https://developers.facebook.com/tools/debug/accesstoken/",
  tokensDoc: "https://developers.facebook.com/docs/facebook-login/guides/access-tokens/get-long-lived",
  appModesDoc: "https://developers.facebook.com/docs/development/build-and-test/app-modes",
} as const;

/** A run: plain, bold, code, an external link (new tab), an in-app link, or the three permission names. */
export type Run = string | { b: string } | { c: string } | { ext: string; href: string } | { link: string; href: string } | { perms: true };
export type GuideBlock = { p: Run[] } | { muted: Run[] } | { ol: Run[][] } | { ul: Run[][] } | { note: Run[] };

export interface FacebookGuide {
  title: string;
  sub: string;
  before: Run[][];
  beforeNote: Run[];
  steps: { title: string; blocks: GuideBlock[] }[];
  tip: Run[];
  problems: { title: string; cause: Run[]; fix: Run[] }[];
}

export const GUIDE_CHROME = defineMessages(
  {
    back: "Back to Integrations",
    beforeTitle: "Before you start",
    steps: "Steps",
    problems: "Common problems",
    why: "Why:",
    fix: "Fix:",
  },
  {
    back: "Về trang Kết nối",
    beforeTitle: "Trước khi bắt đầu",
    steps: "Các bước",
    problems: "Sự cố thường gặp",
    why: "Nguyên nhân:",
    fix: "Cách khắc phục:",
  },
);

const EN: FacebookGuide = {
  title: "Connect your Facebook Page",
  sub: "A one-time setup of about 15 minutes. At the end you paste a Page ID and a Page Access Token into the portal, and your content can be published to your Page.",
  before: [
    ["A Facebook account that has full control of your Facebook Page (Page admin)."],
    ["A Meta developer account. It is free: sign in at ", { ext: "developers.facebook.com", href: META.apps }, " with that Facebook account and accept the terms."],
    ["The web address of your privacy policy (for example a page on your website)."],
  ],
  beforeNote: ["Meta changes its screens from time to time. If a button has a slightly different name, look for the closest match."],
  steps: [
    { title: "Create or choose a Meta app", blocks: [
      { p: ["Open ", { ext: "My Apps", href: META.apps }, ". If your business already has an app, you can use it. Otherwise click ", { b: "Create app" }, ", give it a name (for example your company name) and a contact email."] },
      { p: ["When Meta asks what the app is for, choose the option for managing a Facebook Page (for example “Manage everything on your Page”). If Meta asks for an app type instead, choose ", { b: "Business" }, "."] },
    ] },
    { title: "Add a privacy policy and switch the app to Live", blocks: [
      { p: ["In the app dashboard, open ", { b: "App settings → Basic" }, ". Fill in the ", { b: "Privacy Policy URL" }, " and a category, and save. Meta does not allow Live mode without a privacy policy."] },
      { p: ["Then switch ", { b: "App Mode" }, " from ", { b: "Development" }, " to ", { b: "Live" }, ". In newer dashboards this is the ", { b: "Publish" }, " item in the left menu."] },
      { note: ["Why this matters: while the app is in Development mode, its posts are shown only to people with a role on the app, which includes you. Your customers would see “This content isn’t available”. ", { ext: "Meta: app modes", href: META.appModesDoc }] },
    ] },
    { title: "Get a User token with the three permissions", blocks: [
      { p: ["Open the ", { ext: "Graph API Explorer", href: META.explorer }, ". On the right:"] },
      { ol: [
        ["Under ", { b: "Meta App" }, ", select your app."],
        ["Under ", { b: "User or Page" }, ", choose ", { b: "Get User Access Token" }, "."],
        ["Under ", { b: "Permissions" }, ", add ", { perms: true }, "."],
        ["Click ", { b: "Generate Access Token" }, ". In the Facebook window, select your Page and allow the permissions."],
      ] },
      { p: ["The ", { b: "Access Token" }, " field now holds a User token. It is short-lived: it expires within a few hours, so continue straight to step 4."] },
    ] },
    { title: "Make the User token long-lived", blocks: [
      { ol: [
        ["Copy the token and open the ", { ext: "Access Token Debugger", href: META.debugger }, "."],
        ["Paste the token and click ", { b: "Debug" }, "."],
        ["At the bottom, click ", { b: "Extend Access Token" }, " and copy the new token it shows."],
      ] },
      { p: ["This long-lived User token lasts about 60 days. You only need it for the next step."] },
    ] },
    { title: "Get the Page Access Token", blocks: [
      { ol: [
        ["Back in the ", { ext: "Graph API Explorer", href: META.explorer }, ", paste the long-lived User token into the ", { b: "Access Token" }, " field."],
        ["In the query box, enter ", { c: "me/accounts" }, " and click ", { b: "Submit" }, "."],
        ["Find your Page in the result. Copy its ", { c: "access_token" }, " (the Page Access Token) and its ", { c: "id" }, " (the Page ID)."],
      ] },
      { p: ["A Page token taken from a long-lived User token has no expiry date. To check, paste it into the Access Token Debugger: ", { b: "Expires" }, " should say ", { b: "Never" }, "."] },
      { p: ["Meta can still cancel the token, for example when you change your Facebook password, lose your admin role on the Page, or remove the app from your account. If that happens, repeat steps 3 to 5. ", { ext: "Meta: long-lived tokens", href: META.tokensDoc }] },
    ] },
    { title: "Find your Page ID", blocks: [
      { p: ["The ", { c: "id" }, " next to your Page in step 5 is your Page ID. You can also find it on Facebook: open your Page, then ", { b: "About → Page transparency" }, ". It is a number such as ", { c: "104123456789012" }, "."] },
    ] },
    { title: "Test & Connect in the portal", blocks: [
      { p: ["Go to ", { link: "Publish", href: "/portal/content/publish" }, ". In the ", { b: "Connect Facebook Page" }, " card, paste the Page ID and the Page Access Token, then click ", { b: "Test & Connect" }, "."] },
      { p: ["The test checks that the token can open your Page; it does not post anything. After your first post, open it in a private browser window to confirm that people who are not logged in can see it."] },
    ] },
  ],
  tip: [{ b: "Keep the Page Access Token private." }, " Anyone who has it can post to your Page. Paste it only into the portal. The portal stores it securely and never shows it again."],
  problems: [
    {
      title: "“Invalid or expired Page Access Token”",
      cause: ["The token was cut off when you copied it, it is the short-lived token from step 3 (it expires within a few hours), or Meta has cancelled it (see step 5)."],
      fix: ["Repeat steps 3 to 5 and paste the new Page token. If you were already connected, use Publish → Manage connections → Change Page or token."],
    },
    {
      title: "“Facebook Page not found — check the Page ID”",
      cause: ["The Page ID is not the numeric ID of the Page, or the token belongs to a different Page."],
      fix: ["Use the number from step 6, not the Page name or its web address, and the token that was listed next to that same Page in step 5."],
    },
    {
      title: "Publishing fails with a message about a permission",
      cause: ["The token is missing one of the three permissions, or your Page was not selected when Facebook asked which Pages the app may use."],
      fix: ["Repeat steps 3 to 5. Add all three permissions and select your Page in the Facebook window before you click Continue."],
    },
    {
      title: "The connection works, but publishing fails",
      cause: ["You probably pasted the User token instead of the Page token. The User token can open your Page, but it cannot post as your Page."],
      fix: ["Paste the ", { c: "access_token" }, " shown next to your Page in step 5."],
    },
    {
      title: "Only you can see the posts; others see “This content isn’t available”",
      cause: ["Your Meta app is still in Development mode. In that mode Meta shows the app’s posts only to people who have a role on the app, so the posts look fine to you."],
      fix: ["Switch the app to Live (step 2). Meta then shows the posts to everyone, including the ones made before the switch."],
    },
  ],
};

const VI: FacebookGuide = {
  title: "Kết nối Trang Facebook của bạn",
  sub: "Thiết lập một lần, mất khoảng 15 phút. Cuối cùng bạn dán Page ID và Page Access Token vào cổng, và nội dung của bạn có thể được đăng lên Trang.",
  before: [
    ["Một tài khoản Facebook có toàn quyền quản lý Trang Facebook của bạn (quản trị viên Trang)."],
    ["Một tài khoản nhà phát triển Meta. Tài khoản này miễn phí: đăng nhập tại ", { ext: "developers.facebook.com", href: META.apps }, " bằng tài khoản Facebook đó và chấp nhận điều khoản."],
    ["Địa chỉ web của chính sách quyền riêng tư của bạn (ví dụ một trang trên website của bạn)."],
  ],
  beforeNote: ["Meta thỉnh thoảng thay đổi giao diện. Nếu một nút có tên hơi khác, hãy chọn mục gần giống nhất."],
  steps: [
    { title: "Tạo hoặc chọn một ứng dụng Meta", blocks: [
      { p: ["Mở ", { ext: "My Apps", href: META.apps }, ". Nếu doanh nghiệp của bạn đã có ứng dụng, bạn có thể dùng ứng dụng đó. Nếu chưa, bấm ", { b: "Create app" }, ", đặt tên (ví dụ tên công ty của bạn) và nhập email liên hệ."] },
      { p: ["Khi Meta hỏi ứng dụng dùng để làm gì, hãy chọn mục quản lý Trang Facebook (ví dụ “Manage everything on your Page”). Nếu Meta hỏi loại ứng dụng, hãy chọn ", { b: "Business" }, "."] },
    ] },
    { title: "Thêm chính sách quyền riêng tư và chuyển ứng dụng sang Live", blocks: [
      { p: ["Trong bảng điều khiển ứng dụng, mở ", { b: "App settings → Basic" }, ". Điền ", { b: "Privacy Policy URL" }, " và một danh mục, rồi lưu. Meta không cho bật chế độ Live nếu chưa có chính sách quyền riêng tư."] },
      { p: ["Sau đó chuyển ", { b: "App Mode" }, " từ ", { b: "Development" }, " sang ", { b: "Live" }, ". Ở giao diện mới, đây là mục ", { b: "Publish" }, " trong menu bên trái."] },
      { note: ["Vì sao điều này quan trọng: khi ứng dụng còn ở chế độ Development, bài đăng của ứng dụng chỉ hiển thị với những người có vai trò trong ứng dụng, trong đó có bạn. Khách hàng của bạn sẽ thấy “This content isn’t available”. ", { ext: "Meta: chế độ ứng dụng", href: META.appModesDoc }] },
    ] },
    { title: "Lấy User token có đủ ba quyền", blocks: [
      { p: ["Mở ", { ext: "Graph API Explorer", href: META.explorer }, ". Ở cột bên phải:"] },
      { ol: [
        ["Tại ", { b: "Meta App" }, ", chọn ứng dụng của bạn."],
        ["Tại ", { b: "User or Page" }, ", chọn ", { b: "Get User Access Token" }, "."],
        ["Tại ", { b: "Permissions" }, ", thêm ", { perms: true }, "."],
        ["Bấm ", { b: "Generate Access Token" }, ". Trong cửa sổ Facebook, chọn Trang của bạn và cho phép các quyền."],
      ] },
      { p: ["Ô ", { b: "Access Token" }, " giờ chứa một User token. Token này ngắn hạn: hết hạn sau vài giờ, vì vậy hãy làm ngay bước 4."] },
    ] },
    { title: "Chuyển User token sang loại dài hạn", blocks: [
      { ol: [
        ["Sao chép token và mở ", { ext: "Access Token Debugger", href: META.debugger }, "."],
        ["Dán token và bấm ", { b: "Debug" }, "."],
        ["Ở cuối trang, bấm ", { b: "Extend Access Token" }, " và sao chép token mới hiện ra."],
      ] },
      { p: ["User token dài hạn này dùng được khoảng 60 ngày. Bạn chỉ cần nó cho bước tiếp theo."] },
    ] },
    { title: "Lấy Page Access Token", blocks: [
      { ol: [
        ["Quay lại ", { ext: "Graph API Explorer", href: META.explorer }, ", dán User token dài hạn vào ô ", { b: "Access Token" }, "."],
        ["Trong ô truy vấn, nhập ", { c: "me/accounts" }, " và bấm ", { b: "Submit" }, "."],
        ["Tìm Trang của bạn trong kết quả. Sao chép ", { c: "access_token" }, " (Page Access Token) và ", { c: "id" }, " (Page ID) của Trang."],
      ] },
      { p: ["Page token lấy từ User token dài hạn không có ngày hết hạn. Để kiểm tra, dán token vào Access Token Debugger: mục ", { b: "Expires" }, " phải ghi ", { b: "Never" }, "."] },
      { p: ["Meta vẫn có thể huỷ token, ví dụ khi bạn đổi mật khẩu Facebook, mất quyền quản trị Trang, hoặc gỡ ứng dụng khỏi tài khoản. Nếu vậy, hãy làm lại bước 3 tới 5. ", { ext: "Meta: token dài hạn", href: META.tokensDoc }] },
    ] },
    { title: "Tìm Page ID của bạn", blocks: [
      { p: ["Giá trị ", { c: "id" }, " cạnh Trang của bạn ở bước 5 chính là Page ID. Bạn cũng có thể tìm trên Facebook: mở Trang, rồi vào ", { b: "Giới thiệu → Tính minh bạch của Trang" }, ". Đó là một dãy số, ví dụ ", { c: "104123456789012" }, "."] },
    ] },
    { title: "Thử và kết nối trong cổng", blocks: [
      { p: ["Vào ", { link: "Đăng bài", href: "/portal/content/publish" }, ". Trong thẻ ", { b: "Kết nối Trang Facebook" }, ", dán Page ID và Page Access Token, rồi bấm ", { b: "Thử và kết nối" }, "."] },
      { p: ["Bước thử chỉ kiểm tra token có mở được Trang của bạn; không đăng gì cả. Sau bài đăng đầu tiên, hãy mở bài trong cửa sổ ẩn danh để chắc chắn người chưa đăng nhập cũng xem được."] },
    ] },
  ],
  tip: [{ b: "Giữ bí mật Page Access Token." }, " Bất kỳ ai có token đều đăng được lên Trang của bạn. Chỉ dán token vào cổng. Cổng lưu token an toàn và không bao giờ hiển thị lại."],
  problems: [
    {
      title: "“Invalid or expired Page Access Token”",
      cause: ["Token bị cắt mất khi sao chép, là token ngắn hạn từ bước 3 (hết hạn sau vài giờ), hoặc đã bị Meta huỷ (xem bước 5)."],
      fix: ["Làm lại bước 3 tới 5 và dán Page token mới. Nếu bạn đã kết nối trước đó, vào Đăng bài → Quản lý kết nối → Đổi Trang hoặc mã truy cập."],
    },
    {
      title: "“Facebook Page not found — check the Page ID”",
      cause: ["Page ID không phải mã số của Trang, hoặc token thuộc về một Trang khác."],
      fix: ["Dùng dãy số ở bước 6, không dùng tên Trang hay địa chỉ web của Trang, và dùng token nằm cạnh chính Trang đó ở bước 5."],
    },
    {
      title: "Đăng bài lỗi với thông báo về quyền",
      cause: ["Token thiếu một trong ba quyền, hoặc Trang của bạn chưa được chọn khi Facebook hỏi ứng dụng được dùng những Trang nào."],
      fix: ["Làm lại bước 3 tới 5. Thêm đủ ba quyền và chọn Trang của bạn trong cửa sổ Facebook trước khi bấm Continue."],
    },
    {
      title: "Kết nối được nhưng đăng bài bị lỗi",
      cause: ["Có thể bạn đã dán User token thay vì Page token. User token mở được Trang của bạn nhưng không đăng bài dưới danh nghĩa Trang được."],
      fix: ["Dán ", { c: "access_token" }, " nằm cạnh Trang của bạn ở bước 5."],
    },
    {
      title: "Chỉ bạn thấy bài đăng; người khác thấy “This content isn’t available”",
      cause: ["Ứng dụng Meta của bạn vẫn ở chế độ Development. Ở chế độ này Meta chỉ hiển thị bài của ứng dụng với người có vai trò trong ứng dụng, nên bạn vẫn thấy bài bình thường."],
      fix: ["Chuyển ứng dụng sang Live (bước 2). Khi đó Meta hiển thị bài cho mọi người, kể cả các bài đăng trước khi chuyển."],
    },
  ],
};

export const FACEBOOK_GUIDE: Readonly<Record<Lang, FacebookGuide>> = { en: EN, vi: VI };
