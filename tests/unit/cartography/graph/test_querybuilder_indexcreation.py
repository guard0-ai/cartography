from cartography.graph.querybuilder import build_create_index_queries
from cartography.models.aws.dynamodb.tables import DynamoDBTableSchema
from cartography.models.aws.emr import EMRClusterSchema
from cartography.models.aws.iam.principal_service_access import (
    AWSPrincipalServiceAccessSchema,
)
from cartography.models.trivy.findings import TrivyImageFindingSchema
from tests.data.graph.querybuilder.sample_models.interesting_asset import (
    InterestingAssetSchema,
)


def test_build_create_index_queries():
    result = build_create_index_queries(InterestingAssetSchema())
    assert set(result) == {
        "CREATE CONSTRAINT IF NOT EXISTS FOR (n:InterestingAsset) REQUIRE (n.guard0_org_id, n.id) IS UNIQUE;",
        "CREATE INDEX IF NOT EXISTS FOR (n:InterestingAsset) ON (n.guard0_org_id, n.lastupdated);",
        "CREATE INDEX IF NOT EXISTS FOR (n:AnotherNodeLabel) ON (n.guard0_org_id, n.id);",
        "CREATE INDEX IF NOT EXISTS FOR (n:AnotherNodeLabel) ON (n.guard0_org_id, n.lastupdated);",
        "CREATE INDEX IF NOT EXISTS FOR (n:YetAnotherNodeLabel) ON (n.guard0_org_id, n.id);",
        "CREATE INDEX IF NOT EXISTS FOR (n:YetAnotherNodeLabel) ON (n.guard0_org_id, n.lastupdated);",
    }


def test_build_create_index_queries_for_emr():
    """
    The EMR sync is our poster child for testing out the Cartography data model. This is a realistic scenario of index
    creation.
    """
    result = build_create_index_queries(EMRClusterSchema())
    assert {
        "CREATE CONSTRAINT IF NOT EXISTS FOR (n:AWSEMRCluster) REQUIRE (n.guard0_org_id, n.id) IS UNIQUE;",
        "CREATE INDEX IF NOT EXISTS FOR (n:AWSEMRCluster) ON (n.guard0_org_id, n.lastupdated);",
        "CREATE INDEX IF NOT EXISTS FOR (n:AWSEMRCluster) ON (n.guard0_org_id, n.arn);",
        "CREATE INDEX IF NOT EXISTS FOR (n:ComputeCluster) ON (n.guard0_org_id, n.id);",
    }.issubset(set(result))

    assert {
        "CREATE INDEX IF NOT EXISTS FOR (n:ComputeCluster) ON (n.guard0_org_id, n._ont_source);",
        "CREATE INDEX IF NOT EXISTS FOR (n:ComputeCluster) ON (n.guard0_org_id, n._ont_name);",
        "CREATE INDEX IF NOT EXISTS FOR (n:ComputeCluster) ON (n.guard0_org_id, n._ont_region);",
        "CREATE INDEX IF NOT EXISTS FOR (n:ComputeCluster) ON (n.guard0_org_id, n._ont_version);",
    }.issubset(set(result))


def test_relationship_target_id_does_not_preempt_target_identity_constraint():
    result = set(build_create_index_queries(InterestingAssetSchema()))

    for target_label in ("SubResource", "HelloAsset", "WorldAsset"):
        assert not any(f"FOR (n:{target_label})" in query for query in result)


def test_build_create_index_queries_skips_unindexed_ontology_fields():
    """
    Unbounded ontology fields (references, description, problem_types) declare indexed=False
    so they do not get a RANGE index on the semantic labels. Their `_ont_<field>` values can
    exceed Neo4j's index value limit (~8 KB) and crash the sync. Bounded fields and _ont_source
    must still be indexed. TrivyImageFinding carries the "Risk" and "CVE" semantic labels.
    """
    result = set(build_create_index_queries(TrivyImageFindingSchema()))

    for label in ("Risk", "CVE"):
        for field in ("references", "description", "problem_types"):
            assert (
                f"CREATE INDEX IF NOT EXISTS FOR (n:{label}) ON (n._ont_{field});"
                not in result
            )

    # Bounded ontology fields and _ont_source are still indexed on the semantic labels.
    assert {
        "CREATE INDEX IF NOT EXISTS FOR (n:Risk) ON (n.guard0_org_id, n._ont_source);",
        "CREATE INDEX IF NOT EXISTS FOR (n:Risk) ON (n.guard0_org_id, n._ont_cve_id);",
        "CREATE INDEX IF NOT EXISTS FOR (n:CVE) ON (n.guard0_org_id, n._ont_source);",
        "CREATE INDEX IF NOT EXISTS FOR (n:CVE) ON (n.guard0_org_id, n._ont_cve_id);",
    }.issubset(result)


def test_build_create_index_queries_for_dynamodb_table_arn():
    result = build_create_index_queries(DynamoDBTableSchema())

    assert (
        "CREATE INDEX IF NOT EXISTS FOR (n:AWSDynamoDBTable) "
        "ON (n.guard0_org_id, n.arn);"
    ) in result


def test_composite_aws_principal_schema_reuses_shared_label_index():
    result = set(build_create_index_queries(AWSPrincipalServiceAccessSchema()))

    assert (
        "CREATE INDEX IF NOT EXISTS FOR (n:AWSPrincipal) "
        "ON (n.guard0_org_id, n.id);"
    ) in result
    assert not any(
        "CREATE CONSTRAINT IF NOT EXISTS FOR (n:AWSPrincipal)" in query
        for query in result
    )
