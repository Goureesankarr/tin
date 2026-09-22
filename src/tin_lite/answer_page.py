from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

MAX_ANSWER_PAGE_SOURCE_BYTES = 300_000
MAX_ANSWER_PAGE_BYTES = 150_000
MAX_ANSWER_PAGE_EVIDENCE_BYTES = 1_000_000
MAX_WEB_SOURCES = 30


class ResponsesClient(Protocol):
    model: str

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class AnswerPageProtocolError(RuntimeError):
    """The model returned data outside the answer-page contract."""


@dataclass(frozen=True)
class AnswerPageSource:
    label: str
    artifact_ref: str
    content: str


class AnswerPageDrafter:
    def __init__(self, *, responses: ResponsesClient, skill_suite: str) -> None:
        self._responses = responses
        self._skill_suite = skill_suite

    async def draft(self, *, project_name: str, sources: list[AnswerPageSource]) -> dict[str, Any]:
        source_bytes = sum(len(source.content.encode()) for source in sources)
        if source_bytes > MAX_ANSWER_PAGE_SOURCE_BYTES:
            raise ValueError(f"answer-page sources exceed {MAX_ANSWER_PAGE_SOURCE_BYTES} bytes")
        reference = {
            "project_name": project_name,
            "sources": [
                {
                    "label": source.label,
                    "artifact_ref": source.artifact_ref,
                    "content": source.content,
                }
                for source in sources
            ],
        }
        response = await self._responses.create(
            {
                "instructions": self._skill_suite,
                "input": json.dumps(reference, separators=(",", ":")),
                "store": False,
                "tools": [{"type": "web_search"}],
                "tool_choice": {"type": "web_search"},
                "include": ["web_search_call.action.sources"],
                "max_tool_calls": 4,
                "text": {"verbosity": "medium"},
            }
        )
        result = _normalize_response(response)
        if "ANSWER_PLAN_V1" in self._skill_suite:
            result["argument_plan"], result["markdown"] = extract_argument_plan(result["markdown"])
        result["model"] = self._responses.model
        validate_answer_page(result["markdown"].encode())
        return result

    @staticmethod
    def build_artifacts(
        *,
        run_id: str,
        source_refs: list[str],
        draft: dict[str, Any],
        artifact_path: str,
        evidence_path: str,
    ) -> tuple[bytes, bytes]:
        markdown = str(draft["markdown"]).encode()
        evidence = (
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "artifact_path": artifact_path,
                    "source_refs": source_refs,
                    "model": draft["model"],
                    "response_id": draft["response_id"],
                    "search_calls": draft["search_calls"],
                    "queries": draft["queries"],
                    "sources": draft["sources"],
                    "citations": draft["citations"],
                    "usage": draft["usage"],
                    **(
                        {"argument_plan": draft["argument_plan"]}
                        if "argument_plan" in draft
                        else {}
                    ),
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode()
        validate_answer_page_artifacts(
            markdown,
            evidence,
            artifact_path=artifact_path,
            evidence_path=evidence_path,
        )
        return markdown, evidence


def extract_argument_plan(markdown: str) -> tuple[dict, str]:
    match = re.match(r"<!-- tin-answer-plan-v1\s*(\{.*?\})\s*-->\s*", markdown, re.S)
    if not match or len(match.group(1).encode()) > 16000:
        raise AnswerPageProtocolError("answer page lacks a bounded saved argument plan")
    try:
        plan = json.loads(match.group(1))
    except ValueError as exc:
        raise AnswerPageProtocolError("invalid argument plan JSON") from exc
    fields = {"buyer_decision", "positioning", "answer", "proof", "objection", "next_step"}
    if (
        not isinstance(plan, dict)
        or set(plan) != fields
        or any(not isinstance(v, str) or not v.strip() or len(v) > 3000 for v in plan.values())
    ):
        raise AnswerPageProtocolError(
            "argument plan must identify decision, positioning, answer, "
            "proof, objection and next step"
        )
    return plan, markdown[match.end() :].lstrip()


def validate_answer_page(content: bytes) -> None:
    if not content or len(content) > MAX_ANSWER_PAGE_BYTES:
        raise ValueError(f"answer page must contain between 1 and {MAX_ANSWER_PAGE_BYTES} bytes")
    text = content.decode("utf-8")
    if not text.startswith("# "):
        raise ValueError("answer page must begin with a Markdown title")
    if "\n## Sources\n" not in text:
        raise ValueError("answer page must contain a Sources section")
    if len(re.findall(r"^## ", text, flags=re.MULTILINE)) < 3:
        raise ValueError("answer page must contain at least three sections")


def validate_answer_page_artifacts(
    content: bytes,
    evidence: bytes,
    *,
    artifact_path: str,
    evidence_path: str,
) -> None:
    validate_answer_page(content)
    if not evidence or len(evidence) > MAX_ANSWER_PAGE_EVIDENCE_BYTES:
        raise ValueError(
            "answer-page evidence must contain between 1 and "
            f"{MAX_ANSWER_PAGE_EVIDENCE_BYTES} bytes"
        )
    payload = json.loads(evidence)
    if payload.get("artifact_path") != artifact_path:
        raise ValueError("answer-page evidence points to the wrong artifact")
    if not evidence_path.endswith("/evidence.json"):
        raise ValueError("answer-page evidence path is invalid")
    if not isinstance(payload.get("source_refs"), list):
        raise ValueError("answer-page evidence has invalid source references")
    if not isinstance(payload.get("search_calls"), int) or payload["search_calls"] < 1:
        raise ValueError("answer-page evidence has no web search")


def _normalize_response(response: dict[str, Any]) -> dict[str, Any]:
    output = response.get("output")
    if not isinstance(output, list):
        raise AnswerPageProtocolError("answer-page response has no output list")
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
        parts = item.get("content")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            text = part.get("text")
            if isinstance(text, str):
                fragments.append(text)
            annotations = part.get("annotations")
            if isinstance(annotations, list):
                citations.extend(_normalize_links(annotations))
    markdown = "\n".join(fragments).strip()
    if not markdown:
        raise AnswerPageProtocolError("answer-page response returned no Markdown")
    if search_calls < 1:
        raise AnswerPageProtocolError("answer-page response did not search the web")
    usage = response.get("usage")
    return {
        "response_id": _response_id(response),
        "markdown": markdown + "\n",
        "search_calls": search_calls,
        "queries": _unique_strings(queries),
        "sources": _unique_links(sources)[:MAX_WEB_SOURCES],
        "citations": _unique_links(citations)[:MAX_WEB_SOURCES],
        "usage": usage if isinstance(usage, dict) else {},
    }


def _normalize_links(values: list[Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        url = value.get("url")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            continue
        title = value.get("title")
        result.append({"url": url, "title": title if isinstance(title, str) else ""})
    return result


def _unique_links(values: list[dict[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        if value["url"] in seen:
            continue
        seen.add(value["url"])
        result.append(value)
    return result


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _response_id(response: dict[str, Any]) -> str:
    response_id = response.get("id")
    if not isinstance(response_id, str) or not response_id:
        raise AnswerPageProtocolError("answer-page response has no ID")
    return response_id
