"""AA system mail to agency people (root ADR 0006b decision 3 pointer: invitation and security mail for the partner
pool is AA system mail through AA's SES, never the agency's own mailbox; AA-939).

Two messages, each in English then Vietnamese in one mail (the person's language is not known before they sign in):

- the **invitation**: the agency's name, the role, the join link ``<PARTNER_WEB_ORIGIN>/join?t=<token>`` and the
  expiry. The token is the only secret in it; it is single-use, and its SHA-256 is all the database keeps;
- the **owner recovery notice** (Q1 6.3): AA opened an owner recovery, the date it can complete, and how to cancel
  (any member, from the banner in the CRM).

Mail bodies hold no client data. Nothing here is logged except ids and the SES message id.

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
    kind: str  # invitation | recovery_notice


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
