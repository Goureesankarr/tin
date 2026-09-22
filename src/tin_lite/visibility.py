from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from tin_lite.domain import WorkflowRun

MAX_VISIBILITY_SOURCE_BYTES = 200_000
MAX_VISIBILITY_REPORT_BYTES = 150_000
MAX_VISIBILITY_EVIDENCE_BYTES = 2_000_000
MAX_SOURCES_PER_ANSWER = 20
MAX_VISIBILITY_RESPONSE_BYTES = 250_000
DOMAIN_PATTERN = r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}"
ResponseRequest = Callable[[], Awaitable[dict[str, Any]]]
ResponseCheckpoint = Callable[[ResponseRequest], Awaitable[dict[str, Any]]]
QUESTION_FAMILIES = (
    "best_tool",
    "alternatives",
    "problem",
    "provider",
    "stack",
)


class ResponsesClient(Protocol):
    model: str

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class VisibilityProtocolError(RuntimeError):
    """The visibility model returned data outside the audit contract."""


class VisibilityRecoveryError(RuntimeError):
    """A previous request has no durable response and cannot be purchased again."""


@dataclass(frozen=True)
class VisibilitySource:
    label: str
    artifact_ref: str
    content: str


class VisibilityAuditor:
    def __init__(self, *, responses: ResponsesClient, skill_suite: str) -> None:
        self._responses = responses
        self._skill_suite = skill_suite

    @property
    def model(self) -> str:
        return self._responses.model

    async def prepare_panel(
        self,
        *,
        project_name: str,
        target_request: str,
        sources: list[VisibilitySource],
        checkpoint: ResponseCheckpoint | None = None,
    ) -> dict[str, Any]:
        target_request = target_request.strip()
        if not 1 <= len(target_request) <= 200:
            raise ValueError("visibility target request must contain 1 to 200 characters")
        source_bytes = sum(len(source.content.encode()) for source in sources)
        if source_bytes > MAX_VISIBILITY_SOURCE_BYTES:
            raise ValueError(f"visibility sources exceed {MAX_VISIBILITY_SOURCE_BYTES} bytes")
        reference = {
            "project_name": project_name,
            "target_request": target_request,
            "sources": [
                {
                    "label": source.label,
                    "artifact_ref": source.artifact_ref,
                    "content": source.content,
                }
                for source in sources
            ],
        }
        response = await self._respond(
            {
                "instructions": self._stage_instructions("BUILD_PANEL"),
                "input": _json_input(reference),
                "store": False,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "visibility_panel",
                        "strict": True,
                        "schema": _panel_schema(
                            candidate_bank="CANDIDATE_BANK_V1" in self._skill_suite
                        ),
                    },
                    "verbosity": "low",
                },
            },
            checkpoint=checkpoint,
        )
        panel = _structured_output(response)
        panel["response_id"] = _response_id(response)
        panel["model"] = self.model
        if isinstance(panel.get("target"), dict):
            panel["target"]["domain"] = _canonical_domain(panel["target"].get("domain"))
        _validate_panel(panel, target_request=target_request)
        if "CANDIDATE_BANK_V1" in self._skill_suite:
            _validate_candidate_bank(panel)
        panel["panel_hash"] = _panel_hash(panel)
        return panel

    async def answer(
        self, *, question: str, searched: bool, checkpoint: ResponseCheckpoint | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "instructions": self._stage_instructions(
                "ANSWER_WITH_SEARCH" if searched else "ANSWER_WITHOUT_SEARCH"
            ),
            "input": question,
            "store": False,
            "text": {"verbosity": "low"},
        }
        if searched:
            payload.update(
                {
                    "tools": [{"type": "web_search"}],
                    "tool_choice": {"type": "web_search"},
                    "include": ["web_search_call.action.sources"],
                    "max_tool_calls": 3,
                }
            )
        response = await self._respond(payload, checkpoint=checkpoint)
        return _normalize_answer(response, searched=searched)

    async def adjudicate(
        self,
        *,
        panel: dict[str, Any],
        measurements: list[dict[str, Any]],
        checkpoint: ResponseCheckpoint | None = None,
    ) -> dict[str, Any]:
        reference = {
            "target": panel["target"],
            "questions": panel["questions"],
            "measurements": measurements,
        }
        response = await self._respond(
            {
                "instructions": self._stage_instructions("ADJUDICATE"),
                "input": _json_input(reference),
                "store": False,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "visibility_adjudication",
                        "strict": True,
                        "schema": _adjudication_schema(),
                    },
                    "verbosity": "low",
                },
            },
            checkpoint=checkpoint,
        )
        result = _structured_output(response)
        result["response_id"] = _response_id(response)
        _normalize_adjudication(result, panel=panel, measurements=measurements)
        return result

    async def _respond(
        self, payload: dict[str, Any], *, checkpoint: ResponseCheckpoint | None
    ) -> dict[str, Any]:
        async def request() -> dict[str, Any]:
            return await self._responses.create(payload)

        return await checkpoint(request) if checkpoint is not None else await request()

    def build_artifacts(
        self,
        *,
        run_id: str,
        project_name: str,
        target_request: str,
        source_refs: list[str],
        panel: dict[str, Any],
        measurements: list[dict[str, Any]],
        adjudication: dict[str, Any],
        evidence_path: str,
    ) -> tuple[bytes, bytes]:
        evidence = {
            "schema_version": "1.1",
            "workflow": "visibility.audit",
            "run_id": run_id,
            "project_name": project_name,
            "target_request": target_request,
            "model": self.model,
            "source_refs": source_refs,
            "panel": panel,
            "measurements": measurements,
            "adjudication": adjudication,
        }
        evidence_bytes = (
            json.dumps(evidence, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode()
        if len(evidence_bytes) > MAX_VISIBILITY_EVIDENCE_BYTES:
            raise ValueError(f"visibility evidence exceeds {MAX_VISIBILITY_EVIDENCE_BYTES} bytes")
        report = _render_report(
            project_name=project_name,
            target_request=target_request,
            source_refs=source_refs,
            panel=panel,
            measurements=measurements,
            adjudication=adjudication,
            evidence_path=evidence_path,
        ).encode()
        validate_visibility_report(report, evidence_path=evidence_path)
        return report, evidence_bytes

    def _stage_instructions(self, stage: str) -> str:
        return f"{self._skill_suite}\n\nCURRENT STAGE: {stage}"


def validate_visibility_report(content: bytes, *, evidence_path: str) -> None:
    if not content or len(content) > MAX_VISIBILITY_REPORT_BYTES:
        raise ValueError(
            f"visibility report must contain between 1 and {MAX_VISIBILITY_REPORT_BYTES} bytes"
        )
    text = content.decode("utf-8")
    required = (
        "# AI visibility audit",
        "\n## Summary\n",
        "\n## Buyer questions\n",
        "\n## Primary bottleneck\n",
        "\n## Recommended next move\n",
        "\n## Evidence\n",
        "\n## Method\n",
    )
    if any(item not in text for item in required):
        raise ValueError("visibility report is missing a required Markdown section")
    if evidence_path not in text:
        raise ValueError("visibility report omitted its durable evidence path")


def validate_visibility_artifacts(
    report: bytes, evidence: bytes, *, run_id: str, evidence_path: str
) -> None:
    """Validate the bounded report/evidence pair before recording trusted publication facts."""
    validate_visibility_report(report, evidence_path=evidence_path)
    if not 1 <= len(evidence) <= MAX_VISIBILITY_EVIDENCE_BYTES:
        raise ValueError("visibility evidence is outside its byte limit")
    value = json.loads(evidence.decode("utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") not in {"1.0", "1.1"}
        or value.get("workflow") != "visibility.audit"
        or value.get("run_id") != run_id
        or not isinstance(value.get("panel"), dict)
        or not isinstance(value.get("measurements"), list)
        or len(value["measurements"]) != 5
        or not isinstance(value.get("adjudication"), dict)
        or not isinstance(value.get("source_refs"), list)
        or any(not isinstance(ref, str) for ref in value["source_refs"])
    ):
        raise ValueError("visibility evidence does not identify a complete result for this run")


def visibility_publication_facts(
    *, run: WorkflowRun, canonical_sha: str, report: bytes, evidence: bytes
) -> dict[str, Any]:
    evidence_path = f"reports/visibility/{run.id}/evidence.json"
    validate_visibility_artifacts(report, evidence, run_id=str(run.id), evidence_path=evidence_path)
    result = {
        "version": 1,
        "run_id": str(run.id),
        "project_id": str(run.project_id),
        "definition_commit_sha": run.definition_commit_sha,
        "canonical_commit_sha": canonical_sha,
        "artifacts": [
            {"path": path, "sha256": hashlib.sha256(body).hexdigest(), "byte_count": len(body)}
            for path, body in (("reports/AI_VISIBILITY.md", report), (evidence_path, evidence))
        ],
    }
    validate_visibility_publication(result, run=run, canonical_sha=canonical_sha)
    return result


def validate_visibility_publication(value: Any, *, run: WorkflowRun, canonical_sha: str) -> None:
    """Receipt identity/shape check; deliberately does not reread already-validated storage."""
    identity = {
        "version": 1,
        "run_id": str(run.id),
        "project_id": str(run.project_id),
        "definition_commit_sha": run.definition_commit_sha,
        "canonical_commit_sha": canonical_sha,
    }
    if (
        not isinstance(value, dict)
        or any(value.get(key) != expected for key, expected in identity.items())
        or type(value.get("version")) is not int
        or re.fullmatch(r"[0-9a-f]{40}", canonical_sha) is None
        or not isinstance(value.get("artifacts"), list)
        or len(value["artifacts"]) != 2
    ):
        raise ValueError("visibility publication does not belong to this run")
    for item, path, limit in zip(
        value["artifacts"],
        ("reports/AI_VISIBILITY.md", f"reports/visibility/{run.id}/evidence.json"),
        (MAX_VISIBILITY_REPORT_BYTES, MAX_VISIBILITY_EVIDENCE_BYTES),
        strict=True,
    ):
        if (
            not isinstance(item, dict)
            or item.get("path") != path
            or not isinstance(item.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None
            or type(item.get("byte_count")) is not int
            or not 1 <= item["byte_count"] <= limit
        ):
            raise ValueError("visibility publication has invalid artifact facts")


def _panel_schema(*, candidate_bank: bool = False) -> dict[str, Any]:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "target": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "domain": {
                        "type": "string",
                        "maxLength": 253,
                        "pattern": rf"^(?:{DOMAIN_PATTERN})?$",
                        "description": "Bare DNS hostname, without a scheme, path or port; "
                        "empty when unknown. Do not invent a domain.",
                    },
                    "aliases": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "domain", "aliases"],
            },
            "questions": {
                "type": "array",
                "minItems": 5,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "family": {"type": "string", "enum": list(QUESTION_FAMILIES)},
                        "fit": {
                            "type": "string",
                            "enum": ["strong", "adjacent", "wrong_category"],
                        },
                        "text": {"type": "string"},
                    },
                    "required": ["id", "family", "fit", "text"],
                },
            },
        },
        "required": ["target", "questions"],
    }

    if candidate_bank:
        schema["properties"]["candidate_intents"] = {
            "type": "array",
            "minItems": 5,
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "intent": {"type": "string", "maxLength": 120},
                    "questions": {
                        "type": "array",
                        "minItems": 3,
                        "maxItems": 5,
                        "items": {"type": "string", "minLength": 15, "maxLength": 500},
                    },
                },
                "required": ["intent", "questions"],
            },
        }
        schema["required"].append("candidate_intents")
    return schema


