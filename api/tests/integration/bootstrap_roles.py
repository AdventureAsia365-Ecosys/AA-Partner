"""Give a throwaway PostgreSQL the role shape of the Dev RDS before the integration tests run.

Usage (from api/, once per fresh database; idempotent)::

    python -m tests.integration.bootstrap_roles <superuser DSN> [<admin DSN>]

The first argument is a SUPERUSER DSN for the same database as the admin DSN the tests use (``CRM_TEST_ADMIN_DSN``,
``aa_cis_admin``, whose password is set here). The superuser is used only here: the tests log in as ``aa_cis_admin``
WITHOUT superuser, like the RDS master (``rds_superuser`` is not a superuser), because a superuser skips every
membership, ownership and RLS check (X1 plan S5, the lesson behind AA-Booking's bootstrap).

Roles, as AA-CIS-Infra ``db/migrations/roles/003_crm_roles.sql`` and ``004_crm_nologin_roles.sql`` (AA-932) leave them:

* ``aa_cis_admin`` LOGIN NOSUPERUSER CREATEROLE CREATEDB, owner of the test database (the DB Migrate login), member of
  ``crm_owner`` (rl-003 grants it, so DB Migrate can SET ROLE crm_owner);
* ``crm_owner`` NOLOGIN (owns schema crm and every object in it);
* ``crm_app`` LOGIN NOINHERIT CONNECTION LIMIT 6, the API's runtime role (CI-only password from ``conftest.py``);
* ``crm_ai_reader``, ``crm_insights``, ``crm_support_reader``, ``crm_eraser`` NOLOGIN, granted to ``crm_app`` only;
* ``cis_app`` and ``booking`` NOLOGIN stand-ins of the other apps' runtime roles, so crm-001's isolation guard and the
  tests' 42501 checks have something to refuse.

Schema ``crm`` itself is (re)created by ``conftest.py`` as the master, owned by ``crm_owner``, like rl-003 does.
"""

from __future__ import annotations

import asyncio
import sys
from urllib.parse import unquote, urlsplit

import asyncpg

from tests.integration.conftest import ADMIN_DSN, APP_ROLE, CRM_TEST_PASSWORD, MASTER, OWNER, READERS

OTHER_APP_ROLES = ("cis_app", "booking")


async def _ensure_role(conn: asyncpg.Connection, name: str, attributes: str, password: str | None) -> None:
    exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", name)
    verb = "ALTER" if exists else "CREATE"
    pw = "" if password is None else " PASSWORD " + await conn.fetchval("SELECT quote_literal($1)", password)
    await conn.execute(f"{verb} ROLE {name} {attributes}{pw}")


async def bootstrap(superuser_dsn: str, admin_dsn: str) -> None:
    admin = urlsplit(admin_dsn)
    target = urlsplit(superuser_dsn)
    if admin.username != MASTER:
        raise SystemExit(f"the admin DSN must log in as {MASTER}, got {admin.username!r}")
    if target.path != admin.path:
        raise SystemExit("the superuser DSN and the admin DSN must name the same database")
    if target.username == MASTER:
        raise SystemExit(f"{MASTER} must not be the container superuser: bootstrap as another superuser (postgres)")
    conn = await asyncpg.connect(superuser_dsn)
    try:
        if not await conn.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"):
            raise SystemExit(f"{target.username!r} is not a superuser")
        await _ensure_role(
            conn,
            MASTER,
            "LOGIN NOSUPERUSER CREATEROLE CREATEDB NOBYPASSRLS NOREPLICATION",
            unquote(admin.password or ""),
        )
        database = admin.path.lstrip("/")
        await conn.execute(f'ALTER DATABASE "{database}" OWNER TO {MASTER}')
        await _ensure_role(conn, OWNER, "NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS", None)
        await conn.execute(f"GRANT {OWNER} TO {MASTER}")
        await _ensure_role(
            conn,
            APP_ROLE,
            "LOGIN NOINHERIT NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS CONNECTION LIMIT 6",
            CRM_TEST_PASSWORD,
        )
        for reader in READERS:
            await _ensure_role(conn, reader, "NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS", None)
            await conn.execute(f"GRANT {reader} TO {APP_ROLE}")
        for other in OTHER_APP_ROLES:
            await _ensure_role(conn, other, "NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS", None)
    finally:
        await conn.close()
    print(f"bootstrap ok: {MASTER} (no superuser, member of {OWNER}), {APP_ROLE} LOGIN NOINHERIT, readers {READERS}")


def main(argv: list[str]) -> None:
    if len(argv) not in (2, 3):
        raise SystemExit(f"usage: python -m tests.integration.bootstrap_roles <superuser DSN> [<admin DSN>]\n{__doc__}")
    admin_dsn = argv[2] if len(argv) == 3 else ADMIN_DSN
    if not admin_dsn:
        raise SystemExit("no admin DSN: pass it as the second argument or set CRM_TEST_ADMIN_DSN")
    asyncio.run(bootstrap(argv[1], admin_dsn))


if __name__ == "__main__":
    main(sys.argv)
