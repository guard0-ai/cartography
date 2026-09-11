import json
from types import SimpleNamespace

from cartography.intel.kubernetes.pods import transform_pods


def test_transform_pods_defaults_service_account_name():
    pod = SimpleNamespace(
        metadata=SimpleNamespace(
            uid="pod-1",
            name="default-sa-pod",
            namespace="my-namespace",
            creation_timestamp=None,
            deletion_timestamp=None,
            labels={},
        ),
        spec=SimpleNamespace(
            containers=[],
            volumes=[],
            node_name="node-a",
            service_account_name=None,
        ),
        status=SimpleNamespace(phase="Running", container_statuses=[]),
    )

    transformed = transform_pods([pod], "my-cluster-1")

    assert transformed == [
        {
            "uid": "pod-1",
            "name": "default-sa-pod",
            "status_phase": "Running",
            "creation_timestamp": None,
            "deletion_timestamp": None,
            "namespace": "my-namespace",
            "service_account_name": "default",
            "automount_service_account_token": None,
            "host_pid": None,
            "host_ipc": None,
            "host_network": None,
            "seccomp_profile_type": None,
            "host_path_volume_paths": [],
            "service_account_id": "my-cluster-1/my-namespace/default",
            "node": "node-a",
            "node_id": "my-cluster-1/node-a",
            "architecture_normalized": None,
            "labels": "{}",
            "owner_kind": None,
            "owner_name": None,
            "workload_kind": "Pod",
            "workload_name": "default-sa-pod",
            "containers": [],
            "secret_volume_ids": [],
            "secret_env_ids": [],
        },
    ]


def test_transform_pods_propagates_node_architecture_to_pod_and_container():
    pod = SimpleNamespace(
        metadata=SimpleNamespace(
            uid="pod-2",
            name="arch-pod",
            namespace="my-namespace",
            creation_timestamp=None,
            deletion_timestamp=None,
            labels={},
        ),
        spec=SimpleNamespace(
            containers=[
                SimpleNamespace(
                    name="app",
                    image="example:latest",
                    image_pull_policy="IfNotPresent",
                    resources=None,
                    env=None,
                    env_from=None,
                ),
            ],
            volumes=[],
            node_name="node-a",
            service_account_name="default",
        ),
        status=SimpleNamespace(phase="Running", container_statuses=[]),
    )

    transformed = transform_pods(
        [pod],
        "my-cluster-1",
        node_arch_map={"node-a": "arm64"},
    )

    assert transformed[0]["architecture_normalized"] == "arm64"
    assert transformed[0]["containers"][0]["image_pull_policy"] == "IfNotPresent"
    assert transformed[0]["containers"][0]["architecture_normalized"] == "arm64"


def test_transform_pods_extracts_container_ports():
    pod = SimpleNamespace(
        metadata=SimpleNamespace(
            uid="pod-3",
            name="ports-pod",
            namespace="my-namespace",
            creation_timestamp=None,
            deletion_timestamp=None,
            labels={},
        ),
        spec=SimpleNamespace(
            containers=[
                SimpleNamespace(
                    name="app",
                    image="example:latest",
                    image_pull_policy="IfNotPresent",
                    resources=None,
                    env=None,
                    env_from=None,
                    ports=[
                        SimpleNamespace(
                            container_port=8080,
                            protocol="TCP",
                            name="http",
                            host_port=None,
                        ),
                        SimpleNamespace(
                            container_port=53,
                            protocol="UDP",
                            name="dns",
                            host_port=30053,
                        ),
                        # SCTP and protocol-less ports are retained in the raw
                        # spec but excluded from the flat TCP/UDP number list.
                        SimpleNamespace(
                            container_port=9000,
                            protocol="SCTP",
                            name="sctp",
                            host_port=None,
                        ),
                    ],
                ),
            ],
            volumes=[],
            node_name="node-a",
            service_account_name="default",
        ),
        status=SimpleNamespace(phase="Running", container_statuses=[]),
    )

    transformed = transform_pods([pod], "my-cluster-1")
    container = transformed[0]["containers"][0]

    assert container["container_port_numbers"] == [53, 8080]
    assert container["host_ports"] == [30053]
    assert json.loads(container["container_ports"]) == [
        {"container_port": 8080, "protocol": "TCP", "name": "http"},
        {"container_port": 53, "protocol": "UDP", "name": "dns"},
        {"container_port": 9000, "protocol": "SCTP", "name": "sctp"},
    ]


def _owned_pod(name, labels, owner_references):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            uid=f"uid-{name}",
            name=name,
            namespace="my-namespace",
            creation_timestamp=None,
            deletion_timestamp=None,
            labels=labels,
            owner_references=owner_references,
        ),
        spec=SimpleNamespace(
            containers=[],
            volumes=[],
            node_name="node-a",
            service_account_name="default",
        ),
        status=SimpleNamespace(phase="Running", container_statuses=[]),
    )


def test_transform_pods_resolves_deployment_from_replicaset_owner():
    pod = _owned_pod(
        "web-frontend-7d9f8b6c5-abcde",
        {"pod-template-hash": "7d9f8b6c5"},
        [
            SimpleNamespace(
                kind="ReplicaSet", name="web-frontend-7d9f8b6c5", controller=True
            )
        ],
    )

    transformed = transform_pods([pod], "my-cluster-1")[0]

    assert transformed["owner_kind"] == "ReplicaSet"
    assert transformed["owner_name"] == "web-frontend-7d9f8b6c5"
    assert transformed["workload_kind"] == "Deployment"
    assert transformed["workload_name"] == "web-frontend"


def test_transform_pods_strips_last_segment_when_template_hash_label_is_missing():
    pod = _owned_pod(
        "api-5c6d7e8f9-xyz12",
        {},
        [SimpleNamespace(kind="ReplicaSet", name="api-5c6d7e8f9", controller=True)],
    )

    transformed = transform_pods([pod], "my-cluster-1")[0]

    assert transformed["workload_kind"] == "Deployment"
    assert transformed["workload_name"] == "api"


def test_transform_pods_uses_non_replicaset_owner_as_workload():
    pod = _owned_pod(
        "database-0",
        {},
        [SimpleNamespace(kind="StatefulSet", name="database", controller=True)],
    )

    transformed = transform_pods([pod], "my-cluster-1")[0]

    assert transformed["owner_kind"] == "StatefulSet"
    assert transformed["workload_kind"] == "StatefulSet"
    assert transformed["workload_name"] == "database"


def test_transform_pods_prefers_the_controller_owner_reference():
    pod = _owned_pod(
        "worker-abc",
        {},
        [
            SimpleNamespace(kind="ConfigMap", name="settings", controller=False),
            SimpleNamespace(kind="DaemonSet", name="worker", controller=True),
        ],
    )

    transformed = transform_pods([pod], "my-cluster-1")[0]

    assert transformed["owner_kind"] == "DaemonSet"
    assert transformed["workload_name"] == "worker"
