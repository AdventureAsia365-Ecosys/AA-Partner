"""The field value format: round trip, AAD binding (tenant, table, column, row), header and body tampering."""

from __future__ import annotations

import os
import uuid

import pytest

from app.vault.cipher import (
    MIN_BLOB,
    FieldFormatError,
    InvalidTag,
    field_aad,
    header_of,
    key_version_of,
    open_sealed,
    seal,
)
from tests.unit.vault_fakes import TENANT_A, TENANT_B

KEY = os.urandom(32)
ROW = uuid.UUID("00000000-0000-4000-8000-000000000001")


def _aad(tenant: uuid.UUID = TENANT_A, table: str = "traveler", column: str = "email_enc", row=ROW) -> bytes:  # noqa: ANN001
    return field_aad(tenant, table, column, row)


def test_round_trip_and_format() -> None:
    blob = seal(KEY, 7, "Nguyễn Văn An".encode(), _aad())
    assert key_version_of(blob) == 7
    assert blob[:2] == header_of(7) == b"\x00\x07"
    assert len(blob) == 2 + 12 + len("Nguyễn Văn An".encode()) + 16
    assert open_sealed(KEY, blob, _aad()).decode() == "Nguyễn Văn An"


def test_each_value_has_its_own_nonce() -> None:
    assert seal(KEY, 1, b"same", _aad()) != seal(KEY, 1, b"same", _aad())


@pytest.mark.parametrize(
    "other",
    [
        {"tenant": TENANT_B},
        {"table": "member"},
        {"column": "phone_enc"},
        {"row": uuid.UUID("00000000-0000-4000-8000-000000000002")},
    ],
)
def test_value_cannot_move(other: dict) -> None:
    blob = seal(KEY, 1, b"secret", _aad())
    with pytest.raises(InvalidTag):
        open_sealed(KEY, blob, _aad(**other))


def test_tampered_header_body_or_key_fails() -> None:
    blob = bytearray(seal(KEY, 1, b"secret", _aad()))
    for index in (1, 5, len(blob) - 1):
        tampered = bytearray(blob)
        tampered[index] ^= 0x01
        with pytest.raises(InvalidTag):
            open_sealed(KEY, bytes(tampered), _aad())
    with pytest.raises(InvalidTag):
        open_sealed(os.urandom(32), bytes(blob), _aad())


def test_short_values_and_bad_names_are_refused() -> None:
    with pytest.raises(FieldFormatError):
        open_sealed(KEY, b"\x00" * (MIN_BLOB - 1), _aad())
    with pytest.raises(FieldFormatError):
        key_version_of(b"\x00\x01")
    assert len(seal(KEY, 1, b"", _aad())) == MIN_BLOB
    with pytest.raises(ValueError):
        field_aad(TENANT_A, "Traveler; DROP", "email_enc", ROW)
    with pytest.raises(ValueError):
        field_aad(TENANT_A, "traveler", "email_enc", "row\x1fid")
    with pytest.raises(TypeError):
        field_aad(str(TENANT_A), "traveler", "email_enc", ROW)  # type: ignore[arg-type]
    for version in (0, 65536):
        with pytest.raises(ValueError):
            header_of(version)
