"""Render a reviewable CloudFormation template and bounded parameters. No AWS calls."""

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def ref(name):
    return {"Ref": name}


def sub(value):
    return {"Fn::Sub": value}


def arn(name):
    return {"Fn::GetAtt": [name, "Arn"]}


def policy(statements):
    return {"Version": "2012-10-17", "Statement": statements}


def allow(actions, resources):
    return {"Effect": "Allow", "Action": actions, "Resource": resources}


def build_template():
    resources = {}

    def add(name, kind, properties, **extra):
        resources[name] = {"Type": "AWS::" + kind, "Properties": properties, **extra}

    tags = [{"Key": "Project", "Value": "RelayPrivateRehearsal"}]
    add(
        "Network",
        "EC2::VPC",
        {
            "CidrBlock": "10.87.0.0/24",
            "EnableDnsSupport": True,
            "EnableDnsHostnames": True,
            "Tags": tags,
        },
    )
    add("Gateway", "EC2::InternetGateway", {"Tags": tags})
    add(
        "GatewayAttachment",
        "EC2::VPCGatewayAttachment",
        {
            "VpcId": ref("Network"),
            "InternetGatewayId": ref("Gateway"),
        },
    )
    add(
        "Subnet",
        "EC2::Subnet",
        {
            "VpcId": ref("Network"),
            "CidrBlock": "10.87.0.0/26",
            "AvailabilityZone": ref("AvailabilityZone"),
            "MapPublicIpOnLaunch": True,
            "Tags": tags,
        },
    )
    add("Routes", "EC2::RouteTable", {"VpcId": ref("Network"), "Tags": tags})
    add(
        "InternetRoute",
        "EC2::Route",
        {
            "RouteTableId": ref("Routes"),
            "DestinationCidrBlock": "0.0.0.0/0",
            "GatewayId": ref("Gateway"),
        },
        DependsOn="GatewayAttachment",
    )
    add(
        "RouteAssociation",
        "EC2::SubnetRouteTableAssociation",
        {
            "SubnetId": ref("Subnet"),
            "RouteTableId": ref("Routes"),
        },
    )
    add(
        "HostSecurityGroup",
        "EC2::SecurityGroup",
        {
            "GroupDescription": "No ingress; TLS outbound for SSM, packages, Cognito and S3",
            "VpcId": ref("Network"),
            "SecurityGroupIngress": [],
            "SecurityGroupEgress": [
                {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "CidrIp": "0.0.0.0/0"}
            ],
            "Tags": tags,
        },
    )
    add(
        "BackupBucket",
        "S3::Bucket",
        {
            "BucketEncryption": {
                "ServerSideEncryptionConfiguration": [
                    {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                ]
            },
            "PublicAccessBlockConfiguration": dict.fromkeys(
                [
                    "BlockPublicAcls",
                    "BlockPublicPolicy",
                    "IgnorePublicAcls",
                    "RestrictPublicBuckets",
                ],
                True,
            ),
            "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]},
            "VersioningConfiguration": {"Status": "Enabled"},
            "LifecycleConfiguration": {
                "Rules": [
                    {
                        "Id": "SyntheticRehearsalRetention",
                        "Status": "Enabled",
                        "Prefix": "",
                        "ExpirationInDays": 7,
                        "NoncurrentVersionExpiration": {"NoncurrentDays": 7},
                        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
                    },
                    {
                        "Id": "ExpiredMarkers",
                        "Status": "Enabled",
                        "Prefix": "",
                        "ExpiredObjectDeleteMarker": True,
                    },
                ]
            },
            "Tags": tags,
        },
        DeletionPolicy="Retain",
        UpdateReplacePolicy="Retain",
    )
    add(
        "BackupPolicy",
        "S3::BucketPolicy",
        {
            "Bucket": ref("BackupBucket"),
            "PolicyDocument": policy(
                [
                    {
                        "Effect": "Deny",
                        "Principal": "*",
                        "Action": "s3:*",
                        "Resource": [arn("BackupBucket"), sub("${BackupBucket.Arn}/*")],
                        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                    },
                ]
            ),
        },
        DeletionPolicy="Retain",
        UpdateReplacePolicy="Retain",
    )
    add(
        "DataVolume",
        "EC2::Volume",
        {
            "AvailabilityZone": ref("AvailabilityZone"),
            "Size": 12,
            "VolumeType": "gp3",
            "Encrypted": True,
            "Tags": tags,
        },
        DeletionPolicy="Retain",
        UpdateReplacePolicy="Retain",
    )
    add(
        "HostRole",
        "IAM::Role",
        {
            "AssumeRolePolicyDocument": policy(
                [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "ec2.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    }
                ]
            ),
            "ManagedPolicyArns": [
                sub("arn:${AWS::Partition}:iam::aws:policy/AmazonSSMManagedInstanceCore")
            ],
            "Policies": [
                {
                    "PolicyName": "RehearsalBackupAndRelease",
                    "PolicyDocument": policy(
                        [
                            allow(
                                ["s3:PutObject", "s3:AbortMultipartUpload"],
                                [sub("${BackupBucket.Arn}/relay/*")],
                            ),
                            allow(["s3:GetObject"], [sub("${BackupBucket.Arn}/release/*")]),
                        ]
                    ),
                }
            ],
            "Tags": tags,
        },
    )
    add("HostProfile", "IAM::InstanceProfile", {"Roles": [ref("HostRole")]})
    bootstrap = (ROOT / "deploy/bootstrap-host.sh").read_text()
    bootstrap = bootstrap.replace("@@EXPIRY@@", "${ExpiryUtc}")
    bootstrap = bootstrap.replace("@@VOLUME@@", "${DataVolume}")
    add(
        "Host",
        "EC2::Instance",
        {
            "ImageId": ref("AmiId"),
            "InstanceType": "t3.small",
            "AvailabilityZone": ref("AvailabilityZone"),
            "IamInstanceProfile": ref("HostProfile"),
            "CreditSpecification": {"CPUCredits": "standard"},
            "InstanceInitiatedShutdownBehavior": "stop",
            "MetadataOptions": {
                "HttpTokens": "required",
                "HttpEndpoint": "enabled",
                "HttpPutResponseHopLimit": 2,
            },
            "NetworkInterfaces": [
                {
                    "DeviceIndex": "0",
                    "AssociatePublicIpAddress": True,
                    "SubnetId": ref("Subnet"),
                    "GroupSet": [ref("HostSecurityGroup")],
                }
            ],
            "BlockDeviceMappings": [
                {
                    "DeviceName": "/dev/xvda",
                    "Ebs": {
                        "VolumeSize": 8,
                        "VolumeType": "gp3",
                        "Encrypted": True,
                        "DeleteOnTermination": True,
                    },
                }
            ],
            "UserData": {"Fn::Base64": sub(bootstrap)},
            "Tags": tags + [{"Key": "ExpiresUtc", "Value": ref("ExpiryUtc")}],
        },
        DependsOn=["InternetRoute", "RouteAssociation"],
    )
    add(
        "DataAttachment",
        "EC2::VolumeAttachment",
        {
            "Device": "/dev/sdf",
            "InstanceId": ref("Host"),
            "VolumeId": ref("DataVolume"),
        },
    )
    add("ExpiryGroup", "Scheduler::ScheduleGroup", {"Tags": tags})
    add(
        "ExpiryRole",
        "IAM::Role",
        {
            "AssumeRolePolicyDocument": policy(
                [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "scheduler.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                        "Condition": {
                            "StringEquals": {"aws:SourceAccount": ref("AWS::AccountId")},
                            "ArnEquals": {"aws:SourceArn": arn("ExpiryGroup")},
                        },
                    }
                ]
            ),
            "Policies": [
                {
                    "PolicyName": "StopOnlyThisHost",
                    "PolicyDocument": policy(
                        [
                            allow(
                                ["ec2:StopInstances"],
                                [
                                    sub(
                                        "arn:${AWS::Partition}:ec2:${AWS::Region}:${AWS::AccountId}:instance/${Host}"
                                    )
                                ],
                            ),
                        ]
                    ),
                }
            ],
            "Tags": tags,
        },
    )
    add(
        "AbsoluteStop",
        "Scheduler::Schedule",
        {
            "GroupName": ref("ExpiryGroup"),
            "ScheduleExpression": sub("at(${ExpiryUtc})"),
            "ScheduleExpressionTimezone": "UTC",
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "State": "ENABLED",
            "Target": {
                "Arn": sub("arn:${AWS::Partition}:scheduler:::aws-sdk:ec2:stopInstances"),
                "RoleArn": arn("ExpiryRole"),
                "Input": sub('{"InstanceIds":["${Host}"]}'),
                "RetryPolicy": {"MaximumEventAgeInSeconds": 3600, "MaximumRetryAttempts": 5},
            },
        },
    )
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Relay private synthetic rehearsal; reviewed parameters and hosting approval required",
        "Parameters": {
            "AmiId": {
                "Type": "AWS::EC2::Image::Id",
                "Description": "Resolved AL2023 x86_64 AMI; pin before launch",
            },
            "AvailabilityZone": {"Type": "AWS::EC2::AvailabilityZone::Name"},
            "ExpiryUtc": {
                "Type": "String",
                "AllowedPattern": r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}",
                "Description": "Absolute UTC stop deadline; at most 24 hours from approval",
            },
        },
        "Rules": {
            "Region": {
                "Assertions": [
                    {
                        "Assert": {"Fn::Equals": [ref("AWS::Region"), "us-east-1"]},
                        "AssertDescription": "This reviewed package is limited to us-east-1",
                    }
                ]
            }
        },
        "Resources": resources,
        "Outputs": {
            name: {"Value": ref(resource)}
            for name, resource in {
                "InstanceId": "Host",
                "RetainedDataVolumeId": "DataVolume",
                "RetainedBucketName": "BackupBucket",
                "ExpiryScheduleName": "AbsoluteStop",
                "ExpiryScheduleGroup": "ExpiryGroup",
                "DeadlineUtc": "ExpiryUtc",
            }.items()
        },
    }


