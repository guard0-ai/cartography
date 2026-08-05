from unittest.mock import Mock
from unittest.mock import patch

import pytest

from cartography.graph.cleanupbuilder import build_cleanup_queries
from cartography.graph.analysis import AddRelationship
from cartography.graph.analysis import AnalysisStatement
from cartography.graph.analysisbuilder import compile_query
from cartography.graph.querybuilder import build_create_index_queries
from cartography.graph.querybuilder import build_ingestion_query
from cartography.graph.statement import GraphStatement
from cartography.tenancy import guard0_tenant_scope
from tests.data.graph.querybuilder.sample_models.interesting_asset import (
    InterestingAssetSchema,
)


def test_generated_ingestion_uses_composite_tenant_identity_and_relationship_scope():
    # Arrange
    schema = InterestingAssetSchema()

    # Act
    query = build_ingestion_query(schema)

    # Assert
    assert "guard0_org_id: $GUARD0_ORG_ID" in query
    assert "i.guard0_org_id = $GUARD0_ORG_ID" in query
    assert "r.guard0_org_id = $GUARD0_ORG_ID" in query


def test_generated_cleanup_never_uses_update_tag_without_tenant_scope():
    # Arrange
    schema = InterestingAssetSchema()

    # Act
    queries = build_cleanup_queries(schema)

    # Assert
    assert queries
    for query in queries:
        assert "$GUARD0_ORG_ID" in query
        assert "$UPDATE_TAG" in query


def test_schema_indexes_include_composite_identity_constraint():
    # Arrange
    schema = InterestingAssetSchema()

    # Act
    queries = build_create_index_queries(schema)

    # Assert
    assert any("ON (n.guard0_org_id, n.id)" in query for query in queries)
    assert any(
        "REQUIRE (n.guard0_org_id, n.id) IS UNIQUE" in query for query in queries
    )


def test_guard0_sync_rejects_unscoped_raw_graph_job_before_execution():
    # Arrange
    statement = GraphStatement("MATCH (n) DETACH DELETE n")
    session = Mock()

    # Act and assert
    with guard0_tenant_scope("org-a"):
        with pytest.raises(ValueError, match="missing explicit guard0 organization"):
            statement.run(session)
    session.execute_write.assert_not_called()


def test_reused_graph_statement_keeps_tenant_parameters_execution_local():
    # Arrange
    statement = GraphStatement(
        "MATCH (n {guard0_org_id: $GUARD0_ORG_ID}) RETURN n",
        {"caller_parameter": "unchanged"},
    )
    session = Mock()
    execution_parameters = []

    def capture_parameters(_session, callback):
        execution_parameters.append(callback.keywords["parameters"])
        return Mock()

    # Act
    with patch(
        "cartography.graph.statement.execute_write_with_retry",
        side_effect=capture_parameters,
    ):
        with guard0_tenant_scope("org-a"):
            statement.run(session)
        with guard0_tenant_scope("org-b"):
            statement.run(session)

    # Assert
    assert execution_parameters[0]["GUARD0_ORG_ID"] == "org-a"
    assert execution_parameters[1]["GUARD0_ORG_ID"] == "org-b"
    assert "GUARD0_ORG_ID" not in statement.parameters
    assert statement.parameters["caller_parameter"] == "unchanged"


def test_typed_analysis_compiler_scopes_every_graph_pattern_in_guard0_context():
    statement = AnalysisStatement(
        match=(
            "MATCH (l:AWSLambda)-[:HAS_IMAGE]->(i:AWSECRImage) "
            "WHERE EXISTS { MATCH (i)-[:PACKAGED_FROM]->"
            "(:GitHubRepository {id: 'repo'}) }"
        ),
        effects=(
            AddRelationship(
                "l",
                "RESOLVED_IMAGE",
                "i",
                source_label="AWSLambda",
                target_label="AWSECRImage",
            ),
        ),
    )

    with guard0_tenant_scope("org-a"):
        query = compile_query(statement)

    assert "(l:AWSLambda {guard0_org_id: $GUARD0_ORG_ID})" in query
    assert "[:HAS_IMAGE {guard0_org_id: $GUARD0_ORG_ID}]" in query
    assert "(i:AWSECRImage {guard0_org_id: $GUARD0_ORG_ID})" in query
    assert "[:PACKAGED_FROM {guard0_org_id: $GUARD0_ORG_ID}]" in query
    assert "(:GitHubRepository {guard0_org_id: $GUARD0_ORG_ID, id: 'repo'})" in query
    assert "[r:RESOLVED_IMAGE {guard0_org_id: $GUARD0_ORG_ID}]" in query
