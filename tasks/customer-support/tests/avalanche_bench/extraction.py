# Copyright 2026 Snowflake Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Format-robust, auditable extraction of per-question report sections."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

_ATX_HEADING = re.compile(
    r"^[ \t]{0,3}(?P<marks>#{1,6})[ \t]+(?P<text>.*?)[ \t]*#*[ \t]*$"
)
_FENCE = re.compile(r"^[ \t]{0,3}(?P<marks>`{3,}|~{3,})")
_BLOCK_QUOTE = re.compile(r"^[ \t]{0,3}>")
_BOLD_LINE = re.compile(r"^[ \t]{0,3}(?:\*\*|__)(?P<text>.+?)(?:\*\*|__)[ \t]*$")
_RAW_OPEN = re.compile(r"^[ \t]{0,3}<(?P<tag>pre|code|script|style)\b", re.I)
_RAW_CLOSE_TEMPLATE = r"</{tag}\s*>"
_MARKDOWN_WRAPPER = re.compile(r"(?:\*{1,3}|_{1,3}|`+)")
_QUESTION_NUMBER = re.compile(
    r"^[ \t]*(?:q(?:uestion)?)[ \t]*(?:[-:.\u2010-\u2015][ \t]*)?"
    r"0*(?P<number>\d{1,3})\b(?P<trailing>.*)$",
    re.I,
)
_TRAILING_SEPARATOR = re.compile(r"^[ \t]*(?:[.):\-\u2010-\u2015]+[ \t]*)?")
_PLAIN_QUESTION_MARKER = re.compile(
    r"^[ \t]{0,3}(?:\*{1,3}|_{1,3}|`+)?[ \t]*"
    r"(?:q(?:uestion)?)[ \t]*(?:[-:.\u2010-\u2015][ \t]*)?"
    r"0*\d{1,3}\b.*$",
    re.I,
)
_TITLE_PUNCTUATION = re.compile(r"[^\w]+", re.UNICODE)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_title(value: str) -> str:
    """Normalize harmless typography while retaining exact-title semantics."""

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    normalized = _MARKDOWN_WRAPPER.sub("", normalized)
    normalized = normalized.replace("&", " and ")
    normalized = _TITLE_PUNCTUATION.sub(" ", normalized)
    return " ".join(normalized.split())


def _without_markdown_wrappers(value: str) -> str:
    return _MARKDOWN_WRAPPER.sub("", unicodedata.normalize("NFKC", value)).strip()


def _numeric_question(value: str) -> int | None:
    match = _QUESTION_NUMBER.match(_without_markdown_wrappers(value))
    if match is None:
        return None
    trailing = match.group("trailing")
    # Any harmless punctuation/title may follow the numeric token. The token's
    # word boundary prevents Q1 from silently matching Q10.
    _TRAILING_SEPARATOR.match(trailing)
    return int(match.group("number"))


def _char_to_byte_offset(report: str, offset: int) -> int:
    return len(report[:offset].encode("utf-8"))


@dataclass(frozen=True)
class _Marker:
    question_ids: tuple[int, ...]
    method: str
    confidence: str
    original_heading: str
    normalized_heading: str
    heading_start: int
    heading_end: int
    content_start: int
    ambiguous_title: bool = False


@dataclass(frozen=True)
class ExtractionCandidate:
    question_id: int
    method: str
    confidence: str
    original_heading: str
    normalized_heading: str
    heading_start: int
    heading_end: int
    span_start: int
    span_end: int
    text: str
    ambiguous_title: bool = False

    def audit(self, report: str) -> dict[str, Any]:
        return {
            "extraction_method": self.method,
            "confidence": self.confidence,
            "original_matched_heading": self.original_heading,
            "normalized_matched_heading": self.normalized_heading,
            "heading_character_offsets": {
                "start": self.heading_start,
                "end": self.heading_end,
            },
            "heading_byte_offsets": {
                "start": _char_to_byte_offset(report, self.heading_start),
                "end": _char_to_byte_offset(report, self.heading_end),
            },
            "span_character_offsets": {
                "start": self.span_start,
                "end": self.span_end,
            },
            "span_byte_offsets": {
                "start": _char_to_byte_offset(report, self.span_start),
                "end": _char_to_byte_offset(report, self.span_end),
            },
            "extracted_span_sha256": sha256_text(self.text),
            "extracted_span_character_length": len(self.text),
            "extracted_span_byte_length": len(self.text.encode("utf-8")),
            "ambiguous_title_match": self.ambiguous_title,
        }


