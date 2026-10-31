// web/app/staff/(console)/page.tsx — /staff: the first section the staff member's groups open (AA-1019).
import { redirect } from "next/navigation";
import { homeFor } from "@/lib/staffConsole";
import { staffContext } from "@/lib/staffContext";

export const dynamic = "force-dynamic";

export default async function StaffHome() {
  const { session } = await staffContext();
  redirect(homeFor(session?.groups ?? []));
}
