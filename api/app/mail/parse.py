"""Parse one raw RFC 5322 message (stdlib ``email``, no third-party parser) into what the ingest stores (AA-945).

Nothing here touches the database or logs content. The result carries:

- the Message-ID (and its SHA-256 over the normalised id: angle brackets removed, lower case), the In-Reply-To and
  References hashes, the subject, From, To / Cc, Date, the text body (from text/plain, else the HTML stripped to text)
  and the HTML body, capped;
- the attachments (name, declared type, bytes);
- the **forward-in recipient**: the envelope recipient from the ``for <x>`` clause of the **topmost** ``Received``
  header when SES wrote it (``by inbound-smtp.<region>.amazonaws.com``; every lower header came from the sender and is
  not trusted for this). SES leaves the clause out when a mail has several recipients; then the first address of the
  ingest domain among ``X-Original-To``, ``Delivered-To``, ``X-Forwarded-To``, ``To`` and ``Cc`` (a forwarded mail
  keeps the agency's own address in ``To``, so the forwarding headers come first). ``local+tag@`` gives the local part
  and the plus tag;
- the **trip token** (card E): ``[ref:<token>]`` in the subject, else the plus tag;
- the **Gmail forwarding confirmation code** when the mail is Google's forwarding confirmation;
- whether SES's spam or virus scan said ``FAIL`` (``X-SES-Spam-Verdict`` / ``X-SES-Virus-Verdict``);
- whether the **From domain is authenticated** (``sender_authenticated``): both scan verdicts ``PASS`` and SES's own
  ``Authentication-Results: amazonses.com`` (the topmost such header) says ``dkim=pass`` for a signature of the From
  domain or ``dmarc=pass header.from=<From domain>``. Forwarding breaks SPF / DKIM for a client's forwarded mail, so
  this never drops a mail; it only decides whether a mail **From one of the agency's mailboxes** may be stored as the
  agency's own sent copy (:func:`app.mail.ingest.store_message`). A BCC from the agency's mailbox is signed by its
  provider and passes.
"""

from __future__ import annotations

import datetime as dt
import email
import email.policy
import email.utils
import hashlib
import html
import re
from dataclasses import dataclass
from email.message import EmailMessage
from html.parser import HTMLParser

MAX_RAW_BYTES = 15 * 1024 * 1024
MAX_TEXT_CHARS = 200_000
MAX_HTML_CHARS = 500_000
MAX_SUBJECT_CHARS = 500
MAX_REFERENCES = 100
MAX_ADDRESSES = 50
TOKEN_RE = re.compile(r"\[ref:([a-z0-9]{8,16})\]", re.IGNORECASE)
PLUS_TAG_RE = re.compile(r"^[a-z0-9]{8,16}$")
RECEIVED_FOR_RE = re.compile(r"\bfor\s+<?([^\s<>;]+@[^\s<>;]+)>?", re.IGNORECASE)
GMAIL_FORWARDING_SENDER = "forwarding-noreply@google.com"
GMAIL_CODE_RE = re.compile(r"(?:confirmation code|mã xác nhận)\s*[:：]?\s*(\d{6,12})", re.IGNORECASE)
GMAIL_SUBJECT_CODE_RE = re.compile(r"\(#(\d{6,12})\)")
ENVELOPE_HEADERS = ("x-original-to", "delivered-to", "x-forwarded-to")
SES_RECEIVED_RE = re.compile(r"\bby\s+inbound-smtp\.[a-z0-9-]+\.amazonaws\.com\b", re.IGNORECASE)
SES_AUTHSERV = "amazonses.com"
DKIM_PASS_RE = re.compile(r"\bdkim=pass\b[^;]*?\bheader\.[id]=@?([^\s;]+)", re.IGNORECASE)
DMARC_PASS_RE = re.compile(r"\bdmarc=pass\b[^;]*?\bheader\.from=([^\s;]+)", re.IGNORECASE)


class UnparsableMail(ValueError):
    """The bytes are not a message the ingest can read (too large, no headers). Code only."""


@dataclass(frozen=True)
class Attachment:
    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True)