def _validate_candidate_bank(panel):
    intents = panel.get("candidate_intents")
    if not isinstance(intents, list) or not 5 <= len(intents) <= 8:
        raise VisibilityProtocolError("panel requires five to eight candidate intents")
    candidates, labels = set(), set()
    target = panel["target"]
    markers = [target["name"], target["domain"], *target["aliases"]]
    for intent in intents:
        if not isinstance(intent, dict) or not isinstance(intent.get("intent"), str):
            raise VisibilityProtocolError("invalid candidate intent")
        label = intent["intent"].strip().casefold()
        questions = intent.get("questions")
        if (
            not label
            or len(label) > 120
            or label in labels
            or not isinstance(questions, list)
            or not 3 <= len(questions) <= 5
        ):
            raise VisibilityProtocolError(
                "candidate intent needs a distinct label and three to five questions"
            )
        labels.add(label)
        for question in questions:
            if (
                not isinstance(question, str)
                or not 15 <= len(question) <= 500
                or _contains_target(question, markers)
            ):
                raise VisibilityProtocolError("invalid or branded candidate question")
            if question.casefold() in candidates:
                raise VisibilityProtocolError("candidate question is duplicated")
            candidates.add(question.casefold())
    if any(q["text"].casefold() not in candidates for q in panel["questions"]):
        raise VisibilityProtocolError("measured questions must come from the frozen candidate bank")


