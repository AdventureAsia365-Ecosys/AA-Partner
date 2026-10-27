// portal/app/portal/create/page.tsx — AA-759: /portal/create has no page of its own; land on Social Posts.
import { redirect } from "next/navigation";
import { ROUTES } from "@/app/_kit/local/portalNav";

export default function CreateIndexPage() {
  redirect(ROUTES.createSocial);
}
