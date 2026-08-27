import base64
import logging
import re
from typing import Any

import neo4j
import requests

from cartography.analysis.ontology.analysis import SUPPLY_CHAIN_SOURCE_FILE
from cartography.client.core.tx import load_matchlinks
from cartography.graph.job import GraphJob
from cartography.intel.github.util import call_github_rest_api
from cartography.intel.supply_chain import ContainerImage
from cartography.intel.supply_chain import convert_layer_history_records
from cartography.intel.supply_chain import get_unmatched_gcp_images_with_history
from cartography.intel.supply_chain import get_unmatched_scaleway_images_with_history
from cartography.intel.supply_chain import match_images_to_dockerfiles
from cartography.intel.supply_chain import parse_dockerfile_info
from cartography.intel.supply_chain import transform_matches_for_matchlink
from cartography.models.github.packaged_matchlink import (
    GitHubRepoDockerfilePackagedFromMatchLink,
)
from cartography.models.github.packaged_matchlink import (
    GitHubRepoPackageOwnerPackagedFromMatchLink,
)
from cartography.models.github.packaged_matchlink import (
    GitHubRepoProvenancePackagedFromMatchLink,
)
from cartography.models.github.packaged_matchlink import (
    GitHubRepoTagRefPackagedFromMatchLink,
)
from cartography.models.github.packaged_matchlink import (
    ImagePackagedByWorkflowMatchLink,
)
from cartography.tenancy import current_guard0_org_id
from cartography.util import run_typed_analysis_job
from cartography.util import timeit

logger = logging.getLogger(__name__)

_DEFAULT_IMAGE_LIMIT: int | None = None
_DEFAULT_MIN_MATCH_CONFIDENCE: float = 0.5

# Confidence applied to the package-owner fallback (GHCR HAS_PACKAGE -> repo).
# The link is deterministic (one repo per package per the GitHub API) but the
# repo isn't guaranteed to be the build source — it's the repo that owns the
# package, which usually but not always matches the build source.
_PACKAGE_OWNER_FALLBACK_CONFIDENCE: float = 0.6

# Confidences for image-tag -> Git-ref matching. A commit SHA embedded in an
# image tag is near-unforgeable evidence; a version string matching a Git tag
# in exactly one repo is strong; a version string found in several repos that
# a registry/repo name comparison disambiguates is weaker but still specific.
_TAG_SHA_CONFIDENCE: float = 0.95
_TAG_SEMVER_UNIQUE_CONFIDENCE: float = 0.9
_TAG_SEMVER_NAME_TIEBREAK_CONFIDENCE: float = 0.75

# A version string at the start of an image tag, e.g. "1.2.3" or "v1.2.3-rc1".
_SEMVER_IN_TAG_RE = re.compile(r"^v?(\d+\.\d+\.\d+)")
# A hex fragment that plausibly embeds a commit SHA, e.g. "main-a1b2c3d".
# Requires at least one a-f character so numeric build IDs don't false-match.
_HEX_FRAGMENT_RE = re.compile(r"(?<![0-9a-fA-F])([0-9a-f]{7,40})(?![0-9a-fA-F])")

# Ceiling on per-fragment commit-existence lookups against the GitHub API.
# Candidate repos are ordered by registry/repo name similarity first, so the
# true repo is nearly always probed within the first few requests.
_MAX_COMMIT_LOOKUP_REPOS: int = 8