def _adjudication_schema() -> dict[str, Any]:
    outcome = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "question_id": {"type": "string"},
            "found": {"type": "boolean"},
            "mentioned": {"type": "boolean"},
            "evaluated": {"type": "boolean"},
            "shortlisted": {"type": "boolean"},
            "top_choice": {"type": "boolean"},
            "notes": {"type": "string"},
        },
        "required": [
            "question_id",
            "found",
            "mentioned",
            "evaluated",
            "shortlisted",
            "top_choice",
            "notes",
        ],
    }
    recommendation = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string"},
            "action": {"type": "string"},
            "evidence_question_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["title", "action", "evidence_question_ids"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string"},
            "outcomes": {
                "type": "array",
                "minItems": 5,
                "maxItems": 5,
                "items": outcome,
            },
            "recommendations": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": recommendation,
            },
        },
        "required": ["summary", "outcomes", "recommendations"],
    }


def _validate_panel(panel: dict[str, Any], *, target_request: str) -> None:
    target = panel.get("target")
    questions = panel.get("questions")
    if not isinstance(target, dict) or not isinstance(questions, list):
        raise VisibilityProtocolError("visibility panel is incomplete")
    name = target.get("name")
    domain = target.get("domain")
    aliases = target.get("aliases")
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise VisibilityProtocolError("visibility target name is invalid")
    if not isinstance(domain, str) or (domain and not _valid_domain(domain)):
        raise VisibilityProtocolError("visibility target domain is invalid")
    if not isinstance(aliases, list) or not all(
        isinstance(alias, str) and 1 <= len(alias) <= 100 for alias in aliases
    ):
        raise VisibilityProtocolError("visibility target aliases are invalid")
    if target_request.casefold() != "this project":
        requested_domain = _requested_domain(target_request)
        if requested_domain is not None:
            resolved_domain = domain.casefold().removeprefix("www.")
            related_domain = (
                resolved_domain == requested_domain
                or resolved_domain.endswith(f".{requested_domain}")
                or requested_domain.endswith(f".{resolved_domain}")
            )
            if not resolved_domain or not related_domain:
                raise VisibilityProtocolError("visibility panel resolved a different target domain")
        else:
            markers = [name, domain, *aliases]
            matches_request = _contains_target(target_request, markers) or any(
                _contains_target(marker, [target_request]) for marker in markers
            )
            if not matches_request:
                raise VisibilityProtocolError("visibility panel resolved a different target")
    if len(questions) != 5:
        raise VisibilityProtocolError("visibility panel must have five questions")
    ids: set[str] = set()
    families: set[str] = set()
    strong = 0
    markers = [name, domain, *aliases]
    for item in questions:
        if not isinstance(item, dict):
            raise VisibilityProtocolError("visibility question is invalid")
        question_id = item.get("id")
        family = item.get("family")
        fit = item.get("fit")
        text = item.get("text")
        if (
            not isinstance(question_id, str)
            or not re.fullmatch(r"[a-z0-9_-]{1,32}", question_id)
            or question_id in ids
        ):
            raise VisibilityProtocolError("visibility question ID is invalid")
        if family not in QUESTION_FAMILIES or family in families:
            raise VisibilityProtocolError("visibility question family is invalid")
        if fit not in {"strong", "adjacent", "wrong_category"}:
            raise VisibilityProtocolError("visibility question fit is invalid")
        if not isinstance(text, str) or not 15 <= len(text) <= 500:
            raise VisibilityProtocolError("visibility question text is invalid")
        if _contains_target(text, markers):
            raise VisibilityProtocolError("visibility question leaked the target identity")
        ids.add(question_id)
        families.add(family)
        strong += fit == "strong"
    if families != set(QUESTION_FAMILIES):
        raise VisibilityProtocolError("visibility panel omitted a question family")
    if strong < 3:
        raise VisibilityProtocolError("visibility panel needs at least three strong-fit questions")


