import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts.package_aws_release import sha256, verify_release
from scripts.prepare_aws_launch import ROOT, build_template, launch_parameters


def test_reviewed_template_matches_generator_and_has_no_unresolved_shell_substitutions():
    template = build_template()
    assert template == json.loads((ROOT / "infra/private-rehearsal.json").read_text())
    userdata = template["Resources"]["Host"]["Properties"]["UserData"]["Fn::Base64"]["Fn::Sub"]
    import re

    assert set(re.findall(r"\$\{([^}]+)\}", userdata)) == {"ExpiryUtc", "DataVolume"}
    assert "@@" not in userdata


def test_private_launch_guards_and_retention():
    resources = build_template()["Resources"]
    host = resources["Host"]["Properties"]
    assert host["CreditSpecification"]["CPUCredits"] == "standard"
    assert host["InstanceInitiatedShutdownBehavior"] == "stop"
    assert host["MetadataOptions"]["HttpTokens"] == "required"
    assert host["MetadataOptions"]["HttpPutResponseHopLimit"] == 2
    assert resources["HostSecurityGroup"]["Properties"]["SecurityGroupIngress"] == []
    assert resources["DataVolume"]["DeletionPolicy"] == "Retain"
    assert resources["DataVolume"]["Properties"]["Encrypted"] is True
    assert resources["BackupBucket"]["DeletionPolicy"] == "Retain"
    role = resources["HostRole"]["Properties"]["Policies"]
    assert "DeleteObject" not in json.dumps(role)
    assert "bedrock" not in json.dumps(role).lower()
    assert "ses:" not in json.dumps(role).lower()
    stop = resources["AbsoluteStop"]["Properties"]
    assert stop["ScheduleExpression"] == {"Fn::Sub": "at(${ExpiryUtc})"}
    assert stop["Target"]["Input"] == {"Fn::Sub": '{"InstanceIds":["${Host}"]}'}


@pytest.mark.parametrize("hours", [-1, 0, 25])
def test_parameters_reject_expired_or_unbounded_deadlines(hours):
    now = datetime(2026, 9, 14, tzinfo=UTC)
    expiry = (now + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    with pytest.raises(ValueError, match="Deadline"):
        launch_parameters("ami-0123456789abcdef0", "us-east-1a", expiry, now)


def test_parameters_require_pinned_ami_and_reviewed_region():
    now = datetime(2026, 9, 14, tzinfo=UTC)
    expiry = "2026-09-15T00:00:00"
    assert len(launch_parameters("ami-0123456789abcdef0", "us-east-1a", expiry, now)) == 3
    with pytest.raises(ValueError, match="AMI"):
        launch_parameters("latest", "us-east-1a", expiry, now)
    with pytest.raises(ValueError, match="availability zone"):
        launch_parameters("ami-0123456789abcdef0", "us-west-2a", expiry, now)


def test_packaging_rejects_changed_release_before_export(tmp_path):
    (tmp_path / "docs").mkdir()
    source = tmp_path / "source.py"
    source.write_text("tested")
    manifest = {"source_sha256": {"source.py": sha256(source)}}
    (tmp_path / "docs/deployment-release-manifest.json").write_text(json.dumps(manifest))
    source.write_text("changed")
    with pytest.raises(ValueError, match="changed since rehearsal"):
        verify_release(tmp_path)
