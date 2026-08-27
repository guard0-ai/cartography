from cartography.intel.github.supply_chain import _extract_workflow_image_names
from cartography.intel.github.supply_chain import _registry_repo_match_rank
from cartography.intel.github.supply_chain import _registry_repo_name_matches
from cartography.intel.github.supply_chain import match_image_tags_to_git_refs

ORG = "https://github.com/example-org"

GIT_TAGS = [
    {"name": "v1.2.3", "commit_sha": "a" * 40, "repo_url": f"{ORG}/service-alpha"},
    {"name": "v9.9.9", "commit_sha": "b" * 40, "repo_url": f"{ORG}/service-alpha"},
    {"name": "v9.9.9", "commit_sha": "c" * 40, "repo_url": f"{ORG}/service-beta"},
    {
        "name": "v4.5.6",
        "commit_sha": "deadbeef" + "0" * 32,
        "repo_url": f"{ORG}/service-gamma",
    },
]


def test_semver_unique_across_repos_matches():
    rows = [{"digest": "sha256:d1", "tag": "1.2.3", "registry_name": "anything"}]

    matches = match_image_tags_to_git_refs(rows, GIT_TAGS)

    assert len(matches) == 1
    assert matches[0]["repo_url"] == f"{ORG}/service-alpha"
    assert matches[0]["match_method"] == "tag_semver"
    assert matches[0]["matched_git_ref"] == "1.2.3"
    assert matches[0]["confidence"] == 0.9


def test_semver_leading_v_and_suffix_normalized():
    rows = [{"digest": "sha256:d2", "tag": "v1.2.3-rc1", "registry_name": "x"}]

    matches = match_image_tags_to_git_refs(rows, GIT_TAGS)

    assert len(matches) == 1
    assert matches[0]["repo_url"] == f"{ORG}/service-alpha"


def test_semver_in_multiple_repos_resolved_by_registry_name():
    rows = [
        {"digest": "sha256:d3", "tag": "9.9.9", "registry_name": "service-beta"},
    ]

    matches = match_image_tags_to_git_refs(rows, GIT_TAGS)

    assert len(matches) == 1
    assert matches[0]["repo_url"] == f"{ORG}/service-beta"
    assert matches[0]["confidence"] == 0.75


def test_semver_in_multiple_repos_without_name_signal_is_not_matched():
    rows = [{"digest": "sha256:d4", "tag": "9.9.9", "registry_name": "unrelated"}]

    assert match_image_tags_to_git_refs(rows, GIT_TAGS) == []


def test_sha_fragment_resolved_against_git_tag_commits():
    rows = [{"digest": "sha256:d5", "tag": "main-deadbeef", "registry_name": "x"}]

    matches = match_image_tags_to_git_refs(rows, GIT_TAGS)

    assert len(matches) == 1
    assert matches[0]["repo_url"] == f"{ORG}/service-gamma"
    assert matches[0]["match_method"] == "tag_sha"
    assert matches[0]["matched_git_ref"] == "deadbeef"
    assert matches[0]["confidence"] == 0.95


def test_sha_fragment_falls_back_to_commit_lookup():
    rows = [
        {"digest": "sha256:d6", "tag": "abc1237", "registry_name": "service-delta"},
    ]
    probed = {}

    def lookup(fragment, ordered_repo_urls):
        probed["fragment"] = fragment
        probed["order"] = ordered_repo_urls
        return [f"{ORG}/service-delta"]

    git_tags = GIT_TAGS + [
        {"name": "v0.1.0", "commit_sha": "e" * 40, "repo_url": f"{ORG}/service-delta"},
    ]
    matches = match_image_tags_to_git_refs(rows, git_tags, lookup)

    assert len(matches) == 1
    assert matches[0]["repo_url"] == f"{ORG}/service-delta"
    assert matches[0]["match_method"] == "tag_sha"
    assert probed["fragment"] == "abc1237"
    assert probed["order"][0] == f"{ORG}/service-delta"


def test_sha_fragment_ambiguous_lookup_is_not_matched():
    rows = [{"digest": "sha256:d7", "tag": "abc1237", "registry_name": "x"}]

    def lookup(fragment, ordered_repo_urls):
        return [f"{ORG}/service-alpha", f"{ORG}/service-beta"]

    assert match_image_tags_to_git_refs(rows, GIT_TAGS, lookup) == []