def _normalize_adjudication(
    result: dict[str, Any],
    *,
    panel: dict[str, Any],
    measurements: list[dict[str, Any]],
) -> None:
    outcomes = result.get("outcomes")
    recommendations = result.get("recommendations")
    summary = result.get("summary")
    question_ids = [str(item["id"]) for item in panel["questions"]]
    if not isinstance(summary, str) or not summary.strip():
        raise VisibilityProtocolError("visibility adjudication has no summary")
    if not isinstance(outcomes, list) or len(outcomes) != len(question_ids):
        raise VisibilityProtocolError("visibility adjudication has invalid outcomes")
    if not isinstance(recommendations, list) or not 1 <= len(recommendations) <= 3:
        raise VisibilityProtocolError("visibility adjudication has invalid recommendations")
    by_id: dict[str, dict[str, Any]] = {}
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            raise VisibilityProtocolError("visibility outcome is invalid")
        question_id = outcome.get("question_id")
        if question_id not in question_ids or question_id in by_id:
            raise VisibilityProtocolError("visibility outcome question is invalid")
        for field in ("found", "mentioned", "evaluated", "shortlisted", "top_choice"):
            if not isinstance(outcome.get(field), bool):
                raise VisibilityProtocolError("visibility outcome score is invalid")
        if not isinstance(outcome.get("notes"), str):
            raise VisibilityProtocolError("visibility outcome notes are invalid")
        by_id[str(question_id)] = outcome
    if set(by_id) != set(question_ids):
        raise VisibilityProtocolError("visibility adjudication omitted an outcome")

    measurements_by_id = {str(item["question_id"]): item for item in measurements}
    markers = [
        panel["target"]["name"],
        panel["target"]["domain"],
        *panel["target"]["aliases"],
    ]
    normalized: list[dict[str, Any]] = []
    for question_id in question_ids:
        outcome = by_id[question_id]
        measurement = measurements_by_id.get(question_id)
        if measurement is None:
            raise VisibilityProtocolError("visibility measurement is missing")
        searched = measurement["searched"]
        found_text = json.dumps(
            {
                "answer": searched.get("answer"),
                "queries": searched.get("queries"),
                "sources": searched.get("sources"),
                "citations": searched.get("citations"),
            },
            sort_keys=True,
        )
        mentioned = _contains_target(str(searched.get("answer", "")), markers)
        found = mentioned or _contains_target(found_text, markers)
        outcome["found"] = bool(outcome["found"] or found)
        outcome["mentioned"] = bool(outcome["mentioned"] or mentioned)
        if outcome["top_choice"]:
            outcome["shortlisted"] = True
        if outcome["shortlisted"]:
            outcome["evaluated"] = True
        if outcome["evaluated"]:
            outcome["mentioned"] = True
        if outcome["mentioned"]:
            outcome["found"] = True
        normalized.append(outcome)
    result["outcomes"] = normalized
    for recommendation in recommendations:
        if not isinstance(recommendation, dict):
            raise VisibilityProtocolError("visibility recommendation is invalid")
        if not isinstance(recommendation.get("title"), str) or not recommendation["title"]:
            raise VisibilityProtocolError("visibility recommendation title is invalid")
        if not isinstance(recommendation.get("action"), str) or not recommendation["action"]:
            raise VisibilityProtocolError("visibility recommendation action is invalid")
        evidence_ids = recommendation.get("evidence_question_ids")
        if not isinstance(evidence_ids, list) or any(
            item not in question_ids for item in evidence_ids
        ):
            raise VisibilityProtocolError("visibility recommendation evidence is invalid")


