"""The result kinds of the global search and who may see each (card CRM-32: "the CRM records the user is entitled
to"; root ADR 0019 invariant 6: features are checked by key, never by plan name).

Each kind names:

- ``action``: the role-matrix action (:mod:`app.permissions`) of the page its results open; a role whose rule is
  ``DENY`` never gets the kind (the kind is not searched at all, no query runs);
- ``feature``: the CRM feature key (:data:`app.entitlements.FEATURES`) the agency must hold, or ``None``;
- ``modes``: the term modes (:mod:`app.search.query`) the kind answers. Only ``client`` answers an e-mail or a phone
  (through its blind indexes); the plaintext kinds answer text only.

Content pieces are not a kind here: they live in CIS and the web filters the person's own pieces (section "Content
pieces" of ``docs/implementation-notes/AA-963.md``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.entitlements import FEATURES
from app.permissions import Action, may
from app.search.query import Mode


@dataclass(frozen=True)
class Kind:
    code: str
    action: Action
    feature: str | None
    modes: frozenset[Mode]


TEXT: frozenset[Mode] = frozenset({"text"})

KINDS: tuple[Kind, ...] = (
    Kind("client", Action.CLIENT_VIEW, None, frozenset({"email", "phone", "text"})),
    Kind("trip", Action.TRIP_VIEW, None, TEXT),
    Kind("booking", Action.TRIP_VIEW, None, TEXT),
    Kind("supplier", Action.TRIP_VIEW, None, TEXT),
    Kind("service", Action.TRIP_VIEW, None, TEXT),
    Kind("message_template", Action.TRIP_VIEW, None, TEXT),
    Kind("workflow", Action.PIPELINE_VIEW, None, TEXT),
    Kind("inclusion", Action.TRIP_VIEW, None, TEXT),
    Kind("tag", Action.TAG_APPLY, None, TEXT),
    Kind("whatsapp_template", Action.TRIP_VIEW, "whatsapp_sms", TEXT),
)
CODES = tuple(k.code for k in KINDS)


def entitled(role: str, features: Iterable[str], kinds: Iterable[Kind] = KINDS) -> list[Kind]:
    """The kinds a member of ``role`` in an agency holding ``features`` may search, in registry order. Pure."""
    held = set(features)
    unknown = held - set(FEATURES)
    if unknown:
        raise ValueError(f"unknown CRM feature(s) {sorted(unknown)!r}")
    return [k for k in kinds if may(role, k.action) and (k.feature is None or k.feature in held)]


def features_needed(kinds: Iterable[Kind] = KINDS) -> list[str]:
    """The feature keys some kind needs (read once per search, :func:`app.entitlements.feature_allowed`)."""
    return sorted({k.feature for k in kinds if k.feature is not None})