def _get_unmatched_ghcr_image_owner_repos(
    neo4j_session: neo4j.Session,
    organization: str,
    update_tag: int,
) -> list[dict[str, Any]]:
    """
    Return ``[{image_digest, repo_url}, ...]`` for every GHCR image scoped to
    ``organization`` that has no ``PACKAGED_FROM`` relationship after the
    higher-confidence steps ran, but whose owning ``GitHubPackage`` has a
    single ``HAS_PACKAGE`` link back to a ``GitHubRepository``.
    """
    # Filter on the generic ``Image`` label so manifest lists (multi-arch
    # indexes) are excluded — only platform-specific images claim a build repo.
    # Match against PACKAGED_FROM relationships from THIS run only (lastupdated
    # = update_tag); stale rels from previous runs must not block the fallback,
    # they will be reaped by the cleanup that runs after this step.
    query = """
    MATCH (org:GitHubOrganization {guard0_org_id: $guard0_org_id, id: $org_url})
          -[:RESOURCE {guard0_org_id: $guard0_org_id}]->
          (img:GitHubContainerImage {guard0_org_id: $guard0_org_id})
    WHERE img:Image
      AND NOT exists((img)-[:PACKAGED_FROM {
          guard0_org_id: $guard0_org_id,
          lastupdated: $update_tag
      }]->())
    MATCH (pkg:GitHubPackage {guard0_org_id: $guard0_org_id})
          -[:HAS_IMAGE {guard0_org_id: $guard0_org_id}]->(img)
    MATCH (repo:GitHubRepository {guard0_org_id: $guard0_org_id})
          -[:HAS_PACKAGE {guard0_org_id: $guard0_org_id}]->(pkg)
    WITH img, collect(DISTINCT repo.id) AS repo_ids
    WHERE size(repo_ids) = 1
    RETURN img.digest AS image_digest, repo_ids[0] AS repo_url
    """
    org_url = f"https://github.com/{organization}"
    result = neo4j_session.run(
        query,
        org_url=org_url,
        update_tag=update_tag,
        guard0_org_id=current_guard0_org_id(),
    )
    return [
        {"image_digest": record["image_digest"], "repo_url": record["repo_url"]}
        for record in result
        if record["image_digest"] and record["repo_url"]
    ]


@timeit
def get_unmatched_image_tag_rows(
    neo4j_session: neo4j.Session,
    organization: str,
    update_tag: int,
) -> list[dict[str, Any]]:
    """
    Query (image digest, image tag, registry name) rows for images that no
    earlier matching stage has claimed in this sync iteration. Uses the
    generic ontology labels so it works across registries, and applies the
    same cross-organization guard as the Dockerfile stage. Requires only
    registry metadata: no layer history or provenance fields.
    """
    query = """
        MATCH (img:Image {guard0_org_id: $guard0_org_id})
              <-[:IMAGE {guard0_org_id: $guard0_org_id}]-
              (repo_img:ImageTag {guard0_org_id: $guard0_org_id})
              <-[:REPO_IMAGE {guard0_org_id: $guard0_org_id}]-
              (repo:ContainerRegistry {guard0_org_id: $guard0_org_id})
        WHERE repo_img.tag IS NOT NULL
          AND NOT exists((img)-[:PACKAGED_FROM {
              guard0_org_id: $guard0_org_id,
              lastupdated: $update_tag
          }]->())
          AND (
              NOT exists((img)-[:PACKAGED_FROM {_sub_resource_label: 'GitHubOrganization'}]->())
              OR exists((img)-[:PACKAGED_FROM {_sub_resource_id: $organization}]->())
          )
        RETURN DISTINCT
            img.digest AS digest,
            repo_img.tag AS tag,
            repo.name AS registry_name
    """
    result = neo4j_session.run(
        query,
        guard0_org_id=current_guard0_org_id(),
        update_tag=update_tag,
        organization=organization,
    )
    return [dict(record) for record in result]


def get_org_git_tags(
    neo4j_session: neo4j.Session,
    organization: str,
) -> list[dict[str, Any]]:
    """
    Query the organization's Git tags (name, commit SHA, owning repo URL)
    loaded by cartography.intel.github.tags.
    """
    query = """
        MATCH (org:GitHubOrganization {guard0_org_id: $guard0_org_id, id: $org_url})
              -[:RESOURCE {guard0_org_id: $guard0_org_id}]->
              (tag:GitHubTag {guard0_org_id: $guard0_org_id})
        RETURN tag.name AS name, tag.commit_sha AS commit_sha, tag.repo_url AS repo_url
    """
    result = neo4j_session.run(
        query,
        guard0_org_id=current_guard0_org_id(),
        org_url=f"https://github.com/{organization}",
    )
    return [dict(record) for record in result]


