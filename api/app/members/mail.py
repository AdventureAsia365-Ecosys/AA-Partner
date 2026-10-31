"""AA system mail to agency people (root ADR 0006b decision 3 pointer: invitation and security mail for the partner
pool is AA system mail through AA's SES, never the agency's own mailbox; AA-939).

Two messages, each in English then Vietnamese in one mail (the person's language is not known before they sign in):

- the **invitation**: the agency's name, the role, the join link ``<PARTNER_WEB_ORIGIN>/join?t=<token>`` and the
  expiry. The token is the only secret in it; it is single-use, and its SHA-256 is all the database keeps;
- the **owner recovery notice** (Q1 6.3): AA opened an owner recovery, the date it can complete, and how to cancel
  (any member, from the banner in the CRM);
- the **data request notice** (AA-941, S-01): AA raised an agency export or deletion request that only the owner can
  confirm or decline, in Settings > Data and privacy;
- the **mailbox disconnected** alert (AA-945, errata S247 §3 item 3): a sending mailbox of the agency stopped working.

Mail bodies hold no client data. Nothing here is logged except ids and the SES message id.

An address in the reserved ``.invalid`` top-level domain (:mod:`app.mail.recipients`, AA-1014) is never handed to
SES: :meth:`SesMailer.send` logs the skip and returns an empty message id.

Configuration: ``SES_FROM_ADDRESS`` (the AA sender, ``ses:FromAddress`` pins it in IAM) and
``SES_CONFIGURATION_SET`` (the transactional stream). Without them, outside test / local, sending answers 503
``mail_not_configured``; test / local use :class:`MemoryMailer`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import threading
from dataclasses import dataclass
from typing import Any, Protocol

from app.errors import Unavailable
from app.mail.recipients import INVALID_TLD, deliverable

log = logging.getLogger(__name__)

ROLE_WORDS = {
    "owner": ("Owner", "Chủ đại lý"),
    "agent": ("Agent", "Nhân viên tư vấn"),
    "assistant": ("Assistant", "Trợ lý"),
}


@dataclass(frozen=True)
class Message:
    to: str
    subject: str
    text: str
    # invitation | recovery_notice | data_request_notice | mailbox_disconnected (AA-945) | studio_invitation |
    # breakglass_notice (AA-971)
    kind: str


class Mailer(Protocol):
    async def send(self, message: Message) -> str: ...


def _date(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).strftime("%d %b %Y %H:%M UTC")


def invitation_message(to: str, *, agency: str, role: str, link: str, expires_at: dt.datetime) -> Message:
    en_role, vi_role = ROLE_WORDS[role]
    text = "\n".join(
        [
            f"You are invited to join {agency} on AA Partner as {en_role}.",
            "",
            f"Accept the invitation: {link}",
            "",
            "You will get a one-time sign-in password on that page, then choose your own password and set up an",
            "authenticator app (two-step sign-in is required for every AA Partner account).",
            f"The link works once and expires on {_date(expires_at)}. If you did not expect it, ignore this mail.",
            "",
            "---",
            "",
            f"Bạn được mời tham gia {agency} trên AA Partner với vai trò {vi_role}.",
            "",
            f"Nhận lời mời: {link}",
            "",
            "Trang đó sẽ hiện một mật khẩu đăng nhập dùng một lần; sau đó bạn tự đặt mật khẩu và cài ứng dụng",
            "xác thực (mọi tài khoản AA Partner đều phải đăng nhập hai bước).",
            f"Liên kết chỉ dùng được một lần và hết hạn vào {_date(expires_at)}.",
            "Nếu bạn không chờ thư này, hãy bỏ qua.",
            "",
            "Adventure Asia — AA Partner",
        ]
    )
    subject = f"Invitation to {agency} on AA Partner / Lời mời tham gia {agency}"
    return Message(to=to, subject=subject[:200], text=text, kind="invitation")


def studio_invitation_message(to: str, *, link: str, expires_at: dt.datetime) -> Message:
    """AA-979 P2b: Adventure Asia invites a person to an agency's Content studio (re-enrolment in the partner pool).
    The agency name is not known to the CRM for a studio-only tenant, so the mail names the product only; the link is
    the same join page as a CRM invitation."""
    text = "\n".join(
        [
            "Adventure Asia invites you to the Content studio on AA Partner.",
            "",
            f"Accept the invitation: {link}",
            "",
            "You will get a one-time sign-in password on that page (or sign in with your AA Partner account if you",
            "have one), then choose your own password and set up an authenticator app (two-step sign-in is required).",
            "After that you open the Content studio with that account; the old API-key sign-in stops later.",
            f"The link works once and expires on {_date(expires_at)}. If you did not expect it, ignore this mail.",
            "",
            "---",
            "",
            "Adventure Asia mời bạn sử dụng Content studio trên AA Partner.",
            "",
            f"Nhận lời mời: {link}",
            "",
            "Trang đó sẽ hiện một mật khẩu đăng nhập dùng một lần (hoặc đăng nhập bằng tài khoản AA Partner",
            "nếu bạn đã có), sau đó bạn tự đặt mật khẩu và cài ứng dụng xác thực (bắt buộc đăng nhập hai bước).",
            "Từ đó bạn mở Content studio bằng tài khoản này; cách đăng nhập bằng API key cũ sẽ ngừng sau.",
            f"Liên kết chỉ dùng được một lần và hết hạn vào {_date(expires_at)}.",
            "Nếu bạn không chờ thư này, hãy bỏ qua.",
            "",
            "Adventure Asia — AA Partner",
        ]
    )
    subject = "Invitation to the AA Partner Content studio / Lời mời sử dụng Content studio"
    return Message(to=to, subject=subject, text=text, kind="studio_invitation")


def recovery_message(to: str, *, agency: str, eligible_at: dt.datetime, link: str) -> Message:
    text = "\n".join(
        [
            f"Adventure Asia opened an owner recovery for {agency} on AA Partner, because the agency asked for a new",
            f"owner. On {_date(eligible_at)} an existing member becomes owner, unless someone in the agency",
            "cancels it.",
            f"Any member can cancel it from the banner in the CRM: {link}",
            "If you are still running the agency, sign in and cancel it.",
            "",
            "---",
            "",
            f"Adventure Asia đã mở yêu cầu khôi phục chủ đại lý cho {agency} trên AA Partner.",
            f"Vào {_date(eligible_at)} một thành viên hiện có sẽ trở thành chủ đại lý, trừ khi có người trong",
            "đại lý huỷ yêu cầu.",
            f"Mọi thành viên đều có thể huỷ từ thông báo trong CRM: {link}",
            "",
            "Adventure Asia — AA Partner",
        ]
    )
    subject = f"Owner recovery opened for {agency} / Yêu cầu khôi phục chủ đại lý"
    return Message(to=to, subject=subject[:200], text=text, kind="recovery_notice")


def data_request_message(to: str, *, agency: str, kind: str, link: str) -> Message:
    """AA-941: AA raised an agency export (``agency_export``) or deletion (``agency_deletion``) request."""
    en, vi = {
        "agency_export": ("an export of all the agency's data", "xuất toàn bộ dữ liệu của đại lý"),
        "agency_deletion": (
            "the deletion of the agency and all its data (after an export)",
            "xoá đại lý cùng toàn bộ dữ liệu (sau khi xuất dữ liệu)",
        ),
    }[kind]
    text = "\n".join(
        [
            f"Adventure Asia raised a request for {en} for {agency} on AA Partner.",
            "Nothing happens until the agency owner confirms it. The owner can confirm or decline it in",
            f"Settings > Data and privacy: {link}/settings/data",
            "If you did not ask Adventure Asia for this, decline it and contact us.",
            "",
            "---",
            "",
            f"Adventure Asia đã tạo yêu cầu {vi} cho {agency} trên AA Partner.",
            "Không có gì xảy ra cho tới khi chủ đại lý xác nhận. Chủ đại lý xác nhận hoặc từ chối trong",
            f"Cài đặt > Dữ liệu và quyền riêng tư: {link}/settings/data",
            "Nếu bạn không yêu cầu Adventure Asia việc này, hãy từ chối và liên hệ với chúng tôi.",
            "",
            "Adventure Asia — AA Partner",
        ]
    )
    subject = f"Data request for {agency} / Yêu cầu về dữ liệu của {agency}"
    return Message(to=to, subject=subject[:200], text=text, kind="data_request_notice")


def mailbox_disconnected_message(to: str, *, agency: str, link: str) -> Message:
    """AA-945 (errata S247 §3 item 3, R24): a sending mailbox of the agency stopped working (token revoked, password
    changed). Client messages wait (``held_reconnect``) until someone reconnects it; nothing is sent another way."""
    text = "\n".join(
        [
            f"A mailbox of {agency} on AA Partner stopped working (it was disconnected or its access was revoked).",
            "Messages to clients from it are held and nothing is sent until the mailbox is connected again.",
            f"Reconnect it in Settings > Mailbox: {link}/settings/mailbox",
            "",
            "---",
            "",
            f"Một hộp thư của {agency} trên AA Partner đã ngừng hoạt động (bị ngắt kết nối hoặc bị thu hồi quyền).",
            "Thư gửi khách từ hộp thư này đang được giữ lại và sẽ không gửi đi cho tới khi kết nối lại.",
            f"Kết nối lại trong Cài đặt > Hộp thư: {link}/settings/mailbox",
            "",
            "Adventure Asia — AA Partner",
        ]
    )
    subject = f"Mailbox disconnected for {agency} / Hộp thư của {agency} đã ngắt kết nối"
    return Message(to=to, subject=subject[:200], text=text, kind="mailbox_disconnected")


BREAKGLASS_REASONS = {
    "security_incident": ("a security incident", "một sự cố bảo mật"),
    "data_corruption": ("damaged or inconsistent data", "dữ liệu bị hỏng hoặc không nhất quán"),
    "legal_request": ("a legal request", "một yêu cầu pháp lý"),
    "owner_unreachable": ("an urgent problem while the agency owner could not be reached",
                          "một sự cố khẩn cấp khi không liên hệ được chủ đại lý"),
    "service_outage": ("a service outage", "một sự cố gián đoạn dịch vụ"),
}  # fmt: skip


def breakglass_message(
    to: str, *, agency: str, reason_code: str, opened_at: dt.datetime, expires_at: dt.datetime, link: str
) -> Message:
    """AA-971 (design v3 §4.8 L3): Adventure Asia opened emergency (break-glass) read access to the agency's data. The
    owner sees every read in Settings > Access log (actor "AA emergency") and the session in Settings > Data and
    privacy. Codes and dates only: no staff name, no ticket text."""
    en, vi = BREAKGLASS_REASONS[reason_code]
    text = "\n".join(
        [
            f"Adventure Asia opened emergency read access to the data of {agency} on AA Partner, because of {en}.",
            f"It started at {_date(opened_at)} and ends at {_date(expires_at)} at the latest. It is read only:",
            "nothing can be changed, sent or exported, and every read is in your access log.",
            f"See it in Settings > Data and privacy: {link}/settings/data",
            f"and Settings > Access log: {link}/settings/access-log",
            "If you have questions, reply to your Adventure Asia contact.",
            "",
            "---",
            "",
            f"Adventure Asia đã mở quyền đọc khẩn cấp dữ liệu của {agency} trên AA Partner, do {vi}.",
            f"Bắt đầu lúc {_date(opened_at)} và kết thúc muộn nhất lúc {_date(expires_at)}. Chỉ được đọc:",
            "không thể sửa, gửi hay xuất dữ liệu, và mọi lần đọc đều có trong nhật ký truy cập của bạn.",
            f"Xem trong Cài đặt > Dữ liệu và quyền riêng tư: {link}/settings/data",
            f"và Cài đặt > Nhật ký truy cập: {link}/settings/access-log",
            "Nếu có câu hỏi, hãy trả lời người liên hệ của bạn tại Adventure Asia.",
            "",
            "Adventure Asia — AA Partner",
        ]
    )
    subject = f"Emergency access to {agency} / Truy cập khẩn cấp vào {agency}"
    return Message(to=to, subject=subject[:200], text=text, kind="breakglass_notice")


def mailbox_test_message(to: str, *, agency: str) -> Message:
    """Not sent through SES: the body of the Settings > Mailbox test the agency's own mailbox sends to the member."""
    text = "\n".join(
        [
            f"This is a test from AA Partner: the mailbox of {agency} can send.",
            "",
            f"Đây là thư thử từ AA Partner: hộp thư của {agency} gửi được thư.",
        ]
    )
    return Message(to=to, subject="AA Partner mailbox test / Thư thử hộp thư", text=text, kind="mailbox_test")


