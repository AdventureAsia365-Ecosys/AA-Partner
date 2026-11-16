// web/lib/whatsNew.ts
// AA-970 (design v3 card CRM-37b, R42) — the What's new feed of Help & what's new, in English and Vietnamese. The
// entries ship with the release that brings the feature (reviewed in its pull request, like the help pages), newest
// first. The design's release_note table is not built: a note only changes with a release, so the code release is
// its source (docs/implementation-notes/AA-970.md, "Decisions").
//
// An entry with `preview` describes a preview feature (lib/preview.ts): only an agency on preview sees it. A released
// entry is for every agency; its link is shown only to a member whose role sees that part of the app (lib/nav.ts),
// the entry itself to everyone. Feature words only: no internal ids (tests/noIssueIds.test.ts reads the web).

import type { Lang } from "./i18n";
import { visibleSections, type Role } from "./nav";
import { isPreviewFlag, previewOn, type PreviewFlag } from "./preview";

export interface WhatsNewEntry {
  /** Stable id (anchors, tests). */
  id: string;
  /** The day it reached the agencies (YYYY-MM-DD). */
  date: string;
  /** The nav section (lib/nav.ts key) the link opens; the link is hidden from a role that does not see it. */
  section?: string;
  href?: string;
  /** A preview feature's flag: shown only to an agency on preview. */
  preview?: PreviewFlag;
  text: Record<Lang, { title: string; body: string }>;
}

