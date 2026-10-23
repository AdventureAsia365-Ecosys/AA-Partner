"""KEK wrappers: local keyring and KMS (fake client) — context binding, error classes, self-test."""

from __future__ import annotations

import os
import threading
import time

import pytest

from app.vault.errors import KeyUnavailable, UnwrapDenied
from app.vault.wrapping import KmsKeyWrapper, LocalKeyWrapper, canonical_context
from tests.unit.vault_fakes import TENANT_A, TENANT_B, FakeKms

CTX_A = {"tenant_id": str(TENANT_A), "purpose": "vault"}
CTX_B = {"tenant_id": str(TENANT_B), "purpose": "vault"}


def test_canonical_context_is_exactly_tenant_and_purpose() -> None:
    assert canonical_context({"purpose": "vault", "tenant_id": "x"}) == b'{"purpose":"vault","tenant_id":"x"}'
    with pytest.raises(ValueError):
        canonical_context({"tenant_id": "x"})


@pytest.mark.asyncio
async def test_local_round_trip_and_context_binding() -> None:
    wrapper = LocalKeyWrapper({"k1": os.urandom(32)}, "k1")
    plaintext, wrapped, kek_id = await wrapper.generate(CTX_A)
    assert kek_id == "k1" and len(plaintext) == 32 and plaintext not in wrapped
    assert await wrapper.unwrap(wrapped, CTX_A, kek_id) == plaintext
    with pytest.raises(UnwrapDenied):
        await wrapper.unwrap(wrapped, CTX_B, kek_id)
    with pytest.raises(UnwrapDenied):
        await wrapper.unwrap(wrapped, {**CTX_A, "purpose": "docs"}, kek_id)
    with pytest.raises(KeyUnavailable):
        await wrapper.unwrap(wrapped, CTX_A, "k-missing")
    await wrapper.self_test()


@pytest.mark.asyncio
async def test_local_keyring_rotation_keeps_old_keks() -> None:
    old = LocalKeyWrapper({"k1": os.urandom(32)}, "k1")
    _, wrapped, kek_id = await old.generate(CTX_A)
    new = old.with_kek("k2", os.urandom(32))
    assert new.active == "k2"
    assert len(await new.unwrap(wrapped, CTX_A, kek_id)) == 32
    with pytest.raises(KeyUnavailable):
        await new.without("k1").unwrap(wrapped, CTX_A, kek_id)
    with pytest.raises(ValueError):
        LocalKeyWrapper({"k1": b"short"}, "k1")
    with pytest.raises(ValueError):
        LocalKeyWrapper({"k1": os.urandom(32)}, "k2")


@pytest.mark.asyncio
async def test_kms_round_trip_with_the_encryption_context() -> None:
    kms = FakeKms("data")
    wrapper = KmsKeyWrapper("alias/crm-dev/data", client_factory=lambda: kms)
    plaintext, wrapped, kek_id = await wrapper.generate(CTX_A)
    assert kek_id == FakeKms.ARN.format("data")
    assert await wrapper.unwrap(wrapped, CTX_A, kek_id) == plaintext
    with pytest.raises(UnwrapDenied, match="InvalidCiphertextException"):
        await wrapper.unwrap(wrapped, CTX_B, kek_id)
    rewrapped, _ = await wrapper.wrap(plaintext, CTX_A)
    assert await wrapper.unwrap(rewrapped, CTX_A, kek_id) == plaintext
    await wrapper.self_test()
    assert kms.calls.count("generate_data_key") == 2


@pytest.mark.parametrize(
    ("code", "error"),
    [
        ("InvalidCiphertextException", UnwrapDenied),
        ("IncorrectKeyException", UnwrapDenied),
        # IAM / key-policy faults: not about the value, so 503 and no false "denied" audit row.
        ("AccessDeniedException", KeyUnavailable),
        ("InvalidKeyUsageException", KeyUnavailable),
        ("ThrottlingException", KeyUnavailable),
        ("KMSInvalidStateException", KeyUnavailable),
        ("DisabledException", KeyUnavailable),
    ],
)
@pytest.mark.asyncio
async def test_kms_errors_are_classified(code: str, error: type[Exception]) -> None:
    kms = FakeKms("data")
    kms.fail_with = code
    wrapper = KmsKeyWrapper("alias/crm-dev/data", client_factory=lambda: kms)
    with pytest.raises(error):
        await wrapper.unwrap(b"x", CTX_A, "k")


@pytest.mark.asyncio
async def test_kms_non_client_errors_are_unavailable_and_short_plaintext_refused() -> None:
    class Broken:
        def decrypt(self, **_: object) -> dict:
            raise ConnectionError("network down")

    with pytest.raises(KeyUnavailable) as info:
        await KmsKeyWrapper("alias/x", client_factory=Broken).unwrap(b"x", CTX_A, "k")
    assert info.value.reason == "ConnectionError"

    class Short:
        def decrypt(self, **_: object) -> dict:
            return {"Plaintext": b"16-bytes-only!!!"}

    with pytest.raises(UnwrapDenied):
        await KmsKeyWrapper("alias/x", client_factory=Short).unwrap(b"x", CTX_A, "k")
    with pytest.raises(ValueError):
        KmsKeyWrapper("")


def test_default_kms_client_is_lazy_and_regional(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.vault import wrapping

    monkeypatch.setenv("AWS_REGION", "us-west-1")
    client = wrapping.default_kms_client()
    assert client.meta.region_name == "us-west-1"


def test_the_kms_client_is_built_once_under_concurrent_first_calls() -> None:
    """The first KMS calls after a deploy run in parallel to_thread workers: one client, built once."""
    built: list[object] = []

    def slow_factory() -> object:
        time.sleep(0.05)  # widen the race window
        built.append(object())
        return built[-1]

    wrapper = KmsKeyWrapper("alias/crm-dev/data", client_factory=slow_factory)
    barrier = threading.Barrier(8)
    seen: list[object] = []

    def first_call() -> None:
        barrier.wait()
        seen.append(wrapper._kms())

    threads = [threading.Thread(target=first_call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(built) == 1 and all(c is built[0] for c in seen)


def test_default_kms_client_uses_its_own_session(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    from app.vault import wrapping

    def no_default_session(*_: object, **__: object) -> None:
        raise AssertionError("the default boto3 session is not thread-safe")

    monkeypatch.setattr(boto3, "client", no_default_session)
    monkeypatch.setenv("AWS_REGION", "us-west-1")
    assert wrapping.default_kms_client().meta.region_name == "us-west-1"
