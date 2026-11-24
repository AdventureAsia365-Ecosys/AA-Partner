"""AA-1119: the one-off Sentry test event of the CRM API (``python -m app.observability.sentry_test_event``)."""

from __future__ import annotations

import json
from unittest.mock import patch

import sentry_sdk
import sentry_sdk.transport

from app.observability import sentry_test_event as ste
from app.observability.sentry import init_sentry
from app.settings import Settings

DSN = "https://k@o1.ingest.example/1"


class _Capture(sentry_sdk.transport.Transport):
    def __init__(self, options=None) -> None:
        super().__init__(options)
        self.events: list[dict] = []

    def capture_envelope(self, envelope) -> None:
        for item in envelope.items:
            if item.type == "event":
                self.events.append(item.payload.json)


def _settings(dsn: str | None) -> Settings:
    return Settings(database_url=None, environment="dev", git_sha="f00dcafe", sentry_dsn=dsn)


def test_sends_one_tagged_warning_with_the_api_options() -> None:
    transport = _Capture()
    lines: list[str] = []
    real_init = sentry_sdk.init
    with patch("sentry_sdk.init", lambda **kw: real_init(**kw, transport=transport)):
        code = ste.send_test_event(_settings(DSN), init=init_sentry, out=lines.append)
    real_init()  # back to a disabled client for the rest of the run
    assert code == 0
    assert len(transport.events) == 1
    ev = transport.events[0]
    assert ev["message"] == ste.MESSAGE or ev.get("logentry", {}).get("message") == ste.MESSAGE
    assert ev["level"] == "warning"
    assert ev["tags"]["test_event"] == "aa-1119" and ev["tags"]["service"] == "sentry-test"
    assert ev["release"] == "f00dcafe" and ev["environment"] == "dev"
    assert lines == [f"sentry test event sent: id={ev['event_id']} environment=dev release=f00dcafe"]
    assert DSN not in json.dumps(lines) and "k@o1" not in lines[0]


def test_without_dsn_it_says_off_and_exits_1() -> None:
    lines: list[str] = []
    with patch("sentry_sdk.init") as init:
        code = ste.send_test_event(_settings(None), out=lines.append)
    init.assert_not_called()
    assert code == 1 and lines and lines[0].startswith("sentry off")


def test_an_event_the_sdk_drops_exits_1() -> None:
    lines: list[str] = []
    with patch("sentry_sdk.capture_message", return_value=None):
        code = ste.send_test_event(_settings(DSN), init=lambda _s, _svc: True, out=lines.append)
    assert code == 1 and "NOT sent" in lines[0]


def test_main_reports_bad_settings_without_echoing_them(capsys) -> None:
    assert ste.main({"SENTRY_DSN": "http://not-https.example/1"}) == 2
    out = capsys.readouterr().out
    assert out.startswith("settings do not load") and "not-https" not in out


def test_main_uses_the_environment(capsys) -> None:
    with patch.object(ste, "send_test_event", return_value=0) as send:
        assert ste.main({"ENVIRONMENT": "dev", "GIT_SHA": "abc1234"}) == 0
    assert send.call_args.args[0].environment == "dev"
