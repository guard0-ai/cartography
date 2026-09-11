import base64
import logging
import tempfile
import time
from dataclasses import dataclass
from typing import Any

import boto3
import botocore.config
import botocore.exceptions
from botocore.model import ServiceId
from botocore.signers import RequestSigner
from kubernetes.client import ApiClient
from kubernetes.client import Configuration

from cartography.intel.aws.ec2 import get_ec2_regions
from cartography.intel.aws.util.botocore_config import create_boto3_client
from cartography.intel.aws.util.botocore_config import get_botocore_config
from cartography.intel.kubernetes.util import K8sClient

logger = logging.getLogger(__name__)

# EKS validates the presigned URL's age server-side, so tokens are re-signed
# locally (no network call) once they pass this age. Kept well under the
# 60-second X-Amz-Expires the URL is signed with.
_TOKEN_MAX_AGE_SECONDS = 45
_PRESIGNED_URL_EXPIRES_IN_SECONDS = 60

# Discovery probes one EKS endpoint per enabled region. A regional endpoint
# that drops the TCP handshake must fail fast: botocore's default client waits
# 60 seconds per attempt, which stalled a sync for ten minutes per region.
_DISCOVERY_CONNECT_TIMEOUT_SECONDS = 10
_DISCOVERY_MAX_ATTEMPTS = 2


@dataclass(frozen=True)
class DiscoveredEKSCluster:
    name: str
    arn: str
    region: str
    client: K8sClient
    boto3_session: boto3.Session


class EKSTokenAuthenticator:
    """
    Bearer-token authentication for an EKS cluster derived from AWS credentials.

    EKS accepts a presigned STS GetCallerIdentity URL as a bearer token
    ("k8s-aws-v1." + unpadded base64url of the URL). Signing happens locally
    with the session's credentials; installed as the kubernetes client's
    refresh_api_key_hook, which runs before every request, so each request
    carries a token younger than _TOKEN_MAX_AGE_SECONDS.
    """

    def __init__(
        self,
        boto3_session: boto3.Session,
        cluster_name: str,
        region: str,
    ) -> None:
        self._session = boto3_session
        self._cluster_name = cluster_name
        self._region = region
        self._signed_at: float | None = None

    def _mint_token(self) -> str:
        signer = RequestSigner(
            ServiceId("STS"),
            self._region,
            "sts",
            "v4",
            self._session.get_credentials(),
            self._session.events,
        )
        presigned_url = signer.generate_presigned_url(
            {
                "method": "GET",
                "url": (
                    f"https://sts.{self._region}.amazonaws.com/"
                    "?Action=GetCallerIdentity&Version=2011-06-15"
                ),
                "body": {},
                "headers": {"x-k8s-aws-id": self._cluster_name},
                "context": {},
            },
            region_name=self._region,
            expires_in=_PRESIGNED_URL_EXPIRES_IN_SECONDS,
            operation_name="",
        )
        encoded = base64.urlsafe_b64encode(presigned_url.encode("utf-8"))
        return "k8s-aws-v1." + encoded.decode("utf-8").rstrip("=")

    def refresh(self, configuration: Configuration) -> None:
        now = time.monotonic()
        if (
            self._signed_at is not None
            and now - self._signed_at < _TOKEN_MAX_AGE_SECONDS
        ):
            return
        configuration.api_key["authorization"] = self._mint_token()
        self._signed_at = now


def _build_api_client(
    endpoint: str,
    certificate_authority_data: str,
    authenticator: EKSTokenAuthenticator,
) -> ApiClient:
    configuration = Configuration()
    configuration.host = endpoint
    # The CA must be a file path for the underlying urllib3 pool. The file
    # lives for the remainder of the sync process.
    ca_file = tempfile.NamedTemporaryFile(
        prefix="eks-ca-",
        suffix=".pem",
        delete=False,
    )
    ca_file.write(base64.b64decode(certificate_authority_data))
    ca_file.close()
    configuration.ssl_ca_cert = ca_file.name
    configuration.api_key_prefix["BearerToken"] = "Bearer"
    configuration.refresh_api_key_hook = authenticator.refresh
    authenticator.refresh(configuration)
    return ApiClient(configuration)


def _tls_diagnostics(endpoint: str) -> dict[str, Any]:
    """
    TLS diagnostics for a discovered cluster. The connection is built from
    DescribeCluster's endpoint and certificate authority rather than a
    kubeconfig, so the facts are known instead of parsed.
    """
    return {
        "api_server_url": endpoint,
        "kubeconfig_insecure_skip_tls_verify": False,
        "kubeconfig_has_certificate_authority_data": True,
        "kubeconfig_has_certificate_authority_file": False,
        "kubeconfig_ca_file_path": None,
        "kubeconfig_has_client_certificate": False,
        "kubeconfig_has_client_key": False,
        "kubeconfig_tls_configuration_status": "valid_config",
    }


