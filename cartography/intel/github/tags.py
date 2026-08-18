"""
GitHub Git Tags Intelligence Module.

Loads ``GitHubTag`` nodes recording each repository's Git tags and the
commits they point at. The supply-chain matcher resolves container image
tags that embed a version string or commit SHA against these nodes, so
tags are fetched newest-first: recent tags are the ones container images
in active use were built from.
"""

import logging
from typing import Any

import neo4j

from cartography.client.core.tx import load
from cartography.graph.job import GraphJob
from cartography.intel.github.util import fetch_page
from cartography.models.github.tags import GitHubTagSchema
from cartography.util import timeit

logger = logging.getLogger(__name__)

# Newest-first pages of 100 per repository. Bounds API cost on repositories
# with very long release histories while covering every tag a still-deployed
# image was plausibly built from.
MAX_TAG_PAGES_PER_REPO = 5

GITHUB_REPO_TAGS_PAGINATED_GRAPHQL = """
    query($login: String!, $repo: String!, $cursor: String) {
        organization(login: $login) {
            repository(name: $repo) {
                name
                url
                refs(
                    refPrefix: "refs/tags/",
                    first: 100,
                    after: $cursor,
                    orderBy: {field: TAG_COMMIT_DATE, direction: DESC}
                ) {
                    pageInfo {
                        endCursor
                        hasNextPage
                    }
                    nodes {
                        name
                        target {
                            __typename
                            oid
                            ... on Tag {
                                target {
                                    oid
                                }
                            }
                        }
                    }
                }
            }
        }
        rateLimit {
            limit
            cost
            remaining
            resetAt
        }
    }
"""


@timeit
def get_repo_tags(
    token: str,
    api_url: str,
    organization: str,
    repo_name: str,
) -> list[dict[str, Any]]:
    """
    Retrieve Git tags for one repository, newest first.

    :param token: The Github API token as string.
    :param api_url: The Github v4 API endpoint as string.
    :param organization: The name of the target Github organization as string.
    :param repo_name: The name of the target Github repository as string.
    :return: A list of raw tag nodes from the GraphQL API.
    """
    all_tags: list[dict[str, Any]] = []
    cursor = None

    for _ in range(MAX_TAG_PAGES_PER_REPO):
        response = fetch_page(
            token,
            api_url,
            organization,
            GITHUB_REPO_TAGS_PAGINATED_GRAPHQL,
            cursor,
            repo=repo_name,
        )

        repo_data = response.get("data", {}).get("organization", {}).get("repository")
        if not repo_data or not repo_data.get("refs"):
            break

        refs = repo_data["refs"]
        all_tags.extend(node for node in refs.get("nodes", []) if node)

        page_info = refs.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")

    return all_tags


def transform_repo_tags(
    raw_tags: list[dict[str, Any]],
    repo_url: str,
) -> list[dict[str, Any]]:
    """
    Transform raw GraphQL tag nodes into GitHubTag node dicts.

    Annotated tags point at a Tag object wrapping the commit; lightweight
    tags point at the commit directly. Both resolve to the commit SHA.
    """
    tags: list[dict[str, Any]] = []
    for raw in raw_tags:
        name = raw.get("name")
        target = raw.get("target") or {}
        if not name or not target.get("oid"):
            continue
        if target.get("__typename") == "Tag":
            inner = target.get("target") or {}
            commit_sha = inner.get("oid")
        else:
            commit_sha = target.get("oid")
        if not commit_sha:
            continue
        tags.append(
            {
                "id": f"{repo_url}#refs/tags/{name}",
                "name": name,
                "commit_sha": commit_sha,
                "repo_url": repo_url,
            },
        )
    return tags


@timeit
def load_repo_tags(
    neo4j_session: neo4j.Session,
    tags: list[dict[str, Any]],
    org_url: str,
    update_tag: int,
) -> None:
    load(
        neo4j_session,
        GitHubTagSchema(),
        tags,
        lastupdated=update_tag,
        org_url=org_url,
    )


@timeit
def cleanup_repo_tags(
    neo4j_session: neo4j.Session,
    common_job_parameters: dict[str, Any],
) -> None:
    GraphJob.from_node_schema(
        GitHubTagSchema(),
        common_job_parameters,
    ).run(neo4j_session)


@timeit
def sync_repo_tags(
    neo4j_session: neo4j.Session,
    token: str,
    api_url: str,
    organization: str,
    repos: list[dict[str, Any]],
    update_tag: int,
    common_job_parameters: dict[str, Any],
) -> None:
    """
    Sync Git tags for every repository in the organization.

    :param repos: Repository dicts with at least 'name' and 'url' keys, as
        returned by cartography.intel.github.repos.get.
    """
    org_url = f"https://github.com/{organization}"
    all_tags: list[dict[str, Any]] = []
    for repo in repos:
        repo_name = repo.get("name")
        repo_url = repo.get("url")
        if not repo_name or not repo_url:
            continue
        raw_tags = get_repo_tags(token, api_url, organization, repo_name)
        all_tags.extend(transform_repo_tags(raw_tags, repo_url))

    logger.info(
        "Loading %d Git tags across %d repositories for %s",
        len(all_tags),
        len(repos),
        organization,
    )
    if all_tags:
        load_repo_tags(neo4j_session, all_tags, org_url, update_tag)
    cleanup_params = dict(common_job_parameters)
    cleanup_params["org_url"] = org_url
    cleanup_repo_tags(neo4j_session, cleanup_params)
