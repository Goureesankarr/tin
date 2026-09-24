"""Reviewed scoring resource for outreach.syllabus_placement; no web or model calls in CI."""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages/outreach.syllabus_placement"
SKILL = PACKAGE / "skills/syllabus-placement"
AS_OF = "2026-09-24"


@pytest.fixture(scope="module")
def scoring():
    text = (SKILL / "SCORING.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)\n```", text, re.S)
    assert len(blocks) == 1
    namespace = {}
    # Only the fixed repository resource executes here, never course text from a run.
    exec(compile(blocks[0], str(SKILL / "SCORING.md"), "exec"), namespace)  # noqa: S102
    return namespace


def course(name="Survey Methods Lab", **overrides):
    return {
        "course": name,
        "institution": "Example Agricultural University",
        "url": "https://example.edu/syllabi/ext-301.pdf",
        "quote": "Students will design a questionnaire and interview 30 farm households.",
        "quote_source": "document",
        "hands_on": True,
        "slot": "open",
        "next_start": "2027-03-01",
        "enrollment": 45,
        "contact": "public_page",
        **overrides,
    }


def test_manifest_declares_every_skill_file_and_matches_its_folder():
    manifest = json.loads((PACKAGE / "workflow.json").read_text(encoding="utf-8"))
    definition = manifest["definition"]
    assert definition["key"] == PACKAGE.name
    declared = definition["procedure"]["skill_files"]
    on_disk = sorted(str(path.relative_to(PACKAGE)) for path in SKILL.iterdir())
    assert sorted(declared) == on_disk
    front = (SKILL / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
    assert "name: syllabus-placement" in front
    assert definition["procedure"]["entry_skill"] == "syllabus-placement"


def test_every_text_input_is_bounded():
    manifest = json.loads((PACKAGE / "workflow.json").read_text(encoding="utf-8"))
    properties = manifest["definition"]["input_schema"]["properties"]
    for name, field in properties.items():
        if field["type"] == "string" and name != "project_id":
            assert field["maxLength"] <= 1000, name


@pytest.mark.parametrize(
    "as_of,status,send_on",
    [
        ("2026-03-01", "later", "2026-09-13"),  # more than 180 days before the window opens
        ("2026-08-01", "soon", "2026-09-13"),
        ("2026-09-13", "open", "2026-09-13"),  # first day of the window
        ("2026-12-21", "open", "2026-12-21"),  # last day: three weeks before start
        ("2026-12-22", "missed", "2027-09-13"),
        ("2027-02-01", "missed", "2027-09-13"),  # term already running
    ],
)
def test_window_follows_the_instructors_choice(scoring, as_of, status, send_on):
    assert scoring["window"]("2027-01-11", as_of) == {"status": status, "send_on": send_on}


def test_unknown_start_is_not_guessed(scoring):
    assert scoring["window"](None, AS_OF) == {"status": "unknown", "send_on": None}
    scored = scoring["score_course"](course(next_start=None), AS_OF)
    assert scored["decision"] == "check_by_hand"
    assert "start date" in scored["reason"]


def test_open_window_with_public_instructor_is_contact_now(scoring):
    scored = scoring["score_course"](course(next_start="2026-11-02"), AS_OF)
    assert scored["window"] == "open"
    assert scored["decision"] == "contact_now"
    assert scored["score"] == 40 + 30 + 10 + 10


def test_open_window_without_an_instructor_page_is_checked_by_hand(scoring):
    scored = scoring["score_course"](course(next_start="2026-11-02", contact="none"), AS_OF)
    assert scored["decision"] == "check_by_hand"


def test_a_snippet_is_never_evidence(scoring):
    # Search engines return pages for words they do not contain; only the document counts.
    scored = scoring["score_course"](course(quote_source="snippet"), AS_OF)
    assert scored["decision"] == "check_by_hand"
    assert scored["score"] > 0


def test_a_lecture_about_the_topic_is_discarded(scoring):
    scored = scoring["score_course"](course(hands_on=False), AS_OF)
    assert scored["decision"] == "discard"
    assert scored["score"] == 0


def test_a_course_already_using_the_product_is_proof_not_a_prospect(scoring):
    scored = scoring["score_course"](course(slot="own", next_start="2026-11-02"), AS_OF)
    assert scored["decision"] == "already_teaching"
    assert scored["score"] == 0


def test_a_snippet_cannot_prove_the_product_is_already_taught(scoring):
    # Found by hand: a 2017 short course that names the product ranked high on a search.
    scored = scoring["score_course"](course(slot="own", quote_source="snippet"), AS_OF)
    assert scored["decision"] == "check_by_hand"


@pytest.mark.parametrize("slot", ["open", "own"])
def test_an_old_page_is_a_lead_not_a_course(scoring, slot):
    scored = scoring["score_course"](course(slot=slot, document_year=2017), AS_OF)
    assert scored["decision"] == "check_by_hand"
    assert "2017" in scored["reason"]


def test_a_recent_page_is_current(scoring):
    scored = scoring["score_course"](course(document_year=2024), AS_OF)
    assert scored["decision"] == "schedule"


def test_an_empty_slot_outranks_a_required_competitor(scoring):
    empty = scoring["score_course"](course(slot="open"), AS_OF)
    manual = scoring["score_course"](course(slot="manual"), AS_OF)
    suggested = scoring["score_course"](course(slot="incumbent_suggested"), AS_OF)
    required = scoring["score_course"](course(slot="incumbent_required"), AS_OF)
    assert empty["score"] > manual["score"] > suggested["score"] > required["score"]


@pytest.mark.parametrize(
    "enrollment,points", [(None, 0), (0, 5), (19, 5), (20, 10), (59, 10), (60, 15), (150, 20)]
)
def test_enrollment_points(scoring, enrollment, points):
    assert scoring["enrollment_points"](enrollment) == points


def test_plan_sequences_this_week_before_later_dates(scoring):
    courses = [
        course("Later but large", next_start="2027-08-02", enrollment=200),
        course("Open now", next_start="2026-11-02", enrollment=15),
        course("Missed", next_start="2026-10-01"),
        course("Soon"),
        course("Lecture only", hands_on=False),
        course("Behind a login", quote_source="snippet"),
        course("Already ours", slot="own"),
        course("Open now", next_start="2026-11-02"),  # same course found twice
    ]
    result = scoring["plan"](courses, AS_OF, 5, searches=12, opened=9)
    order = [(row["course"], row["decision"]) for row in result["shortlist"]]
    assert order == [
        ("Open now", "contact_now"),
        ("Soon", "schedule"),  # ties with the larger course on score, so the earlier date leads
        ("Later but large", "schedule"),
        ("Missed", "next_cycle"),
    ]
    assert [row["course"] for row in result["check_by_hand"]] == ["Behind a login"]
    assert [row["course"] for row in result["already_teaching"]] == ["Already ours"]
    assert [row["course"] for row in result["discarded"]] == ["Lecture only"]
    assert result["funnel"] == {
        "searches": 12,
        "documents_opened": 9,
        "courses_recorded": 7,
        "duplicates_dropped": 1,
        "verified_from_document": 6,
        "hands_on": 5,
        "shortlisted": 4,
        "contact_now": 1,
    }


def test_verdict_comes_from_the_counts_not_the_run(scoring):
    # The first hosted run called one shortlisted course "fit"; one course is a lead.
    one = scoring["plan"]([course(next_start="2026-11-02")], AS_OF, 5)
    three = scoring["plan"]([course(f"C{n}") for n in range(3)], AS_OF, 5)
    only_by_hand = scoring["plan"]([course(quote_source="snippet")], AS_OF, 5)
    none = scoring["plan"]([course(hands_on=False)], AS_OF, 5)
    assert one["verdict"] == "thin"
    assert three["verdict"] == "fit"
    assert only_by_hand["verdict"] == "not a fit"
    assert none["verdict"] == "not a fit"


def test_plan_keeps_the_shortlist_to_the_limit(scoring):
    courses = [course(f"Course {n}", next_start="2026-11-02") for n in range(8)]
    result = scoring["plan"](courses, AS_OF, 5)
    assert len(result["shortlist"]) == 5
    assert len(result["over_limit"]) == 3


def test_plan_is_empty_but_valid_when_nothing_fits(scoring):
    result = scoring["plan"]([course(hands_on=False), course("B", slot="none")], AS_OF, 5)
    assert result["shortlist"] == []
    assert result["funnel"]["hands_on"] == 0


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"slot": "maybe"}, "slot"),
        ({"quote_source": "memory"}, "quote_source"),
        ({"hands_on": "yes"}, "hands_on"),
        ({"quote": "  "}, "quote"),
        ({"url": "example.edu/syllabus"}, "url"),
        ({"contact": "guessed_email"}, "contact"),
        ({"next_start": "Spring 2027"}, "next_start"),
        ({"enrollment": -3}, "enrollment"),
        ({"enrollment": True}, "enrollment"),
        ({"document_year": "2025"}, "document_year"),
        ({"document_year": 25}, "document_year"),
    ],
)
def test_plausible_but_unusable_records_are_rejected(scoring, overrides, message):
    with pytest.raises(ValueError, match=message):
        scoring["score_course"](course(**overrides), AS_OF)


def test_a_record_missing_fields_is_rejected(scoring):
    record = course()
    del record["quote"]
    with pytest.raises(ValueError, match="missing quote"):
        scoring["plan"]([record], AS_OF, 5)


@pytest.mark.parametrize("limit", [4, 31, True, "10"])
def test_max_courses_is_bounded(scoring, limit):
    with pytest.raises(ValueError, match="max_courses"):
        scoring["plan"]([course()], AS_OF, limit)
