"""Possible duplicate clients and the merge (AA-962, card CRM-31 "merge screen"; design v3 R6, A-06 Duplicates,
A-07 [Merge...]; delivery plan: "merging two travellers keeps both trip histories and one F1 record").

**Candidates** (``GET /crm/v1/travelers/duplicates``): live clients sharing the normalised name (``name_bidx``), the
e-mail or the phone (blind indexes, exact only; no decrypt to find them). The page shows display names only, one
``list_view`` audit event naming the ids shown (design §4.4); which fields matched is a flag, never a value.

**Merge** (``POST /crm/v1/travelers/{keep_id}/merge``, owner only like a client delete, R8): in one transaction,

1. refused (409 ``merge_blocked`` with the blocker codes, nothing changed) when the client that goes has records
   whose evidence names it and cannot be moved: client links (forms, proposals, portal), portal access, signatures
   or accepted terms. The page then offers to keep that client instead. A legal hold on either client: 409
   ``legal_hold``;
2. the trips of the other client move to the kept one (both party twins; when both were on a trip, the kept client
   stays once and takes the lead role if the other had it), with the age bands recomputed;
3. notes, documents, conversations, messages to send, call slots and inquiry submissions move to the kept client;
4. relationships are re-made from the kept client (the database writes each inverse; a referrer the kept client
   already has wins), and clients the other one referred now name the kept one;
5. consents are copied to the kept client with their original time and source (the ledger is append-only; the
   latest statement per purpose decides, as everywhere), and health codes move when the kept client has none;
6. every F1 field the kept client lacks is filled from the other one (decrypted and sealed again under the kept
   row's id, blind indexes copied); the kept client takes every import reference the other answered to (its own
   when the kept one has none, else as aliases, crm-030 ``import_ref_alias_bidx``), so re-running an import finds the
   kept client for the other client's rows too (card E);
7. the other client goes to the trash (``app.lifecycle.trash``): the purge after the agency's window erases its
   remaining F1 record, so one F1 record stays.

One ``merge`` audit event names both ids and the F1 fields filled (names only, never values).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import asyncpg

from app.audit import MAX_OBJECT_IDS
from app.errors import Conflict, Invalid, NotFound
from app.lifecycle import trash
from app.lifecycle.window import trash_days
from app.tenancy import TenantContext
from app.travellers import age_bands
from app.vault import Vault

PAGE_SIZE = 50
# F1 columns filled on the kept client when it has none (the column holds a value sealed under the row id).
FILL_COLUMNS = (
    "given_name_enc",
    "family_name_enc",
    "email_enc",
    "phone_enc",
    "dob_enc",
    "nationality_enc",
    "passport_no_enc",
    "passport_expiry_enc",
    "passport_country_enc",
    "emergency_contact_enc",
)
BLIND = {"email_enc": "email_bidx", "phone_enc": "phone_bidx"}

CANDIDATES_SQL = """
SELECT a.traveler_id AS a_id, b.traveler_id AS b_id, a.display_name_enc AS a_name, b.display_name_enc AS b_name,
       a.name_bidx = b.name_bidx AS same_name, coalesce(a.email_bidx = b.email_bidx, false) AS same_email,
       coalesce(a.phone_bidx = b.phone_bidx, false) AS same_phone,
       a.created_at AS a_created, b.created_at AS b_created, a.lead_source AS a_source, b.lead_source AS b_source,
       (SELECT count(*) FROM crm.trip_traveler t WHERE t.traveler_id = a.traveler_id AND t.deleted_at IS NULL)::int
           AS a_trips,
       (SELECT count(*) FROM crm.trip_traveler t WHERE t.traveler_id = b.traveler_id AND t.deleted_at IS NULL)::int
           AS b_trips
  FROM crm.traveler a
  JOIN crm.traveler b ON b.tenant_id = a.tenant_id AND a.traveler_id < b.traveler_id
   AND (a.name_bidx = b.name_bidx OR a.email_bidx = b.email_bidx OR a.phone_bidx = b.phone_bidx)
 WHERE a.deleted_at IS NULL AND b.deleted_at IS NULL
 ORDER BY greatest(a.created_at, b.created_at) DESC, a.traveler_id, b.traveler_id
 LIMIT $1
