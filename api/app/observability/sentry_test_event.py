"""Send one test event to the CRM API's Sentry project (AA-1119, S282 audit: prove the API reaches Sentry).

Run it once inside a running ``aa-crm-dev-api`` task (container ``api``; ECS Exec, SSM only), after a deploy:

    python -m app.observability.sentry_test_event

It reads the same settings as the API (``SENTRY_DSN`` from the task definition, ``ENVIRONMENT``, ``GIT_SHA``), starts
Sentry with the API's own options and scrubbers (:func:`app.observability.sentry.init_sentry`, tag ``service`` =
``sentry-test``), sends one ``warning`` message tagged ``test_event=aa-1119``, waits for it to leave the process and
prints one line: the event id, the environment and the release. No request, no customer data, no database.

Exit 0 = sent (find it in the Sentry project by the printed id or ``test_event:aa-1119``); 1 = Sentry is off (no
``SENTRY_DSN``) or the event did not leave the process in time; 2 = the settings do not load. The DSN is never printed.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping

import sentry_sdk

from app.observability.sentry import init_sentry
from app.settings import Settings, SettingsError, load_settings

MESSAGE = "CRM API Sentry test event"
TAG = ("test_event", "aa-1119")
# technical-constant: how long the one-off command waits for the SDK's background sender, not a policy value
FLUSH_TIMEOUT_S = 10.0


def send_test_event(
    settings: Settings,
    *,
    init: Callable[[Settings, str], bool] = init_sentry,
    out: Callable[[str], None] = print,
) -> int:
    """Send the test event with ``settings``; returns the process exit code."""
    if not init(settings, "sentry-test"):
        out("sentry off: SENTRY_DSN is not set in this task (AA-CIS-Infra crm_sentry_enabled maps it)")
        return 1
    try:
        with sentry_sdk.new_scope() as scope:
            scope.set_tag(*TAG)
            event_id = sentry_sdk.capture_message(MESSAGE, level="warning")
        client = sentry_sdk.get_client()
        flushed = client.is_active() and event_id is not None
        if flushed:
            client.flush(timeout=FLUSH_TIMEOUT_S)
    finally:
        sentry_sdk.get_client().close(timeout=FLUSH_TIMEOUT_S)
    if not flushed:
        out("sentry test event NOT sent (the SDK dropped it)")
        return 1
    out(f"sentry test event sent: id={event_id} environment={settings.environment} release={settings.git_sha or '-'}")
    return 0


def main(env: Mapping[str, str] | None = None) -> int:
    try:
        settings = load_settings(env)
    except SettingsError as exc:
        print(f"settings do not load: {exc}")
        return 2
    return send_test_event(settings)


if __name__ == "__main__":  # pragma: no cover - the module entry point
    sys.exit(main())
