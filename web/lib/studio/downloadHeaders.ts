// web/lib/studio/downloadHeaders.ts
// AA-1040 (EU AI Act Art. 50(2)) — the response headers of a Content studio download that the tenant BFF
// (app/api/tenant/[...path]/route.ts) passes on from CIS, on top of the Content-Type that lib/proxyResponse.ts (a
// vendored CIS copy, unchanged) forwards:
//   - the machine-readable AI mark CIS sends on every piece download (services/acp_content_writing/ai_marking.py
//     response_headers): X-AI-Generated, X-AI-Generator, X-AI-Model;
//   - the file name of a download (the T2 DOCX: `attachment; filename=tour_<id>_v<n>.docx`).
// Each value is checked and the header rebuilt, never copied as it came (the AA-1030 rule of lib/attachmentName.ts):
// X-AI-Generated is exactly "true" / "false" (else no part of the mark passes), the generator and model are printable
// ASCII of 1 to 200 characters, and a file name is letters, digits, ".", "_", "-" ending .docx, .html or .txt.
// Pure (no Next import): the Catalog tab reads the same file name in the browser.

/** The AI mark headers passed on, in the order CIS writes them. */
export const AI_MARK_HEADERS = ["X-AI-Generated", "X-AI-Generator", "X-AI-Model"] as const;

const AI_TEXT = /^[\x20-\x7E]{1,200}$/;
const DOWNLOAD_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,150}\.(?:docx|html|txt)$/;
const DISPOSITION = /^attachment;\s*filename=(?:"([^"]*)"|([^";\s]+))$/i;

/** The checked file name of an upstream `Content-Disposition` (quoted or not); null for anything else. */
export function dispositionFileName(disposition: string | null | undefined): string | null {
  const m = disposition ? DISPOSITION.exec(disposition.trim()) : null;
  const name = m ? (m[1] ?? m[2]) : null;
  return name && DOWNLOAD_NAME.test(name) ? name : null;
}

/** The download headers to set on the BFF's answer (empty for an ordinary JSON read). */
export function studioDownloadHeaders(upstream: Headers): Record<string, string> {
  const out: Record<string, string> = {};
  const generated = upstream.get("x-ai-generated")?.trim();
  if (generated === "true" || generated === "false") {
    out["X-AI-Generated"] = generated;
    for (const name of ["X-AI-Generator", "X-AI-Model"] as const) {
      const value = upstream.get(name)?.trim();
      if (value && AI_TEXT.test(value)) out[name] = value;
    }
  }
  const file = dispositionFileName(upstream.get("content-disposition"));
  if (file) out["Content-Disposition"] = `attachment; filename="${file}"`;
  return out;
}