"""
COUNT_SQL = """
SELECT count(*) FROM crm.traveler a
  JOIN crm.traveler b ON b.tenant_id = a.tenant_id AND a.traveler_id < b.traveler_id
   AND (a.name_bidx = b.name_bidx OR a.email_bidx = b.email_bidx OR a.phone_bidx = b.phone_bidx)
 WHERE a.deleted_at IS NULL AND b.deleted_at IS NULL
"""
ROW_SQL = """
SELECT traveler_id, display_name_enc, given_name_enc, family_name_enc, email_enc, phone_enc, dob_enc, nationality_enc,
       passport_no_enc, passport_expiry_enc, passport_country_enc, emergency_contact_enc, email_bidx, phone_bidx,
       profile_token_enc, import_ref_bidx, import_ref_alias_bidx
  FROM crm.traveler WHERE traveler_id = $1 AND deleted_at IS NULL FOR UPDATE
"""
BLOCKERS_SQL = """
SELECT array_remove(ARRAY[
    CASE WHEN EXISTS (SELECT 1 FROM crm.client_link WHERE traveler_id = $1) THEN 'client_links' END,
    CASE WHEN EXISTS (SELECT 1 FROM crm.traveler_access WHERE traveler_id = $1) THEN 'portal_access' END,
    CASE WHEN EXISTS (SELECT 1 FROM crm.signature_request WHERE traveler_id = $1 OR signer_traveler_id = $1)
         THEN 'signatures' END,
    CASE WHEN EXISTS (SELECT 1 FROM crm.terms_acceptance WHERE traveler_id = $1) THEN 'terms_accepted' END
], NULL)
"""
HOLD_SQL = "SELECT crm.legal_hold_blocks('traveler', $1)"
PARTY_SQL = """
SELECT trip_traveler_id, trip_id, role FROM crm.trip_traveler WHERE traveler_id = $1 AND deleted_at IS NULL
"""
MOVE_PARTY_SQL = "UPDATE crm.trip_traveler SET traveler_id = $2 WHERE trip_traveler_id = $1"
DROP_PARTY_SQL = "DELETE FROM crm.trip_traveler WHERE trip_traveler_id = $1"
LEAD_SQL = "UPDATE crm.trip_traveler SET role = 'lead' WHERE trip_id = $1 AND traveler_id = $2"
MOVE_PARTY_F2_SQL = "UPDATE crm.trip_party SET traveler_token = $3 WHERE trip_id = $1 AND traveler_token = $2"
DROP_PARTY_F2_SQL = "DELETE FROM crm.trip_party WHERE trip_id = $1 AND traveler_token = $2"
LEAD_F2_SQL = "UPDATE crm.trip_party SET role = 'lead' WHERE trip_id = $1 AND traveler_token = $2"
DOB_SQL = "SELECT dob_enc FROM crm.traveler WHERE traveler_id = $1"
EMPTY_PROFILE_SQL = "INSERT INTO crm.traveler_profile (traveler_token) VALUES ($1)"
POINTER_SQL = "UPDATE crm.traveler SET profile_token_enc = $2 WHERE traveler_id = $1"
# Rows that name the client and move as they are (crm-005 / 006 / 012 / 015 / 042 FKs; a form submission that came
# through a client link is a blocker above, so only the public-form ones move here).
MOVE_SQL = (
    "UPDATE crm.note SET traveler_id = $2 WHERE traveler_id = $1",
    "UPDATE crm.document SET traveler_id = $2 WHERE traveler_id = $1",
    "UPDATE crm.conversation SET traveler_id = $2 WHERE traveler_id = $1",
    "UPDATE crm.dispatch_queue SET traveler_id = $2 WHERE traveler_id = $1",
    "UPDATE crm.meeting_slot SET traveler_id = $2 WHERE traveler_id = $1",
    "UPDATE crm.form_submission SET traveler_id = $2 WHERE traveler_id = $1 AND client_link_id IS NULL",
    "UPDATE crm.traveler SET referrer_traveler_id = $2 WHERE referrer_traveler_id = $1 AND traveler_id <> $2",
    "UPDATE crm.traveler SET referrer_traveler_id = NULL WHERE traveler_id = $2 AND referrer_traveler_id = $1",
)
RELATIONS_SQL = """
SELECT relation_id, to_traveler_id, kind FROM crm.traveler_relation WHERE from_traveler_id = $1 AND deleted_at IS NULL
"""
KEPT_RELATED_SQL = """
SELECT to_traveler_id FROM crm.traveler_relation WHERE from_traveler_id = $1 AND deleted_at IS NULL
"""
TRASH_RELATION_SQL = """
UPDATE crm.traveler_relation SET deleted_at = now(), deleted_by = $2, purge_after = now() + make_interval(days => $3)
 WHERE relation_id = $1 AND deleted_at IS NULL
