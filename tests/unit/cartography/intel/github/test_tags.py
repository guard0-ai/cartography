from unittest.mock import patch

import cartography.intel.github.tags as tags_module
from cartography.intel.github.tags import get_repo_tags
from cartography.intel.github.tags import transform_repo_tags

REPO_URL = "https://github.com/example-org/example-repo"


def test_transform_repo_tags_lightweight_tag():
    raw = [
        {
            "name": "v1.2.3",
            "target": {
                "__typename": "Commit",
                "oid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            },
        },
    ]

    result = transform_repo_tags(raw, REPO_URL)

    assert result == [
        {
            "id": f"{REPO_URL}#refs/tags/v1.2.3",
            "name": "v1.2.3",
            "commit_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "repo_url": REPO_URL,
        },
    ]


def test_transform_repo_tags_annotated_tag_resolves_inner_commit():
    raw = [
        {
            "name": "v2.0.0",
            "target": {
                "__typename": "Tag",
                "oid": "tag-object-oid",
                "target": {
                    "oid": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                },
            },
        },
    ]

    result = transform_repo_tags(raw, REPO_URL)

    assert len(result) == 1
    assert result[0]["commit_sha"] == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def test_transform_repo_tags_skips_malformed_entries():
    raw = [
        {"name": "no-target"},
        {"target": {"__typename": "Commit", "oid": "cccc"}},
        {
            "name": "annotated-without-commit",
            "target": {"__typename": "Tag", "oid": "dddd"},
        },
        None if False else {},
    ]

    assert transform_repo_tags(raw, REPO_URL) == []


def test_get_repo_tags_paginates_until_last_page():
    pages = [
        {
            "data": {
                "organization": {
                    "repository": {
                        "refs": {
                            "pageInfo": {"endCursor": "c1", "hasNextPage": True},
                            "nodes": [{"name": "v0.0.2", "target": {"oid": "a1"}}],
                        },
                    },
                },
            },
        },
        {
            "data": {
                "organization": {
                    "repository": {
                        "refs": {
                            "pageInfo": {"endCursor": None, "hasNextPage": False},
                            "nodes": [{"name": "v0.0.1", "target": {"oid": "a2"}}],
                        },
                    },
                },
            },
        },
    ]

    with patch.object(tags_module, "fetch_page", side_effect=pages) as mock_fetch:
        result = get_repo_tags("token", "https://api.github.com/graphql", "org", "repo")

    assert [t["name"] for t in result] == ["v0.0.2", "v0.0.1"]
    assert mock_fetch.call_count == 2


def test_get_repo_tags_stops_at_page_cap():
    page = {
        "data": {
            "organization": {
                "repository": {
                    "refs": {
                        "pageInfo": {"endCursor": "c", "hasNextPage": True},
                        "nodes": [{"name": "v1", "target": {"oid": "a"}}],
                    },
                },
            },
        },
    }

    with patch.object(tags_module, "fetch_page", return_value=page) as mock_fetch:
        result = get_repo_tags("token", "https://api.github.com/graphql", "org", "repo")

    assert mock_fetch.call_count == tags_module.MAX_TAG_PAGES_PER_REPO
    assert len(result) == tags_module.MAX_TAG_PAGES_PER_REPO


def test_get_repo_tags_missing_repository_returns_empty():
    with patch.object(
        tags_module, "fetch_page", return_value={"data": {"organization": {}}}
    ):
        assert (
            get_repo_tags("token", "https://api.github.com/graphql", "org", "repo")
            == []
        )
