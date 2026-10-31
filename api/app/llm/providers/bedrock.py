"""Amazon Bedrock Converse as a CRM gateway provider (AA-964, card CRM-33a; root ADR 0008a §5: Bedrock only).

:class:`BedrockConverseProvider` sends one ``Converse`` request: the fixed system text (ours, no personal data) as the
``system`` block and the **scrubbed** prompt as the only user message. The model gets no tools (prompt-injection rule:
incoming text is data; the gateway's callers wrap it as such). One attempt: botocore retries are off, so a throttle or
an outage is one ``error`` row and the draft says "reply by hand" (the gateway never retries on its own either).

- The client is **injectable** (tests pass a fake; nothing here calls AWS at import or construction). Without one, a
  ``bedrock-runtime`` client is made on first use, with the task role, or with the CRM-only invoker role
  ``CRM_LLM_ROLE_ARN`` when set (STS ``AssumeRole``, credentials cached until 5 minutes before they expire).
- **Cost** is priced here from the configured per-million-token prices (``CRM_LLM_USD_PER_MTOK_IN`` / ``_OUT``,
  ``pricing_mode code_table``), so every call counts against the agency budget. Cache tokens are recorded as reported;
  the CRM sends no cache point, so they are 0 today and not priced.
- Errors leave as :class:`ProviderCallError` with a class code only (the AWS error code, e.g.
  ``AccessDeniedException``, ``ThrottlingException``), never the message: botocore messages can echo request fields.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
import threading
from decimal import Decimal
from typing import Any

from app.llm.gateway import Completion

MTOK = Decimal(1_000_000)
_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
ROLE_SESSION = "crm-llm-gateway"
REFRESH_BEFORE = dt.timedelta(minutes=5)
DEFAULT_TEMPERATURE = 0.3


class ProviderCallError(Exception):
    """A provider call failed; ``error_class`` is the code the ledger records (no message). ``billed``: the tokens and
    cost of a call the provider billed although its answer is unusable (an empty turn), so the budget still sees it."""

    def __init__(self, error_class: str, billed: Completion | None = None) -> None:
        super().__init__(error_class)
        self.error_class = error_class if _CODE.match(error_class or "") else "ProviderError"
        self.billed = billed


def model_family(model_id: str) -> str | None:
    """``us.anthropic.claude-haiku-5-5-...-v1:0`` -> ``claude-haiku``; None when the id has no recognisable name."""
    name = model_id.rsplit("/", 1)[-1]
    parts = name.split(".")
    name = parts[-1] if len(parts) > 1 else name
    words = [w for w in re.split(r"[-:]", name.lower()) if w]
    if len(words) >= 2 and words[0].isalpha() and words[1].isalpha():
        return f"{words[0]}-{words[1]}"
    return words[0] if words and words[0].isalpha() else None


def _error_class(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if isinstance(code, str) and _CODE.match(code):
            return code
    return type(exc).__name__


def _int(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


class BedrockConverseProvider:
    name = "bedrock"
    family: str | None

    def __init__(
        self,
        model_id: str,
        *,
        usd_per_mtok_in: Decimal,
        usd_per_mtok_out: Decimal,
        region: str = "us-west-1",
        max_tokens: int = 800,
        timeout_s: float = 30.0,
        role_arn: str = "",
        client: Any | None = None,
    ) -> None:
        if not model_id:
            raise ValueError("a model id is required")
        if usd_per_mtok_in <= 0 or usd_per_mtok_out <= 0:
            raise ValueError("a real model needs both prices (the budget must see its cost)")
        self.model = model_id
        self.family = model_family(model_id)
        self.connection_key = "bedrock_role" if role_arn else "bedrock"
        self.usd_in = usd_per_mtok_in
        self.usd_out = usd_per_mtok_out
        self.region = region
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self.role_arn = role_arn
        self._client = client
        self._expires: dt.datetime | None = None
        self._lock = threading.Lock()

    # ── the client ──────────────────────────────────────────────────────────────────────────────
    def _config(self) -> Any:
        from botocore.config import Config

        return Config(
            connect_timeout=5,
            read_timeout=max(1, int(self.timeout_s)),
            retries={"max_attempts": 1, "mode": "standard"},
        )

    def _runtime(self) -> Any:
        """The injected client, else a client with the task role or the assumed invoker role (refreshed)."""
        with self._lock:
            now = dt.datetime.now(dt.UTC)
            if self._client is not None and (self._expires is None or now < self._expires - REFRESH_BEFORE):
                return self._client
            import boto3

            if not self.role_arn:
                self._client = boto3.client("bedrock-runtime", region_name=self.region, config=self._config())
                return self._client
            sts = boto3.client("sts", region_name=self.region)
            creds = sts.assume_role(RoleArn=self.role_arn, RoleSessionName=ROLE_SESSION, DurationSeconds=3600)
            c = creds["Credentials"]
            self._client = boto3.client(
                "bedrock-runtime",
                region_name=self.region,
                aws_access_key_id=c["AccessKeyId"],
                aws_secret_access_key=c["SecretAccessKey"],
                aws_session_token=c["SessionToken"],
                config=self._config(),
            )
            self._expires = c["Expiration"]
            return self._client

    # ── the call ────────────────────────────────────────────────────────────────────────────────
    def cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        return (Decimal(input_tokens) * self.usd_in + Decimal(output_tokens) * self.usd_out) / MTOK

    def _request(self, prompt: str, system: str | None) -> dict[str, Any]:
        request: dict[str, Any] = {
            "modelId": self.model,
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": self.max_tokens, "temperature": DEFAULT_TEMPERATURE},
        }
        if system:
            request["system"] = [{"text": system}]
        return request

    async def complete(self, prompt: str, *, system: str | None = None) -> Completion:
        try:
            client = await asyncio.to_thread(self._runtime)
            response = await asyncio.to_thread(client.converse, **self._request(prompt, system))
        except Exception as exc:  # noqa: BLE001 — class only, never the message
            raise ProviderCallError(_error_class(exc)) from None
        return self.parse(response)

    def parse(self, response: Any) -> Completion:
        if not isinstance(response, dict):
            raise ProviderCallError("MalformedResponse")
        content = (((response.get("output") or {}).get("message") or {}).get("content")) or []
        texts = [part.get("text") for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)]
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        tokens_in, tokens_out = _int(usage.get("inputTokens")), _int(usage.get("outputTokens"))
        request_id = ((response.get("ResponseMetadata") or {}).get("RequestId")) or None
        stop = response.get("stopReason")
        done = Completion(
            text="".join(texts),
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cost_usd=self.cost(tokens_in, tokens_out),
            request_id=request_id if isinstance(request_id, str) and _REQUEST_ID.match(request_id) else None,
            cache_read_tokens=_int(usage.get("cacheReadInputTokens")),
            cache_write_tokens=_int(usage.get("cacheWriteInputTokens")),
            pricing_mode="code_table",
            truncated=stop == "max_tokens",
        )
        if not texts:
            # Billed but nothing usable (a refusal or an empty turn): the cost is real, the draft is not.
            raise ProviderCallError("EmptyOutput", billed=done)
        return done