"""
INSERT_RELATION_SQL = "INSERT INTO crm.traveler_relation (from_traveler_id, to_traveler_id, kind) VALUES ($1, $2, $3)"
SET_REFERRER_SQL = """
UPDATE crm.traveler SET referrer_traveler_id = $2 WHERE traveler_id = $1 AND referrer_traveler_id IS DISTINCT FROM $2
"""
COPY_CONSENT_SQL = """
INSERT INTO crm.consent (traveler_id, purpose, value, at, source, submission_id, notice_version)
SELECT $2, purpose, value, at, source, submission_id, notice_version FROM crm.consent WHERE traveler_id = $1
"""
HEALTH_SQL = "SELECT health_id, health_flags_enc FROM crm.traveler_health WHERE traveler_id = $1 AND deleted_at IS NULL"
LATEST_HEALTH_CONSENT_SQL = """
SELECT consent_id, value FROM crm.consent WHERE traveler_id = $1 AND purpose = 'health_data'
 ORDER BY at DESC, consent_id LIMIT 1
"""
INSERT_HEALTH_SQL = """
INSERT INTO crm.traveler_health (health_id, traveler_id, health_flags_enc, consent_id) VALUES ($1, $2, $3, $4)
"""
MIRROR_HEALTH_SQL = "UPDATE crm.traveler_profile SET dietary_flags = $2, mobility_flags = $3 WHERE traveler_token = $1"


async def candidates(ctx: TenantContext, vault: Vault) -> dict[str, Any]:
    rows = await ctx.conn.fetch(CANDIDATES_SQL, PAGE_SIZE)
    total = int(await ctx.conn.fetchval(COUNT_SQL) or 0)
    pairs = []
    shown: list[str] = []
    for r in rows:
        sides = []
        for side in ("a", "b"):
            traveler_id = r[f"{side}_id"]
            sides.append(
                {
                    "traveler_id": str(traveler_id),
                    "display_name": await vault.decrypt(
                        "traveler", "display_name_enc", traveler_id, r[f"{side}_name"], tenant_id=ctx.tenant_id
                    ),
                    "lead_source": r[f"{side}_source"],
                    "trips": r[f"{side}_trips"],
                    "created_at": r[f"{side}_created"].isoformat(),
                }
            )
            shown.append(str(traveler_id))
        matched = [m for m in ("name", "email", "phone") if r[f"same_{m}"]]
        pairs.append({"clients": sides, "matched_on": matched})
    if pairs:
        ctx.audit(
            "list_view",
            "allowed",
            "traveler",
            None,
            fields=("display_name",),
            reason="duplicate_review",
            object_ids=tuple(dict.fromkeys(shown))[:MAX_OBJECT_IDS],
        )
    return {"pairs": pairs, "total": total, "has_more": total > len(pairs)}


async def candidate_count(ctx: TenantContext) -> int:
    return int(await ctx.conn.fetchval(COUNT_SQL) or 0)


async def merge(ctx: TenantContext, vault: Vault, keep_id: uuid.UUID, other_id: uuid.UUID) -> dict[str, Any]:
    if keep_id == other_id:
        raise Invalid("same_traveler", "pick two different clients")
    keep = await ctx.conn.fetchrow(ROW_SQL, keep_id)
    other = await ctx.conn.fetchrow(ROW_SQL, other_id)
    if keep is None or other is None:
        raise NotFound("traveler_not_found")
    for traveler_id in (keep_id, other_id):
        if await ctx.conn.fetchval(HOLD_SQL, traveler_id):
            ctx.audit("merge", "denied", "traveler", str(traveler_id), reason="legal_hold")
            raise Conflict("legal_hold", "an active legal hold reaches one of these clients")
    blockers = list(await ctx.conn.fetchval(BLOCKERS_SQL, other_id) or [])
    if blockers:
        keep_blockers = list(await ctx.conn.fetchval(BLOCKERS_SQL, keep_id) or [])
        raise Conflict(
            "merge_blocked",
            "the client that would go has links, portal access or signatures that name them",
            blockers=blockers,
            swap_possible=not keep_blockers,
        )
    keep_token = await _token(ctx, vault, keep_id, keep["profile_token_enc"])
    other_token = await _token(ctx, vault, other_id, other["profile_token_enc"])

    # 2. trips
    kept_trips = {r["trip_id"]: r for r in await ctx.conn.fetch(PARTY_SQL, keep_id)}
    moved_trips = 0
    for r in await ctx.conn.fetch(PARTY_SQL, other_id):
        trip_id = r["trip_id"]
        if trip_id in kept_trips:
            await ctx.conn.execute(DROP_PARTY_SQL, r["trip_traveler_id"])
            await ctx.conn.execute(DROP_PARTY_F2_SQL, trip_id, other_token)
            if r["role"] == "lead":
                await ctx.conn.execute(LEAD_SQL, trip_id, keep_id)
                await ctx.conn.execute(LEAD_F2_SQL, trip_id, keep_token)
        else:
            await ctx.conn.execute(MOVE_PARTY_SQL, r["trip_traveler_id"], keep_id)
            await ctx.conn.execute(MOVE_PARTY_F2_SQL, trip_id, other_token, keep_token)
        moved_trips += 1

    # 3. rows that name the client
    for sql in MOVE_SQL:
        await ctx.conn.execute(sql, other_id, keep_id)

    # 4. relationships
    days = await trash_days(ctx)
    me = f"member:{ctx.member_id}"
    related = {r["to_traveler_id"] for r in await ctx.conn.fetch(KEPT_RELATED_SQL, keep_id)}
    remade = 0
    for r in await ctx.conn.fetch(RELATIONS_SQL, other_id):
        await ctx.conn.execute(TRASH_RELATION_SQL, r["relation_id"], me, days)
        target = r["to_traveler_id"]
        if target == keep_id or target in related:
            continue
        try:
            async with ctx.conn.transaction():
                await ctx.conn.execute(INSERT_RELATION_SQL, keep_id, target, r["kind"])
        except asyncpg.UniqueViolationError:
            continue  # the kept client already has a referrer: theirs wins
        if r["kind"] == "referrer":
            await ctx.conn.execute(SET_REFERRER_SQL, keep_id, target)
        elif r["kind"] == "referred":
            await ctx.conn.execute(SET_REFERRER_SQL, target, keep_id)
        related.add(target)
        remade += 1

    # 5. consents and health
    await ctx.conn.execute(COPY_CONSENT_SQL, other_id, keep_id)
    health_moved = await _health(ctx, vault, keep_id, other_id, keep_token)

    # 6. F1 fields the kept client lacks
    filled: list[str] = []
    sets: list[str] = []
    args: list[Any] = [keep_id]

    def put(column: str, value: Any) -> None:
        args.append(value)
        sets.append(f"{column} = ${len(args)}")

    for column in FILL_COLUMNS:
        if keep[column] is not None or other[column] is None:
            continue
        plain = await vault.decrypt("traveler", column, other_id, other[column])
        put(column, await vault.encrypt("traveler", column, keep_id, plain))
        if column in BLIND:
            put(BLIND[column], other[BLIND[column]])
        filled.append(column.removesuffix("_enc"))
    refs = _merged_refs(keep, other)
    if refs is not None:
        put("import_ref_bidx", refs[0])
        put("import_ref_alias_bidx", refs[1])
    if sets:
        # The other client keeps its values until the purge; the blind indexes stay unique among live clients
        # because it leaves the live set below.
        await ctx.conn.execute(
            f"UPDATE crm.traveler SET {', '.join(sets)} WHERE traveler_id = $1",  # noqa: S608 — fixed column names
            *args,
        )
    if moved_trips or "dob" in filled:
        await age_bands.recompute_for_traveler(ctx, vault, keep_id, await _dob(ctx, vault, keep_id))

    # 7. the other client to the trash (owner only, hold re-checked, no live trip left)
    trashed = await trash.move_to_trash(ctx, "traveler", other_id)
    ctx.audit(
        "merge",
        "allowed",
        "traveler",
        str(keep_id),
        fields=tuple(filled),
        reason="duplicate_merged",
        object_ids=(str(other_id),),
    )
    return {
        "traveler_id": str(keep_id),
        "merged_traveler_id": str(other_id),
        "trips_moved": moved_trips,
        "relations_remade": remade,
        "health_moved": health_moved,
        "fields_filled": filled,
        "purge_after": trashed["purge_after"],
    }


def _merged_refs(keep: Any, other: Any) -> tuple[bytes | None, list[bytes]] | None:
    """The kept client's own import ref and aliases after the merge: it answers to every ref either client answered
    to (crm-030 ``import_ref_alias_bidx``), so re-running an import finds it for the other client's rows too (card E).
    ``None`` when nothing changes."""
    own = keep["import_ref_bidx"]
    aliases = [bytes(a) for a in keep["import_ref_alias_bidx"] or ()]
    taken = {bytes(own)} if own is not None else set()
    taken.update(aliases)
    incoming = [other["import_ref_bidx"], *(other["import_ref_alias_bidx"] or ())]
    changed = False
    for ref in (bytes(r) for r in incoming if r is not None):
        if ref in taken:
            continue
        if own is None:
            own = ref
        else:
            aliases.append(ref)
        taken.add(ref)
        changed = True
    return (own, aliases) if changed else None


async def _token(ctx: TenantContext, vault: Vault, traveler_id: uuid.UUID, blob: bytes | None) -> uuid.UUID:
    if blob is not None:
        return uuid.UUID(await vault.decrypt("traveler", "profile_token_enc", traveler_id, blob))
    token = uuid.uuid4()
    await ctx.conn.execute(EMPTY_PROFILE_SQL, token)
    await ctx.conn.execute(
        POINTER_SQL, traveler_id, await vault.encrypt("traveler", "profile_token_enc", traveler_id, str(token))
    )
    return token


async def _dob(ctx: TenantContext, vault: Vault, traveler_id: uuid.UUID) -> dt.date | None:
    blob = await ctx.conn.fetchval(DOB_SQL, traveler_id)
    if blob is None:
        return None
    return dt.date.fromisoformat(await vault.decrypt("traveler", "dob_enc", traveler_id, blob))


async def _health(
    ctx: TenantContext, vault: Vault, keep_id: uuid.UUID, other_id: uuid.UUID, keep_token: uuid.UUID
) -> bool:
    """The other client's health codes, sealed again for the kept client, when the kept one has none and holds a
    health_data consent (just copied with the others)."""
    if await ctx.conn.fetchrow(HEALTH_SQL, keep_id) is not None:
        return False
    row = await ctx.conn.fetchrow(HEALTH_SQL, other_id)
    if row is None:
        return False
    consent = await ctx.conn.fetchrow(LATEST_HEALTH_CONSENT_SQL, keep_id)
    if consent is None or not consent["value"]:
        return False
    sealed = await vault.decrypt("traveler_health", "health_flags_enc", row["health_id"], row["health_flags_enc"])
    codes = json.loads(sealed)
    health_id = uuid.uuid4()
    blob = await vault.encrypt("traveler_health", "health_flags_enc", health_id, json.dumps(codes, sort_keys=True))
    await ctx.conn.execute(INSERT_HEALTH_SQL, health_id, keep_id, blob, consent["consent_id"])
    await ctx.conn.execute(MIRROR_HEALTH_SQL, keep_token, codes.get("dietary", []), codes.get("mobility", []))
    return True
