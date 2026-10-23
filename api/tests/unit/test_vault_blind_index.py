"""Blind indexes: normalisation, HKDF per tenant and column, truncation sizes, token lossiness."""

from __future__ import annotations

import os

import pytest

from app.vault import blind_index as bi
from tests.unit.vault_fakes import TENANT_A, TENANT_B

ROOT = os.urandom(32)
K_EMAIL = bi.index_key(ROOT, TENANT_A, "traveler.email")


def test_email_normalisation() -> None:
    assert bi.normalise_email("  An.Nguyen@Example.TEST ") == "an.nguyen@example.test"
    assert bi.email_index(K_EMAIL, "AN@example.test") == bi.email_index(K_EMAIL, "an@EXAMPLE.test ")
    for bad in ("", "no-at", "@x.test", "a@", "a@b@c", "a b@c.test"):
        with pytest.raises(bi.NormalisationError):
            bi.normalise_email(bad)


def test_phone_to_e164() -> None:
    assert bi.normalise_phone("+84 91 234 5678") == "+84912345678"
    assert bi.normalise_phone("091 234 5678", "vn") == "+84912345678"
    assert bi.normalise_phone("0412 345 678", "AU") == "+61412345678"
    key = bi.index_key(ROOT, TENANT_A, "traveler.phone")
    assert bi.phone_index(key, "+84 912-345-678") == bi.phone_index(key, "0912345678", "VN")
    for bad, region in (("0912345678", None), ("+84 1", None), ("not a phone", "VN")):
        with pytest.raises(bi.NormalisationError):
            bi.normalise_phone(bad, region)


def test_vietnamese_names_fold_to_ascii() -> None:
    assert bi.normalise_name("  Đặng   Thị Ngọc-Ánh ") == "dang thi ngoc anh"
    assert bi.normalise_name("NGUYỄN văn AN") == "nguyen van an"
    assert bi.name_tokens("O'Brien_Søren") == ["o", "brien", "soren"]
    with pytest.raises(bi.NormalisationError):
        bi.name_tokens(" -- ")
    key = bi.index_key(ROOT, TENANT_A, "traveler.name")
    assert bi.name_index(key, "Nguyễn Văn An") == bi.name_index(key, "nguyen van an")


def test_sizes_and_tokens() -> None:
    key = bi.index_key(ROOT, TENANT_A, "traveler.name_token")
    assert len(bi.email_index(K_EMAIL, "a@b.test")) == bi.EMAIL_BYTES == 16
    assert len(bi.name_index(key, "An")) == bi.NAME_BYTES == 8
    tokens = bi.name_token_indexes(key, "An An Binh")
    assert len(tokens) == 2 and all(len(t) == bi.TOKEN_BYTES == 4 for t in tokens)
    assert len(bi.name_token_indexes(key, " ".join(f"t{i}" for i in range(40)))) == bi.MAX_TOKENS


def test_keys_differ_per_tenant_and_column() -> None:
    other_tenant = bi.index_key(ROOT, TENANT_B, "traveler.email")
    other_column = bi.index_key(ROOT, TENANT_A, "member.email")
    assert len({K_EMAIL, other_tenant, other_column}) == 3
    value = "same@example.test"
    assert bi.email_index(K_EMAIL, value) != bi.email_index(other_tenant, value)
    with pytest.raises(ValueError):
        bi.index_key(ROOT, TENANT_A, "email")