class ParsedMail:
    message_id: str | None
    message_id_hash: bytes | None
    in_reply_to_hash: bytes | None
    references_hashes: tuple[bytes, ...]
    subject: str
    from_address: str | None
    to_addresses: tuple[str, ...]
    date: dt.datetime | None
    text: str
    html: str | None
    attachments: tuple[Attachment, ...] = ()
    local_part: str | None = None
    plus_tag: str | None = None
    trip_token: str | None = None
    forwarding_code: str | None = None
    # SES receipt scan (scan_enabled): FAIL verdicts make the ingest drop the mail.
    spam_or_virus: bool = False
    # SES verdicts PASS and SES's DKIM / DMARC result aligned with the From domain (see the module docstring).
    sender_authenticated: bool = False


def id_hash(message_id: str | None) -> bytes | None:
    """SHA-256 of a normalised Message-ID (``<A@B>`` and ``a@b`` are the same id)."""
    norm = normalise_id(message_id)
    return hashlib.sha256(norm.encode("utf-8")).digest() if norm else None


def normalise_id(message_id: str | None) -> str | None:
    if not message_id:
        return None
    value = message_id.strip().strip("<>").strip().lower()
    return value if value and "@" in value and len(value) <= 500 and not any(c.isspace() for c in value) else None


def _ids(value: str | None) -> list[str]:
    """Every ``<...>`` id of a References / In-Reply-To header, in order."""
    if not value:
        return []
    found = re.findall(r"<([^<>\s]+)>", value)
    return found or value.split()


def normalise_address(value: str | None) -> str | None:
    if not value:
        return None
    _, addr = email.utils.parseaddr(value)
    addr = addr.strip().lower()
    local, at, domain = addr.partition("@")
    if not at or not local or not domain or "@" in domain or any(c.isspace() for c in addr) or len(addr) > 320:
        return None
    return addr


def _addresses(values: list[str]) -> list[str]:
    out: list[str] = []
    for _, addr in email.utils.getaddresses(values):
        norm = normalise_address(addr)
        if norm and norm not in out:
            out.append(norm)
        if len(out) >= MAX_ADDRESSES:
            break
    return out


class _TextOfHtml(HTMLParser):
    """HTML to plain text for the text column (scripts and styles dropped, block tags as line breaks)."""

    BLOCKS = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "blockquote"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(value: str) -> str:
    parser = _TextOfHtml()
    try:
        parser.feed(value)
        parser.close()
    except Exception:  # noqa: BLE001, S110 — a broken page still gives the text found so far
        pass
    text = html.unescape("".join(parser.parts))
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def _split_recipient(raw: str, domain: str) -> tuple[str | None, str | None] | None:
    addr = normalise_address(raw)
    if not addr:
        return None
    local, _, host = addr.partition("@")
    if host != domain:
        return None
    local, _, tag = local.partition("+")
    return local, (tag if PLUS_TAG_RE.match(tag) else None)


def _recipient(msg: EmailMessage, domain: str) -> tuple[str | None, str | None]:
    received = msg.get_all("received", []) or []
    if received and SES_RECEIVED_RE.search(str(received[0])):
        envelope = RECEIVED_FOR_RE.findall(str(received[0]))
        if envelope:  # SES's own envelope recipient: nothing the sender wrote may override it
            return _split_recipient(envelope[0], domain) or (None, None)
    candidates: list[str] = []
    for name in ENVELOPE_HEADERS:
        candidates += [str(v) for v in msg.get_all(name, []) or []]
    candidates += _addresses([str(v) for v in (msg.get_all("to", []) or []) + (msg.get_all("cc", []) or [])])
    for raw in candidates:
        found = _split_recipient(raw, domain)
        if found:
            return found
    return None, None


def _domain_match(value: str, domain: str) -> bool:
    value = value.strip().strip('"').lower().rpartition("@")[2]
    return value == domain or value.endswith("." + domain)


def sender_authenticated(msg: EmailMessage, from_address: str | None, verdicts: dict[str, str]) -> bool:
    """SES scanned the mail clean and authenticated the From domain (DKIM or DMARC pass, from SES's own header)."""
    if not from_address or verdicts.get("spam") != "PASS" or verdicts.get("virus") != "PASS":
        return False
    domain = from_address.rpartition("@")[2]
    results = [str(v) for v in msg.get_all("authentication-results", []) or []]
    # SES prepends its header: only the topmost one counts, and only when SES wrote it.
    if not results or results[0].strip().split(";", 1)[0].strip().lower() != SES_AUTHSERV:
        return False
    top = results[0]
    if any(_domain_match(d, domain) for d in DKIM_PASS_RE.findall(top)):
        return True
    return any(_domain_match(d, domain) for d in DMARC_PASS_RE.findall(top))


