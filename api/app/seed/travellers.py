"""Synthetic travellers (Faker EN/VI) for the Dev agencies (AA-935, card CRM-07).

Deterministic: the same agency key and count always give the same people (Faker seeded per agency and index), so a
re-run is idempotent and a test can pin a value. Every string is NFC-normalised, the form the API stores, so a
Vietnamese name such as "Nguyễn Thị Ngọc Lan" round-trips byte for byte (card E test).

Vietnamese names are composed in Vietnamese order (family, middle, given) from the ``vi_VN`` provider's gendered
given names (its plain ``first_name`` returns English placeholders). E-mail addresses use the reserved ``.invalid``
top-level domain (RFC 2606), so no message to a synthetic traveller can ever be delivered; phones are fake numbers in
E.164 form.

The vault table ``crm.traveler`` exists (crm-003, AA-934); rows are written by the intake card (AA-936) through
:mod:`app.vault` field encryption: this module is the generator those cards import; :mod:`app.seed` reports the
planned travellers and does not write them before that path exists (no personal data outside the vault rules).
"""

from __future__ import annotations

import hashlib
import unicodedata
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