def test_all_digit_fragment_is_ignored():
    rows = [{"digest": "sha256:d8", "tag": "build-1234567", "registry_name": "x"}]

    def lookup(fragment, ordered_repo_urls):
        raise AssertionError("numeric fragment must not trigger commit lookup")

    assert match_image_tags_to_git_refs(rows, GIT_TAGS, lookup) == []


def test_sha_takes_precedence_over_semver():
    rows = [
        {
            "digest": "sha256:d9",
            "tag": "1.2.3-deadbeef",
            "registry_name": "service-gamma",
        },
    ]

    matches = match_image_tags_to_git_refs(rows, GIT_TAGS)

    assert matches[0]["match_method"] == "tag_sha"
    assert matches[0]["repo_url"] == f"{ORG}/service-gamma"


def test_no_signal_tags_produce_no_matches():
    rows = [
        {"digest": "sha256:da", "tag": "latest", "registry_name": "x"},
        {"digest": "sha256:db", "tag": "", "registry_name": "x"},
        {"digest": "sha256:dc", "tag": None, "registry_name": "x"},
    ]

    assert match_image_tags_to_git_refs(rows, GIT_TAGS) == []


def test_one_match_per_digest():
    rows = [
        {"digest": "sha256:dd", "tag": "1.2.3", "registry_name": "x"},
        {"digest": "sha256:dd", "tag": "v1.2.3", "registry_name": "x"},
    ]

    matches = match_image_tags_to_git_refs(rows, GIT_TAGS)

    assert len(matches) == 1


def test_registry_repo_name_matches():
    assert _registry_repo_name_matches("billing-service", f"{ORG}/billing")
    assert _registry_repo_name_matches("frontend", f"{ORG}/frontend")
    assert not _registry_repo_name_matches("unrelated", f"{ORG}/frontend")
    assert not _registry_repo_name_matches(None, f"{ORG}/frontend")
    assert not _registry_repo_name_matches("", f"{ORG}/frontend")


def test_registry_match_rank_tiers():
    assert _registry_repo_match_rank("billing", f"{ORG}/billing") == 0
    assert _registry_repo_match_rank("acme-prod-docker/billing", f"{ORG}/billing") == 0
    assert _registry_repo_match_rank("billing", f"{ORG}/money-core", {"billing"}) == 1
    assert _registry_repo_match_rank("billing-service", f"{ORG}/billing") == 2
    assert _registry_repo_match_rank("worker-billing", f"{ORG}/billing") == 3
    assert _registry_repo_match_rank("unrelated", f"{ORG}/billing") is None


def test_extract_workflow_image_names():
    text = """
    env:
      DOCKER_IMAGE_NAME: ${{ inputs.docker_image_name || 'billing-api' }}
    steps:
      - run: |
          ECR_REPO="${{ env.AWS_ECR_REGISTRY }}/shield-train"
          docker push 123456789012.dkr.ecr.us-east-1.amazonaws.com/acme-prod/ledger:v1
          docker push ghcr.io/acme/frontend:latest
          docker tag $ECR_REGISTRY/payments:$TAG something
          docker push "${{ env.ACR_LOGIN_SERVER }}/reports"
          docker push "${{ secrets.DOCKERHUB_USERNAME }}/notifier"
          echo "${GITHUB_REPOSITORY}/not-an-image"
    """
    names = _extract_workflow_image_names(text)
    assert names == {
        "billing-api",
        "shield-train",
        "ledger",
        "frontend",
        "payments",
        "reports",
        "notifier",
    }


def test_semver_collision_resolved_by_workflow_declared_name():
    git_tags = [
        {"name": "v0.0.7", "commit_sha": "1" * 40, "repo_url": f"{ORG}/money-core"},
        {"name": "v0.0.7", "commit_sha": "2" * 40, "repo_url": f"{ORG}/ledger"},
    ]
    rows = [
        {"digest": "sha256:d30", "tag": "0.0.7", "registry_name": "billing-api"},
    ]

    def workflow_names(repo_url):
        return {"billing-api"} if repo_url == f"{ORG}/money-core" else set()

    matches = match_image_tags_to_git_refs(
        rows, git_tags, workflow_names_lookup=workflow_names
    )

    assert len(matches) == 1
    assert matches[0]["repo_url"] == f"{ORG}/money-core"


