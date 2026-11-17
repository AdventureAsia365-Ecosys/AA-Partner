"""AA-1113 (S282 audit R28, G12): every CRM model call goes through the CRM gateway wrapper (``app/llm/gateway.py``),
so it writes its ``crm.llm_call`` row, meets the agency budget, the app profile caps and the F3 scrub.

``test_gateway_bypass.py`` already fails on a raw boto3 Bedrock method, a vendor SDK import or a vendor host. This guard
is the next level up: a module that builds or reaches the gateway's own parts would call a model with no ledger row,
no budget and no scrub without touching boto3. Python under ``api/app`` and ``api/scripts`` is parsed with ``ast``
(comments and docstrings never count). Outside the wrapper modules (``WRAPPER``) a hit is:

- importing ``app.llm.providers`` (any module) or ``app.llm.stage_caps``;
- calling a constructor of the wrapper's parts: ``LlmGateway``, ``StubProvider``, ``StubScrubber``,
  ``BedrockConverseProvider``, ``PiiScrubber``, ``BedrockGuardrailsDetector``, ``build_scrubber``, ``stub_gateway``
  (``build_gateway`` only from ``app/main.py``);
- calling ``.scrub(...)`` (the F3 scrubber; its Bedrock pass is billed and only the gateway logs it);
- reading a gateway's parts: ``<gateway>.provider`` / ``.scrubber`` / ``.keys`` / ``.limiter`` / ``.profiles`` or a
  private name, on a name or attribute called ``gateway`` / ``llm_gateway``;
- in an outbound client module (one that imports an HTTP transport), a string that names an AI path of another app
  (``/llm``, ``/ai/``, ``/gateway``, ``/completions``, ``/converse``, ``/generate``, ``/embed``): the CRM never calls
  the CIS or TripPlanner gateway.

A type hint (``gateway: LlmGateway``) is not a hit. A reviewed exception goes into ``ALLOWED`` with its reason; an entry
whose file has no hit left fails, so the list cannot go stale.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PY_ROOTS = ("api/app", "api/scripts")

# The wrapper: the gateway and the modules it is built from. Their own internals are not a bypass.
WRAPPER = frozenset(
    {
        "api/app/llm/gateway.py",
        "api/app/llm/stage_caps.py",
        "api/app/llm/providers/__init__.py",
        "api/app/llm/providers/bedrock.py",
        "api/app/privacy/guardrails.py",
        "api/app/privacy/scrubber.py",
    }
)
WRAPPER_MODULES = ("app.llm.providers", "app.llm.stage_caps")
WRAPPER_PARTS = frozenset(
    {
        "LlmGateway",
        "StubProvider",
        "StubScrubber",
        "BedrockConverseProvider",
        "PiiScrubber",
        "BedrockGuardrailsDetector",
        "build_scrubber",
        "stub_gateway",
        "build_gateway",
    }
)
GATEWAY_NAMES = frozenset({"gateway", "llm_gateway"})
GATEWAY_PARTS = frozenset({"provider", "scrubber", "keys", "limiter", "profiles"})
HTTP_TRANSPORTS = frozenset({"app.cis.http", "urllib.request", "httpx", "requests", "aiohttp"})
AI_PATH = re.compile(r"/(?:llm|ai|gateway|completions?|converse|generate|embed(?:dings?)?)(?:[/?]|$)", re.IGNORECASE)

# path -> {hit: reason}: reviewed exceptions outside WRAPPER. Ratcheted like test_gateway_bypass.ALLOWED.
ALLOWED: dict[str, dict[str, str]] = {
    "api/app/main.py": {"call build_gateway": "the process builds its one gateway at start (app.state.llm_gateway)"},
}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    docs: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docs.add(id(first.value))
    return docs


def _callee(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_gateway(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id in GATEWAY_NAMES
    return isinstance(node, ast.Attribute) and node.attr in GATEWAY_NAMES


def _imports_transport(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name in HTTP_TRANSPORTS for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ""
            if module in HTTP_TRANSPORTS or any(f"{module}.{a.name}" in HTTP_TRANSPORTS for a in node.names):
                return True
    return False


def find_hits(source: str) -> list[tuple[int, str]]:
    """(line, what) for every way past the gateway wrapper in one Python module's source."""
    tree = ast.parse(source)
    docs = _docstring_nodes(tree)
    client = _imports_transport(tree)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hits.extend((node.lineno, f"import {a.name}") for a in node.names if a.name.startswith(WRAPPER_MODULES))
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ""
            if module.startswith(WRAPPER_MODULES):
                hits.append((node.lineno, f"import {module}"))
            elif module == "app.llm":
                hits.extend(
                    (node.lineno, f"import app.llm.{a.name}")
                    for a in node.names
                    if a.name in ("providers", "stage_caps")
                )
        elif isinstance(node, ast.Call):
            name = _callee(node.func)
            if name in WRAPPER_PARTS:
                hits.append((node.lineno, f"call {name}"))
            elif name == "scrub" and isinstance(node.func, ast.Attribute):
                hits.append((node.lineno, "call .scrub"))
        elif isinstance(node, ast.Attribute) and _is_gateway(node.value):
            if node.attr in GATEWAY_PARTS or node.attr.startswith("_"):
                hits.append((node.lineno, f"gateway.{node.attr}"))
        elif client and isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            if AI_PATH.search(node.value):
                hits.append((node.lineno, f"ai path {node.value[:60]}"))
    return hits


