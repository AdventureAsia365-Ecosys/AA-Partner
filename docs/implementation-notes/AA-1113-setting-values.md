# AA-1113 follow-up — CRM json setting values served whole by crm.ops.v1

Lane l17 of S284 (Claude Code). Bug found after the CRM connection cap shipped: the setting
`crm.ai.connection_monthly_cap_usd`, stored as an object keyed by connection (approved v1, enforced by the CRM
gateway), was served as `{}` by `GET /crm/ops/v1/settings` and `GET /settings/{key}/history`. Production log:
`crm_ops_key_dropped path=/crm/ops/v1/settings keys=<connection key>`. CIS Platform > Settings (CRM tab) and the CIS AI
gateway connection split both read `{}`.

## Decisions

- **Root cause.** The L1 response allow-list (`app/ops/serializer.py`) walks every key at any depth and requires each
  to be on `OPS_ALLOWED_KEYS`. A json setting's own keys are data chosen by AA staff, not contract field names, so they
  can never be on the list; outside test / local they were dropped. The existing settings tests only used int values,
  so the 500 that test / local would give never fired.
- **Exemption by named field on one route class.** `OpsAllowListRoute.opaque_value_fields` (empty) and a subclass
  `OpsSettingsRoute` with `SETTING_VALUE_FIELDS = {value, previous_value, default_value, allowed_values}`; only
  `app/routers/ops_settings.py` uses it. Those names cover every place a setting value sits in a settings answer:
  the setting itself, `last_change`, `pending[]`, `scheduled[]`, `agency_overrides[]`, `agency_pending[]`,
  `history[]`, `definition`, and the POST change / decision answer. The field name itself is still checked; inside
  its value every key (any depth) is checked against `OPS_FORBIDDEN` only. A forbidden inner key keeps today's
  behaviour (500 `ops_key_not_allowed` on test / local, dropped + `crm_ops_key_dropped` elsewhere).
- **`allowed_values` included.** It carries setting values too (the enum / list domain). Today they are strings, so
  this changes nothing now; it keeps a future json enum from hitting the same bug.
- **`class_router(OPS, ops_route=...)`** instead of passing FastAPI's `route_class` (the first positional parameter
  of `class_router` already has that name). It refuses any class that is not an `OpsAllowListRoute` subclass, and
  refuses `ops_route` on a non-OPS router, so the exemption cannot become a way around the allow-list.

## Changed

- `api/app/ops/serializer.py`: `SETTING_VALUE_FIELDS`, `key_forbidden`, `bad_keys` / `drop_keys` take the opaque
  field names, `OpsAllowListRoute.opaque_value_fields`, `OpsSettingsRoute`; module docstring documents the rule.
- `api/app/auth/route_guard.py`: `class_router(..., ops_route=...)`.
- `api/app/routers/ops_settings.py`: the router uses `OpsSettingsRoute`.
- Tests: `tests/unit/test_ops_serializer.py` (walk, drop, strict 500, dev drop with names-only log, class_router
  refusals); `tests/unit/test_ops_contract.py` (the scenario loop uses each route's own opaque fields; only the
  `/settings` routes are `OpsSettingsRoute`; a json value round-trips through the real `/settings` and `/history`
  routes, incl. `previous_value`, `last_change`, `pending`; a forbidden inner key still fails with 500).
- `CONTEXT.md` (L1 console, response allow-list) and the root contract `docs/contracts/crm.ops.v1.openapi.yaml`
  (`SettingValue` description, root PR).
- No migration, no LLM call, no CIS change: the CIS relay (`api/services/crm_settings_relay.py`) and the connection
  split read pass the CRM body through as it comes.

## Tradeoffs

- A path-based exemption (only `settings[].value`, `settings[].last_change.value`, ...) would be narrower but brittle:
  every new place a setting row appears would need a new path, and a missed one is this same bug again. Field names on
  one route class are narrow enough: no other ops route can use them.
- Inner keys are free-form data. They are still refused when they look like a trip, money or personal-data name, so a
  setting cannot be used to carry such data out through L1.

## Should know

- Live: after deploy, `GET /crm/ops/v1/settings?area=ai` shows the stored object for
  `crm.ai.connection_monthly_cap_usd`, and the CRM log has no new `crm_ops_key_dropped` for `/settings`.