def _registry_repo_match_rank(registry_name: str | None, repo_url: str) -> int | None:
    """
    Rank how strongly a container registry name refers to a Git repository,
    strongest first: 0 the names are equal, 1 one name is a prefix of the
    other (registry "billing-service", repo ".../billing"), 2 one name's
    hyphen/underscore tokens are a subset of the other's (registry
    "worker-billing", repo ".../billing"). None means no correspondence.
    Only the registry name's last path segment is compared, because registry
    namespaces ("acme-prod-docker/billing") describe the environment, not the
    service.
    """
    if not registry_name:
        return None
    registry = registry_name.lower().rsplit("/", 1)[-1]
    repo_name = repo_url.rstrip("/").rsplit("/", 1)[-1].lower()
    if registry == repo_name:
        return 0
    if registry.startswith(repo_name) or repo_name.startswith(registry):
        return 1
    registry_tokens = set(re.split(r"[-_]", registry)) - {""}
    repo_tokens = set(re.split(r"[-_]", repo_name)) - {""}
    if registry_tokens and repo_tokens:
        if registry_tokens <= repo_tokens or repo_tokens <= registry_tokens:
            return 2
    return None


def _registry_repo_name_matches(registry_name: str | None, repo_url: str) -> bool:
    """Whether a container registry name plausibly refers to a Git repository."""
    return _registry_repo_match_rank(registry_name, repo_url) is not None


def match_image_tags_to_git_refs(
    image_rows: list[dict[str, Any]],
    git_tags: list[dict[str, Any]],
    commit_repo_lookup: Any = None,
) -> list[dict[str, Any]]:
    """
    Resolve image tags to (repository, Git ref) matches.

    For each image tag, evidence is tried in order of specificity:
    1. tag_sha: a hex fragment in the image tag names a commit. Resolved
       against Git-tag commit SHAs first, then (when provided) through
       ``commit_repo_lookup``, a callable ``(fragment, ordered_repo_urls) ->
       list[matching_repo_urls]`` backed by the GitHub commits API.
    2. tag_semver: a version at the start of the image tag equals a Git tag
       (ignoring a leading "v"). Unique across repos matches directly; a
       registry/repo name comparison breaks ties.

    Returns matchlink rows for GitHubRepoTagRefPackagedFromMatchLink. Emits at
    most one match per image digest; ambiguous evidence produces no match.
    """
    tags_by_version: dict[str, set[str]] = {}
    repos_by_sha: dict[str, set[str]] = {}
    all_repo_urls: set[str] = set()
    for git_tag in git_tags:
        name, sha, repo_url = (
            git_tag.get("name"),
            git_tag.get("commit_sha"),
            git_tag.get("repo_url"),
        )
        if not name or not repo_url:
            continue
        all_repo_urls.add(repo_url)
        tags_by_version.setdefault(name.lstrip("vV"), set()).add(repo_url)
        if sha:
            repos_by_sha.setdefault(sha, set()).add(repo_url)

    matches: dict[str, dict[str, Any]] = {}
    fragment_cache: dict[str, list[str]] = {}

    def emit(
        digest: str, repo_url: str, method: str, ref: str, confidence: float
    ) -> None:
        matches[digest] = {
            "image_digest": digest,
            "repo_url": repo_url,
            "match_method": method,
            "matched_git_ref": ref,
            "confidence": confidence,
            "dockerfile_path": None,
            "matched_commands": 0,
            "total_commands": 0,
            "command_similarity": 0.0,
        }

    for row in image_rows:
        digest, image_tag = row.get("digest"), row.get("tag")
        if not digest or not image_tag or digest in matches:
            continue
        registry_name = row.get("registry_name")

        hex_match = _HEX_FRAGMENT_RE.search(image_tag)
        fragment = hex_match.group(1) if hex_match else None
        if fragment and not any(c in "abcdef" for c in fragment):
            fragment = None
        if fragment:
            tagged = {
                url
                for sha, urls in repos_by_sha.items()
                if sha.startswith(fragment)
                for url in urls
            }
            if len(tagged) == 1:
                emit(
                    digest,
                    next(iter(tagged)),
                    "tag_sha",
                    fragment,
                    _TAG_SHA_CONFIDENCE,
                )
                continue
            if not tagged and commit_repo_lookup is not None:
                if fragment not in fragment_cache:
                    def lookup_order(url: str) -> tuple[bool, int, str]:
                        rank = _registry_repo_match_rank(registry_name, url)
                        return (rank is None, rank if rank is not None else 0, url)

                    ordered = sorted(all_repo_urls, key=lookup_order)[
                        :_MAX_COMMIT_LOOKUP_REPOS
                    ]
                    fragment_cache[fragment] = commit_repo_lookup(fragment, ordered)
                hits = fragment_cache[fragment]
                if len(hits) == 1:
                    emit(digest, hits[0], "tag_sha", fragment, _TAG_SHA_CONFIDENCE)
                    continue

        semver_match = _SEMVER_IN_TAG_RE.match(image_tag)
        if semver_match:
            version = semver_match.group(1)
            candidates = tags_by_version.get(version, set())
            if len(candidates) == 1:
                emit(
                    digest,
                    next(iter(candidates)),
                    "tag_semver",
                    version,
                    _TAG_SEMVER_UNIQUE_CONFIDENCE,
                )
                continue
            if len(candidates) > 1:
                ranked: dict[int, set[str]] = {}
                for url in candidates:
                    rank = _registry_repo_match_rank(registry_name, url)
                    if rank is not None:
                        ranked.setdefault(rank, set()).add(url)
                if ranked:
                    # The strongest populated rank decides; weaker ranks are
                    # never consulted once a stronger one names any candidate,
                    # and a rank naming several candidates is ambiguous.
                    named = ranked[min(ranked)]
                    if len(named) == 1:
                        emit(
                            digest,
                            next(iter(named)),
                            "tag_semver",
                            version,
                            _TAG_SEMVER_NAME_TIEBREAK_CONFIDENCE,
                        )

    return list(matches.values())


