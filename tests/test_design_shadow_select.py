from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def load_selector():
    path = ROOT / "scripts" / "design_shadow_select.py"
    spec = importlib.util.spec_from_file_location("design_shadow_select", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def catalog():
    return yaml.safe_load((ROOT / "design-skill-catalog.yaml").read_text())


def cases():
    return yaml.safe_load((ROOT / "routing-evals" / "design-shadow-cases.yaml").read_text())["cases"]


def test_shadow_cases_match_contracts():
    selector = load_selector()
    loaded = cases()
    assert len(loaded) == 13
    for case in loaded:
        result = selector.select(case["task"], catalog())
        expected = case["expect"]
        records = result["records"]
        if "record_count" in expected:
            assert len(records) == expected["record_count"]
            assert [record["visual_author"] for record in records] == expected["visual_authors"]
            continue
        record = records[0]
        assert record["status"] == expected["status"], case["id"]
        for key, value in expected.items():
            if key == "status":
                continue
            if key == "usage_claim_permitted":
                assert record["usage_claim"]["permitted"] is value
            elif key == "baseline_active_facets":
                assert record["baselines"][0]["active_facets"] == value
            elif key == "baseline_suppressed_facets":
                assert record["baselines"][0]["suppressed_facets"] == value
            elif key == "overlays":
                assert [overlay["skill"] for overlay in record["overlays"]] == value
            elif key == "overlay_precedence_note":
                assert record["overlays"][0]["precedence_note"] == value
            else:
                assert record[key] == value, case["id"]


def test_apple_cjk_facets_are_scoped_and_cjk_wins():
    selector = load_selector()
    task = next(case["task"] for case in cases() if case["id"] == "apple-cjk-product-ui")
    record = selector.select(task, catalog())["records"][0]
    baseline = record["baselines"][0]
    overlay = record["overlays"][0]
    assert record["visual_author"] == "frontend-design"
    assert baseline["active_facets"] == ["cjk-typography", "cjk-spacing"]
    assert baseline["suppressed_facets"] == ["erp-structure"]
    assert "forbids negative letter-spacing" in baseline["precedence_note"]
    assert "outranks typography-micro" in overlay["precedence_note"]


def test_apple_latin_has_no_false_cjk_precedence_claim():
    selector = load_selector()
    task = next(case["task"] for case in cases() if case["id"] == "apple-latin-mobile")
    record = selector.select(task, catalog())["records"][0]
    assert record["baselines"] == []
    assert record["overlays"][0]["precedence_note"] == "No CJK baseline; typography-micro applies unconstrained."


def test_invalid_contracts_fail_or_stay_invalid():
    selector = load_selector()
    try:
        selector.select({"id": "none", "deliverables": []}, catalog())
    except ValueError as error:
        assert "non-empty" in str(error)
    else:
        raise AssertionError("empty deliverables must fail")

    result = selector.select({"id": "bad", "deliverables": [{"id": "x"}]}, catalog())
    assert result["records"][0]["status"] == "invalid"


def test_cli_writes_yaml_record(tmp_path):
    source = tmp_path / "task.yaml"
    output = tmp_path / "record.yaml"
    source.write_text(yaml.safe_dump({"task": cases()[0]["task"]}, allow_unicode=True))
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "design_shadow_select.py"), "--input", str(source), "--output", str(output)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout == ""
    record = yaml.safe_load(output.read_text())
    assert record["mode"] == "manual-shadow"
    assert record["records"][0]["visual_author"] == "frontend-design"


def test_checked_in_apple_cjk_record_is_generated_by_selector():
    selector = load_selector()
    source = yaml.safe_load(
        (ROOT / "examples" / "design-domain-shadow" / "apple-cjk-task.yaml").read_text()
    )
    checked_in = yaml.safe_load(
        (ROOT / "examples" / "design-domain-shadow" / "apple-cjk-selection-record.yaml").read_text()
    )
    assert checked_in == selector.select(source["task"], catalog())
