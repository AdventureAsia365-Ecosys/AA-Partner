"""Amazon Bedrock Converse as a CRM gateway provider (AA-964, card CRM-33a; root ADR 0008a §5: Bedrock only).

:class:`BedrockConverseProvider` sends one ``Converse`` request: the fixed system text (ours, no personal data) as the
``system`` block and the **scrubbed** prompt as the only user message. The model gets no tools (prompt-injection rule:
incoming text is data; the gateway's callers wrap it as such). One attempt: botocore retries are off, so a throttle or
an outage is one ``error`` row and the draft says "reply by hand" (the gateway never retries on its own either).

- The client is **injectable** (tests pass a fake; nothing here calls AWS at import or construction). Without one, a
  ``bedrock-runtime`` client is made on first use, with the task role, or with the CRM-only invoker role
  ``CRM_LLM_ROLE_ARN`` when set (STS ``AssumeRole``, credentials cached until 5 minutes before they expire).
- **Cost** is priced with the shared core (``app.aa_llm.pricing.quote``, AA-964 S263 F1): the route entry's catalog
  prices (crm-037 ``crm.llm_stage_route``, ``pricing_mode catalog``), overridden by ``CRM_LLM_USD_PER_MTOK_IN`` /
  ``_OUT`` for the configured model (``override``), else the core's code table (``code_table``) or its high estimate
  (``estimate``, never 0), so every call counts against the agency budget. Cache tokens are recorded as reported and
  priced at the core's cache rates (the CRM sends no cache point, so they are 0 today).
- **Route** (S263 F2): :meth:`BedrockConverseProvider.for_route` gives the provider of one route entry (its model,
  family and prices) on the same client.
- **Named connections** (AA-1138, crm-078): a route entry with a ``connection_key`` runs on that named connection
  instead (today ``bedrock_acc1``: acc1's invoker role, assumed with its ExternalId; :class:`Connection`). A key the
  process has no configuration for is never called: :meth:`complete` raises ``ConnectionNotConfigured`` (no AWS call,
  no cost) and the gateway moves on to the next entry. A route entry with ``sends_temperature = false`` (a model that
  rejects one, Claude Haiku 5.5) leaves the temperature out of the request.
- Errors leave as :class:`ProviderCallError` with a class code only (the AWS error code, e.g.
  ``AccessDeniedException``, ``ThrottlingException``), never the message: botocore messages can echo request fields.
  Its ``llm_error_class`` is the shared core's class (``app.aa_llm.errors.classify`` of the whole exception, read here
  and never stored), so the gateway records ``access_denied`` / ``quota_exhausted`` / ``throttled`` / ``other``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.aa_llm import errors as llm_errors
from app.aa_llm import pricing
from app.aa_llm.ledger import TRUNCATED_STOP_REASONS
from app.llm.gateway import Completion
from app.llm.profiles import RouteEntry, family_of

_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
ROLE_SESSION = "crm-llm-gateway"
REFRESH_BEFORE = dt.timedelta(minutes=5)
DEFAULT_TEMPERATURE = 0.3
ACC1_CONNECTION = "bedrock_acc1"  # crm-078: the acc1 invoker role (root ADR 0001a, Note S278)
NOT_CONFIGURED = "ConnectionNotConfigured"

log = logging.getLogger(__name__)


class ProviderCallError(Exception):
    """A provider call failed; ``error_class`` is the code the ledger records (no message). ``billed``: the tokens and
    cost of a call the provider billed although its answer is unusable (an empty turn), so the budget still sees it."""

    def __init__(self, error_class: str, billed: Completion | None = None, llm_error_class: str | None = None) -> None:
        super().__init__(error_class)
        self.error_class = error_class if _CODE.match(error_class or "") else "ProviderError"
        self.billed = billed
        self.llm_error_class = llm_error_class or llm_errors.classify_text(self.error_class)


@dataclass(frozen=True)
class Connection:
    """A named Bedrock connection a route entry may run on: the role to assume and the ExternalId its trust pins."""

    role_arn: str
    external_id: str = field(default="", repr=False)


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
        usd_per_mtok_in: Decimal | None = None,
        usd_per_mtok_out: Decimal | None = None,
        catalog: tuple[Decimal, Decimal] | None = None,
        family: str | None = None,
        region: str = "us-west-1",
        max_tokens: int | Callable[[], int] = 800,
        timeout_s: float = 30.0,
        role_arn: str = "",
        client: Any | None = None,
        parent: BedrockConverseProvider | None = None,
        external_id: str = "",
        connection_key: str | None = None,
        connections: Mapping[str, Connection] | None = None,
        sends_temperature: bool = True,
    ) -> None:
        """``usd_per_mtok_in`` / ``_out``: the configured price override of this model (both or neither, > 0).
        ``catalog``: the route entry's catalog prices (in, out per million tokens). ``parent``: the provider whose
        client this one shares (a route entry of the process's provider). ``connections``: the named connections a
        route entry may name (AA-1138); ``connection_key`` / ``external_id``: those of a named connection's root."""
        if not model_id:
            raise ValueError("a model id is required")
        if (usd_per_mtok_in is None) != (usd_per_mtok_out is None):
            raise ValueError("a price override needs both prices")
        if usd_per_mtok_in is not None and (usd_per_mtok_in <= 0 or usd_per_mtok_out <= 0):  # type: ignore[operator]
            raise ValueError("a price override must be above 0 (the budget must see the cost)")
        self.model = model_id
        self.family = family or family_of(model_id)
        self.connection_key = connection_key or ("bedrock_role" if role_arn else "bedrock")
        # None = the process's own connection (a route entry without a connection_key); else the entry's named one
        self.route_connection: str | None = connection_key
        self.external_id = external_id
        self.connections: dict[str, Connection] = dict(connections or {})
        self.sends_temperature = sends_temperature
        self.configured = True
        self.usd_in = usd_per_mtok_in
        self.usd_out = usd_per_mtok_out
        self.catalog = catalog
        self.region = region
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self.role_arn = role_arn
        self._client = client
        self._parent = parent
        self._expires: dt.datetime | None = None
        self._lock = threading.Lock()
        self._children: dict[RouteEntry, BedrockConverseProvider] = {}
        self._roots: dict[str, BedrockConverseProvider] = {}

    @classmethod
    def from_settings(cls, settings: Any, *, max_tokens: int | Callable[[], int]) -> BedrockConverseProvider:
        """The process's provider from ``app.settings``: its model, price override, region and role, and the named
        connections it knows (AA-1138: ``bedrock_acc1`` when ``CRM_LLM_ACC1_ROLE_ARN`` is set)."""
        connections = {}
        if settings.crm_llm_acc1_role_arn:
            connections[ACC1_CONNECTION] = Connection(settings.crm_llm_acc1_role_arn, settings.crm_llm_acc1_external_id)
        return cls(
            settings.crm_llm_model_id,
            usd_per_mtok_in=settings.crm_llm_usd_per_mtok_in,
            usd_per_mtok_out=settings.crm_llm_usd_per_mtok_out,
            region=settings.crm_llm_region,
            max_tokens=max_tokens,
            role_arn=settings.crm_llm_role_arn,
            connections=connections,
        )

    def for_route(self, entry: RouteEntry) -> BedrockConverseProvider:
        """The provider of one route entry: its model, family and catalog prices, on this provider's client. The
        configured price override applies only to the configured model."""
        child = self._children.get(entry)
        if child is None:
            same = entry.model == self.model and not entry.connection_key
            root = self._connection_root(entry.connection_key) if entry.connection_key else (self._parent or self)
            child = BedrockConverseProvider(
                entry.model,
                usd_per_mtok_in=self.usd_in if same else None,
                usd_per_mtok_out=self.usd_out if same else None,
                catalog=entry.catalog_prices,
                family=entry.family,
                region=self.region,
                max_tokens=self.max_tokens,
                timeout_s=self.timeout_s,
                role_arn=root.role_arn,
                parent=root,
                external_id=root.external_id,
                connection_key=entry.connection_key,
                sends_temperature=entry.sends_temperature,
            )
            child.configured = root.configured
            self._children[entry] = child
        return child

    def _connection_root(self, key: str) -> BedrockConverseProvider:
        """The provider that owns the client of the named connection ``key`` (one per key and process). A key without
        configuration gets a root that is never called (``configured`` false)."""
        owner = self._parent or self
        root = owner._roots.get(key)
        if root is None:
            conn = owner.connections.get(key)
            root = BedrockConverseProvider(
                owner.model,
                region=owner.region,
                timeout_s=owner.timeout_s,
                role_arn=conn.role_arn if conn else "",
                external_id=conn.external_id if conn else "",
                connection_key=key,
            )
            root.configured = conn is not None
            owner._roots[key] = root
        return root

    # ── the client ──────────────────────────────────────────────────────────────────────────────
    def _config(self) -> Any:
        from botocore.config import Config

        return Config(
            connect_timeout=5,
            read_timeout=max(1, int(self.timeout_s)),
            retries={"max_attempts": 1, "mode": "standard"},
        )

    def _runtime(self) -> Any:
        """The injected client, else a client with the task role or the assumed invoker role (refreshed). A route
        entry's provider uses its parent's client."""
        if self._parent is not None:
            return self._parent._runtime()
        with self._lock:
            now = dt.datetime.now(dt.UTC)
            if self._client is not None and (self._expires is None or now < self._expires - REFRESH_BEFORE):
                return self._client
            import boto3

            if not self.role_arn:
                self._client = boto3.client("bedrock-runtime", region_name=self.region, config=self._config())
                return self._client
            sts = boto3.client("sts", region_name=self.region)
            assume: dict[str, Any] = {
                "RoleArn": self.role_arn,
                "RoleSessionName": ROLE_SESSION,
                "DurationSeconds": 3600,
            }
            if self.external_id:
                assume["ExternalId"] = self.external_id  # AA-1138: the acc1 invoker's trust pins it
            creds = sts.assume_role(**assume)
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

    # ── the price ───────────────────────────────────────────────────────────────────────────────
    def quote(self, usage: pricing.Usage) -> pricing.Quote:
        """The shared core's quote: the override, else the catalog prices, else the code table, else the estimate."""
        catalog = pricing.catalog_rates(*self.catalog) if self.catalog else None
        override = (
            {"price_in_override": self.usd_in, "price_out_override": self.usd_out} if self.usd_in is not None else None
        )
        return pricing.quote(self.model, usage, override, catalog=catalog, on_estimate=_log_estimate)

    def output_cap(self) -> int:
        """The output token cap of this call: a number, or (AA-1096 L4) the admin-editable setting
        ``crm.ai.draft_reply_max_tokens`` read at each call (the gateway passes ``platform_settings.limit``)."""
        cap = self.max_tokens
        return int(cap()) if callable(cap) else int(cap)

    # ── the call ────────────────────────────────────────────────────────────────────────────────
    def _request(self, prompt: str, system: str | None, max_tokens: int | None = None) -> dict[str, Any]:
        cap = int(max_tokens) if max_tokens is not None else self.output_cap()  # AA-965: a stage's own cap
        config: dict[str, Any] = {"maxTokens": cap}
        if self.sends_temperature:  # AA-1138: Claude Haiku 5.5 rejects a temperature (crm-078 sends_temperature)
            config["temperature"] = DEFAULT_TEMPERATURE
        request: dict[str, Any] = {
            "modelId": self.model,
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": config,
        }
        if system:
            request["system"] = [{"text": system}]
        return request

    async def complete(self, prompt: str, *, system: str | None = None, max_tokens: int | None = None) -> Completion:
        if not self.configured:  # AA-1138: a named connection this process has no role for is never called
            raise ProviderCallError(NOT_CONFIGURED, llm_error_class=llm_errors.CREDENTIAL)
        try:
            client = await asyncio.to_thread(self._runtime)
            response = await asyncio.to_thread(client.converse, **self._request(prompt, system, max_tokens))
        except Exception as exc:  # noqa: BLE001 — class only, never the message
            raise ProviderCallError(_error_class(exc), llm_error_class=llm_errors.classify(exc)) from None
        return self.parse(response)

    def parse(self, response: Any) -> Completion:
        if not isinstance(response, dict):
            raise ProviderCallError("MalformedResponse")
        content = (((response.get("output") or {}).get("message") or {}).get("content")) or []
        texts = [part.get("text") for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)]
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        tokens_in, tokens_out = _int(usage.get("inputTokens")), _int(usage.get("outputTokens"))
        cache_read, cache_write = _int(usage.get("cacheReadInputTokens")), _int(usage.get("cacheWriteInputTokens"))
        request_id = ((response.get("ResponseMetadata") or {}).get("RequestId")) or None
        stop = response.get("stopReason")
        stop = stop if isinstance(stop, str) and _CODE.match(stop) else None
        quote = self.quote(
            pricing.Usage(
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            )
        )
        done = Completion(
            text="".join(texts),
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cost_usd=Decimal(str(quote.cost_usd)),
            request_id=request_id if isinstance(request_id, str) and _REQUEST_ID.match(request_id) else None,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            pricing_mode=quote.pricing_mode,
            truncated=stop in TRUNCATED_STOP_REASONS,
            stop_reason=stop,
        )
        if not texts:
            # Billed but nothing usable (a refusal or an empty turn): the cost is real, the draft is not.
            raise ProviderCallError("EmptyOutput", billed=done)
        return done


def _log_estimate(model: str) -> None:
    log.warning("crm_llm_price_estimate model=%s (no catalog price, no override, not in the core table)", model)