def get_unmatched_container_images_with_history(
    neo4j_session: neo4j.Session,
    organization: str,
    update_tag: int,
    limit: int | None = None,
) -> list[ContainerImage]:
    """
    Query container images not yet matched by provenance in this sync iteration.

    Uses the generic ontology labels (Image, ImageTag, ImageLayer, ContainerRegistry)
    which work across different registries (ECR, GCR, etc.).

    Returns one image per registry repository (preferring 'latest' tag, then most recently pushed).
    Excludes images that:
    - Already have a PACKAGED_FROM created in this sync iteration (by provenance matching)
    - Already claimed by a different GitHub organization (prevents cross-org duplication
      and cleanup issues when orgs don't run at the same time)

    :param neo4j_session: Neo4j session
    :param organization: The GitHub organization name, used for cross-org scoping
    :param update_tag: The current sync update tag
    :param limit: Optional limit on number of images to return
    :return: List of ContainerImage objects with layer history populated
    """
    query = """
        MATCH (img:Image {guard0_org_id: $guard0_org_id})
              <-[:IMAGE {guard0_org_id: $guard0_org_id}]-
              (repo_img:ImageTag {guard0_org_id: $guard0_org_id})
              <-[:REPO_IMAGE {guard0_org_id: $guard0_org_id}]-
              (repo:ContainerRegistry {guard0_org_id: $guard0_org_id})
        WHERE img.layer_diff_ids IS NOT NULL
          AND size(img.layer_diff_ids) > 0
          AND NOT exists((img)-[:PACKAGED_FROM {
              guard0_org_id: $guard0_org_id,
              lastupdated: $update_tag
          }]->())
          AND (
              NOT exists((img)-[:PACKAGED_FROM {_sub_resource_label: 'GitHubOrganization'}]->())
              OR exists((img)-[:PACKAGED_FROM {_sub_resource_id: $organization}]->())
          )
        WITH repo, img, repo_img
        ORDER BY
            CASE WHEN repo_img.tag = 'latest' THEN 0 ELSE 1 END,
            repo_img.image_pushed_at DESC
        WITH repo, collect({
            digest: img.digest,
            uri: repo_img.uri,
            repo_uri: repo.uri,
            repo_name: repo.name,
            tag: repo_img.tag,
            layer_diff_ids: img.layer_diff_ids,
            type: img.type,
            architecture: img.architecture,
            os: img.os
        })[0] AS best
        // Get layer history for each best image
        WITH best
        UNWIND range(0, size(best.layer_diff_ids) - 1) AS idx
        WITH best, best.layer_diff_ids[idx] AS diff_id, idx
        OPTIONAL MATCH (layer:ImageLayer {
            guard0_org_id: $guard0_org_id,
            diff_id: diff_id
        })
        WITH best, idx, {
            diff_id: diff_id,
            history: layer.history,
            is_empty: layer.is_empty
        } AS layer_info
        ORDER BY idx
        WITH best, collect(layer_info) AS layer_history
        RETURN
            best.digest AS digest,
            best.uri AS uri,
            best.repo_uri AS repo_uri,
            best.repo_name AS repo_name,
            best.tag AS tag,
            best.layer_diff_ids AS layer_diff_ids,
            best.type AS type,
            best.architecture AS architecture,
            best.os AS os,
            layer_history
    """

    if limit:
        query += f" LIMIT {limit}"

    result = neo4j_session.run(
        query,
        update_tag=update_tag,
        organization=organization,
        guard0_org_id=current_guard0_org_id(),
    )
    images = []

    for record in result:
        layer_history = convert_layer_history_records(record["layer_history"])

        images.append(
            ContainerImage(
                digest=record["digest"],
                uri=record["uri"] or "",
                registry_id=record["repo_uri"] or None,
                display_name=record["repo_name"] or None,
                tag=record["tag"],
                layer_diff_ids=record["layer_diff_ids"] or [],
                image_type=record["type"],
                architecture=record["architecture"],
                os=record["os"],
                layer_history=layer_history,
            )
        )

    logger.info(
        "Found %d container images with layer history (one per repository)",
        len(images),
    )
    return images


