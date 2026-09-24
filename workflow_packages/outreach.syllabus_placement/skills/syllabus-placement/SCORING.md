# Scoring and sequencing

Run this block exactly as written. Save the verified course records from Station 3 as a JSON
list, then call `plan(courses, as_of, max_courses, searches, opened)` and use its result for
the report. The ranking lives here so that two runs on the same evidence give the same plan.

The timing window is an assumption, kept in two constants: instructors settle next term's
tools between about four months and three weeks before it starts (bookstore adoption and
syllabus deadlines usually fall in that span). Courses are assumed to run once a year unless
the page gives a later start date; for courses that run every term, enter the next start.
A document dated more than two years before `as_of` is not evidence of a current course,
and a course only seen in a search snippet is not evidence of anything yet, including that
it already uses the product.

```python
import json
from datetime import date, timedelta

WINDOW_OPENS_DAYS = 120
WINDOW_CLOSES_DAYS = 21
SOON_DAYS = 180
STALE_YEARS = 2

SLOT_POINTS = {
    "open": 40,
    "manual": 35,
    "incumbent_suggested": 25,
    "incumbent_required": 12,
    "own": 0,
    "none": 0,
}
TIMING_POINTS = {"open": 30, "soon": 20, "later": 10, "missed": 5, "unknown": 0}
CONTACT_POINTS = {"public_page": 10, "none": 0}
DECISION_ORDER = ["contact_now", "schedule", "next_cycle"]
REQUIRED = ("course", "institution", "url", "quote", "quote_source", "hands_on", "slot")


def _day(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} must be YYYY-MM-DD") from error


def window(next_start, as_of):
    """Where today falls against the instructor's tool choice for the next offering."""
    today = _day(as_of, "as_of")
    if next_start is None:
        return {"status": "unknown", "send_on": None}
    start = _day(next_start, "next_start")
    opens = start - timedelta(days=WINDOW_OPENS_DAYS)
    closes = start - timedelta(days=WINDOW_CLOSES_DAYS)
    if today < opens:
        status = "soon" if (opens - today).days <= SOON_DAYS else "later"
        return {"status": status, "send_on": opens.isoformat()}
    if today <= closes:
        return {"status": "open", "send_on": today.isoformat()}
    following = start + timedelta(days=365) - timedelta(days=WINDOW_OPENS_DAYS)
    return {"status": "missed", "send_on": following.isoformat()}


def enrollment_points(enrollment):
    if enrollment is None:
        return 0
    if not isinstance(enrollment, int) or isinstance(enrollment, bool) or enrollment < 0:
        raise ValueError("enrollment must be a non-negative integer or null")
    if enrollment < 20:
        return 5
    if enrollment < 60:
        return 10
    if enrollment < 150:
        return 15
    return 20


def check(course):
    missing = [field for field in REQUIRED if field not in course]
    if missing:
        raise ValueError(f"course record is missing {', '.join(missing)}")
    for field in ("course", "institution", "quote"):
        if not isinstance(course[field], str) or not course[field].strip():
            raise ValueError(f"{field} must be non-empty text")
    if not isinstance(course["url"], str) or not course["url"].startswith(("https://", "http://")):
        raise ValueError("url must be the course document's web address")
    if course["quote_source"] not in {"document", "snippet"}:
        raise ValueError("quote_source must be document or snippet")
    if not isinstance(course["hands_on"], bool):
        raise ValueError("hands_on must be true or false")
    if course["slot"] not in SLOT_POINTS:
        raise ValueError(f"slot must be one of {', '.join(SLOT_POINTS)}")
    if course.get("contact", "none") not in CONTACT_POINTS:
        raise ValueError("contact must be public_page or none")
    year = course.get("document_year")
    if year is not None and (not isinstance(year, int) or isinstance(year, bool) or year < 1990):
        raise ValueError("document_year must be a year or null")


def score_course(course, as_of):
    check(course)
    timing = window(course.get("next_start"), as_of)
    contact = course.get("contact", "none")
    score = (
        SLOT_POINTS[course["slot"]]
        + TIMING_POINTS[timing["status"]]
        + enrollment_points(course.get("enrollment"))
        + CONTACT_POINTS[contact]
    )
    year = course.get("document_year")
    reason = ""
    if course["slot"] == "none" or not course["hands_on"]:
        decision, score, reason = "discard", 0, "students do not do the job in this course"
    elif course["quote_source"] != "document":
        decision, reason = "check_by_hand", "not verified from the course document"
    elif year is not None and year < _day(as_of, "as_of").year - STALE_YEARS:
        decision, reason = "check_by_hand", f"document is from {year}; find a current offering"
    elif course["slot"] == "own":
        decision, score = "already_teaching", 0
    elif timing["status"] == "unknown":
        decision, reason = "check_by_hand", "next start date not found"
    elif timing["status"] == "open" and contact != "public_page":
        decision, reason = "check_by_hand", "window is open but no public instructor page"
    elif timing["status"] == "open":
        decision = "contact_now"
    elif timing["status"] == "missed":
        decision = "next_cycle"
    else:
        decision = "schedule"
    return {
        **course,
        "window": timing["status"],
        "send_on": timing["send_on"],
        "score": score,
        "decision": decision,
        "reason": reason,
    }


def plan(courses, as_of, max_courses=15, searches=0, opened=0):
    if not isinstance(max_courses, int) or isinstance(max_courses, bool):
        raise ValueError("max_courses must be an integer")
    if not 5 <= max_courses <= 30:
        raise ValueError("max_courses must be 5-30")
    seen, unique, duplicates = set(), [], 0
    for course in courses:
        check(course)
        identity = (course["institution"].strip().lower(), course["course"].strip().lower())
        if identity in seen:
            duplicates += 1
            continue
        seen.add(identity)
        unique.append(score_course(course, as_of))

    def rank(row):
        return (DECISION_ORDER.index(row["decision"]), -row["score"], row["send_on"] or "")

    def by(decision):
        return [row for row in unique if row["decision"] == decision]

    eligible = sorted((row for row in unique if row["decision"] in DECISION_ORDER), key=rank)
    shortlist = eligible[:max_courses]
    verified = [row for row in unique if row["quote_source"] == "document"]
    return {
        "shortlist": shortlist,
        "over_limit": eligible[max_courses:],
        "check_by_hand": by("check_by_hand"),
        "already_teaching": by("already_teaching"),
        "discarded": by("discard"),
        "funnel": {
            "searches": searches,
            "documents_opened": opened,
            "courses_recorded": len(unique),
            "duplicates_dropped": duplicates,
            "verified_from_document": len(verified),
            "hands_on": sum(1 for row in verified if row["hands_on"] and row["slot"] != "none"),
            "shortlisted": len(shortlist),
            "contact_now": sum(1 for row in shortlist if row["decision"] == "contact_now"),
        },
    }


if __name__ == "__main__":
    import sys

    records = json.loads(open(sys.argv[1], encoding="utf-8").read())
    print(json.dumps(plan(records, sys.argv[2], *map(int, sys.argv[3:6])), indent=2))
```

Run it as `python3 score.py courses.json YYYY-MM-DD <max_courses> <searches> <opened>` after
copying the block to a scratch file, or import `plan` from it.
