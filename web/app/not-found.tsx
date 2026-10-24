// web/app/not-found.tsx — AA-935.
import { CenteredCard } from "@/components/CenteredCard";
import { K } from "@/app/_kit";

export default function NotFound() {
  return (
    <CenteredCard testId="crm-not-found">
      <h1 style={{ margin: "0 0 6px", fontSize: 18, color: K.ink }}>404</h1>
      <p style={{ margin: 0, fontSize: 13.5, color: K.muted }}>
        This page does not exist. <a href="/">Home</a>
      </p>
    </CenteredCard>
  );
}
