"""
GitHub Git Tag Schema.

Represents Git tags (refs/tags/*) on a GitHub repository. Each tag records
the commit it points at, so image tags that embed a version or commit SHA
can be resolved to a repository and exact commit by the supply-chain
matcher in cartography/intel/github/supply_chain.py.
"""

from dataclasses import dataclass

from cartography.models.core.common import PropertyRef
from cartography.models.core.nodes import CartographyNodeProperties
from cartography.models.core.nodes import CartographyNodeSchema
from cartography.models.core.relationships import CartographyRelProperties
from cartography.models.core.relationships import CartographyRelSchema
from cartography.models.core.relationships import LinkDirection
from cartography.models.core.relationships import make_target_node_matcher
from cartography.models.core.relationships import OtherRelationships
from cartography.models.core.relationships import TargetNodeMatcher


@dataclass(frozen=True)
class GitHubTagNodeProperties(CartographyNodeProperties):
    id: PropertyRef = PropertyRef("id")
    name: PropertyRef = PropertyRef("name", extra_index=True)
    commit_sha: PropertyRef = PropertyRef("commit_sha", extra_index=True)
    repo_url: PropertyRef = PropertyRef("repo_url", extra_index=True)
    lastupdated: PropertyRef = PropertyRef("lastupdated", set_in_kwargs=True)


@dataclass(frozen=True)
class GitHubTagRelProperties(CartographyRelProperties):
    lastupdated: PropertyRef = PropertyRef("lastupdated", set_in_kwargs=True)


@dataclass(frozen=True)
class GitHubTagToOrgRel(CartographyRelSchema):
    target_node_label: str = "GitHubOrganization"
    target_node_matcher: TargetNodeMatcher = make_target_node_matcher(
        {"id": PropertyRef("org_url", set_in_kwargs=True)},
    )
    direction: LinkDirection = LinkDirection.INWARD
    rel_label: str = "RESOURCE"
    properties: GitHubTagRelProperties = GitHubTagRelProperties()


@dataclass(frozen=True)
class GitHubTagToRepositoryRel(CartographyRelSchema):
    target_node_label: str = "GitHubRepository"
    target_node_matcher: TargetNodeMatcher = make_target_node_matcher(
        {"id": PropertyRef("repo_url")},
    )
    direction: LinkDirection = LinkDirection.INWARD
    rel_label: str = "TAG"
    properties: GitHubTagRelProperties = GitHubTagRelProperties()


@dataclass(frozen=True)
class GitHubTagSchema(CartographyNodeSchema):
    label: str = "GitHubTag"
    properties: GitHubTagNodeProperties = GitHubTagNodeProperties()
    sub_resource_relationship: GitHubTagToOrgRel = GitHubTagToOrgRel()
    other_relationships: OtherRelationships = OtherRelationships(
        [GitHubTagToRepositoryRel()],
    )