@dataclass(frozen=True)
class ExtractionResult:
    question_id: int
    report_sha256: str
    rubric_sha256: str
    report: str
    candidates: tuple[ExtractionCandidate, ...]
    selected: ExtractionCandidate | None
    confidence: str
    fallback_reason: str | None

    @property
    def text(self) -> str | None:
        return self.selected.text if self.selected is not None else None

    def provenance(self, final_prompt_scope: str) -> dict[str, Any]:
        selected = self.selected.audit(self.report) if self.selected else None
        candidate_audits = [
            candidate.audit(self.report) for candidate in self.candidates
        ]
        return {
            "question_id": self.question_id,
            "report_sha256": self.report_sha256,
            "rubric_sha256": self.rubric_sha256,
            "extraction_method": (
                self.selected.method if self.selected is not None else "none"
            ),
            "confidence": self.confidence,
            "original_matched_heading": (
                self.selected.original_heading if self.selected is not None else None
            ),
            "normalized_matched_heading": (
                self.selected.normalized_heading if self.selected is not None else None
            ),
            "span_character_offsets": (
                selected["span_character_offsets"] if selected else None
            ),
            "span_byte_offsets": selected["span_byte_offsets"] if selected else None,
            "extracted_span_sha256": (
                selected["extracted_span_sha256"] if selected else None
            ),
            "extracted_span_character_length": (
                selected["extracted_span_character_length"] if selected else 0
            ),
            "extracted_span_byte_length": (
                selected["extracted_span_byte_length"] if selected else 0
            ),
            "candidate_spans": candidate_audits,
            "duplicate_candidates": (
                candidate_audits if len(candidate_audits) > 1 else []
            ),
            "fallback_reason": self.fallback_reason,
            "final_prompt_scope": final_prompt_scope,
        }


def _rubric_title_index(rubric: list[dict[str, str]]) -> dict[str, tuple[int, ...]]:
    mapping: dict[str, set[int]] = {}
    for question_id, row in enumerate(rubric, start=1):
        for field in ("question", "subcategory"):
            title = normalize_title(row.get(field, ""))
            if not title:
                continue
            mapping.setdefault(title, set()).add(question_id)
    return {key: tuple(sorted(value)) for key, value in mapping.items()}


def _markers(report: str, rubric: list[dict[str, str]]) -> list[_Marker]:
    title_index = _rubric_title_index(rubric)
    markers: list[_Marker] = []
    active_fence: tuple[str, int] | None = None
    active_raw_tag: str | None = None
    offset = 0
    for line in report.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        line_start = offset
        line_end = offset + len(line)
        offset = line_end

        if active_raw_tag is not None:
            if re.search(
                _RAW_CLOSE_TEMPLATE.format(tag=re.escape(active_raw_tag)),
                content,
                re.I,
            ):
                active_raw_tag = None
            continue
        fence = _FENCE.match(content)
        if fence is not None:
            token = fence.group("marks")
            if active_fence is None:
                active_fence = (token[0], len(token))
            elif token[0] == active_fence[0] and len(token) >= active_fence[1]:
                active_fence = None
            continue
        if active_fence is not None:
            continue
        if _BLOCK_QUOTE.match(content) or content.startswith(("    ", "\t")):
            continue

        raw_open = _RAW_OPEN.match(content)
        if raw_open is not None:
            tag = raw_open.group("tag").lower()
            if not re.search(_RAW_CLOSE_TEMPLATE.format(tag=tag), content, re.I):
                active_raw_tag = tag
            continue

        atx = _ATX_HEADING.match(content)
        if atx is not None:
            heading = atx.group("text").strip()
            number = _numeric_question(heading)
            if number is not None:
                markers.append(
                    _Marker(
                        question_ids=(number,),
                        method="numeric_atx_heading",
                        confidence="high",
                        original_heading=heading,
                        normalized_heading=normalize_title(heading),
                        heading_start=line_start,
                        heading_end=line_start + len(content),
                        content_start=line_end,
                    )
                )
                continue
            normalized = normalize_title(heading)
            title_questions = title_index.get(normalized, ())
            if title_questions:
                markers.append(
                    _Marker(
                        question_ids=title_questions,
                        method="rubric_title_atx_heading",
                        confidence="high" if len(title_questions) == 1 else "uncertain",
                        original_heading=heading,
                        normalized_heading=normalized,
                        heading_start=line_start,
                        heading_end=line_start + len(content),
                        content_start=line_end,
                        ambiguous_title=len(title_questions) > 1,
                    )
                )
            continue

        bold = _BOLD_LINE.match(content)
        if bold is not None:
            heading = bold.group("text").strip()
            number = _numeric_question(heading)
            if number is not None:
                markers.append(
                    _Marker(
                        question_ids=(number,),
                        method="numeric_bold_marker",
                        confidence="high",
                        original_heading=heading,
                        normalized_heading=normalize_title(heading),
                        heading_start=line_start,
                        heading_end=line_start + len(content),
                        content_start=line_end,
                    )
                )
            continue

        if _PLAIN_QUESTION_MARKER.match(content):
            heading = content.strip()
            number = _numeric_question(heading)
            if number is not None:
                markers.append(
                    _Marker(
                        question_ids=(number,),
                        method="numeric_plain_marker",
                        confidence="high",
                        original_heading=heading,
                        normalized_heading=normalize_title(heading),
                        heading_start=line_start,
                        heading_end=line_start + len(content),
                        content_start=line_end,
                    )
                )
    return markers