def _body(msg: EmailMessage) -> tuple[str, str | None, list[Attachment]]:
    text_parts: list[str] = []
    html_part: str | None = None
    attachments: list[Attachment] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        ctype = part.get_content_type()
        filename = part.get_filename()
        if disposition == "attachment" or (filename and disposition != "inline"):
            data = part.get_payload(decode=True) or b""
            attachments.append(Attachment(filename=(filename or "attachment")[:200], content_type=ctype, data=data))
            continue
        if ctype == "text/plain":
            try:
                text_parts.append(str(part.get_content()))
            except (LookupError, ValueError):
                text_parts.append((part.get_payload(decode=True) or b"").decode("utf-8", "replace"))
        elif ctype == "text/html" and html_part is None:
            try:
                html_part = str(part.get_content())
            except (LookupError, ValueError):
                html_part = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    text = "\n".join(t.strip("\n") for t in text_parts).strip()
    if not text and html_part:
        text = html_to_text(html_part)
    return text[:MAX_TEXT_CHARS], (html_part[:MAX_HTML_CHARS] if html_part else None), attachments


def parse(raw: bytes, *, ingest_domain: str) -> ParsedMail:
    if not raw or len(raw) > MAX_RAW_BYTES:
        raise UnparsableMail("too_large" if raw else "empty")
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    if not isinstance(msg, EmailMessage) or not msg.keys():
        raise UnparsableMail("no_headers")
    subject = " ".join(str(msg.get("subject", "") or "").split())[:MAX_SUBJECT_CHARS]
    from_address = normalise_address(str(msg.get("from", "") or ""))
    to_addresses = tuple(_addresses([str(v) for v in (msg.get_all("to", []) or []) + (msg.get_all("cc", []) or [])]))
    try:
        date = email.utils.parsedate_to_datetime(str(msg["date"])) if msg.get("date") else None
        if date is not None and date.tzinfo is None:
            date = date.replace(tzinfo=dt.UTC)
    except (TypeError, ValueError, IndexError):
        date = None
    message_id = normalise_id(str(msg.get("message-id", "") or ""))
    in_reply = _ids(str(msg.get("in-reply-to", "") or ""))
    references = _ids(str(msg.get("references", "") or ""))[-MAX_REFERENCES:]
    ref_hashes: list[bytes] = []
    for value in references + in_reply:
        digest = id_hash(value)
        if digest and digest not in ref_hashes:
            ref_hashes.append(digest)
    text, html_body, attachments = _body(msg)
    local_part, plus_tag = _recipient(msg, ingest_domain.lower())
    token_match = TOKEN_RE.search(subject)
    trip_token = token_match.group(1).lower() if token_match else plus_tag
    forwarding_code = None
    if from_address == GMAIL_FORWARDING_SENDER:
        found = GMAIL_CODE_RE.search(text) or GMAIL_SUBJECT_CODE_RE.search(subject)
        forwarding_code = found.group(1) if found else None
    verdicts = {
        "spam": str(msg.get("x-ses-spam-verdict", "")).strip().upper(),
        "virus": str(msg.get("x-ses-virus-verdict", "")).strip().upper(),
    }
    return ParsedMail(
        spam_or_virus="FAIL" in verdicts.values(),
        sender_authenticated=sender_authenticated(msg, from_address, verdicts),
        message_id=message_id,
        message_id_hash=id_hash(message_id),
        in_reply_to_hash=id_hash(in_reply[-1]) if in_reply else None,
        references_hashes=tuple(ref_hashes[-MAX_REFERENCES:]),
        subject=subject,
        from_address=from_address,
        to_addresses=to_addresses,
        date=date,
        text=text,
        html=html_body,
        attachments=tuple(attachments),
        local_part=local_part,
        plus_tag=plus_tag,
        trip_token=trip_token,
        forwarding_code=forwarding_code,
    )
