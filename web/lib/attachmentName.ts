// web/lib/attachmentName.ts
// AA-1030 — the file name of an API `Content-Disposition` the BFF passes on for a download route (lib/bff.ts
// `download`), and the name bffDownload saves under. Only a plain CSV file name is kept (letters, digits, `.`, `_`, `-`);
// anything else is null, so a header is rebuilt from a checked name and never copied as it came.

const ATTACHMENT_NAME = /^attachment; filename="([A-Za-z0-9][A-Za-z0-9._-]{0,150}\.csv)"$/;

export function attachmentName(disposition: string | null): string | null {
  const m = disposition ? ATTACHMENT_NAME.exec(disposition.trim()) : null;
  return m ? m[1] : null;
}