def _scan() -> dict[str, list[tuple[int, str]]]:
    found: dict[str, list[tuple[int, str]]] = {}
    for root in PY_ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            if rel in WRAPPER:
                continue
            hits = find_hits(path.read_text(encoding="utf-8"))
            if hits:
                found[rel] = hits
    return found


def test_no_module_reaches_past_the_gateway_wrapper() -> None:
    bad = [
        f"{rel}:{line} {what}"
        for rel, hits in _scan().items()
        for line, what in hits
        if what not in ALLOWED.get(rel, {})
    ]
    assert not bad, (
        "A model call must go through app.state.llm_gateway.call(...) (ledger row, budgets, caps, F3 scrub); "
        "these reach the wrapper's parts directly, or add a reviewed ALLOWED entry:\n" + "\n".join(bad)
    )


def test_allowlist_entries_still_match() -> None:
    found = _scan()
    stale = [
        f"{rel}: {what}"
        for rel, entries in ALLOWED.items()
        for what in entries
        if what not in {w for _, w in found.get(rel, [])}
    ]
    assert not stale, f"stale ALLOWED entries (no such hit left, remove them): {stale}"


def test_the_wrapper_files_exist() -> None:
    missing = [rel for rel in WRAPPER if not (REPO / rel).is_file()]
    assert not missing, f"WRAPPER names files that are gone (update the guard): {missing}"


# ── The detectors on invented snippets ──────────────────────────────────────────────────────────

_HITS = {
    "from app.llm.providers.bedrock import BedrockConverseProvider": ["import app.llm.providers.bedrock"],
    "import app.llm.stage_caps": ["import app.llm.stage_caps"],
    "from app.llm import stage_caps": ["import app.llm.stage_caps"],
    "p = BedrockConverseProvider('m')": ["call BedrockConverseProvider"],
    "p = gateway_module.StubProvider()": ["call StubProvider"],
    "g = LlmGateway(s, p)": ["call LlmGateway"],
    "s = build_scrubber(settings)": ["call build_scrubber"],
    "g = build_gateway(settings)": ["call build_gateway"],
    "out = await scrubber.scrub(text)": ["call .scrub"],
    "await request.app.state.llm_gateway.provider.complete(text)": ["gateway.provider"],
    "await gateway._attempts(ctx)": ["gateway._attempts"],
    "s = gateway.scrubber": ["gateway.scrubber"],
}
_CLIENT = "from app.cis.http import urllib_transport\n"


def test_detector_catches_each_way_past_the_wrapper() -> None:
    for snippet, expected in _HITS.items():
        assert [what for _, what in find_hits(snippet)] == expected, snippet


def test_detector_catches_an_ai_path_only_in_a_client_module() -> None:
    for path in ("/tenant/v1/llm/complete", "/v1/ai/draft", "/internal/gateway", "/v1/chat/completions"):
        assert [what for _, what in find_hits(f"{_CLIENT}PATH = {path!r}")] == [f"ai path {path}"], path
    assert find_hits("PATH = '/llm/usage'") == []  # a route of our own (no transport): not a client call
    assert find_hits(f"{_CLIENT}PATH = '/tenant/v1/tenants/{{tenant_id}}/add-ons'") == []


def test_detector_ignores_type_hints_docstrings_and_unrelated_calls() -> None:
    source = '''
"""Calls LlmGateway(...) and scrubber.scrub() only in this docstring."""
from app.llm.gateway import CallMeta, LlmGateway
async def run(ctx, gateway: LlmGateway, queue, oauth):
    """gateway.provider is named here only."""
    # gateway.provider.complete(...) in a comment
    await queue.complete(ctx, 1)
    await oauth.complete(ctx)
    return await gateway.call(ctx, stage="crm_x", role="writer", prompt="hi", meta=CallMeta())
'''
    assert find_hits(source) == []