def _boto3_sessions(aws_sync_all_profiles: bool) -> list[boto3.Session]:
    """
    AWS sessions to discover clusters with, mirroring the aws module's profile
    handling: every configured non-default profile when profile fan-out is on,
    otherwise the default session (env-var or default-profile credentials).
    """
    if aws_sync_all_profiles:
        base_session = boto3.Session()
        profiles = [
            profile
            for profile in base_session.available_profiles
            if profile != "default"
        ]
        if profiles:
            return [boto3.Session(profile_name=profile) for profile in profiles]
    return [boto3.Session()]


def _regions_for_session(
    boto3_session: boto3.Session,
    requested_regions: list[str] | None,
) -> list[str]:
    """
    Regions to probe for EKS clusters: the caller's explicit list, else the
    regions enabled on the account (ec2:DescribeRegions, the same source the
    AWS module uses). The partition-wide region list is the last resort, for
    credentials that cannot describe regions; it includes opt-in regions the
    account never enabled, whose endpoints may not answer at all.
    """
    if requested_regions:
        return requested_regions
    try:
        return get_ec2_regions(boto3_session)
    except (
        botocore.exceptions.ClientError,
        botocore.exceptions.BotoCoreError,
    ) as e:
        logger.warning(
            "Could not list the enabled regions for EKS discovery (%s); "
            "probing every region in the AWS partition instead",
            e,
        )
        return boto3_session.get_available_regions("eks")


def _discovery_client_config() -> botocore.config.Config:
    return get_botocore_config(max_attempts=_DISCOVERY_MAX_ATTEMPTS).merge(
        botocore.config.Config(connect_timeout=_DISCOVERY_CONNECT_TIMEOUT_SECONDS),
    )


def discover_eks_clusters(
    aws_sync_all_profiles: bool,
    requested_regions: list[str] | None = None,
) -> list[DiscoveredEKSCluster]:
    """
    Enumerate every EKS cluster reachable with the process's AWS credentials
    and return a connected K8sClient for each. Regions or clusters that the
    credentials cannot reach are skipped and logged; a failure to build one
    cluster's client never aborts discovery of the others.
    """
    discovered: list[DiscoveredEKSCluster] = []
    for boto3_session in _boto3_sessions(aws_sync_all_profiles):
        for region in _regions_for_session(boto3_session, requested_regions):
            eks_client = create_boto3_client(
                boto3_session,
                "eks",
                region_name=region,
                config=_discovery_client_config(),
            )
            try:
                cluster_names = [
                    name
                    for page in eks_client.get_paginator("list_clusters").paginate()
                    for name in page.get("clusters", [])
                ]
            except (
                botocore.exceptions.ClientError,
                botocore.exceptions.BotoCoreError,
            ) as e:
                logger.debug(
                    "Skipping EKS discovery in region %s: %s",
                    region,
                    e,
                )
                continue
            for cluster_name in cluster_names:
                try:
                    discovered.append(
                        _connect_cluster(
                            boto3_session,
                            eks_client,
                            cluster_name,
                            region,
                        ),
                    )
                except (
                    botocore.exceptions.ClientError,
                    botocore.exceptions.BotoCoreError,
                ):
                    logger.warning(
                        "Unable to connect to discovered EKS cluster %s in %s",
                        cluster_name,
                        region,
                        exc_info=True,
                    )
    return discovered


def _connect_cluster(
    boto3_session: boto3.Session,
    eks_client: Any,
    cluster_name: str,
    region: str,
) -> DiscoveredEKSCluster:
    cluster = eks_client.describe_cluster(name=cluster_name)["cluster"]
    endpoint = cluster["endpoint"]
    api_client = _build_api_client(
        endpoint,
        cluster["certificateAuthority"]["data"],
        EKSTokenAuthenticator(boto3_session, cluster_name, region),
    )
    k8s_client = K8sClient(
        cluster_name,
        config_file="",
        external_id=cluster["arn"],
        api_client=api_client,
        tls_diagnostics=_tls_diagnostics(endpoint),
    )
    return DiscoveredEKSCluster(
        name=cluster_name,
        arn=cluster["arn"],
        region=region,
        client=k8s_client,
        boto3_session=boto3_session,
    )