def launch_parameters(ami, zone, expiry, now=None):
    now = now or datetime.now(UTC)
    deadline = datetime.strptime(expiry, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    if not timedelta(minutes=30) <= deadline - now <= timedelta(hours=24):
        raise ValueError("Deadline must be 30 minutes to 24 hours from now")
    import re

    if not re.fullmatch(r"ami-[0-9a-f]{17}", ami):
        raise ValueError("A pinned AMI ID is required")
    if not re.fullmatch(r"us-east-1[a-z]", zone):
        raise ValueError("Choose a us-east-1 availability zone")
    return [
        {"ParameterKey": key, "ParameterValue": value}
        for key, value in {
            "AmiId": ami,
            "AvailabilityZone": zone,
            "ExpiryUtc": expiry,
        }.items()
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parameters", type=Path)
    parser.add_argument("--ami")
    parser.add_argument("--zone")
    parser.add_argument("--expiry")
    args = parser.parse_args()
    template = ROOT / "infra/private-rehearsal.json"
    template.write_text(json.dumps(build_template(), indent=2) + "\n")
    if args.parameters:
        if not all([args.ami, args.zone, args.expiry]):
            parser.error("--parameters requires --ami, --zone and --expiry")
        parameters = launch_parameters(args.ami, args.zone, args.expiry)
        args.parameters.write_text(json.dumps(parameters, indent=2) + "\n")
    print("Prepared local configuration only; no AWS calls or launch authorization implied.")


if __name__ == "__main__":
    main()
