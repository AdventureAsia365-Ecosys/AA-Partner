"""AA-945: parsing a forwarded mail (stdlib email): recipient, trip token (card E), threading hashes, bodies,
attachments, the Gmail forwarding code; and the composer's MIME message."""

from __future__ import annotations

import email
import email.policy

import pytest

from app.mail import compose
from app.mail.parse import (
    MAX_RAW_BYTES,
    UnparsableMail,
    html_to_text,
    id_hash,
    normalise_address,
    normalise_id,
    parse,
)
from tests.unit.mail_fakes import INGEST_DOMAIN, SES_AUTHENTICATED, raw_mail

SES_BY = "by inbound-smtp.us-west-1.amazonaws.com with SMTP id q1w2e3"


def test_a_plain_mail() -> None:
    mail = parse(raw_mail(), ingest_domain=INGEST_DOMAIN)
    assert mail.local_part == "sunshine-ab12cd"
    assert mail.from_address == "lan@example.test"
    assert mail.subject == "Our trip to Hoi An"
    assert mail.text.startswith("Hello")
    assert mail.html is None
    assert mail.message_id == "abc-1@mail.example.test"
    assert mail.message_id_hash == id_hash("<ABC-1@mail.example.test>")
    assert mail.trip_token is None and mail.forwarding_code is None
    assert mail.date is not None and mail.date.utcoffset() is not None


def test_vietnamese_html_only_mail_gets_a_text_body() -> None:
    html = "<html><head><style>p{}</style></head><body><p>Xin chào,</p><p>Chúng tôi muốn đi Đà Lạt &amp; Huế</p></body>"
    mail = parse(raw_mail(text=None, html=html, subject="Chuyến đi Đà Lạt"), ingest_domain=INGEST_DOMAIN)
    assert "Xin chào" in mail.text and "Đà Lạt & Huế" in mail.text and "p{}" not in mail.text
    assert mail.html is not None and mail.subject == "Chuyến đi Đà Lạt"


def test_card_e_a_forwarded_thread_without_references_carries_the_trip_token() -> None:
    mail = parse(raw_mail(subject="Fwd: Re: Hoi An [ref:ref000000001]", references=None), ingest_domain=INGEST_DOMAIN)
    assert mail.trip_token == "ref000000001"
    assert mail.references_hashes == () and mail.in_reply_to_hash is None


def test_plus_tag_is_a_trip_token_and_the_envelope_header_wins() -> None:
    # Gmail auto-forward keeps the agency's own address in To; SES's Received header names the real recipient.
    raw = raw_mail(
        to="Sunshine Travel <hello@sunshine.example>",
        extra={"Received": f"from mail.google.com {SES_BY} for sunshine-ab12cd+a1b2c3d4e5@{INGEST_DOMAIN}; Sat"},
    )
    mail = parse(raw, ingest_domain=INGEST_DOMAIN)
    assert mail.local_part == "sunshine-ab12cd" and mail.plus_tag == "a1b2c3d4e5" and mail.trip_token == "a1b2c3d4e5"
    other = parse(raw_mail(to="x@other.example"), ingest_domain=INGEST_DOMAIN)
    assert other.local_part is None


def test_only_the_topmost_ses_received_header_names_the_recipient() -> None:
    """Review S250: Received headers below SES's were written by the sender; with SES's own envelope recipient they
    are ignored, and without SES's header (or without its ``for`` clause) the forwarding headers and To / Cc decide."""
    forged = f"Received: from evil.example by mx.evil.example for <other-agency1@{INGEST_DOMAIN}>; Sat\r\n".encode()
    ses = f"Received: from evil.example {SES_BY} for sunshine-ab12cd@{INGEST_DOMAIN}; Sat\r\n".encode()
    body = raw_mail(to=f"other-agency1@{INGEST_DOMAIN}")
    assert parse(ses + forged + body, ingest_domain=INGEST_DOMAIN).local_part == "sunshine-ab12cd"
    # No SES header on top: the sender's Received line is never read; To decides.
    assert parse(forged + body, ingest_domain=INGEST_DOMAIN).local_part == "other-agency1"
    # SES omits "for" when the mail has several recipients: X-Forwarded-To, then To / Cc.
    no_for = f"Received: from mail.google.com {SES_BY}; Sat\r\n".encode()
    fwd = raw_mail(to="hello@sunshine.example", extra={"X-Forwarded-To": f"sunshine-ab12cd@{INGEST_DOMAIN}"})
    assert parse(no_for + fwd, ingest_domain=INGEST_DOMAIN).local_part == "sunshine-ab12cd"
    # SES's recipient outside the ingest domain: nothing (never a guess from the headers).
    wrong = f"Received: from x {SES_BY} for someone@elsewhere.example; Sat\r\n".encode()
    assert parse(wrong + body, ingest_domain=INGEST_DOMAIN).local_part is None


