"""The catalog-unavailable message must name a remedy the reader can act on."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location(
        "agent_provider_run", ROOT / "scripts" / "agent_provider_run.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_remedy_points_at_sign_in_for_every_known_provider():
    module = _module()
    for provider in ("cursor", "claude", "codex", "grok"):
        remedy = module._catalog_remedy(provider, "catalog-unavailable")
        # "catalog-unavailable" alone told a new operator nothing; the whole
        # point is that it now says which command answers the question.
        assert "signed in" in remedy
        assert "`" in remedy, f"{provider} remedy names no command: {remedy}"
        # Being signed in is the likeliest cause, not the only one -- the same
        # status covers timeouts, network faults and CLI mismatches. Claiming
        # certainty would misdirect the operator who IS signed in, which is the
        # very failure mode this change exists to remove.
        assert "network" in remedy and "version" in remedy


def test_remedy_degrades_gracefully_for_an_unknown_provider():
    module = _module()
    remedy = module._catalog_remedy("something-new", "catalog-unavailable")
    assert "signed in" in remedy
    # No invented command for a provider we have never probed.
    assert "`" not in remedy
    assert "network" in remedy


def test_remedy_stays_silent_when_the_catalog_is_fine():
    module = _module()
    for status in ("catalog-listed", "static-config", "catalog-error"):
        assert module._catalog_remedy("cursor", status) == ""


@pytest.mark.parametrize(
    "provider,argv",
    [
        ("cursor", ["cursor-agent", "status"]),
        ("claude", ["claude", "auth", "status"]),
        ("codex", ["codex", "login", "status"]),
        ("grok", ["grok", "login"]),
    ],
)
def test_quoted_commands_exist_on_this_machine(provider, argv):
    """A remedy nobody verified sends the reader after a command that isn't there.

    grok is the reason this test exists: it has `login` but no way to ask
    whether you already are, and the first draft quoted an invented
    `grok status` that fails with an unrelated OS error.
    """

    if shutil.which(argv[0]) is None:
        pytest.skip(f"{argv[0]} is not installed on this machine")
    module = _module()
    assert " ".join(argv) in module._catalog_remedy(provider, "catalog-unavailable")
    help_text = subprocess.run(
        [argv[0], "--help"], capture_output=True, text=True, timeout=30, check=False
    ).stdout
    assert argv[1] in help_text, f"{argv[0]} --help does not list {argv[1]}"
