"""Every task YAML must parse, carry a task_type, and have a fixture that
the input loader can read. This catches typos + schema drift before a
trial-day demo."""

from pathlib import Path

import pytest
import yaml

from andera.agent.classify import _VALID as VALID_TASK_TYPES
from andera.orchestrator.inputs import load_inputs
from andera.orchestrator.runner import _apply_task_overrides
from andera.config import load_profile

TASKS_DIR = Path(__file__).resolve().parents[3] / "config" / "tasks"
FIXTURES_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures"

# Task id <-> fixture file mapping. Phase 2 dev task (03-github-issue) is
# excluded from the library tests; it's a single-page probe, not one of
# the 5 spec tasks.
LIBRARY = [
    ("01-github-workday-join",  "github-workday-join",    "list_iter"),
    ("02-linear-tickets",       "linear-tickets",         "extract"),
    ("03-github-commits-audit", "github-commits-audit",   "navigate"),
    ("04-linkedin-enrichment",  "linkedin-enrichment",    "extract"),
    ("05-workday-form-download","workday-form-download",  "form_fill"),
]


@pytest.mark.parametrize("filename, task_id, task_type", LIBRARY)
def test_task_yaml_shape(filename, task_id, task_type):
    p = TASKS_DIR / f"{filename}.yaml"
    assert p.exists(), f"missing task YAML: {p}"
    data = yaml.safe_load(p.read_text())
    assert data["task_id"] == task_id
    assert data["task_type"] == task_type
    assert data["task_type"] in VALID_TASK_TYPES
    assert data.get("prompt"), "task must have a prompt"
    assert data.get("extract_schema"), "task must have an extract_schema"
    schema = data["extract_schema"]
    assert schema.get("type") == "object"
    # every schema has at least one required field
    assert schema.get("required"), f"{filename} schema missing required list"


@pytest.mark.parametrize("filename, task_id, _task_type", LIBRARY)
def test_fixture_loads(filename, task_id, _task_type):
    fixture = FIXTURES_DIR / f"{filename}.csv"
    assert fixture.exists(), f"missing fixture: {fixture}"
    rows = load_inputs(fixture)
    assert len(rows) >= 2, f"{fixture} should have >= 2 rows for a demo"


def test_linkedin_overrides_tighten_profile():
    """LinkedIn YAML drops concurrency to 1 + turns stealth on + slows rps."""
    p = TASKS_DIR / "04-linkedin-enrichment.yaml"
    task = yaml.safe_load(p.read_text())
    assert task.get("profile_overrides"), "LinkedIn must tighten the profile"
    base = load_profile()
    # Force baseline values so the test checks the DIRECTION of the override.
    base.browser.concurrency = 8
    base.browser.stealth = False
    base.browser.per_host_rps = 10.0
    tightened = _apply_task_overrides(base, task)
    assert tightened.browser.concurrency == 1
    assert tightened.browser.stealth is True
    assert tightened.browser.per_host_rps == 0.5


def test_tasks_without_overrides_return_same_profile():
    p = TASKS_DIR / "02-linear-tickets.yaml"
    task = yaml.safe_load(p.read_text())
    base = load_profile()
    out = _apply_task_overrides(base, task)
    assert out.browser.concurrency == base.browser.concurrency
    assert out.browser.stealth == base.browser.stealth
