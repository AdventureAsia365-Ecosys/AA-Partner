"""AA-1126 (crm-076): the CRM AI spend rules setting is registered with what the CIS admin page renders (crm.ops.v1
/settings: label, dev | prod, default dev, platform scope, maker-checker, the EN + VI guide), the two budget keys say
they are the fallback of the plan's AI budget, and the reader answers dev unless the setting says prod. No DB."""

from __future__ import annotations

import pytest

from app import platform_settings as ps
from app.llm import spend_environment as se
from app.platform_settings import service


def test_the_key_is_registered_and_rendered_on_the_admin_page():
    out = service.definition_out(ps.READER.definition(ps.SPEND_ENVIRONMENT))
    assert out["key"] == "crm.spend.environment" and out["area"] == "spend" and out["label"]
    assert (out["value_type"], out["allowed_values"], out["default_value"]) == ("enum", ["dev", "prod"], "dev")
    assert (out["scope_kind"], out["approval"], out["enforced"], out["env_override"]) == (
        "platform",
        "maker_checker",
        True,
        None,
    )
    for lang in ("en", "vi"):
        assert all(out["guide"][lang].get(part) for part in ("what", "raise", "lower", "when", "example"))


@pytest.mark.parametrize("key", [ps.AI_BUDGET_DEFAULT, ps.AI_BUDGET_MAX])
def test_the_budget_keys_are_described_as_the_plan_fallback(key):
    d = ps.READER.definition(key)
    assert "crm.ai_budget_usd_monthly" in d.description
    assert "plan" in ps.guide(key)["en"]["what"] and "gói" in ps.guide(key)["vi"]["what"]


@pytest.mark.parametrize(
    "value, expected", [("prod", "prod"), ("PROD", "prod"), ("dev", "dev"), (None, "dev"), ("staging", "dev")]
)
def test_the_reader_answers_dev_unless_prod(monkeypatch, value, expected):
    monkeypatch.setattr(ps, "get", lambda key, tenant_id=None: value)
    assert se.spend_environment() == expected
    assert se.is_prod() is (expected == "prod")


def test_the_reader_answers_dev_when_the_setting_cannot_be_read(monkeypatch):
    def boom(key, tenant_id=None):
        raise RuntimeError("zz no settings")

    monkeypatch.setattr(ps, "get", boom)
    assert se.spend_environment() == "dev"
