#!/usr/bin/env python3
"""AA-969 (CRM-37a): sign the load-test identity in and write its access token to a file (mode 0600), for
loadtest/crm_loadtest.py. Run from a workstation with an MFA session of the Dev account; needs boto3.

    python3 loadtest/get_token.py --out <scratch>/lt-token [--profile aa365-admin] [--env dev]

Read-only AWS calls plus the sign-in itself: the identity's sign-in material from Secrets Manager
``crm/<env>/e2e-test-crm/lt-agent`` (written by AA-CIS-Infra ``tools/identity/crm_dev_identity.py e2e-users``), the
partner pool and its ``partner-web`` client found by NAME (``aa-partner-<env>``, ``aa-partner-<env>-partner-web``),
the client secret read from the client, then Cognito ``USER_AUTH`` with the password and the authenticator code (the
partner pool requires MFA). Nothing is printed but the file name and the token's lifetime: no token, password, code
or secret ever reaches the terminal. The token lives 30 minutes (identity_partner.tf ``access_minutes``).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import struct
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

REGION = "us-west-1"


def totp(secret_b32: str, at: float | None = None, step: int = 30) -> str:
    """RFC 6238 (SHA-1, 6 digits), as the e2e sign-in helpers."""
    key = base64.b32decode(secret_b32.upper() + "=" * (-len(secret_b32) % 8))
    counter = struct.pack(">Q", int((time.time() if at is None else at) // step))
    mac = hmac.new(key, counter, hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    return f"{(struct.unpack('>I', mac[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def secret_hash(username: str, client_id: str, client_secret: str) -> str:
    digest = hmac.new(client_secret.encode(), (username + client_id).encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _find(pages: Any, key: str, name_key: str, name: str, id_key: str) -> str:
    for page in pages:
        for item in page.get(key, []):
            if item.get(name_key) == name:
                return str(item[id_key])
    raise SystemExit(f"{name} not found")


def sign_in(idp: Any, client_id: str, client_secret: str, who: dict[str, str], now: float | None = None) -> str:
    """USER_AUTH + PASSWORD, then SOFTWARE_TOKEN_MFA. Returns the access token."""
    email = who["email"]
    sh = secret_hash(email, client_id, client_secret)
    resp = idp.initiate_auth(
        ClientId=client_id,
        AuthFlow="USER_AUTH",
        AuthParameters={
            "USERNAME": email,
            "PREFERRED_CHALLENGE": "PASSWORD",
            "PASSWORD": who["password"],
            "SECRET_HASH": sh,
        },
    )
    if resp.get("ChallengeName") == "SELECT_CHALLENGE":
        resp = idp.respond_to_auth_challenge(
            ClientId=client_id,
            ChallengeName="SELECT_CHALLENGE",
            Session=resp["Session"],
            ChallengeResponses={
                "USERNAME": email,
                "ANSWER": "PASSWORD",
                "PASSWORD": who["password"],
                "SECRET_HASH": sh,
            },
        )
    if resp.get("ChallengeName") == "SOFTWARE_TOKEN_MFA":
        resp = idp.respond_to_auth_challenge(
            ClientId=client_id,
            ChallengeName="SOFTWARE_TOKEN_MFA",
            Session=resp["Session"],
            ChallengeResponses={
                "USERNAME": email,
                "SOFTWARE_TOKEN_MFA_CODE": totp(who["totp_secret"], now),
                "SECRET_HASH": sh,
            },
        )
    result = resp.get("AuthenticationResult")
    if not result or not result.get("AccessToken"):
        raise SystemExit(f"sign-in did not finish ({resp.get('ChallengeName')})")
    return str(result["AccessToken"])


def write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="get_token.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--out", required=True)
    p.add_argument("--profile", default="aa365-admin")
    p.add_argument("--env", default="dev", choices=["dev"])
    args = p.parse_args(sys.argv[1:] if argv is None else argv)
    import boto3  # noqa: PLC0415 — only this tool needs it

    session = boto3.Session(profile_name=args.profile, region_name=REGION)
    sm, idp = session.client("secretsmanager"), session.client("cognito-idp")
    who = json.loads(sm.get_secret_value(SecretId=f"crm/{args.env}/e2e-test-crm/lt-agent")["SecretString"])
    pool_id = _find(
        idp.get_paginator("list_user_pools").paginate(MaxResults=60),
        "UserPools",
        "Name",
        f"aa-partner-{args.env}",
        "Id",
    )
    client_id = _find(
        idp.get_paginator("list_user_pool_clients").paginate(UserPoolId=pool_id, MaxResults=60),
        "UserPoolClients",
        "ClientName",
        f"aa-partner-{args.env}-partner-web",
        "ClientId",
    )
    client_secret = idp.describe_user_pool_client(UserPoolId=pool_id, ClientId=client_id)["UserPoolClient"][
        "ClientSecret"
    ]
    token = sign_in(idp, client_id, client_secret, who)
    write_private(Path(args.out), token)
    print(json.dumps({"token_file": args.out, "valid_minutes": 30}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