def _normalize_answer(response: dict[str, Any], *, searched: bool) -> dict[str, Any]:
    output = response.get("output")
    if not isinstance(output, list):
        raise VisibilityProtocolError("visibility response has no output list")
    fragments: list[str] = []
    queries: list[str] = []
    sources: list[dict[str, str]] = []
    citations: list[dict[str, str]] = []
    search_calls = 0
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "web_search_call":
            search_calls += 1
            action = item.get("action")
            if isinstance(action, dict):
                query = action.get("query")
                if isinstance(query, str):
                    queries.append(query)
                action_queries = action.get("queries")
                if isinstance(action_queries, list):
                    queries.extend(value for value in action_queries if isinstance(value, str))
                action_sources = action.get("sources")
                if isinstance(action_sources, list):
                    sources.extend(_normalize_links(action_sources))
        if item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            text = part.get("text")
            if isinstance(text, str):
                fragments.append(text)
            annotations = part.get("annotations")
            if isinstance(annotations, list):
                citations.extend(_normalize_links(annotations))
    answer = "\n".join(fragments).strip()
    if not answer:
        raise VisibilityProtocolError("visibility answer is empty")
    if searched and search_calls == 0:
        raise VisibilityProtocolError("web-enabled visibility answer did not search")
    usage = response.get("usage")
    unique_sources = _unique_links(sources)
    return {
        "mode": "web" if searched else "model_only",
        "response_id": _response_id(response),
        "answer": answer,
        "search_calls": search_calls,
        "queries": _unique_strings(queries),
        "sources_returned": len(unique_sources),
        "sources": unique_sources[:MAX_SOURCES_PER_ANSWER],
        "citations": _unique_links(citations),
        "usage": usage if isinstance(usage, dict) else {},
    }