def test_sender_authenticated_needs_ses_verdicts_and_an_aligned_signature() -> None:
    def auth(sender: str = "hello@sunshine.example", **over: str) -> bool:
        raw = raw_mail(sender=sender, extra={**SES_AUTHENTICATED, **over})
        return parse(raw, ingest_domain=INGEST_DOMAIN).sender_authenticated

    assert auth()
    assert auth(**{"Authentication-Results": "amazonses.com; dkim=pass header.i=@mail.sunshine.example;"})
    assert auth(**{"Authentication-Results": "amazonses.com; dkim=fail; dmarc=pass header.from=sunshine.example;"})
    assert not auth("hello@other.example")  # signed by sunshine.example, From another domain
    assert not auth(**{"X-SES-Virus-Verdict": "FAIL"})
    assert not auth(**{"Authentication-Results": "mx.evil.example; dkim=pass header.i=@sunshine.example;"})
    assert not auth(**{"Authentication-Results": "amazonses.com; dkim=pass header.i=@evilsunshine.example;"})
    # Only the topmost header is read (SES prepends its own): a second one further down never counts.
    forged = b"Authentication-Results: amazonses.com; dkim=fail;\r\n"
    raw = forged + raw_mail(sender="hello@sunshine.example", extra=SES_AUTHENTICATED)
    assert not parse(raw, ingest_domain=INGEST_DOMAIN).sender_authenticated


def test_reply_headers_are_hashed() -> None:
    mail = parse(raw_mail(in_reply_to="<p2@x.test>", references="<p1@x.test> <p2@x.test>"), ingest_domain=INGEST_DOMAIN)
    assert mail.in_reply_to_hash == id_hash("p2@x.test")
    assert mail.references_hashes == (id_hash("p1@x.test"), id_hash("p2@x.test"))


def test_attachments_and_the_gmail_forwarding_code() -> None:
    pdf = b"%PDF-1.4 tiny"
    mail = parse(raw_mail(attachments=[("visa.pdf", "application/pdf", pdf)]), ingest_domain=INGEST_DOMAIN)
    assert [(a.filename, a.content_type, a.data) for a in mail.attachments] == [("visa.pdf", "application/pdf", pdf)]
    code = parse(
        raw_mail(
            sender="Gmail Team <forwarding-noreply@google.com>",
            subject="(#123456789) Gmail Forwarding Confirmation - Receive Mail from hello@sunshine.example",
            text="Confirmation code: 123456789\nTo allow ...",
        ),
        ingest_domain=INGEST_DOMAIN,
    )
    assert code.forwarding_code == "123456789"
    subject_only = parse(
        raw_mail(sender="forwarding-noreply@google.com", subject="(#987654321) Gmail Forwarding", text="hi"),
        ingest_domain=INGEST_DOMAIN,
    )
    assert subject_only.forwarding_code == "987654321"


def test_unparsable_and_odd_values() -> None:
    with pytest.raises(UnparsableMail):
        parse(b"", ingest_domain=INGEST_DOMAIN)
    with pytest.raises(UnparsableMail):
        parse(b"x" * (MAX_RAW_BYTES + 1), ingest_domain=INGEST_DOMAIN)
    no_id = parse(raw_mail(message_id=None, extra={"Date": "not a date"}), ingest_domain=INGEST_DOMAIN)
    assert no_id.message_id_hash is None
    assert normalise_id("no-at-sign") is None and normalise_id(None) is None and id_hash("") is None
    assert normalise_address("Not an address") is None and normalise_address("A@B.test") == "a@b.test"
    assert html_to_text("<script>x()</script><div>a</div><br>b") == "a\nb"


def test_compose_tags_the_subject_once_and_threads_the_reply() -> None:
    built = compose.build(
        from_address="hello@sunshine.example",
        from_name="Sunshine Travel",
        to_address="lan@example.test",
        subject="Re: Our trip",
        text="Thanks, Lan!",
        mail_token="ref000000001",
        in_reply_to="abc-1@mail.example.test",
    )
    msg = email.message_from_bytes(built.raw, policy=email.policy.default)
    assert msg["Subject"] == "Re: Our trip [ref:ref000000001]"
    assert msg["Message-ID"].strip("<>") == built.message_id and built.message_id.endswith("@sunshine.example")
    # References: the client's mail, then our own Message-ID last (S252: Gmail's API replaces the Message-ID; the
    # client's reply still carries ours through References).
    assert msg["In-Reply-To"] == "<abc-1@mail.example.test>"
    assert msg["References"].split() == ["<abc-1@mail.example.test>", msg["Message-ID"]]
    assert "Sunshine Travel" in msg["From"]
    assert compose.tagged_subject("Hi [ref:ref000000001]", "ref000000001") == "Hi [ref:ref000000001]"
    assert compose.tagged_subject("  Hi  there ", None) == "Hi there"
