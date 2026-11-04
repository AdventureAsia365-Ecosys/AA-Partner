// web/app/(studio)/portal/account/page.tsx — AA-759: /portal/account has no page of its own; land on Team & Settings.
import { redirect } from "next/navigation";
import { ROUTES } from "@/app/_kit/local/studio/portalNav";

export default function AccountIndexPage() {
  redirect(ROUTES.settings);
}