def _render_report(
    *,
    project_name: str,
    target_request: str,
    source_refs: list[str],
    panel: dict[str, Any],
    measurements: list[dict[str, Any]],
    adjudication: dict[str, Any],
    evidence_path: str,
) -> str:
    outcomes = {item["question_id"]: item for item in adjudication["outcomes"]}
    measurements_by_id = {item["question_id"]: item for item in measurements}
    counts = {
        field: sum(bool(item[field]) for item in adjudication["outcomes"])
        for field in ("found", "mentioned", "evaluated", "shortlisted", "top_choice")
    }
    strong_ids = {question["id"] for question in panel["questions"] if question["fit"] == "strong"}
    strong_top = sum(outcomes[question_id]["top_choice"] for question_id in strong_ids)
    probe_mentions = sum(
        _contains_target(
            str(item["probe"]["answer"]),
            [
                panel["target"]["name"],
                panel["target"]["domain"],
                *panel["target"]["aliases"],
            ],
        )
        for item in measurements
    )
    bottleneck = _primary_bottleneck(counts, panel=panel, outcomes=outcomes)
    target_label = _md(panel["target"]["name"])
    if panel["target"]["domain"]:
        target_label += f" ({_md(panel['target']['domain'])})"
    lines = [
        "# AI visibility audit",
        "",
        f"**Project:** {_md(project_name)}  ",
        f"**Target request:** {_md(target_request)}  ",
        f"**Resolved target:** {target_label}  ",
        f"**Panel:** 5 target-blind buyer questions · `{panel['panel_hash'][:12]}`",
        "",
        "## Summary",
        "",
        _md(adjudication["summary"]),
        "",
        (
            f"Web-enabled answers: **{counts['found']}/5 found**, "
            f"**{counts['mentioned']}/5 mentioned**, "
            f"**{counts['evaluated']}/5 evaluated**, "
            f"**{counts['shortlisted']}/5 shortlisted**, "
            f"**{counts['top_choice']}/5 selected first**."
        ),
        "",
        (
            f"Model-only baseline: **{probe_mentions}/5 mentions**. "
            f"Strong-fit questions: **{strong_top}/{len(strong_ids)} selected first**."
        ),
        "",
        "## Buyer questions",
        "",
        "| Question | Fit | With web search | Without web search |",
        "| --- | --- | --- | --- |",
    ]
    for question in panel["questions"]:
        question_id = question["id"]
        outcome = outcomes[question_id]
        measurement = measurements_by_id[question_id]
        stage = _outcome_stage(outcome)
        probe = (
            "mentioned"
            if _contains_target(
                measurement["probe"]["answer"],
                [
                    panel["target"]["name"],
                    panel["target"]["domain"],
                    *panel["target"]["aliases"],
                ],
            )
            else "not mentioned"
        )
        lines.append(
            f"| **{_md(question['text'])}**<br><sub>{_md(question['family'])}</sub> "
            f"| {_md(question['fit'])} | **{stage}** — {_md(outcome['notes'])} "
            f"| {probe} |"
        )

    lines.extend(
        [
            "",
            "## Primary bottleneck",
            "",
            f"**{bottleneck['label']}** — {bottleneck['explanation']}",
            "",
            "## Recommended next move",
            "",
        ]
    )
    for recommendation in adjudication["recommendations"]:
        evidence_ids = ", ".join(f"`{item}`" for item in recommendation["evidence_question_ids"])
        suffix = f" Evidence: {evidence_ids}." if evidence_ids else ""
        lines.append(
            f"- **{_md(recommendation['title'])}:** {_md(recommendation['action'])}{suffix}"
        )

    links: list[dict[str, str]] = []
    for measurement in measurements:
        links.extend(measurement["searched"]["citations"])
        links.extend(measurement["searched"]["sources"])
    lines.extend(["", "## Evidence", "", f"Raw run evidence: `{evidence_path}`", ""])
    if links:
        for link in _unique_links(links):
            title = link.get("title") or link["url"]
            lines.append(f"- [{_md(title)}]({_safe_url(link['url'])})")
    else:
        lines.append("- No cited web source was returned by the provider.")
    if source_refs:
        lines.extend(["", "Project context:", ""])
        lines.extend(f"- `{ref}`" for ref in source_refs)

    lines.extend(
        [
            "",
            "## Method",
            "",
            (
                "Tin generated five buyer questions without the target name or domain, then "
                "asked Luna each question twice: once with required web search and once with no "
                "tools. The ladder is found → mentioned → evaluated → shortlisted → selected "
                "first. Evidence keeps every citation and up to 20 returned sources per question. "
                "This is a bounded diagnostic panel, not a market-share estimate."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _primary_bottleneck(
    counts: dict[str, int],
    *,
    panel: dict[str, Any],
    outcomes: dict[str, dict[str, Any]],
) -> dict[str, str]:
    for question in panel["questions"]:
        if question["fit"] == "wrong_category" and outcomes[question["id"]]["top_choice"]:
            return {
                "label": "Category precision",
                "explanation": (
                    "The target wins a question marked as a poor category fit, which can "
                    "overstate useful visibility."
                ),
            }
    stages = [
        ("found", 5, "Discovery", "The target is absent from the evidence Luna retrieves."),
        (
            "mentioned",
            counts["found"],
            "Answer inclusion",
            "The target appears in retrieval but is omitted from the actual answer.",
        ),
        (
            "evaluated",
            counts["mentioned"],
            "Evidence",
            "The target is named but is not evaluated against the buyer's criteria.",
        ),
        (
            "shortlisted",
            counts["evaluated"],
            "Differentiation",
            "The target is evaluated but does not make the recommendation set.",
        ),
        (
            "top_choice",
            counts["shortlisted"],
            "Preference",
            "The target reaches shortlists but is rarely selected first.",
        ),
    ]
    largest = max(stages, key=lambda item: item[1] - counts[item[0]])
    if largest[1] - counts[largest[0]] <= 0:
        return {
            "label": "No single break",
            "explanation": "The target moves through the full recommendation ladder in this panel.",
        }
    return {"label": largest[2], "explanation": largest[3]}


def _outcome_stage(outcome: dict[str, Any]) -> str:
    for field, label in (
        ("top_choice", "selected first"),
        ("shortlisted", "shortlisted"),
        ("evaluated", "evaluated"),
        ("mentioned", "mentioned"),
        ("found", "found"),
    ):
        if outcome[field]:
            return label
    return "not found"


def _json_input(value: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": json.dumps(value, separators=(",", ":")),
                }
            ],
        }
    ]