@timeit
def search_dockerfiles_in_org(
    token: str,
    org: str,
    base_url: str = "https://api.github.com",
) -> list[dict[str, Any]]:
    """
    Search for all Dockerfile-related files in an organization using GitHub Code Search API.

    This performs a single org-wide search instead of per-repo queries, which is more
    efficient and reduces API rate limit consumption.

    The search is case-insensitive and matches files containing "dockerfile" in the name.
    This includes: Dockerfile, dockerfile, DOCKERFILE, Dockerfile.*, *.dockerfile, etc.

    :param token: The GitHub API token
    :param org: The organization name
    :param base_url: The base URL for the GitHub API
    :return: List of file items from the search results (with pagination)
    """
    query = f"filename:dockerfile org:{org}"

    all_items: list[dict[str, Any]] = []
    page = 1
    max_pages = 10  # GitHub limits to 1000 results (10 pages * 100 per_page)

    while page <= max_pages:
        params = {
            "q": query,
            "per_page": 100,
            "page": page,
        }

        try:
            response = call_github_rest_api("/search/code", token, base_url, params)
            items: list[dict[str, Any]] = response.get("items", [])
            all_items.extend(items)

            # Check if there are more pages
            total_count = response.get("total_count", 0)
            if len(all_items) >= total_count or len(items) < 100:
                break

            page += 1

        except requests.exceptions.HTTPError as e:
            # Only 422 (validation error for empty search results) is acceptable
            # Other errors (403 rate limit, 429 too many requests) should propagate
            if e.response is not None and e.response.status_code == 422:
                logger.debug(
                    "Search validation error for org %s (may have no results): %s",
                    org,
                    e.response.status_code,
                )
                break
            raise

    logger.info("Found %d dockerfile(s) in org %s", len(all_items), org)
    return all_items


def get_file_content(
    token: str,
    owner: str,
    repo: str,
    path: str,
    ref: str = "HEAD",
    base_url: str = "https://api.github.com",
) -> str | None:
    """
    Download the content of a file from a GitHub repository using the Contents API.

    :param token: The GitHub API token
    :param owner: The repository owner
    :param repo: The repository name
    :param path: The path to the file within the repository
    :param ref: The git reference (branch, tag, or commit SHA) to get the file from
    :param base_url: The base URL for the GitHub API
    :return: The file content as a string, or None if retrieval fails
    """
    endpoint = f"/repos/{owner}/{repo}/contents/{path}"
    params = {"ref": ref}

    try:
        response = call_github_rest_api(endpoint, token, base_url, params)

        # The content is base64 encoded
        if response.get("encoding") == "base64":
            content_b64 = response.get("content", "")
            # GitHub returns content with newlines for readability, remove them
            content_b64 = content_b64.replace("\n", "")
            content = base64.b64decode(content_b64).decode("utf-8")
            return content

        # If not base64 encoded, try to get raw content
        return response.get("content")

    except requests.exceptions.HTTPError as e:
        # 404: File not found, 403: No access, 422: Validation error
        # Note: 429 (rate limit) should propagate to trigger retry/failure
        if e.response is not None and e.response.status_code in (403, 404, 422):
            logger.debug(
                "Cannot fetch file %s/%s/%s: %d",
                owner,
                repo,
                path,
                e.response.status_code,
            )
            return None
        raise


