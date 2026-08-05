from unittest import mock

from cartography.intel.aws.ec2 import security_groups


def test_loads_primary_ip_rule_schema_before_shared_inbound_label():
    data = security_groups.Ec2SecurityGroupData(
        groups=[],
        inbound_rules=[{"RuleId": "inbound"}],
        egress_rules=[{"RuleId": "egress"}],
        ranges=[],
    )

    with (
        mock.patch.object(security_groups, "load"),
        mock.patch.object(security_groups, "load_ip_rules") as load_ip_rules,
        mock.patch.object(security_groups, "load_ip_ranges"),
    ):
        security_groups.load_ec2_security_groupinfo(
            mock.Mock(),
            data,
            "ap-south-1",
            "123456789012",
            123,
        )

    assert load_ip_rules.call_args_list[0].kwargs["inbound"] is False
    assert load_ip_rules.call_args_list[0].args[1] == data.egress_rules
    assert load_ip_rules.call_args_list[1].kwargs["inbound"] is True
    assert load_ip_rules.call_args_list[1].args[1] == data.inbound_rules
