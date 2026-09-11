import base64
from types import SimpleNamespace
from unittest.mock import MagicMock

import botocore.exceptions
from botocore.credentials import Credentials
from botocore.hooks import HierarchicalEmitter
from kubernetes.client import Configuration

import cartography.intel.kubernetes as kubernetes
import cartography.intel.kubernetes.eks_discovery as eks_discovery
from cartography.intel.kubernetes.eks_discovery import DiscoveredEKSCluster
from cartography.intel.kubernetes.eks_discovery import discover_eks_clusters
from cartography.intel.kubernetes.eks_discovery import EKSTokenAuthenticator

CLUSTER_NAME = "example-eks-cluster"
CLUSTER_ARN = "arn:aws:eks:us-east-1:111122223333:cluster/example-eks-cluster"
REGION = "us-east-1"
CA_DATA = base64.b64encode(
    b"-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n"
).decode()


def _fake_boto3_session(eks_client):
    session = MagicMock()
    session.get_credentials.return_value = Credentials(
        "AKIDEXAMPLE", "secret-key-example"
    )
    session.events = HierarchicalEmitter()
    session.client.side_effect = lambda service, **kwargs: {"eks": eks_client}[service]
    return session


def _decoded_token_url(token: str) -> str:
    assert token.startswith("k8s-aws-v1.")
    encoded = token.removeprefix("k8s-aws-v1.")
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding).decode()


def test_token_is_presigned_sts_url_scoped_to_cluster():
    import boto3

    session = boto3.Session(
        aws_access_key_id="AKIDEXAMPLE",
        aws_secret_access_key="secret-key-example",
        region_name=REGION,
    )
    authenticator = EKSTokenAuthenticator(session, CLUSTER_NAME, REGION)
    configuration = Configuration()

    authenticator.refresh(configuration)
    url = _decoded_token_url(configuration.api_key["authorization"])

    assert url.startswith(f"https://sts.{REGION}.amazonaws.com/")
    assert "Action=GetCallerIdentity" in url
    assert "x-k8s-aws-id" in url  # signed header binds the token to the cluster
    assert "X-Amz-Signature=" in url


def test_token_is_reused_within_max_age_and_resigned_after(monkeypatch):
    import boto3

    session = boto3.Session(
        aws_access_key_id="AKIDEXAMPLE",
        aws_secret_access_key="secret-key-example",
        region_name=REGION,
    )
    authenticator = EKSTokenAuthenticator(session, CLUSTER_NAME, REGION)
    configuration = Configuration()

    now = {"value": 1000.0}
    monkeypatch.setattr(eks_discovery.time, "monotonic", lambda: now["value"])

    authenticator.refresh(configuration)
    first = configuration.api_key["authorization"]

    now["value"] += 1
    authenticator.refresh(configuration)
    assert configuration.api_key["authorization"] == first

    minted = {"count": 0}
    original_mint = authenticator._mint_token

    def _counting_mint():
        minted["count"] += 1
        return original_mint()

    monkeypatch.setattr(authenticator, "_mint_token", _counting_mint)
    now["value"] += eks_discovery._TOKEN_MAX_AGE_SECONDS + 1
    authenticator.refresh(configuration)
    assert minted["count"] == 1


def test_discover_eks_clusters_connects_each_discovered_cluster(monkeypatch):
    eks_client = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [{"clusters": [CLUSTER_NAME]}]
    eks_client.get_paginator.return_value = paginator
    eks_client.describe_cluster.return_value = {
        "cluster": {
            "name": CLUSTER_NAME,
            "arn": CLUSTER_ARN,
            "endpoint": "https://example-endpoint.eks.amazonaws.com",
            "certificateAuthority": {"data": CA_DATA},
        },
    }
    session = _fake_boto3_session(eks_client)
    monkeypatch.setattr(eks_discovery, "_boto3_sessions", lambda _: [session])
    monkeypatch.setattr(eks_discovery, "_regions_for_session", lambda *_: [REGION])

    discovered = discover_eks_clusters(aws_sync_all_profiles=False)

    assert len(discovered) == 1
    assert discovered[0].name == CLUSTER_NAME
    assert discovered[0].arn == CLUSTER_ARN
    assert discovered[0].region == REGION
    assert discovered[0].client.external_id == CLUSTER_ARN
    assert discovered[0].client.tls_diagnostics["api_server_url"] == (
        "https://example-endpoint.eks.amazonaws.com"
    )


def test_discover_eks_clusters_skips_unreachable_regions(monkeypatch):
    eks_client = MagicMock()
    eks_client.get_paginator.return_value.paginate.side_effect = (
        botocore.exceptions.ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "ListClusters",
        )
    )
    session = _fake_boto3_session(eks_client)
    monkeypatch.setattr(eks_discovery, "_boto3_sessions", lambda _: [session])
    monkeypatch.setattr(
        eks_discovery, "_regions_for_session", lambda *_: [REGION, "us-west-2"]
    )

    assert discover_eks_clusters(aws_sync_all_profiles=False) == []


