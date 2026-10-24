"""The synthetic agencies of Dev (AA-935, card CRM-07: "seeder: 2-3 synthetic agencies, Faker EN/VI travellers").

Each agency has a fixed key; its ``tenant_id`` is a UUIDv5 of that key, so every run of the seeder, on any machine,
addresses the same rows (idempotent by construction, no lookup by name). The names are invented, carry the word
"synthetic" in the key only (the label stays a realistic agency name), and one of them is Vietnamese with diacritics
so the round trip of a Vietnamese name is exercised on every seed (card E test).

``roles`` lists the e2e test users each agency expects (``e2e-test-crm-<key>-<role>``, created by the AA-CIS-Infra
tool ``tools/identity/crm_dev_identity.py e2e-users``, names only in the repos). The seeder adds a member only for the
identities it is given (``--member``); it never invents a Cognito subject.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

# Fixed namespace for the synthetic agency ids (uuid5). Never reuse it for anything else.
SYNTHETIC_NAMESPACE = uuid.UUID("6f1d3c2e-9a7b-5c4d-8e2f-a935c0de0007")


@dataclass(frozen=True)
class SyntheticAgency:
    key: str
    display_name: str
    default_locale: str
    # Faker locales of the agency's travellers, in proportion (one entry = one share).
    traveller_locales: tuple[str, ...]
    traveller_count: int
    roles: tuple[str, ...]

    @property
    def tenant_id(self) -> uuid.UUID:
        return agency_id(self.key)


def agency_id(key: str) -> uuid.UUID:
    return uuid.uuid5(SYNTHETIC_NAMESPACE, f"aa-crm-synthetic-agency:{key}")


AGENCIES: tuple[SyntheticAgency, ...] = (
    SyntheticAgency(
        key="sen-vang",
        display_name="Lữ hành Sen Vàng",
        default_locale="vi-VN",
        traveller_locales=("vi_VN", "vi_VN", "vi_VN", "en_AU"),
        traveller_count=24,
        roles=("owner", "agent", "assistant"),
    ),
    SyntheticAgency(
        key="southern-cross",
        display_name="Southern Cross Journeys",
        default_locale="en-AU",
        traveller_locales=("en_AU", "en_AU", "en_GB", "vi_VN"),
        traveller_count=24,
        roles=("owner", "agent"),
    ),
    SyntheticAgency(
        key="harbour-lights",
        display_name="Harbour Lights Travel Co.",
        default_locale="en-US",
        traveller_locales=("en_US", "en_US", "vi_VN"),
        traveller_count=12,
        roles=("owner",),
    ),
)

BY_KEY: dict[str, SyntheticAgency] = {a.key: a for a in AGENCIES}


def e2e_username(agency_key: str, role: str) -> str:
    """The Cognito username of an agency's e2e test user (matches the Infra script)."""
    return f"e2e-test-crm-{agency_key}-{role}"
