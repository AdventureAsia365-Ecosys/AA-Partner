// web/app/staff/(console)/layout.tsx — every AA staff console page (AA-1019, S-02 / S-04): the STAFF session (the proxy
// already sent a page without a valid staff token through /staff/auth/refresh; this is the second line) and the staff
// shell. The agency session is never read here.
import { redirect } from "next/navigation";
import { StaffShell } from "@/app/_kit/local/StaffShell";
import { dictionary } from "@/lib/i18n";
import { staffContext } from "@/lib/staffContext";
import { staffAccessValid } from "@/lib/staffSession";

export const dynamic = "force-dynamic";

export default async function StaffConsoleLayout({ children }: { children: React.ReactNode }) {
  const ctx = await staffContext();
  if (!ctx.cfg.staffAuthConfigured) redirect("/staff/signed-out?reason=unavailable");
  if (!staffAccessValid(ctx.session)) redirect(`/staff/auth/refresh?next=${encodeURIComponent(ctx.path)}`);
  return (
    <StaffShell
      dict={dictionary(ctx.lang)}
      groups={ctx.session.groups}
      locale={ctx.locale}
      syntheticBadge={ctx.cfg.environment !== "prod"}
    >
      {children}
    </StaffShell>
  );
}
