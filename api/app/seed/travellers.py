"""Synthetic travellers (Faker EN/VI) for the Dev agencies (AA-935, card CRM-07).

Deterministic: the same agency key and count always give the same people (Faker seeded per agency and index), so a
re-run is idempotent and a test can pin a value. Every string is NFC-normalised, the form the API stores, so a
Vietnamese name such as "Nguyễn Thị Ngọc Lan" round-trips byte for byte (card E test).

Vietnamese names are composed in Vietnamese order (family, middle, given) from the ``vi_VN`` provider's gendered
given names (its plain ``first_name`` returns English placeholders). E-mail addresses use the reserved ``.invalid``
top-level domain (RFC 2606), so no message to a synthetic traveller can ever be delivered; phones are fake numbers in
E.164 form.

AA-936: :func:`inquiry_for` turns a traveller into a Stage 1 inquiry; ``python -m app.seed --apply --travellers``
stores those through the same path as the public form (encrypted submission + ``crm_intake_process`` job), so the
synthetic travellers reach ``crm.traveler`` only through :mod:`app.vault` field encryption.
"""

from __future__ import annotations

import hashlib
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from datetime import date

from faker import Faker

FAKER_LOCALES = ("vi_VN", "en_AU", "en_GB", "en_US")
PREFERRED_LANGUAGE = {"vi_VN": "vi", "en_AU": "en", "en_GB": "en", "en_US": "en"}
COUNTRY_CODE = {"vi_VN": "84", "en_AU": "61", "en_GB": "44", "en_US": "1"}
EMAIL_DOMAIN = "example.invalid"  # RFC 2606: never deliverable


@dataclass(frozen=True)
class SyntheticTraveller:
    agency_key: str
    index: int
    faker_locale: str
    given_name: str
    family_name: str
    full_name: str
    email: str
    phone_e164: str
    date_of_birth: date
    preferred_language: str

    def as_dict(self) -> dict[str, object]:
        out = asdict(self)
        out["date_of_birth"] = self.date_of_birth.isoformat()
        return out


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def _seed(agency_key: str, index: int) -> int:
    digest = hashlib.sha256(f"{agency_key}:{index}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _ascii_slug(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.replace("đ", "d").replace("Đ", "D"))
    return "".join(c for c in decomposed if c.isascii() and c.isalnum()).lower() or "traveller"


def make_traveller(agency_key: str, index: int, faker_locale: str) -> SyntheticTraveller:
    if faker_locale not in FAKER_LOCALES:
        raise ValueError(f"unsupported Faker locale {faker_locale!r}")
    fake = Faker(faker_locale)
    fake.seed_instance(_seed(agency_key, index))
    female = fake.boolean()
    if faker_locale == "vi_VN":
        family = nfc(fake.last_name())
        middle = nfc(fake.middle_name()) if hasattr(fake, "middle_name") else ""
        given = nfc(fake.first_name_female() if female else fake.first_name_male())
        full = nfc(" ".join(p for p in (family, middle, given) if p))
        given_for_record = nfc(f"{middle} {given}") if middle else given
    else:
        given = nfc(fake.first_name_female() if female else fake.first_name_male())
        family = nfc(fake.last_name())
        full = nfc(f"{given} {family}")
        given_for_record = given
    local = f"{_ascii_slug(given)}.{_ascii_slug(family)}.{index}"
    number = fake.numerify("9########")
    return SyntheticTraveller(
        agency_key=agency_key,
        index=index,
        faker_locale=faker_locale,
        given_name=given_for_record,
        family_name=family,
        full_name=full,
        email=f"{local}+{agency_key}@{EMAIL_DOMAIN}",
        phone_e164=f"+{COUNTRY_CODE[faker_locale]}{number}",
        date_of_birth=fake.date_of_birth(minimum_age=1, maximum_age=85),
        preferred_language=PREFERRED_LANGUAGE[faker_locale],
    )


def travellers_for(agency_key: str, locales: tuple[str, ...], count: int) -> list[SyntheticTraveller]:
    """``count`` travellers whose Faker locales cycle through ``locales`` (the agency's mix)."""
    if count < 0 or not locales:
        raise ValueError("count must be >= 0 and locales non-empty")
    return [make_traveller(agency_key, i, locales[i % len(locales)]) for i in range(count)]


def inquiry_for(person: SyntheticTraveller) -> dict[str, object]:
    """A Stage 1 inquiry (``app.intake.stage1`` shape) this synthetic traveller submits (AA-936): deterministic per
    agency and index, varied enough to fill the board (destinations, months, party, budget band, style)."""
    from app.intake import stage1

    i = person.index
    places = [c for c in stage1.DESTINATIONS if c != "not_sure"]
    answers: dict[str, object] = {
        "destinations": [places[i % len(places)]] + ([places[(i + 3) % len(places)]] if i % 3 == 0 else []),
        "month": f"{2027 + (i // 12) % 2}-{(i % 12) + 1:02d}",
        "nights": 5 + (i * 3) % 12,
        "adults": 1 + i % 4,
        "child_ages": [6 + i % 9] if i % 4 == 0 else [],
        "party_type": stage1.PARTY_TYPES[i % len(stage1.PARTY_TYPES)],
        "budget_band": stage1.BUDGET_BANDS[i % len(stage1.BUDGET_BANDS)],
        "trip_style": stage1.TRIP_STYLES[i % len(stage1.TRIP_STYLES)],
        "name": person.full_name,
        "email": person.email,
        "phone": person.phone_e164,
        "preferred_channel": "email",
        "heard_from": stage1.HEARD_FROM[i % len(stage1.HEARD_FROM)],
        "consent_contact": True,
        "consent_marketing": i % 2 == 0,
        "locale": person.preferred_language,
        "client_submission_id": str(uuid.UUID(hashlib.sha256(f"{person.agency_key}:{i}".encode()).hexdigest()[:32])),
    }
    return answers
