"""Bounded semantic handoff contract for analysis-producing tasks."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping


MARKER = "AGENT_RUN_ANALYSIS_RESULT:"
CONSUMED_MARKER = "AGENT_RUN_CONSUMED_ARTIFACTS:"
MAX_RESULT_BYTES = 64 * 1024
MAX_SUMMARY_CHARS = 4000
MAX_ITEMS = 40
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SEVERITIES = {"info", "low", "medium", "high", "critical"}
TOP_KEYS = {"version", "summary", "findings", "decisions", "open_questions", "verification"}
FINDING_KEYS = {"id", "severity", "title", "evidence_refs", "recommendation", "confidence"}


class AnalysisResultError(ValueError):
    """A provider supplied an invalid or unsafe semantic handoff."""


def _bounded_text(value: Any, where: str, limit: int = 1000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise AnalysisResultError(f"{where} must be non-empty bounded text")
    if "\x00" in value:
        raise AnalysisResultError(f"{where} contains NUL")
    return value.strip()


def _text_list(value: Any, where: str, *, limit: int = MAX_ITEMS) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise AnalysisResultError(f"{where} must be a bounded list")
    return [_bounded_text(item, f"{where}[]") for item in value]


def validate_analysis_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - TOP_KEYS:
        raise AnalysisResultError("analysis result has unknown top-level fields")
    if value.get("version") != 1:
        raise AnalysisResultError("analysis result version must be 1")
    result: dict[str, Any] = {
        "version": 1,
        "summary": _bounded_text(value.get("summary"), "summary", MAX_SUMMARY_CHARS),
    }
    findings_raw = value.get("findings", [])
    if not isinstance(findings_raw, list) or len(findings_raw) > MAX_ITEMS:
        raise AnalysisResultError("findings must be a bounded list")
    findings: list[dict[str, Any]] = []
    for index, raw in enumerate(findings_raw):
        if not isinstance(raw, Mapping) or set(raw) - FINDING_KEYS:
            raise AnalysisResultError(f"findings[{index}] has unknown fields")
        severity = raw.get("severity")
        if severity not in SEVERITIES:
            raise AnalysisResultError(f"findings[{index}].severity is invalid")
        confidence = raw.get("confidence")
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1
        ):
            raise AnalysisResultError(f"findings[{index}].confidence is invalid")
        finding: dict[str, Any] = {
            "id": _bounded_text(raw.get("id"), f"findings[{index}].id", 120),
            "severity": severity,
            "title": _bounded_text(raw.get("title"), f"findings[{index}].title", 500),
            "evidence_refs": _text_list(raw.get("evidence_refs", []), f"findings[{index}].evidence_refs"),
            "recommendation": _bounded_text(raw.get("recommendation"), f"findings[{index}].recommendation", 1500),
        }
        if confidence is not None:
            finding["confidence"] = float(confidence)
        findings.append(finding)
    result["findings"] = findings
    for key in ("decisions", "open_questions", "verification"):
        result[key] = _text_list(value.get(key, []), key)
    encoded = (json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    if len(encoded) > MAX_RESULT_BYTES:
        raise AnalysisResultError("analysis result exceeds size limit")
    return result


def extract_analysis_result(answer: str) -> dict[str, Any]:
    lines = [line for line in answer.splitlines() if line.startswith(MARKER)]
    if len(lines) != 1:
        raise AnalysisResultError("exactly one analysis result marker is required")
    try:
        value = json.loads(lines[0][len(MARKER):].strip())
    except (TypeError, ValueError) as exc:
        raise AnalysisResultError("analysis result marker is not valid JSON") from exc
    return validate_analysis_result(value)


def analysis_result_bytes(value: Mapping[str, Any]) -> bytes:
    validated = validate_analysis_result(value)
    return (json.dumps(validated, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def consumed_hashes(answer: str) -> set[str]:
    lines = [line for line in answer.splitlines() if line.startswith(CONSUMED_MARKER)]
    if len(lines) != 1:
        raise AnalysisResultError("exactly one consumed-artifacts marker is required")
    try:
        value = json.loads(lines[0][len(CONSUMED_MARKER):].strip())
    except (TypeError, ValueError) as exc:
        raise AnalysisResultError("consumed-artifacts marker is not valid JSON") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) or not SHA256_RE.fullmatch(item) for item in value):
        raise AnalysisResultError("consumed-artifacts must be a SHA-256 list")
    if len(value) != len(set(value)):
        raise AnalysisResultError("consumed-artifacts contains duplicates")
    return set(value)


__all__ = [
    "AnalysisResultError",
    "CONSUMED_MARKER",
    "MARKER",
    "MAX_RESULT_BYTES",
    "analysis_result_bytes",
    "consumed_hashes",
    "extract_analysis_result",
    "validate_analysis_result",
]