export const WHATS_NEW: readonly WhatsNewEntry[] = [
  {
    id: "preview-channel",
    date: "2026-11-16",
    section: "help",
    href: "/help#preview",
    preview: "help.preview-panel",
    text: {
      en: {
        title: "Your agency is on preview",
        body: "Your agency sees some new features before every agency gets them. They are listed under Preview on this page; tell the Adventure Asia team what works and what does not.",
      },
      vi: {
        title: "Đại lý của bạn đang ở chế độ xem trước",
        body: "Đại lý của bạn thấy một số tính năng mới trước khi mọi đại lý cùng có. Chúng được liệt kê ở mục Xem trước trên trang này; hãy cho đội Adventure Asia biết điều gì dùng tốt và điều gì chưa.",
      },
    },
  },
  {
    id: "help-centre",
    date: "2026-11-16",
    section: "help",
    href: "/help#using",
    text: {
      en: {
        title: "More help topics, and how we handle your data",
        body: "Help now covers the pipeline, clients, proposals, the inbox and tasks. Two public pages say where your agency's data is kept and which companies may receive it.",
      },
      vi: {
        title: "Thêm chủ đề hướng dẫn, và cách chúng tôi xử lý dữ liệu",
        body: "Trang Hướng dẫn nay có thêm quy trình bán, khách hàng, báo giá, hộp thư và công việc. Hai trang công khai cho biết dữ liệu của đại lý được lưu ở đâu và những công ty nào có thể nhận dữ liệu đó.",
      },
    },
  },
  {
    id: "ai-supplier-voices",
    date: "2026-11-14",
    section: "settings",
    href: "/settings/ai",
    text: {
      en: {
        title: "AI drafts for supplier confirmations, and writing voices",
        body: "With the AI assistant on, the CRM can draft a confirmation request to a supplier and turn your notes into suggested AA options. Each member can keep a few writing voices for reply drafts. A person always reviews a draft before anything is sent.",
      },
      vi: {
        title: "AI soạn thư xác nhận nhà cung cấp, và giọng văn",
        body: "Khi bật trợ lý AI, CRM có thể soạn thư xin xác nhận gửi nhà cung cấp và biến ghi chú của bạn thành gợi ý lựa chọn AA. Mỗi thành viên giữ được vài giọng văn cho bản nháp trả lời. Luôn có người xem lại bản nháp trước khi gửi.",
      },
    },
  },
  {
    id: "signed-pdf",
    date: "2026-11-14",
    section: "pipeline",
    href: "/pipeline",
    text: {
      en: {
        title: "Signed PDF of an e-signed agreement",
        body: "Once a client signs, the agency and the client can download the signed PDF of the agreement.",
      },
      vi: {
        title: "Tệp PDF đã ký của thoả thuận ký điện tử",
        body: "Khi khách đã ký, đại lý và khách đều tải được tệp PDF đã ký của thoả thuận.",
      },
    },
  },
  {
    id: "aa-tour-on-trip",
    date: "2026-11-14",
    section: "pipeline",
    href: "/pipeline",
    text: {
      en: {
        title: "The AA tour and departure on the trip header",
        body: "A trip built on an Adventure Asia tour shows that tour and its departure at the top of the trip.",
      },
      vi: {
        title: "Tour và chuyến khởi hành của AA trên đầu trang chuyến đi",
        body: "Chuyến đi dựng từ một tour của Adventure Asia hiện tour đó và chuyến khởi hành ngay đầu trang chuyến đi.",
      },
    },
  },
  {
    id: "retired-tours",
    date: "2026-11-13",
    section: "pipeline",
    href: "/pipeline",
    text: {
      en: {
        title: "Trips on tours Adventure Asia no longer offers",
        body: "When Adventure Asia stops offering a tour, the trips built on it are flagged so you can offer the client another one in time.",
      },
      vi: {
        title: "Chuyến đi trên tour Adventure Asia không còn bán",
        body: "Khi Adventure Asia ngừng bán một tour, các chuyến đi dựng từ tour đó được đánh dấu để bạn kịp đề xuất tour khác cho khách.",
      },
    },
  },
  {
    id: "tags",
    date: "2026-11-12",
    section: "clients",
    href: "/clients",
    text: {
      en: {
        title: "Tags on trips and clients",
        body: "Tag trips and clients with your own words, use the system tags, start workflows from a tag, and find clients who are ready to travel again.",
      },
      vi: {
        title: "Nhãn cho chuyến đi và khách hàng",
        body: "Gắn nhãn của riêng bạn cho chuyến đi và khách hàng, dùng nhãn hệ thống, chạy quy trình từ một nhãn, và tìm khách sẵn sàng đi tiếp.",
      },
    },
  },
  {
    id: "inclusions",
    date: "2026-11-12",
    section: "pipeline",
    href: "/settings/inclusions",
    text: {
      en: {
        title: "Included and not included lists on proposals",
        body: "Keep your own lists of what a trip includes and does not include, and add them to a proposal; the client sees them on the proposal page and in the PDF.",
      },
      vi: {
        title: "Danh sách bao gồm và không bao gồm trên báo giá",
        body: "Giữ danh sách riêng về những gì chuyến đi bao gồm và không bao gồm, rồi thêm vào báo giá; khách thấy danh sách trên trang báo giá và trong tệp PDF.",
      },
    },
  },
  {
    id: "aa-cancel-guide",
    date: "2026-11-12",
    section: "pipeline",
    href: "/pipeline",
    text: {
      en: {
        title: "A guided flow when Adventure Asia cancels a departure",
        body: "If Adventure Asia cancels a departure, each trip on it gets a card that walks you through telling the client and choosing what happens next.",
      },
      vi: {
        title: "Hướng dẫn từng bước khi Adventure Asia huỷ một chuyến khởi hành",
        body: "Nếu Adventure Asia huỷ một chuyến khởi hành, mỗi chuyến đi trên đó có một thẻ hướng dẫn bạn báo cho khách và chọn bước tiếp theo.",
      },
    },
  },
  {
    id: "travel-ops",
    date: "2026-11-11",
    section: "departures",
    href: "/departures",
    text: {
      en: {
        title: "Travel operations",
        body: "Departure rosters, vouchers and other PDFs, the rooming list, a passport check and a visa tracker for the trips about to travel.",
      },
      vi: {
        title: "Điều hành chuyến đi",
        body: "Danh sách đoàn theo chuyến khởi hành, voucher và các tệp PDF khác, danh sách phòng, kiểm tra hộ chiếu và theo dõi visa cho các chuyến sắp đi.",
      },
    },
  },
  {
    id: "service-catalogue",
    date: "2026-11-11",
    section: "services",
    href: "/services",
    text: {
      en: {
        title: "Your service catalogue",
        body: "Keep the services you sell often in a catalogue, add them to a proposal in one step, and send booking requests to your suppliers.",
      },
      vi: {
        title: "Danh mục dịch vụ của bạn",
        body: "Lưu các dịch vụ hay bán vào danh mục, thêm vào báo giá chỉ một bước, và gửi yêu cầu đặt chỗ cho nhà cung cấp.",
      },
    },
  },
];

/** The entries this member sees, newest first: the released ones, and the preview ones only on preview. */
export function visibleEntries(me: { preview?: boolean } | null | undefined): WhatsNewEntry[] {
  return WHATS_NEW.filter((e) => !e.preview || (isPreviewFlag(e.preview) && previewOn(me, e.preview))).sort((a, b) =>
    b.date.localeCompare(a.date),
  );
}

/** The entry's link for this role, or null when the role does not see that part of the app. */
export function entryHref(entry: WhatsNewEntry, role: Role): string | null {
  if (!entry.href) return null;
  if (!entry.section) return entry.href;
  return visibleSections(role).some((s) => s.key === entry.section) ? entry.href : null;
}