def test_discovery_sync_is_best_effort_and_reports_outcome(monkeypatch):
    clusters = [
        DiscoveredEKSCluster(
            name=f"cluster-{index}",
            arn=f"arn:aws:eks:us-east-1:111122223333:cluster/cluster-{index}",
            region=REGION,
            client=SimpleNamespace(name=f"cluster-{index}"),
            boto3_session=MagicMock(),
        )
        for index in range(2)
    ]
    monkeypatch.setattr(
        kubernetes, "discover_eks_clusters", lambda *args, **kwargs: clusters
    )

    def _sync_cluster(session, client, config, params, is_eks, boto3_session=None):
        assert is_eks
        if client.name == "cluster-0":
            raise RuntimeError("unreachable")

    monkeypatch.setattr(kubernetes, "_sync_cluster", _sync_cluster)
    outcomes = []
    monkeypatch.setattr(
        kubernetes, "emit_connector_outcome", lambda outcome: outcomes.append(outcome)
    )

    config = SimpleNamespace(
        update_tag=123456789,
        k8s_kubeconfig=None,
        managed_kubernetes=None,
        aws_sync_all_profiles=False,
        aws_regions=None,
    )
    kubernetes.start_k8s_ingestion(MagicMock(), config)

    assert len(outcomes) == 1
    assert outcomes[0].provider == "kubernetes"
    assert outcomes[0].attempted == 2
    assert outcomes[0].succeeded == 1
    assert outcomes[0].failed == 1
    assert outcomes[0].degraded == 0


def test_discovery_sync_reports_degraded_clusters(monkeypatch):
    from cartography.intel.kubernetes.util import record_denied_resource

    clusters = [
        DiscoveredEKSCluster(
            name=f"cluster-{index}",
            arn=f"arn:aws:eks:us-east-1:111122223333:cluster/cluster-{index}",
            region=REGION,
            client=SimpleNamespace(name=f"cluster-{index}"),
            boto3_session=MagicMock(),
        )
        for index in range(2)
    ]
    monkeypatch.setattr(
        kubernetes, "discover_eks_clusters", lambda *args, **kwargs: clusters
    )

    def _sync_cluster(session, client, config, params, is_eks, boto3_session=None):
        if client.name == "cluster-0":
            record_denied_resource("gateway.networking.k8s.io/v1/gateways")

    monkeypatch.setattr(kubernetes, "_sync_cluster", _sync_cluster)
    outcomes = []
    monkeypatch.setattr(
        kubernetes, "emit_connector_outcome", lambda outcome: outcomes.append(outcome)
    )

    config = SimpleNamespace(
        update_tag=123456789,
        k8s_kubeconfig=None,
        managed_kubernetes=None,
        aws_sync_all_profiles=False,
        aws_regions=None,
    )
    kubernetes.start_k8s_ingestion(MagicMock(), config)

    assert outcomes[0].succeeded == 2
    assert outcomes[0].failed == 0
    assert outcomes[0].degraded == 1


def test_regions_for_session_prefers_requested_regions():
    session = MagicMock()

    regions = eks_discovery._regions_for_session(session, ["eu-west-1"])

    assert regions == ["eu-west-1"]
    session.client.assert_not_called()
    session.get_available_regions.assert_not_called()


def test_regions_for_session_uses_the_regions_enabled_on_the_account(monkeypatch):
    session = MagicMock()
    monkeypatch.setattr(
        eks_discovery,
        "get_ec2_regions",
        lambda _: ["us-east-1", "eu-west-1"],
    )

    regions = eks_discovery._regions_for_session(session, None)

    assert regions == ["us-east-1", "eu-west-1"]
    session.get_available_regions.assert_not_called()


def test_regions_for_session_falls_back_to_partition_regions(monkeypatch):
    session = MagicMock()
    session.get_available_regions.return_value = ["us-east-1", "me-south-1"]

    def _denied(_):
        raise botocore.exceptions.ClientError(
            {"Error": {"Code": "UnauthorizedOperation", "Message": "denied"}},
            "DescribeRegions",
        )

    monkeypatch.setattr(eks_discovery, "get_ec2_regions", _denied)

    regions = eks_discovery._regions_for_session(session, None)

    assert regions == ["us-east-1", "me-south-1"]
    session.get_available_regions.assert_called_once_with("eks")


def test_discovery_clients_fail_fast_on_unreachable_endpoints(monkeypatch):
    eks_client = MagicMock()
    eks_client.get_paginator.return_value.paginate.return_value = [{"clusters": []}]
    session = _fake_boto3_session(eks_client)
    monkeypatch.setattr(eks_discovery, "_boto3_sessions", lambda _: [session])
    monkeypatch.setattr(eks_discovery, "_regions_for_session", lambda *_: [REGION])

    discover_eks_clusters(aws_sync_all_profiles=False)

    call = session.client.call_args
    assert call.args[0] == "eks"
    assert call.kwargs["region_name"] == REGION
    config = call.kwargs["config"]
    assert config.connect_timeout == eks_discovery._DISCOVERY_CONNECT_TIMEOUT_SECONDS
    assert config.retries["max_attempts"] == eks_discovery._DISCOVERY_MAX_ATTEMPTS
