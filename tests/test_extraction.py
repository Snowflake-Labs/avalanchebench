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

import hashlib

import pytest

from avalanche_bench.extraction import extract_questions, normalize_title


def rubric(count: int = 3) -> list[dict[str, str]]:
    return [
        {
            "category": "Quality",
            "subcategory": f"Topic {number}",
            "question": f"What is answer {number}?",
            "target_answer": f"Answer {number}",
            "scoring_criteria": "Perfect (5 pts): correct. Zero (0 pts): wrong.",
            "max_points": "5",
        }
        for number in range(1, count + 1)
    ]


@pytest.mark.parametrize(
    "marker",
    [
        "# Q1",
        "## q 1",
        "### **Q-01** — title",
        "#### Q:1. title",
        "##### Q.1: title",
        "###### Question 1 — title",
        "**Q1**",
        "Question 1",
    ],
)
def test_numeric_marker_variants_are_high_confidence(marker: str) -> None:
    result = extract_questions(f"{marker}\nAnswer one.\n", rubric(1))[0]
    assert result.text == "Answer one."
    assert result.confidence == "high"
    assert result.fallback_reason is None


def test_exact_normalized_rubric_title_without_number() -> None:
    rows = rubric(1)
    rows[0]["question"] = "How many widgets sold?"
    result = extract_questions("### HOW MANY WIDGETS SOLD —\nForty-two.\n", rows)[0]
    assert result.text == "Forty-two."
    assert result.selected is not None
    assert result.selected.method == "rubric_title_atx_heading"
    assert normalize_title(result.selected.original_heading) == (
        "how many widgets sold"
    )


def test_section_ends_only_at_next_question_marker() -> None:
    report = """## Q1
Answer one.
### Analysis detail
- item
#### Table
| value |
| --- |
| 1 |
## Q2
Answer two.
"""
    results = extract_questions(report, rubric(2))
    assert "Analysis detail" in (results[0].text or "")
    assert "| value |" in (results[0].text or "")
    assert "Answer two" not in (results[0].text or "")
    assert results[1].text == "Answer two."


def test_fenced_quoted_indented_and_html_fake_markers_are_ignored() -> None:
    report = """## Q1
Answer one.
```markdown
## Q2
fake fenced answer
```
> ## Q2
> fake quoted answer
    ## Q2
    fake indented answer
<pre>
## Q2
fake raw answer
</pre>
## Q2
Answer two.
"""
    first, second = extract_questions(report, rubric(2))
    assert "fake fenced answer" in (first.text or "")
    assert second.text == "Answer two."
    assert len(second.candidates) == 1


def test_duplicate_ids_are_preserved_and_force_uncertain_fallback() -> None:
    report = "## Q1\nFirst.\n### q-01\nSecond.\n"
    result = extract_questions(report, rubric(1))[0]
    assert result.selected is None
    assert result.fallback_reason == "duplicate_question_markers"
    provenance = result.provenance("whole_report")
    assert len(provenance["duplicate_candidates"]) == 2
    assert {
        item["extracted_span_character_length"]
        for item in provenance["duplicate_candidates"]
    }


@pytest.mark.parametrize(
    "example", ["```html\n<pre>\n```", "    <pre>", "\t<pre>", "> <pre>"]
)
def test_html_in_code_or_quote_does_not_hide_later_questions(example: str) -> None:
    first, second = extract_questions(
        f"## Q1\nAnswer one.\n{example}\n## Q2\nAnswer two.\n", rubric(2)
    )
    assert "Answer two" not in (first.text or "")
    assert second.text == "Answer two."
    assert second.fallback_reason is None


def test_ambiguous_duplicate_rubric_titles_force_fallback() -> None:
    rows = rubric(2)
    rows[0]["question"] = rows[1]["question"] = "Same title"
    results = extract_questions("## Same title\nAn answer.\n", rows)
    assert all(result.fallback_reason == "ambiguous_rubric_title" for result in results)
    assert all(result.selected is None for result in results)


def test_reordered_and_missing_middle_question() -> None:
    report = "## Q3\nThree.\n# Q1\nOne.\n"
    first, second, third = extract_questions(report, rubric())
    assert first.text == "One."
    assert second.fallback_reason == "no_question_marker"
    assert third.text == "Three."


def test_q1_does_not_match_q10() -> None:
    results = extract_questions("## Q10\nTen.\n", rubric(10))
    assert results[0].fallback_reason == "no_question_marker"
    assert results[9].text == "Ten."


def test_crlf_unicode_and_offsets_are_auditable() -> None:
    report = "Résumé — overview\r\n### Q–01: Café\r\nRéponse.\r\n"
    result = extract_questions(
        report,
        rubric(1),
        report_sha256=hashlib.sha256(report.encode()).hexdigest(),
        rubric_sha256="b" * 64,
    )[0]
    provenance = result.provenance("section")
    start = provenance["span_character_offsets"]["start"]
    end = provenance["span_character_offsets"]["end"]
    assert report[start:end] == "Réponse."
    assert provenance["span_byte_offsets"]["start"] > start
    assert (
        provenance["extracted_span_sha256"]
        == hashlib.sha256("Réponse.".encode()).hexdigest()
    )
    assert provenance["report_sha256"] == hashlib.sha256(report.encode()).hexdigest()
    assert provenance["rubric_sha256"] == "b" * 64


@pytest.mark.parametrize("report", ["", " \n\t\r\n"])
def test_empty_reports_are_certainly_absent(report: str) -> None:
    result = extract_questions(report, rubric(1))[0]
    assert result.confidence == "certain_absent"
    assert result.fallback_reason == "empty_report"
    assert result.text is None


def test_long_section_is_not_truncated_by_extractor() -> None:
    answer = "x" * 200_000
    result = extract_questions(f"## Q1\n{answer}\n", rubric(1))[0]
    assert result.text == answer
    assert result.provenance("section")["extracted_span_character_length"] == 200_000