def _extract_repo_info(
    repo: dict[str, Any],
) -> tuple[str | None, str | None, str | None]:
    """Extract owner, repo_name, and repo_url from a repository dict."""
    owner = None
    repo_name = None
    repo_url = None

    if isinstance(repo.get("owner"), dict):
        owner = repo["owner"].get("login")
    elif "nameWithOwner" in repo:
        name_with_owner = repo["nameWithOwner"]
        if "/" in name_with_owner:
            owner = name_with_owner.split("/")[0]

    repo_name = repo.get("name")
    repo_url = repo.get("url")

    return owner, repo_name, repo_url


def _build_dockerfile_info(
    item: dict[str, Any],
    content: str,
    repo_url: str | None,
    full_name: str,
) -> dict[str, Any] | None:
    """Build dockerfile info dict with parsed content."""
    path = item.get("path", "")

    info = parse_dockerfile_info(content, path, full_name)
    if info is None:
        return None
    info["repo_url"] = repo_url
    info["repo_name"] = full_name
    info["sha"] = item.get("sha")
    info["html_url"] = item.get("html_url")
    # Used by the shared matching algorithm
    info["source_repo_id"] = repo_url
    return info


@timeit
def get_dockerfiles_for_repos(
    token: str,
    repos: list[dict[str, Any]],
    org: str,
    base_url: str = "https://api.github.com",
) -> list[dict[str, Any]]:
    """
    Search and download Dockerfiles for a list of repositories using org-wide search.

    :param token: The GitHub API token
    :param repos: List of repository dictionaries (from GitHub API or transformed data)
    :param org: Organization name for org-wide search
    :param base_url: The base URL for the GitHub API
    :return: List of dictionaries containing repo info, file path, and content
    """
    if not repos:
        return []

    repo_info_map: dict[str, tuple[str, str, str | None]] = {}

    for repo in repos:
        owner, repo_name, repo_url = _extract_repo_info(repo)
        if not owner or not repo_name:
            continue
        full_name = f"{owner}/{repo_name}"
        repo_info_map[full_name] = (owner, repo_name, repo_url)

    if not repo_info_map:
        logger.warning("No valid repositories found")
        return []

    dockerfile_items = search_dockerfiles_in_org(token, org, base_url)

    items_by_repo: dict[str, list[dict[str, Any]]] = {}
    for item in dockerfile_items:
        repo_info = item.get("repository", {})
        full_name = repo_info.get("full_name", "")
        if full_name in repo_info_map:
            items_by_repo.setdefault(full_name, []).append(item)

    all_dockerfiles: list[dict[str, Any]] = []
    for full_name, items in items_by_repo.items():
        owner, repo_name, repo_url = repo_info_map[full_name]
        for item in items:
            path = item.get("path")
            if not path:
                continue
            content = get_file_content(token, owner, repo_name, path, base_url=base_url)
            if content:
                dockerfile_info = _build_dockerfile_info(
                    item, content, repo_url, full_name
                )
                if dockerfile_info is not None:
                    all_dockerfiles.append(dockerfile_info)

    logger.info(
        "Retrieved %d dockerfile(s) from %d repositories",
        len(all_dockerfiles),
        len(repo_info_map),
    )
    return all_dockerfiles


