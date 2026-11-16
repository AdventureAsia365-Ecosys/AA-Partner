// web/app/(studio)/_legal/annex.messages.ts
// AA-970 (design v3 card CRM-37b) — the frame of the two public pages rendered from the root DPA Annex 3 (the page
// titles, the intro and the version line) in English and Vietnamese. The body of each page is the annex's own English
// text (lib/legal/annexMarkdown.ts), the version that applies; the Vietnamese frame says so.
import { defineMessages } from "@/app/_kit/core/i18n";

const MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

function dayEn(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  return y && m && d ? `${d} ${MONTHS_EN[m - 1]} ${y}` : iso;
}

function dayVi(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  return y && m && d ? `${d} tháng ${m} năm ${y}` : iso;
}

export const ANNEX_MESSAGES = defineMessages(
  {
    dataMeta: "How we handle your data — Adventure Asia agency app",
    dataTitle: "How we handle your data",
    dataIntro:
      "Where the data of an agency using the Adventure Asia agency app is kept, how it is protected, and which companies may receive it. This page is generated from Annex 3 of the data processing agreement.",
    subMeta: "Sub-processors — Adventure Asia agency app",
    subTitle: "Sub-processors",
    subIntro:
      "The companies that may receive your agency's data when the Adventure Asia agency app runs, what they do and where. This page is generated from Annex 3 of the data processing agreement; we tell your agency in advance before we add or replace one.",
    englishOnly: "",
    seeAlso: "See also:",
    versionLabel: "Data processing agreement, Annex 3",
    version: (v: string) => String(v).replace(/^draft /, "Draft "),
    updated: (iso: string) => `last reviewed ${dayEn(String(iso))}`,
  },
  {
    dataMeta: "Cách chúng tôi xử lý dữ liệu — ứng dụng đại lý Adventure Asia",
    dataTitle: "Cách chúng tôi xử lý dữ liệu của bạn",
    dataIntro:
      "Dữ liệu của đại lý dùng ứng dụng đại lý Adventure Asia được lưu ở đâu, được bảo vệ thế nào, và những công ty nào có thể nhận dữ liệu đó. Trang này được tạo từ Phụ lục 3 của thoả thuận xử lý dữ liệu.",
    subMeta: "Bên xử lý phụ — ứng dụng đại lý Adventure Asia",
    subTitle: "Bên xử lý phụ",
    subIntro:
      "Những công ty có thể nhận dữ liệu của đại lý khi ứng dụng đại lý Adventure Asia hoạt động, họ làm gì và ở đâu. Trang này được tạo từ Phụ lục 3 của thoả thuận xử lý dữ liệu; chúng tôi báo trước cho đại lý khi thêm hoặc thay một bên.",
    englishOnly: "Nội dung bên dưới lấy nguyên văn từ phụ lục bằng tiếng Anh, là bản được áp dụng.",
    seeAlso: "Xem thêm:",
    versionLabel: "Thoả thuận xử lý dữ liệu, Phụ lục 3",
    version: (v: string) => String(v).replace(/^draft v/, "Bản nháp v"),
    updated: (iso: string) => `rà soát lần cuối ${dayVi(String(iso))}`,
  },
);