def _trimmed_span(report: str, start: int, end: int) -> tuple[int, int, str]:
    raw = report[start:end]
    leading = len(raw) - len(raw.lstrip())
    trailing = len(raw) - len(raw.rstrip())
    trimmed_start = start + leading
    trimmed_end = end - trailing if trailing else end
    return trimmed_start, trimmed_end, report[trimmed_start:trimmed_end]


def extract_questions(
    report: str,
    rubric: list[dict[str, str]],
    *,
    report_sha256: str | None = None,
    rubric_sha256: str | None = None,
) -> list[ExtractionResult]:
    """Extract every rubric question and retain all competing candidate spans."""

    report_hash = report_sha256 or sha256_text(report)
    rubric_hash = rubric_sha256 or sha256_text(
        repr(sorted(sorted(row.items()) for row in rubric))
    )
    markers = _markers(report, rubric)
    by_question: dict[int, list[ExtractionCandidate]] = {
        question_id: [] for question_id in range(1, len(rubric) + 1)
    }
    for index, marker in enumerate(markers):
        end = (
            markers[index + 1].heading_start
            if index + 1 < len(markers)
            else len(report)
        )
        span_start, span_end, text = _trimmed_span(report, marker.content_start, end)
        for question_id in marker.question_ids:
            if question_id not in by_question:
                continue
            by_question[question_id].append(
                ExtractionCandidate(
                    question_id=question_id,
                    method=marker.method,
                    confidence=marker.confidence,
                    original_heading=marker.original_heading,
                    normalized_heading=marker.normalized_heading,
                    heading_start=marker.heading_start,
                    heading_end=marker.heading_end,
                    span_start=span_start,
                    span_end=span_end,
                    text=text,
                    ambiguous_title=marker.ambiguous_title,
                )
            )

    empty_report = not report.strip()
    results: list[ExtractionResult] = []
    for question_id in range(1, len(rubric) + 1):
        candidates = tuple(by_question[question_id])
        selected: ExtractionCandidate | None = None
        confidence = "uncertain"
        fallback_reason: str | None
        if empty_report:
            confidence = "certain_absent"
            fallback_reason = "empty_report"
        elif not candidates:
            fallback_reason = "no_question_marker"
        elif any(candidate.ambiguous_title for candidate in candidates):
            fallback_reason = "ambiguous_rubric_title"
        elif len(candidates) > 1:
            fallback_reason = "duplicate_question_markers"
        elif not candidates[0].text.strip():
            fallback_reason = "blank_extracted_span"
        else:
            selected = candidates[0]
            confidence = selected.confidence
            fallback_reason = None
        results.append(
            ExtractionResult(
                question_id=question_id,
                report_sha256=report_hash,
                rubric_sha256=rubric_hash,
                report=report,
                candidates=candidates,
                selected=selected,
                confidence=confidence,
                fallback_reason=fallback_reason,
            )
        )
    return results


def report_section(report: str, question_number: int) -> str:
    """Backward-compatible numeric section helper using robust marker semantics."""

    if question_number < 1:
        raise ValueError("question_number must be positive")
    rubric = [
        {"question": f"unmatchable rubric title {index}", "subcategory": ""}
        for index in range(1, max(1, question_number) + 1)
    ]
    result = extract_questions(report, rubric)[question_number - 1]
    return result.text or ""