class SesMailer:
    """SES v2 ``SendEmail`` as the AA sender through the transactional configuration set (blocking: worker thread)."""

    def __init__(
        self, from_address: str, configuration_set: str, region: str | None = None, client: Any = None
    ) -> None:
        self.from_address = from_address
        self.configuration_set = configuration_set
        self._region = region
        self._client = client
        self._lock = threading.Lock()

    def _ses(self) -> Any:
        with self._lock:
            if self._client is None:
                import boto3

                self._client = boto3.client("sesv2", region_name=self._region)
            return self._client

    async def send(self, message: Message) -> str:
        if not deliverable([message.to]):
            # AA-1014: a .invalid address never resolves (RFC 6761); a bounce would only count against AA's sender.
            log.info("crm_system_mail_skipped kind=%s reason=%s", message.kind, INVALID_TLD)
            return ""

        def call() -> str:
            resp = self._ses().send_email(
                FromEmailAddress=self.from_address,
                Destination={"ToAddresses": [message.to]},
                Content={
                    "Simple": {
                        "Subject": {"Data": message.subject, "Charset": "UTF-8"},
                        "Body": {"Text": {"Data": message.text, "Charset": "UTF-8"}},
                    }
                },
                ConfigurationSetName=self.configuration_set,
                EmailTags=[{"Name": "app", "Value": "crm"}, {"Name": "kind", "Value": message.kind}],
            )
            return str(resp.get("MessageId", ""))

        message_id = await asyncio.to_thread(call)
        log.info("crm_system_mail_sent kind=%s message_id=%s", message.kind, message_id)
        return message_id


class MemoryMailer:
    """Keeps the messages (tests and local runs; never configured on dev / stg / prod)."""

    def __init__(self) -> None:
        self.sent: list[Message] = []

    async def send(self, message: Message) -> str:
        self.sent.append(message)
        return f"memory-{len(self.sent)}"


def require_mailer(mailer: Mailer | None) -> Mailer:
    if mailer is None:
        raise Unavailable("mail_not_configured", "AA system mail is not configured")
    return mailer
