"""Issues the certificate chain of the CRM seal key for PAdES PDFs (AA-1012, card CRM-25b), or checks the committed one.

The seal key (``alias/crm-<env>/seal``, RSA_3072) never leaves KMS. This script reads only its public key
(``kms:GetPublicKey``: the account's IAM admins may, through the key policy's ``IamAdministration`` statement; no crypto
verb is used), then :func:`app.esign.certificate.issue_chain` makes a self-issued company CA in memory, issues the leaf
certificate for that public key, and drops the CA key. It writes the chain (leaf, then CA; PEM, public material only)
to ``app/esign/certs/seal-chain-<env>.pem``, the file the API of that environment reads. Commit that file; no private
key is ever written. Running it again makes a new CA (renewal, or after a seal key rotation).

Usage (from ``api/``; credentials of the admin role exported in the environment, as the steering's
``export-credentials`` pattern, so boto3 does not ask for an MFA code)::

    python3 scripts/esign_seal_certificate.py issue --environment dev            # writes the chain file
    python3 scripts/esign_seal_certificate.py check --environment dev            # the file matches the KMS key?

``--key-id`` (default ``alias/crm-<env>/seal``), ``--region`` (default us-west-1), ``--profile`` (default empty: the
exported credentials), ``--ca-days`` (default 1826) and ``--leaf-days`` (default 730). Prints the subject, validity
and SHA-256 fingerprints. Exit code 0 on success, 1 when ``check`` finds a mismatch or no file.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography import x509  # noqa: E402

from app.esign.certificate import fingerprint, issue_chain, load_pem  # noqa: E402
from app.esign.pades import chain_path  # noqa: E402

REGION = "us-west-1"
CA_DAYS = 1826
LEAF_DAYS = 730


def kms_client(profile: str, region: str) -> Any:
    import boto3

    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    return session.client("kms", region_name=region)


def describe(pem: bytes) -> list[str]:
    chain = load_pem(pem)
    out = []
    for der in (chain.leaf, *chain.issuers):
        cert = x509.load_der_x509_certificate(der)
        out.append(
            f"{cert.subject.rfc4514_string()} | {cert.not_valid_before_utc:%Y-%m-%d} -> "
            f"{cert.not_valid_after_utc:%Y-%m-%d} | sha256 {fingerprint(der)}"
        )
    return out


def main(argv: Sequence[str] | None = None, *, client: Callable[[str, str], Any] = kms_client,
         out_dir: Path | None = None) -> int:  # fmt: skip
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("issue", "check"))
    parser.add_argument("--environment", required=True, choices=("dev", "stg", "prod"))
    parser.add_argument("--key-id", default="")
    parser.add_argument("--region", default=REGION)
    parser.add_argument("--profile", default="")
    parser.add_argument("--ca-days", type=int, default=CA_DAYS)
    parser.add_argument("--leaf-days", type=int, default=LEAF_DAYS)
    args = parser.parse_args(argv)
    key_id = args.key_id or f"alias/crm-{args.environment}/seal"
    path = (out_dir / f"seal-chain-{args.environment}.pem") if out_dir else chain_path(args.environment)
    public_key = bytes(client(args.profile, args.region).get_public_key(KeyId=key_id)["PublicKey"])
    if args.mode == "check":
        if not path.is_file():
            print(f"no chain file at {path}")
            return 1
        pem = path.read_bytes()
        ok = load_pem(pem).leaf_public_key() == public_key
        print("\n".join(describe(pem)))
        print("matches the KMS key" if ok else "DOES NOT match the KMS key: issue a new chain")
        return 0 if ok else 1
    pem = issue_chain(
        public_key,
        environment=args.environment,
        now=dt.datetime.now(dt.UTC),
        ca_days=args.ca_days,
        leaf_days=args.leaf_days,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pem)
    print(f"wrote {path}")
    print("\n".join(describe(pem)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
