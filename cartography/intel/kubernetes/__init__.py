import logging

import boto3
from neo4j import Session

from cartography.analysis.kubernetes.analysis import K8S_COMPUTE_ASSET_EXPOSURE_JOBS
from cartography.analysis.kubernetes.analysis import K8S_LB_EXPOSURE_JOBS
from cartography.config import Config
from cartography.connector_outcome import ConnectorOutcome
from cartography.connector_outcome import emit_connector_outcome
from cartography.intel.aws.util.common import parse_and_validate_aws_regions
from cartography.intel.kubernetes.clusters import sync_kubernetes_cluster
from cartography.intel.kubernetes.eks import sync as sync_eks
from cartography.intel.kubernetes.eks_discovery import discover_eks_clusters
from cartography.intel.kubernetes.gateway_api import sync_gateway_api
from cartography.intel.kubernetes.ingress import sync_ingress
from cartography.intel.kubernetes.namespaces import sync_namespaces
from cartography.intel.kubernetes.networkpolicies import sync_network_policies
from cartography.intel.kubernetes.nodes import sync_nodes
from cartography.intel.kubernetes.pods import sync_pods
from cartography.intel.kubernetes.rbac import sync_kubernetes_rbac
from cartography.intel.kubernetes.secrets import sync_secrets
from cartography.intel.kubernetes.services import sync_services
from cartography.intel.kubernetes.util import get_k8s_clients
from cartography.intel.kubernetes.util import K8sClient
from cartography.intel.kubernetes.util import track_denied_resources
from cartography.util import run_typed_analysis_job
from cartography.util import timeit

logger = logging.getLogger(__name__)


def get_region_from_arn(arn: str) -> str:
    """
    Extract AWS region from EKS cluster ARN.
    Example: arn:aws:eks:us-east-1:111122223333:cluster/example-eks-cluster → us-east-1
    """
    parts = arn.split(":")
    if len(parts) < 6 or parts[2] != "eks":
        raise ValueError(f"Invalid EKS cluster ARN: {arn}")
    return parts[3]


@timeit
def start_k8s_ingestion(session: Session, config: Config) -> None:
    if not config.update_tag:
        logger.error("Cartography update tag not provided.")
        return

    common_job_parameters = {"UPDATE_TAG": config.update_tag}

    if config.k8s_kubeconfig:
        for client in get_k8s_clients(config.k8s_kubeconfig):
            logger.info(f"Syncing data for k8s cluster {client.name}...")
            try:
                _sync_cluster(
                    session,
                    client,
                    config,
                    common_job_parameters,
                    is_eks=config.managed_kubernetes == "eks",
                )
            except Exception:
                logger.exception(
                    f"Failed to sync data for k8s cluster {client.name}..."
                )
                raise
        return

    _sync_discovered_eks_clusters(session, config, common_job_parameters)


def _sync_discovered_eks_clusters(
    session: Session,
    config: Config,
    common_job_parameters: dict,
) -> None:
    """
    Without a kubeconfig, discover EKS clusters from the AWS credentials
    already available to the process. Each cluster syncs best-effort — one
    unreachable cluster degrades the run instead of failing it — and the
    result is reported the same way the aws and github connectors report
    theirs.
    """
    requested_regions = (
        parse_and_validate_aws_regions(config.aws_regions)
        if config.aws_regions
        else None
    )
    clusters = discover_eks_clusters(
        bool(config.aws_sync_all_profiles),
        requested_regions,
    )
    if not clusters:
        logger.warning(
            "No EKS clusters were discoverable with the available AWS credentials."
        )
    succeeded = 0
    failed = 0
    degraded = 0
    for discovered in clusters:
        logger.info(f"Syncing data for discovered EKS cluster {discovered.name}...")
        try:
            with track_denied_resources() as denied:
                _sync_cluster(
                    session,
                    discovered.client,
                    config,
                    common_job_parameters,
                    is_eks=True,
                    boto3_session=discovered.boto3_session,
                )
            succeeded += 1
            if denied:
                degraded += 1
                logger.warning(
                    "Cluster %s synced with %d resource type(s) skipped for "
                    "missing read access: %s",
                    discovered.name,
                    len(denied),
                    ", ".join(sorted(denied)),
                )
        except Exception:
            failed += 1
            logger.exception(
                f"Failed to sync data for EKS cluster {discovered.name}; "
                "continuing with the remaining clusters"
            )
    emit_connector_outcome(
        ConnectorOutcome(
            provider="kubernetes",
            attempted=len(clusters),
            succeeded=succeeded,
            failed=failed,
            degraded=degraded,
        ),
    )


def _sync_cluster(
    session: Session,
    client: K8sClient,
    config: Config,
    common_job_parameters: dict,
    is_eks: bool,
    boto3_session: boto3.Session | None = None,
) -> None:
    cluster_info = sync_kubernetes_cluster(
        session,
        client,
        config.update_tag,
        common_job_parameters,
    )
    common_job_parameters["CLUSTER_ID"] = cluster_info.get("id")
    cluster_external_ref = cluster_info.get("external_id") or cluster_info.get(
        "name", ""
    )

    sync_namespaces(session, client, config.update_tag, common_job_parameters)
    node_arch_map = sync_nodes(session, client, config.update_tag, common_job_parameters)
    sync_kubernetes_rbac(session, client, config.update_tag, common_job_parameters)

    # Extract region from cluster ARN (works for EKS; None for non-EKS clusters)
    region: str | None = None
    if is_eks:
        # EKS clusters always have a valid ARN — let ValueError propagate if not
        region = get_region_from_arn(cluster_external_ref)
        sync_eks(
            session,
            client,
            boto3_session or boto3.Session(),
            region,
            config.update_tag,
            cluster_info.get("id", ""),
            cluster_external_ref,
        )
    else:
        try:
            region = get_region_from_arn(cluster_external_ref)
        except ValueError:
            pass
    all_pods = sync_pods(
        session,
        client,
        config.update_tag,
        common_job_parameters,
        region=region,
        node_arch_map=node_arch_map,
    )
    sync_secrets(session, client, config.update_tag, common_job_parameters)
    sync_services(
        session,
        client,
        all_pods,
        config.update_tag,
        common_job_parameters,
    )
    sync_network_policies(
        session,
        client,
        all_pods,
        config.update_tag,
        common_job_parameters,
    )
    sync_gateway_api(session, client, config.update_tag, common_job_parameters)
    sync_ingress(session, client, config.update_tag, common_job_parameters)

    for job in K8S_COMPUTE_ASSET_EXPOSURE_JOBS:
        run_typed_analysis_job(job, session, common_job_parameters)
    for job in K8S_LB_EXPOSURE_JOBS:
        run_typed_analysis_job(job, session, common_job_parameters)
