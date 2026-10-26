// @vitest-environment jsdom
// AA-945 (CRM-17) web, component: Settings > Mailbox > Import mail history. The kind follows the file name (with an
// override), a file over 25 MB is refused before any upload, an upload is followed by progress polls that stop on
// done / failed, and API errors are shown plainly.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MailImport } from "@/components/mail/MailImport";
import { MESSAGES } from "@/lib/i18n";
import { IMPORTS_STORAGE_KEY, MAX_IMPORT_BYTES, type MailImport as Row } from "@/lib/mailImport";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const ID = "0f0e0d0c-0b0a-4908-8706-050403020100";
const EN = MESSAGES.en;

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
  window.sessionStorage.clear();
  vi.unstubAllGlobals();
});

async function mount(): Promise<HTMLElement> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  await act(async () => {
    const root = createRoot(container);
    roots.push(root);
    root.render(createElement(MailImport, { lang: "en", pollMs: 5 }));
  });
  return container;
}

function file(name: string, size = 120): File {
  const f = new File(["From a@example.test\n"], name);
  Object.defineProperty(f, "size", { value: size });
  return f;
}

async function pick(c: HTMLElement, f: File) {
  const input = c.querySelector('[data-testid="mail-import-file"]') as HTMLInputElement;
  Object.defineProperty(input, "files", { value: [f], configurable: true });
  await act(async () => {
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

function startButton(c: HTMLElement): HTMLButtonElement {
  return c.querySelector('[data-testid="mail-import-start"]')!.closest("button") as HTMLButtonElement;
}

function row(status: Row["status"], extra: Partial<Row> = {}): Row {
  return {
    mail_import_id: ID,
    source_kind: "csv",
    size_bytes: 120,
    status,
    total_count: 12,
    imported_count: 9,
    duplicate_count: 2,
    skipped_count: 1,
    failed_count: 0,
    error_code: null,
    created_at: "2026-10-26T01:00:00Z",
    finished_at: status === "done" || status === "failed" ? "2026-10-26T01:01:00Z" : null,
    ...extra,
  };
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/** A fake XMLHttpRequest answering every upload with `status` / `body`; `opened` collects the URLs. */
function fakeXhr(status: number, body: unknown) {
  const opened: string[] = [];
  class FakeXhr {
    status = status;
    responseText = JSON.stringify(body);
    withCredentials = false;
    upload: { onprogress: ((e: ProgressEvent) => void) | null } = { onprogress: null };
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;
    ontimeout: (() => void) | null = null;
    open(_method: string, url: string) {
      opened.push(url);
    }
    setRequestHeader() {}
    send() {
      setTimeout(() => this.onload?.(), 0);
    }
  }
  vi.stubGlobal("XMLHttpRequest", FakeXhr);
  return opened;
}

async function settle(ms = 40) {
  await act(async () => {
    await new Promise((r) => setTimeout(r, ms));
  });
}

describe("Import mail history", () => {
  it("explains the accepted files and shows the empty state", async () => {
    const c = await mount();
    expect(c.textContent).toContain(EN["mail.import.helpMbox"]);
    expect(c.textContent).toContain(EN["mail.import.helpCsv"]);
    expect(c.textContent).toContain(EN["mail.import.helpLimits"]);
    expect(c.querySelector('[data-testid="mail-import-empty"]')).not.toBeNull();
    expect(startButton(c).disabled).toBe(true);
  });

  it("takes the kind from the file name, flags a name it cannot read, and lets the member change it", async () => {
    const c = await mount();
    const kind = () => (c.querySelector('[data-testid="mail-import-kind"]') as HTMLSelectElement).value;
    await pick(c, file("clients.csv"));
    expect(kind()).toBe("csv");
    expect(c.querySelector('[data-testid="mail-import-guess"]')).toBeNull();
    await pick(c, file("Inbox"));
    expect(kind()).toBe("mbox");
    expect(c.querySelector('[data-testid="mail-import-guess"]')).not.toBeNull();
    const select = c.querySelector('[data-testid="mail-import-kind"]') as HTMLSelectElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(select, "csv");
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(kind()).toBe("csv");
    expect(c.querySelector('[data-testid="mail-import-guess"]')).toBeNull();
  });

  it("refuses a file over 25 MB before any upload", async () => {
    const opened = fakeXhr(202, {});
    const c = await mount();
    await pick(c, file("big.mbox", MAX_IMPORT_BYTES + 1));
    expect(c.querySelector('[data-testid="mail-import-message"]')?.textContent).toBe(EN["mail.error.import_too_large"]);
    expect(startButton(c).disabled).toBe(true);
    await act(async () => startButton(c).click());
    expect(opened).toEqual([]);
  });

  it("uploads, then polls the status until done and shows the counts; no poll after done", async () => {
    const opened = fakeXhr(202, { mail_import_id: ID, status: "queued", created_at: "2026-10-26T01:00:00Z" });
    const answers = [row("running", { total_count: 3, imported_count: 3, duplicate_count: 0, skipped_count: 0 }), row("done")];
    const fetchMock = vi.fn(async () => json(200, answers.length > 1 ? answers.shift() : answers[0]));
    vi.stubGlobal("fetch", fetchMock);
    const c = await mount();
    await pick(c, file("clients.csv"));
    await act(async () => startButton(c).click());
    await settle();
    expect(opened).toEqual(["/bff/mail/imports?source_kind=csv"]);
    expect(c.querySelector('[data-testid="mail-import-message"]')?.textContent).toBe(EN["mail.import.started"]);
    const item = c.querySelector(`[data-testid="mail-import-row-${ID}"]`) as HTMLElement;
    expect(item.dataset.status).toBe("done");
    expect(item.textContent).toContain(EN["mail.import.status.done"]);
    expect(c.querySelector(`[data-testid="mail-import-imported_count-${ID}"]`)?.textContent).toBe("9");
    expect(c.querySelector(`[data-testid="mail-import-duplicate_count-${ID}"]`)?.textContent).toBe("2");
    const calls = fetchMock.mock.calls.length;
    expect(calls).toBe(2);
    expect(String((fetchMock.mock.calls[0] as unknown as [string])[0])).toBe(`/bff/mail/imports/${ID}`);
    await settle(60);
    expect(fetchMock.mock.calls.length).toBe(calls);
    expect(JSON.parse(window.sessionStorage.getItem(IMPORTS_STORAGE_KEY) ?? "[]")).toEqual([{ id: ID, kind: "csv" }]);
  });

  it("picks up the imports of this tab after a reload and shows why a failed one failed", async () => {
    window.sessionStorage.setItem(IMPORTS_STORAGE_KEY, JSON.stringify([{ id: ID, kind: "csv" }]));
    const fetchMock = vi.fn(async () => json(200, row("failed", { error_code: "csv_columns" })));
    vi.stubGlobal("fetch", fetchMock);
    const c = await mount();
    await settle();
    expect(c.querySelector(`[data-testid="mail-import-failure-${ID}"]`)?.textContent).toBe(EN["mail.import.failure.csv_columns"]);
    expect(c.querySelector(`[data-testid="mail-import-row-${ID}"]`)?.textContent).toContain(EN["mail.import.status.failed"]);
    await settle(40);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("shows the API's 413 as the size message, known codes (incl. the BFF's upload_busy) by their text, an unknown 4xx by the API's message", async () => {
    const c = await mount();
    fakeXhr(413, { detail: "import_too_large", message: "at most 25 MB" });
    await pick(c, file("a.mbox"));
    await act(async () => startButton(c).click());
    await settle();
    expect(c.querySelector('[data-testid="mail-import-message"]')?.textContent).toBe(EN["mail.error.import_too_large"]);

    fakeXhr(503, { detail: "mail_ingest_not_configured", message: "the mail ingest bucket is not configured" });
    await act(async () => startButton(c).click());
    await settle();
    expect(c.querySelector('[data-testid="mail-import-message"]')?.textContent).toBe(EN["mail.error.mail_ingest_not_configured"]);

    fakeXhr(503, { detail: "upload_busy" });
    await act(async () => startButton(c).click());
    await settle();
    expect(c.querySelector('[data-testid="mail-import-message"]')?.textContent).toBe(EN["mail.error.upload_busy"]);

    fakeXhr(422, { detail: "some_new_rule", message: "The mailbox export is from another member." });
    await act(async () => startButton(c).click());
    await settle();
    const msg = c.querySelector('[data-testid="mail-import-message"]') as HTMLElement;
    expect(msg.textContent).toBe("The mailbox export is from another member.");
    expect(msg.getAttribute("role")).toBe("alert");
    expect(c.querySelector('[data-testid="mail-import-empty"]')).not.toBeNull();
  });

  it("stops polling on a 404 and says so", async () => {
    window.sessionStorage.setItem(IMPORTS_STORAGE_KEY, JSON.stringify([{ id: ID, kind: "mbox" }]));
    const fetchMock = vi.fn(async () => json(404, { detail: "mail_import_not_found" }));
    vi.stubGlobal("fetch", fetchMock);
    const c = await mount();
    await settle(60);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(c.querySelector(`[data-testid="mail-import-error-${ID}"]`)?.textContent).toContain(EN["mail.error.mail_import_not_found"]);
  });
});
