// web/app/(crm)/home/page.tsx — AA-1036 (R3 slice 1, root ADR 0019 d7): the CRM home at its own path. Once the bare host
// "/" lands on the Content studio (WEB_CONTENT_HOME, proxy.ts), the sidebar's Home, the app switcher and the studio's
// "Open CRM" reach the CRM home here; "/" keeps serving the same page until then.
export { default } from "../page";

export const dynamic = "force-dynamic";