def visibility_response_checkpoint(response: dict[str, Any]) -> dict[str, Any]:
    """Keep bounded answer/search evidence, never reasoning or the request payload."""
    output = response.get("output")
    saved: dict[str, Any] = {
        "id": response.get("id") if isinstance(response.get("id"), str) else None,
        "output": None if not isinstance(output, list) else [],
        "usage": {},
    }
    usage = response.get("usage")
    if isinstance(usage, dict):
        saved["usage"] = {
            key: usage[key]
            for key in ("input_tokens", "output_tokens", "total_tokens")
            if type(usage.get(key)) is int and usage[key] >= 0
        }
        for name, field in (
            ("input_tokens_details", "cached_tokens"),
            ("output_tokens_details", "reasoning_tokens"),
        ):
            details = usage.get(name)
            if (
                isinstance(details, dict)
                and type(details.get(field)) is int
                and details[field] >= 0
            ):
                saved["usage"][name] = {field: details[field]}
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "web_search_call":
            action = item.get("action")
            action = action if isinstance(action, dict) else {}
            queries = action.get("queries")
            sources = action.get("sources")
            saved["output"].append(
                {
                    "type": "web_search_call",
                    "action": {
                        "query": action.get("query")
                        if isinstance(action.get("query"), str)
                        else None,
                        "queries": [q for q in queries if isinstance(q, str)]
                        if isinstance(queries, list)
                        else [],
                        "sources": _normalize_links(sources) if isinstance(sources, list) else [],
                    },
                }
            )
        elif item.get("type") == "message" and isinstance(item.get("content"), list):
            content = []
            for part in item["content"]:
                if not isinstance(part, dict) or part.get("type") != "output_text":
                    continue
                annotations = part.get("annotations")
                content.append(
                    {
                        "type": "output_text",
                        "text": part.get("text") if isinstance(part.get("text"), str) else None,
                        "annotations": _normalize_links(annotations)
                        if isinstance(annotations, list)
                        else [],
                    }
                )
            saved["output"].append({"type": "message", "content": content})
    if len(json.dumps(saved, ensure_ascii=False).encode()) > MAX_VISIBILITY_RESPONSE_BYTES:
        return {"version": 1, "error": "visibility response exceeds its checkpoint limit"}
    return {"version": 1, "response": saved}


