"""AA-1008: the signed-in visual run only drives the e2e studio sign-in on the agency host.

`.github/workflows/visual.yml` job `deployed` takes `inputs.base_url` from a dispatch and then signs in with the e2e
studio user's password and a live TOTP code. Its first step must refuse any other host before a secret is in reach
(like the CIS and Booking visual runs). The step's script is run here with bash for allowed and refused hosts.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
WF = ROOT / ".github" / "workflows" / "visual.yml"
STEP = "Only the agency hosts"
BASH = shutil.which("bash")


def _deployed_steps() -> str:
    text = WF.read_text()
    at = text.index("\n  deployed:\n")
    return text[at:]


def _guard_script() -> str:
    steps = _deployed_steps()
    at = steps.index(f"- name: {STEP}")
    m = re.compile(r"^        run: \|\n((?:          .*\n|\n)+)", re.M).search(steps, at)
    assert m, "the guard step has a run block"
    return "\n".join(line[10:] for line in m.group(1).splitlines())


def test_the_guard_is_the_first_step_and_comes_before_any_secret() -> None:
    steps = _deployed_steps()
    first = re.search(r"^    steps:\n(?:      #.*\n)*      - name: (.+)$", steps, re.M)
    assert first and first.group(1) == STEP
    assert steps.index(f"- name: {STEP}") < steps.index("secrets.")
    # The workspace exists before the checkout; the job's default working directory (e2e/) does not.
    block = steps[steps.index(f"- name: {STEP}") : steps.index("- name: Checkout main")]
    assert "working-directory: ." in block


def _run(base_url: str, crm_host: str = "") -> subprocess.CompletedProcess[str]:
    assert BASH, "bash is on PATH"
    return subprocess.run(  # noqa: S603
        [BASH, "-c", _guard_script()],
        env={"BASE_URL": base_url, "CRM_HOST": crm_host, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("base_url", "crm_host"),
    [
        ("https://portal.lumiguides.it.com", ""),
        ("https://portal.lumiguides.it.com/", ""),
        ("https://agency.example.test", "https://agency.example.test"),
        ("https://agency.example.test", "https://agency.example.test/"),
    ],
)
def test_the_agency_host_passes(base_url: str, crm_host: str) -> None:
    out = _run(base_url, crm_host)
    assert out.returncode == 0, out.stdout + out.stderr


@pytest.mark.parametrize(
    ("base_url", "crm_host"),
    [
        ("https://attacker.example.test", ""),
        ("https://attacker.example.test", "https://agency.example.test"),
        ("http://portal.lumiguides.it.com", ""),
        ("https://portal.lumiguides.it.com.attacker.example.test", ""),
        ("https://portal.lumiguides.it.com/login", ""),
        ("", ""),
    ],
)
def test_any_other_host_is_refused(base_url: str, crm_host: str) -> None:
    out = _run(base_url, crm_host)
    assert out.returncode == 1
    assert "::error::base_url must be the agency host" in out.stdout
