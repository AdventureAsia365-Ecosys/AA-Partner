"""F1 fields, F2 codes and reveal reasons (AA-937; crm-003 / crm-006 columns, lane §2.3–2.4).

Everything here mirrors a CHECK of the migrations; the database stays the judge, these lists give clear 422s and the
web its choices.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from app.errors import Invalid
from app.intake import stage1
from app.vault.blind_index import NormalisationError, normalise_email, normalise_phone

# One F1 field the agency may reveal -> its encrypted column on crm.traveler (design §4.4: click-to-reveal, logged).
REVEAL_COLUMNS: dict[str, str] = {
    "given_name": "given_name_enc",
    "family_name": "family_name_enc",
    "email": "email_enc",
    "phone": "phone_enc",
    "dob": "dob_enc",
    "nationality": "nationality_enc",
    "passport_no": "passport_no_enc",
    "passport_expiry": "passport_expiry_enc",
    "passport_country": "passport_country_enc",
    "emergency_contact": "emergency_contact_enc",
}
HEALTH_FIELD = "health"
REVEAL_FIELDS = (*REVEAL_COLUMNS, HEALTH_FIELD)
# Stage 3 data (design §4.3 "never in an AI view or prompt"; F5 groups them as identity documents).
IDENTITY_DOCUMENT_FIELDS = frozenset({"dob", "nationality", "passport_no", "passport_expiry", "passport_country"})

# Why someone opens a field (design §2.8 WF-2, §4.9: "each view is logged with a reason"). Codes only.
REVEAL_REASONS = (
    "contact_client",
    "prepare_booking",
    "verify_identity",
    "client_request",
    "fill_forms",
    "ops_handoff",
    "other",
)

# The F1 fields an agency member may write (PATCH /travelers/{id}); display_name is required on create.
EDITABLE_F1 = (
    "display_name",
    "given_name",
    "family_name",
    "email",
    "phone",
    "dob",
    "nationality",
    "passport_no",
    "passport_expiry",
    "passport_country",
    "emergency_contact",
)
CHANNELS = ("email", "phone", "sms", "whatsapp")
LEAD_SOURCE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_PASSPORT = re.compile(r"^[A-Z0-9<]{5,20}$")

# F2 profile codes (crm-006 traveler_profile CHECKs).
BUDGET_BANDS = stage1.BUDGET_BANDS
PACES = stage1.TRIP_STYLES
GROUP_PREFS = ("private", "small_group")
FITNESS_BANDS = ("up_to_2h", "2_4h", "4_6h", "over_6h")
TREK_EXPERIENCE = ("none", "some", "experienced")
ACCOMMODATION_TIERS = ("budget", "comfort", "premium", "luxury")
OCCASIONS = ("none", "honeymoon", "anniversary", "birthday", "family_reunion", "graduation", "retirement", "other")
BUDGET_FLEX = ("fixed", "some", "flexible")
DECISION_MAKERS = ("self", "partner", "family", "group", "company")
BOOKING_TIMELINES = ("ready_now", "within_month", "within_3_months", "later", "just_looking")
DIETARY_FLAGS = (
    "vegetarian",
    "vegan",
    "halal",
    "kosher",
    "gluten_free",
    "lactose_free",
    "nut_allergy",
    "shellfish_allergy",
    "other_allergy",
)
MOBILITY_FLAGS = ("limited_walking", "wheelchair", "no_stairs", "no_long_drives", "other")
# Medical codes stay F1 only (crm.traveler_health); dietary and mobility codes are mirrored to F2.
MEDICAL_FLAGS = ("asthma", "diabetes", "heart_condition", "epilepsy", "pregnancy", "severe_allergy", "other_medical")
CODE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
MAX_CODES = 20

PROFILE_CHOICES: dict[str, tuple[str, ...]] = {
    "budget_band": BUDGET_BANDS,
    "pace": PACES,
    "group_pref": GROUP_PREFS,
    "fitness_band": FITNESS_BANDS,
    "trek_experience": TREK_EXPERIENCE,
    "accommodation_tier": ACCOMMODATION_TIERS,
    "occasion": OCCASIONS,
    "budget_flex": BUDGET_FLEX,
    "decision_maker": DECISION_MAKERS,
    "booking_timeline": BOOKING_TIMELINES,
}
PROFILE_CODE_LISTS = ("interests", "must_do_codes", "must_avoid_codes")
PROFILE_FLAGS = ("altitude_ok", "competing_quotes")
PROFILE_FIELDS = (*PROFILE_CHOICES, *PROFILE_CODE_LISTS, *PROFILE_FLAGS, "residence_country")

RELATION_KINDS = ("spouse", "partner", "parent", "child", "sibling", "friend", "referrer", "referred")


def clean_f1(field: str, value: Any) -> str | None:
    """The normalised plaintext of one editable F1 field (``None`` clears it), or 422 ``invalid_<field>``."""
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            if field == "display_name":
                raise ValueError
            return None
        if field in ("display_name", "given_name", "family_name"):
            text = stage1.clean_text(value, stage1.MAX_NAME)
            if not text:
                raise ValueError
            return text
        if field == "email":
            text = stage1.clean_text(value, 254)
            return normalise_email(text or "")
        if field == "phone":
            text = stage1.clean_text(value, 40)
            return normalise_phone(text or "")
        if field in ("dob", "passport_expiry"):
            day = dt.date.fromisoformat(str(value))
            if field == "dob" and not (dt.date(1900, 1, 1) <= day <= dt.date.today()):
                raise ValueError
            return day.isoformat()
        if field in ("nationality", "passport_country"):
            code = str(value).strip().upper()
            if not _COUNTRY.match(code):
                raise ValueError
            return code
        if field == "passport_no":
            number = re.sub(r"[\s-]", "", str(value)).upper()
            if not _PASSPORT.match(number):
                raise ValueError
            return number
        if field == "emergency_contact":
            text = stage1.clean_text(value, 300)
            if not text:
                raise ValueError
            return text
    except (ValueError, TypeError, NormalisationError):
        raise Invalid(f"invalid_{field}", f"{field} is not valid") from None
    raise Invalid("invalid_field", f"{field} is not an editable field")


def clean_profile(changes: dict[str, Any]) -> dict[str, Any]:
    """F2 profile changes: codes from the fixed lists only (crm-006 CHECKs), or 422 ``invalid_<field>``."""
    out: dict[str, Any] = {}
    for name, value in changes.items():
        if name in PROFILE_CHOICES:
            if value is not None and value not in PROFILE_CHOICES[name]:
                raise Invalid(f"invalid_{name}", f"one of {', '.join(PROFILE_CHOICES[name])}")
            out[name] = value
        elif name in PROFILE_CODE_LISTS:
            codes = _codes(name, value, None)
            out[name] = codes
        elif name in PROFILE_FLAGS:
            if value is not None and not isinstance(value, bool):
                raise Invalid(f"invalid_{name}", "true, false or null")
            out[name] = value
        elif name == "residence_country":
            code = value.strip().upper() if isinstance(value, str) else value
            if code is not None and not (isinstance(code, str) and _COUNTRY.match(code)):
                raise Invalid("invalid_residence_country", "an ISO 3166-1 alpha-2 code")
            out[name] = code
        else:
            raise Invalid("invalid_field", f"{name} is not a profile field")
    return out


def clean_health(dietary: Any, mobility: Any, medical: Any) -> dict[str, list[str]]:
    return {
        "dietary": _codes("dietary", dietary, DIETARY_FLAGS),
        "mobility": _codes("mobility", mobility, MOBILITY_FLAGS),
        "medical": _codes("medical", medical, MEDICAL_FLAGS),
    }


def _codes(name: str, value: Any, allowed: tuple[str, ...] | None) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_CODES:
        raise Invalid(f"invalid_{name}", f"a list of at most {MAX_CODES} codes")
    codes = list(dict.fromkeys(value))
    for code in codes:
        if not isinstance(code, str) or not CODE.match(code) or (allowed is not None and code not in allowed):
            raise Invalid(f"invalid_{name}", "codes only" if allowed is None else f"one of {', '.join(allowed)}")
    return codes


def mask_passport(number: str | None) -> str | None:
    """ "•••• 123": the last three characters only (lane §5.5, write-only passport on the traveller side)."""
    if not number:
        return None
    return "•••• " + number[-3:]