@timeit
def sync(
    neo4j_session: neo4j.Session,
    token: str,
    api_url: str,
    organization: str,
    update_tag: int,
    common_job_parameters: dict[str, Any],
    repos: list[dict[str, Any]],
    workflows: list[dict[str, Any]] | None = None,
    image_limit: int | None = _DEFAULT_IMAGE_LIMIT,
    min_match_confidence: float = _DEFAULT_MIN_MATCH_CONFIDENCE,
) -> None:
    """
    Sync supply chain relationships for a GitHub organization.

    Uses a five-stage matching approach:
    1. PACKAGED_BY: Workflow provenance (Image -> GitHubWorkflow)
    2. PACKAGED_FROM (provenance): SLSA provenance-based matching (100% confidence)
    3. PACKAGED_FROM (tag_sha / tag_semver): image tags that embed a commit SHA
       or version resolved against the org's Git refs — registry metadata only,
       so it covers images whose config blobs are not readable
    4. PACKAGED_FROM (dockerfile): Dockerfile command matching for unmatched images
    5. PACKAGED_FROM (package_owner_repo): For any GHCR image still without a
       PACKAGED_FROM, link it to the repo that owns its GitHubPackage when the
       HAS_PACKAGE relation is unique. Lower confidence than the previous
       stages but deterministic (one repo per package per the GitHub API).

    Only images without an existing PACKAGED_FROM relationship go through the
    expensive Dockerfile analysis step.

    :param neo4j_session: Neo4j session for querying container images
    :param token: The GitHub API token
    :param api_url: The GitHub API URL (typically the GraphQL endpoint)
    :param organization: The GitHub organization name
    :param update_tag: The update timestamp tag
    :param common_job_parameters: Common job parameters
    :param repos: List of repository dictionaries to search for Dockerfiles
    :param workflows: List of workflow dicts (with repo_url and path) from actions sync
    :param image_limit: Optional limit on number of images to process
    :param min_match_confidence: Minimum confidence threshold for matches (default: 0.5)
    """
    logger.info("Starting supply chain sync for %s", organization)

    # Extract base REST API URL from the GraphQL URL
    base_url = api_url
    if base_url.endswith("/graphql"):
        base_url = base_url[:-8]

    # 1. PACKAGED_BY matchlinks (workflow provenance — no pre-query needed)
    workflow_data = [
        {"repo_url": wf["repo_url"], "workflow_path": wf["path"]}
        for wf in (workflows or [])
        if wf.get("repo_url") and wf.get("path")
    ]
    if workflow_data:
        logger.info("Matching PACKAGED_BY for %d workflows", len(workflow_data))
        load_matchlinks(
            neo4j_session,
            ImagePackagedByWorkflowMatchLink(),
            workflow_data,
            lastupdated=update_tag,
            _sub_resource_label="GitHubOrganization",
            _sub_resource_id=organization,
        )

    # 2. PACKAGED_FROM matchlinks (SLSA provenance — no pre-query needed)
    repo_urls = [repo["url"] for repo in repos if repo.get("url")]
    provenance_data = [
        {
            "repo_url": url,
            "match_method": "provenance",
            "dockerfile_path": None,
            "confidence": 1.0,
            "matched_commands": 0,
            "total_commands": 0,
            "command_similarity": 1.0,
        }
        for url in repo_urls
    ]
    if provenance_data:
        logger.info(
            "Loading provenance PACKAGED_FROM for %d repos",
            len(provenance_data),
        )
        load_matchlinks(
            neo4j_session,
            GitHubRepoProvenancePackagedFromMatchLink(),
            provenance_data,
            lastupdated=update_tag,
            _sub_resource_label="GitHubOrganization",
            _sub_resource_id=organization,
        )

    # 3. PACKAGED_FROM (tag -> Git ref): resolve image tags that embed a
    # version or commit SHA against the org's Git tags and commits. Works
    # from registry metadata alone, so it provides provenance for images
    # whose config blobs are not readable. Runs before Dockerfile analysis
    # so matched images skip that more expensive stage.
    image_tag_rows = get_unmatched_image_tag_rows(
        neo4j_session,
        organization,
        update_tag,
    )
    if image_tag_rows:
        org_git_tags = get_org_git_tags(neo4j_session, organization)

        def commit_repo_lookup(
            fragment: str, ordered_repo_urls: list[str]
        ) -> list[str]:
            hits: list[str] = []
            for repo_url in ordered_repo_urls:
                owner_repo = "/".join(repo_url.rstrip("/").rsplit("/", 2)[-2:])
                try:
                    call_github_rest_api(
                        f"/repos/{owner_repo}/commits/{fragment}",
                        token,
                        api_url,
                    )
                except requests.exceptions.HTTPError:
                    continue
                hits.append(repo_url)
                if len(hits) > 1:
                    break
            return hits

        tag_ref_matches = match_image_tags_to_git_refs(
            image_tag_rows,
            org_git_tags,
            commit_repo_lookup,
        )
        if tag_ref_matches:
            logger.info(
                "Loading %d tag-ref PACKAGED_FROM relationships",
                len(tag_ref_matches),
            )
            load_matchlinks(
                neo4j_session,
                GitHubRepoTagRefPackagedFromMatchLink(),
                tag_ref_matches,
                lastupdated=update_tag,
                _sub_resource_label="GitHubOrganization",
                _sub_resource_id=organization,
            )

    # 4. Get images WITHOUT existing PACKAGED_FROM for dockerfile analysis
    unmatched = get_unmatched_container_images_with_history(
        neo4j_session,
        organization,
        update_tag,
        limit=image_limit,
    )
    remaining_limit = (
        None if image_limit is None else max(image_limit - len(unmatched), 0)
    )
    if remaining_limit != 0:
        unmatched += get_unmatched_gcp_images_with_history(
            neo4j_session,
            sub_resource_label="GitHubOrganization",
            sub_resource_id=organization,
            update_tag=update_tag,
            limit=remaining_limit,
        )
    remaining_limit = (
        None if image_limit is None else max(image_limit - len(unmatched), 0)
    )
    if remaining_limit != 0:
        unmatched += get_unmatched_scaleway_images_with_history(
            neo4j_session,
            sub_resource_label="GitHubOrganization",
            sub_resource_id=organization,
            update_tag=update_tag,
            limit=remaining_limit,
        )

    # 5. Dockerfile analysis (only for unmatched images)
    if unmatched:
        dockerfiles = get_dockerfiles_for_repos(token, repos, organization, base_url)
        if dockerfiles:
            matches = match_images_to_dockerfiles(
                unmatched,
                dockerfiles,
                min_confidence=min_match_confidence,
            )
            if matches:
                matchlink_data = transform_matches_for_matchlink(
                    matches,
                    "repo_url",
                )
                if matchlink_data:
                    logger.info(
                        "Loading %d dockerfile-based PACKAGED_FROM relationships",
                        len(matchlink_data),
                    )
                    load_matchlinks(
                        neo4j_session,
                        GitHubRepoDockerfilePackagedFromMatchLink(),
                        matchlink_data,
                        lastupdated=update_tag,
                        _sub_resource_label="GitHubOrganization",
                        _sub_resource_id=organization,
                    )

    # 6. PACKAGED_FROM (package-owner fallback): GHCR images still without a
    # PACKAGED_FROM after steps 1-4 inherit the repo that owns their package.
    package_owner_data = _get_unmatched_ghcr_image_owner_repos(
        neo4j_session,
        organization,
        update_tag,
    )
    if package_owner_data:
        for entry in package_owner_data:
            entry.update(
                match_method="package_owner_repo",
                dockerfile_path=None,
                confidence=_PACKAGE_OWNER_FALLBACK_CONFIDENCE,
                matched_commands=0,
                total_commands=0,
                command_similarity=0.0,
            )
        logger.info(
            "Loading %d package-owner PACKAGED_FROM relationships",
            len(package_owner_data),
        )
        load_matchlinks(
            neo4j_session,
            GitHubRepoPackageOwnerPackagedFromMatchLink(),
            package_owner_data,
            lastupdated=update_tag,
            _sub_resource_label="GitHubOrganization",
            _sub_resource_id=organization,
        )

    # 7. Cleanup stale relationships
    GraphJob.from_matchlink(
        ImagePackagedByWorkflowMatchLink(),
        "GitHubOrganization",
        organization,
        update_tag,
    ).run(neo4j_session)

    GraphJob.from_matchlink(
        GitHubRepoProvenancePackagedFromMatchLink(),
        "GitHubOrganization",
        organization,
        update_tag,
    ).run(neo4j_session)

    GraphJob.from_matchlink(
        GitHubRepoTagRefPackagedFromMatchLink(),
        "GitHubOrganization",
        organization,
        update_tag,
    ).run(neo4j_session)

    GraphJob.from_matchlink(
        GitHubRepoDockerfilePackagedFromMatchLink(),
        "GitHubOrganization",
        organization,
        update_tag,
    ).run(neo4j_session)

    GraphJob.from_matchlink(
        GitHubRepoPackageOwnerPackagedFromMatchLink(),
        "GitHubOrganization",
        organization,
        update_tag,
    ).run(neo4j_session)

    # 8. Enrich PACKAGED_FROM with source_file from Image provenance
    run_typed_analysis_job(
        SUPPLY_CHAIN_SOURCE_FILE,
        neo4j_session,
        common_job_parameters,
    )

    logger.info("Completed supply chain sync for %s", organization)
