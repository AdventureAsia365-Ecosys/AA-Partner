// e2e/helpers/mailbox.ts — AA-936: the Cognito sign-in code of the CRM e2e user, read from the SES inbound mailbox.
//
// The e2e users of the synthetic agencies (AA-CIS-Infra tools/identity/crm_dev_identity.py, AA-935) sign in by
// e-mail code; their addresses are on the e2e mail domain, whose SES receipt rule stores every message in the e2e
// mailbox bucket (AA-CIS-Infra e2e_mailbox.tf). The runner's AWS CLI reads it with the read-only mailbox role
// (fixed argv, no shell). The bucket name embeds the account id, so it is derived from the credentials at run time.
// The code is a live credential: never logged, never screenshotted.
import { execFile } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";

const run = promisify(execFile);
const REGION = "us-west-1";
const PREFIX = "inbound/";
const BUCKET_PREFIX = "aa-cis-dev-e2e-mail-";
const CLI_TIMEOUT_MS = 20_000;
const POLL_MS = 3_000;

async function aws(args: string[]): Promise<string> {
  const { stdout } = await run("aws", [...args, "--region", REGION, "--output", "json"], {
    timeout: CLI_TIMEOUT_MS,
    maxBuffer: 16 * 1024 * 1024,
  });
  return stdout;
}

async function bucket(): Promise<string> {
  const account = String((JSON.parse(await aws(["sts", "get-caller-identity"])) as { Account?: string }).Account ?? "");
  if (!/^\d{12}$/.test(account)) throw new Error("no account from sts get-caller-identity");
  return `${BUCKET_PREFIX}${account}`;
}

async function keys(b: string): Promise<{ key: string; at: number }[]> {
  const out = await aws(["s3api", "list-objects-v2", "--bucket", b, "--prefix", PREFIX]);
  const parsed = (out.trim() ? JSON.parse(out) : {}) as { Contents?: { Key: string; LastModified: string }[] };
  return (parsed.Contents ?? []).map((o) => ({ key: o.Key, at: Date.parse(o.LastModified) }));
}

async function read(b: string, key: string): Promise<string> {
  const dir = await mkdtemp(path.join(os.tmpdir(), "crm-e2e-mail-"));
  const file = path.join(dir, "m.eml");
  try {
    await aws(["s3api", "get-object", "--bucket", b, "--key", key, file]);
    return await readFile(file, "utf8");
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

/** Keys already in the mailbox: a code mail must be new. */
export async function baseline(): Promise<Set<string>> {
  return new Set((await keys(await bucket())).map((k) => k.key));
}

/** The 6-8 digit code of the newest mail to `recipient` that is not in `before` (bounded wait). */
export async function waitForCode(recipient: string, before: Set<string>, timeoutMs = 90_000): Promise<string> {
  const b = await bucket();
  const deadline = Date.now() + timeoutMs;
  const needle = recipient.toLowerCase();
  while (Date.now() < deadline) {
    const fresh = (await keys(b)).filter((k) => !before.has(k.key)).sort((x, y) => y.at - x.at);
    for (const { key } of fresh) {
      const raw = await read(b, key);
      const head = raw.split(/\r?\n\r?\n/)[0].replace(/\r?\n[ \t]+/g, " ").toLowerCase();
      if (!head.split(/\r?\n/).some((l) => /^(to|delivered-to|x-original-to):/.test(l) && l.includes(needle))) continue;
      const body = raw.replace(/=\r?\n/g, "");
      const code = /(?:code|mã)[^0-9]{0,80}(\d{6,8})/i.exec(body)?.[1] ?? /\b(\d{6,8})\b/.exec(body)?.[1];
      if (code) return code;
    }
    await new Promise((r) => setTimeout(r, POLL_MS));
  }
  throw new Error("no sign-in code mail arrived in time");
}


/** AA-939: the join link (/join?t=<token>) of the newest invitation mail to `recipient` not in `before`. */
export async function waitForInviteLink(recipient: string, before: Set<string>, timeoutMs = 90_000): Promise<string> {
  const b = await bucket();
  const deadline = Date.now() + timeoutMs;
  const needle = recipient.toLowerCase();
  while (Date.now() < deadline) {
    const fresh = (await keys(b)).filter((k) => !before.has(k.key)).sort((x, y) => y.at - x.at);
    for (const { key } of fresh) {
      const raw = await read(b, key);
      const head = raw.split(/\r?\n\r?\n/)[0].replace(/\r?\n[ \t]+/g, " ").toLowerCase();
      if (!head.split(/\r?\n/).some((l) => /^(to|delivered-to|x-original-to):/.test(l) && l.includes(needle))) continue;
      const body = raw.replace(/=\r?\n/g, "").replace(/=3D/g, "=");
      const link = /(https:\/\/[^\s"<>]+\/join\?t=[A-Za-z0-9_-]{43})/.exec(body)?.[1];
      if (link) return link;
    }
    await new Promise((r) => setTimeout(r, POLL_MS));
  }
  throw new Error("no invitation mail arrived in time");
}
