"""Known defects and valid exceptions, independent of paid crawl results."""

from tin_lite.organic_audit import (
    AUDIT_POLICY,
    V8_AUDIT_POLICY,
    normalize_pages,
    technical_findings,
)


def test_canonical_preconditions_and_missing_context_do_not_become_passes():
    raw = [
        {
            "url": "https://example.com/missing",
            "resource_type": "html",
            "checks": {"canonical": True, "no_title": True, "no_description": False},
        },
        {
            "url": "https://example.com/tracking",
            "resource_type": "html",
            "checks": {"canonical": False, "no_title": True, "no_description": False},
        },
        {
            "url": "https://example.com/unknown",
            "resource_type": "html",
            "checks": {"no_title": False},
        },
    ]
    pages = normalize_pages(raw, "example.com", policy_version=AUDIT_POLICY["version"])
    findings, rows = technical_findings(
        pages, "example.com", policy_version=AUDIT_POLICY["version"]
    )
    title = next(r for r in rows if r["check_id"] == "metadata.title_missing")
    assert title["outcomes"] == {"problem": 1, "pass": 0, "not_applicable": 1, "unknown": 1}
    assert next(f for f in findings if f["check_id"] == title["check_id"])["urls"] == [
        "https://example.com/missing"
    ]
    assert (
        next(r for r in rows if r["check_id"] == "discovery.possible_orphan")["outcomes"]["unknown"]
        == 3
    )
    # A replay pinned to v8 retains the old meaning and artifact shape.
    old = normalize_pages(raw, "example.com", policy_version=V8_AUDIT_POLICY["version"])
    _, legacy = technical_findings(old, "example.com", policy_version=V8_AUDIT_POLICY["version"])
    assert "provider_context" not in old[0]
    assert next(r for r in legacy if r["check_id"] == title["check_id"]) == {
        "check_id": title["check_id"],
        "observed_pages": 3,
        "status": "observed",
    }


def test_known_link_redirect_and_http_defects_survive_metadata_exceptions():
    pages = normalize_pages(
        [
            {
                "url": "https://example.com/a",
                "resource_type": "html",
                "checks": {
                    "canonical": False,
                    "no_title": False,
                    "broken_links": True,
                    "redirect_chain": True,
                    "is_4xx_code": True,
                    "is_5xx_code": False,
                },
            }
        ],
        "example.com",
        policy_version=AUDIT_POLICY["version"],
    )
    findings, coverage = technical_findings(
        pages, "example.com", policy_version=AUDIT_POLICY["version"]
    )
    assert {f["check_id"] for f in findings} == {
        "links.broken",
        "http.redirect_chain",
        "http.client_error",
    }
    assert all(sum(row["outcomes"].values()) == len(pages) for row in coverage)


def test_search_console_counts_are_scoped_and_do_not_imply_missing_pages_are_unindexed():
    import pytest

    from tin_lite.organic_audit import search_console_pages

    result = search_console_pages(
        {
            "rows": [
                {"keys": ["https://example.com/a"], "clicks": 3, "impressions": 20},
                {"keys": ["https://other.example/a"], "clicks": 10, "impressions": 50},
            ]
        },
        "example.com",
    )
    assert result["pages"] == [{"url": "https://example.com/a", "clicks": 3, "impressions": 20}]
    assert "not proven unindexed" in result["note"]
    with pytest.raises(ValueError):
        search_console_pages(
            {"rows": [{"keys": ["https://example.com/a"], "clicks": True, "impressions": 3}]},
            "example.com",
        )


def test_broken_resources_are_retained_only_by_the_new_policy():
    row = {"url": "https://example.com/missing", "resource_type": "broken", "status_code": 404}
    assert normalize_pages([row], "example.com", policy_version=V8_AUDIT_POLICY["version"]) == []
    pages = normalize_pages([row], "example.com", policy_version=AUDIT_POLICY["version"])
    findings, _ = technical_findings(pages, "example.com", policy_version=AUDIT_POLICY["version"])
    assert [f["check_id"] for f in findings] == ["http.client_error"]