def test_semver_collision_workflow_declaration_beats_prefix():
    git_tags = [
        {"name": "v0.0.7", "commit_sha": "1" * 40, "repo_url": f"{ORG}/money-core"},
        {"name": "v0.0.7", "commit_sha": "2" * 40, "repo_url": f"{ORG}/billing"},
    ]
    rows = [
        {"digest": "sha256:d31", "tag": "0.0.7", "registry_name": "billing-api"},
    ]

    def workflow_names(repo_url):
        return {"billing-api"} if repo_url == f"{ORG}/money-core" else set()

    matches = match_image_tags_to_git_refs(
        rows, git_tags, workflow_names_lookup=workflow_names
    )

    assert len(matches) == 1
    assert matches[0]["repo_url"] == f"{ORG}/money-core"


def test_semver_collision_exact_name_beats_workflow_declaration():
    git_tags = [
        {"name": "v0.0.7", "commit_sha": "1" * 40, "repo_url": f"{ORG}/billing"},
        {"name": "v0.0.7", "commit_sha": "2" * 40, "repo_url": f"{ORG}/money-core"},
    ]
    rows = [
        {"digest": "sha256:d32", "tag": "0.0.7", "registry_name": "billing"},
    ]

    def workflow_names(repo_url):
        return {"billing"} if repo_url == f"{ORG}/money-core" else set()

    matches = match_image_tags_to_git_refs(
        rows, git_tags, workflow_names_lookup=workflow_names
    )

    assert len(matches) == 1
    assert matches[0]["repo_url"] == f"{ORG}/billing"


def test_semver_collision_resolved_for_namespaced_registry():
    git_tags = [
        {"name": "v0.0.7", "commit_sha": "1" * 40, "repo_url": f"{ORG}/billing"},
        {"name": "v0.0.7", "commit_sha": "2" * 40, "repo_url": f"{ORG}/ledger"},
    ]
    rows = [
        {
            "digest": "sha256:d20",
            "tag": "0.0.7",
            "registry_name": "acme-prod-docker/billing",
        },
    ]

    matches = match_image_tags_to_git_refs(rows, git_tags)

    assert len(matches) == 1
    assert matches[0]["repo_url"] == f"{ORG}/billing"
    assert matches[0]["match_method"] == "tag_semver"


def test_semver_collision_resolved_by_token_subset():
    git_tags = [
        {"name": "v0.0.7", "commit_sha": "1" * 40, "repo_url": f"{ORG}/billing"},
        {"name": "v0.0.7", "commit_sha": "2" * 40, "repo_url": f"{ORG}/ledger"},
    ]
    rows = [
        {"digest": "sha256:d21", "tag": "0.0.7", "registry_name": "worker-billing"},
    ]

    matches = match_image_tags_to_git_refs(rows, git_tags)

    assert len(matches) == 1
    assert matches[0]["repo_url"] == f"{ORG}/billing"


def test_semver_collision_exact_name_beats_token_subset():
    git_tags = [
        {"name": "v0.0.7", "commit_sha": "1" * 40, "repo_url": f"{ORG}/gateway"},
        {"name": "v0.0.7", "commit_sha": "2" * 40, "repo_url": f"{ORG}/llm-gateway"},
    ]
    rows = [
        {"digest": "sha256:d22", "tag": "0.0.7", "registry_name": "llm-gateway"},
    ]

    matches = match_image_tags_to_git_refs(rows, git_tags)

    assert len(matches) == 1
    assert matches[0]["repo_url"] == f"{ORG}/llm-gateway"


def test_semver_collision_ambiguous_within_strongest_rank_is_not_matched():
    git_tags = [
        {
            "name": "v0.0.7",
            "commit_sha": "1" * 40,
            "repo_url": f"{ORG}/billing-service",
        },
        {"name": "v0.0.7", "commit_sha": "2" * 40, "repo_url": f"{ORG}/billing-ui"},
    ]
    rows = [
        {"digest": "sha256:d23", "tag": "0.0.7", "registry_name": "billing"},
    ]

    assert match_image_tags_to_git_refs(rows, git_tags) == []
