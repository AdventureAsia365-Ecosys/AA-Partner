"use client";
import { sans } from "./ui";

const GUIDANCE: Record<string, { icon: string; message: string; sub?: string }> = {
  pending:               { icon: "⏳", message: "In queue.",             sub: "AI will start shortly." },
  processing:            { icon: "✍️", message: "AI is writing…",        sub: "Usually 1–2 minutes. You can leave this page." },
  ai_generating:         { icon: "✍️", message: "AI is writing…",        sub: "Usually 1–2 minutes. You can leave this page." },
  ai_generated:          { icon: "👀", message: "Content is ready.",      sub: "Review below, then add to your catalog." },
  approved:              { icon: "✅", message: "In your catalog.",       sub: "Available via API key. Request a new version anytime." },
  needs_review:          { icon: "⚠️", message: "Needs your review.",     sub: "Please check the flagged items below." },
  rejected:              { icon: "🔄", message: "New version requested.", sub: "AI will rewrite using your brand rules." },
  new_version_requested: { icon: "🔄", message: "New version requested.", sub: "AI will rewrite using your brand rules." },
};

export function NextStepGuide({ status }: { status: string }) {
  const guide = GUIDANCE[status];
  if (!guide) return null;
  return (
    <div style={{
      display: "flex", alignItems: "flex-start", gap: 12,
      borderRadius: 8, background: "#EFF6FF", border: "1px solid #DBEAFE",
      padding: "12px 16px", marginBottom: 16, fontFamily: sans,
    }}>
      <span style={{ fontSize: 18, lineHeight: 1, marginTop: 1, flexShrink: 0 }}>{guide.icon}</span>
      <div>
        <p style={{ fontSize: 13, fontWeight: 600, color: "#1E40AF", margin: 0 }}>{guide.message}</p>
        {guide.sub && (
          <p style={{ fontSize: 11.5, color: "#2563EB", margin: "3px 0 0" }}>{guide.sub}</p>
        )}
      </div>
    </div>
  );
}