def read_visibility_response_checkpoint(checkpoint: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(checkpoint, dict) or checkpoint.get("version") != 1:
        raise VisibilityRecoveryError("visibility response checkpoint is invalid")
    if checkpoint.get("error") == "visibility response exceeds its checkpoint limit":
        raise VisibilityProtocolError(checkpoint["error"])
    response = checkpoint.get("response")
    if not isinstance(response, dict):
        raise VisibilityRecoveryError("visibility response checkpoint is invalid")
    # Parsing/identity/search validation runs again on every replay of this response.
    return response


def _structured_output(response: dict[str, Any]) -> dict[str, Any]:
    text = _output_text(response)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VisibilityProtocolError("visibility response is not JSON") from exc
    if not isinstance(value, dict):
        raise VisibilityProtocolError("visibility response JSON is not an object")
    return value


def _output_text(response: dict[str, Any]) -> str:
    output = response.get("output")
    if not isinstance(output, list):
        raise VisibilityProtocolError("visibility response has no output list")
    fragments: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "output_text":
                text = part.get("text")
                if isinstance(text, str):
                    fragments.append(text)
    result = "\n".join(fragments).strip()
    if not result:
        raise VisibilityProtocolError("visibility response has no output text")
    return result


def _response_id(response: dict[str, Any]) -> str:
    response_id = response.get("id")
    if not isinstance(response_id, str) or not response_id:
        raise VisibilityProtocolError("visibility response has no ID")
    return response_id


def _panel_hash(panel: dict[str, Any]) -> str:
    stable = {"target": panel["target"], "questions": panel["questions"]}
    if "candidate_intents" in panel:
        stable["candidate_intents"] = panel["candidate_intents"]
    encoded = json.dumps(stable, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _contains_target(text: str, markers: list[str]) -> bool:
    folded = text.casefold()
    for marker in markers:
        value = marker.strip().casefold()
        if len(value) < 3:
            continue
        pattern = rf"(?<![\w]){re.escape(value)}(?![\w])"
        if re.search(pattern, folded):
            return True
    return False


def _valid_domain(value: str) -> bool:
    return bool(len(value) <= 253 and re.fullmatch(DOMAIN_PATTERN, value))


def _canonical_domain(value: Any) -> str:
    """Normalize a hostname or HTTP(S) URL without guessing or changing its identity."""
    if not isinstance(value, str) or len(value) > 2048:
        raise VisibilityProtocolError("visibility target domain is invalid")
    candidate = value.strip()
    if not candidate:
        return ""
    if (
        any(character.isspace() or ord(character) < 32 for character in candidate)
        or "\\" in candidate
    ):
        raise VisibilityProtocolError("visibility target domain is invalid")
    try:
        parsed = urlsplit(
            candidate if "://" in candidate or candidate.startswith("//") else f"//{candidate}"
        )
        # Accessing port also rejects malformed and out-of-range ports.
        _ = parsed.port
        hostname = (parsed.hostname or "").lower().removesuffix(".")
        if (
            parsed.scheme not in {"", "http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or not _valid_domain(hostname)
        ):
            raise ValueError("invalid hostname")
    except ValueError:
        raise VisibilityProtocolError("visibility target domain is invalid") from None
    return hostname


def _requested_domain(value: str) -> str | None:
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate if "://" in candidate else f"//{candidate}")
    except ValueError:
        return None
    hostname = parsed.hostname.removesuffix(".") if parsed.hostname else None
    if hostname is None or not _valid_domain(hostname):
        return None
    return hostname.casefold().removeprefix("www.")


def _normalize_links(values: list[Any]) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        url = value.get("url")
        title = value.get("title")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            continue
        links.append({"url": url, "title": title if isinstance(title, str) else ""})
    return links


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _unique_links(values: list[dict[str, str]]) -> list[dict[str, str]]:
    by_url: dict[str, dict[str, str]] = {}
    for value in values:
        url = value.get("url")
        if isinstance(url, str) and url not in by_url:
            by_url[url] = {"url": url, "title": value.get("title", "")}
    return list(by_url.values())


def _md(value: str) -> str:
    return html.escape(" ".join(str(value).split())).replace("|", "&#124;")


def _safe_url(value: str) -> str:
    return value.replace("(", "%28").replace(")", "%29").replace(" ", "%20")
