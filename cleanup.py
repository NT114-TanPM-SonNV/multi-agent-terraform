"""
cleanup.py — Quét và xóa AWS resources còn sót sau evaluate.py.

Đọc generated_code từ results JSON, trích resource names từ HCL,
xóa theo đúng thứ tự dependency. Luôn dùng --dry-run trước khi xóa thật.

Usage:
  python cleanup.py --results reviews/dev34.json --dry-run
  python cleanup.py --results reviews/dev34.json
  python cleanup.py --results reviews/dev34.json --rows 3 6 14
"""
import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger("cleanup")

# ── HCL parser ──────────────────────────────────────────────────────────────

_BLOCK_RE = re.compile(
    r'resource\s+"([^"]+)"\s+"([^"]+)"\s*\{', re.DOTALL
)
_ATTR_RE = re.compile(r'^\s*(\w+)\s*=\s*"([^"]+)"', re.MULTILINE)


def _extract_attr(code: str, start: int, attr: str) -> str | None:
    """Trích giá trị của một attribute trong block HCL bắt đầu từ `start`."""
    depth, i = 0, start
    while i < len(code):
        if code[i] == '{':
            depth += 1
        elif code[i] == '}':
            depth -= 1
            if depth == 0:
                block = code[start:i]
                for m in _ATTR_RE.finditer(block):
                    if m.group(1) == attr:
                        return m.group(2)
                return None
        i += 1
    return None


def parse_resources(hcl: str) -> list[tuple[str, str, str | None]]:
    """
    Trả về list (resource_type, tf_name, aws_name) từ HCL.
    aws_name: giá trị attribute định danh AWS (bucket/name/function_name...).
    """
    results = []
    for m in _BLOCK_RE.finditer(hcl):
        rtype, tf_name = m.group(1), m.group(2)
        start = m.end() - 1  # vị trí '{' đầu tiên của block
        # Attribute chính định danh resource trên AWS
        aws_attr = {
            "aws_s3_bucket":                       "bucket",
            "aws_lambda_function":                 "function_name",
            "aws_iam_role":                        "name",
            "aws_iam_policy":                      "name",
            "aws_iam_group":                       "name",
            "aws_codebuild_project":               "name",
            "aws_kinesis_firehose_delivery_stream":"name",
            "aws_cloudwatch_log_group":            "name",
            "aws_cloudwatch_event_rule":           "name",
            "aws_cloudwatch_metric_alarm":         "alarm_name",
            "aws_cloudwatch_composite_alarm":      "alarm_name",
            "aws_ssm_parameter":                   "name",
            "aws_secretsmanager_secret":           "name",
            "aws_kms_alias":                       "name",
            "aws_ecr_repository":                  "repository_name",
            "aws_sns_topic":                       "name",
            "aws_sqs_queue":                       "name",
            "aws_dynamodb_table":                  "name",
            "aws_api_gateway_rest_api":            "name",
            "aws_efs_file_system":                 "creation_token",
            "aws_backup_vault":                    "name",
            "aws_backup_plan":                     "name",
            "aws_route53_zone":                    "name",
            "aws_vpc":                             "tags",  # dùng Name tag
        }.get(rtype, "name")

        aws_name = _extract_attr(hcl, start, aws_attr) or tf_name
        results.append((rtype, tf_name, aws_name))
    return results


def collect_resources(results_path: str, rows: list[int] | None) -> list[tuple[str, str, str | None]]:
    """Đọc results JSON, trả về tất cả (type, tf_name, aws_name) cần xóa."""
    data = json.loads(Path(results_path).read_text())
    all_res: list[tuple[str, str, str | None]] = []
    seen: set[tuple[str, str]] = set()
    for r in data:
        if rows and r["row"] not in rows:
            continue
        code = (r.get("engi") or {}).get("generated_code", "")
        if not code:
            continue
        for entry in parse_resources(code):
            key = (entry[0], entry[2] or entry[1])
            if key not in seen:
                seen.add(key)
                all_res.append(entry)
    return all_res


# ── AWS helpers ──────────────────────────────────────────────────────────────

def _ignore(*codes):
    """Context manager: bỏ qua ClientError với error codes được liệt kê."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        try:
            yield
        except ClientError as e:
            if e.response["Error"]["Code"] in codes:
                pass
            else:
                raise
    return _ctx()


# ── Deleters (mỗi hàm nhận aws_name + boto3 session + dry_run) ──────────────

def del_s3(name: str, s3, dry: bool):
    log.info("S3 bucket: %s", name)
    if dry:
        return
    try:
        paginator = s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=name):
            objs = [{"Key": o["Key"], "VersionId": o["VersionId"]}
                    for o in page.get("Versions", [])]
            objs += [{"Key": o["Key"], "VersionId": o["VersionId"]}
                     for o in page.get("DeleteMarkers", [])]
            if objs:
                s3.delete_objects(Bucket=name, Delete={"Objects": objs})
        # Xóa objects thường (nếu không có versioning)
        paginator2 = s3.get_paginator("list_objects_v2")
        for page in paginator2.paginate(Bucket=name):
            objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objs:
                s3.delete_objects(Bucket=name, Delete={"Objects": objs})
        s3.delete_bucket(Bucket=name)
        log.info("  deleted bucket %s", name)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("NoSuchBucket",):
            log.info("  bucket %s: not found", name)
        elif code == "AccessDenied":
            log.warning("  bucket %s: AccessDenied — xóa tay trên console", name)
        else:
            log.warning("  S3 %s: %s", name, e)


def del_lambda(name: str, lam, dry: bool):
    log.info("Lambda: %s", name)
    if dry:
        return
    with _ignore("ResourceNotFoundException"):
        lam.delete_function(FunctionName=name)
        log.info("  deleted lambda %s", name)


def del_iam_role(name: str, iam, dry: bool):
    log.info("IAM role: %s", name)
    if dry:
        return
    try:
        # Detach managed policies
        try:
            for pg in iam.get_paginator("list_attached_role_policies").paginate(RoleName=name):
                for p in pg["AttachedPolicies"]:
                    with _ignore("NoSuchEntityException"):
                        iam.detach_role_policy(RoleName=name, PolicyArn=p["PolicyArn"])
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchEntityException": raise
        # Delete inline policies
        try:
            for pg in iam.get_paginator("list_role_policies").paginate(RoleName=name):
                for p in pg["PolicyNames"]:
                    with _ignore("NoSuchEntityException"):
                        iam.delete_role_policy(RoleName=name, PolicyName=p)
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchEntityException": raise
        # Remove instance profiles
        try:
            for pg in iam.get_paginator("list_instance_profiles_for_role").paginate(RoleName=name):
                for ip in pg["InstanceProfiles"]:
                    with _ignore("NoSuchEntityException"):
                        iam.remove_role_from_instance_profile(
                            InstanceProfileName=ip["InstanceProfileName"], RoleName=name)
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchEntityException": raise
        iam.delete_role(RoleName=name)
        log.info("  deleted role %s", name)
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchEntityException":
            log.info("  role %s: not found (already deleted)", name)
        else:
            log.warning("  role %s: %s", name, e)


def del_iam_policy(name: str, iam, dry: bool):
    log.info("IAM policy: %s", name)
    if dry:
        return
    acct = boto3.client("sts").get_caller_identity()["Account"]
    arn = f"arn:aws:iam::{acct}:policy/{name}"
    try:
        for pg in iam.get_paginator("list_entities_for_policy").paginate(PolicyArn=arn):
            for role in pg.get("PolicyRoles", []):
                with _ignore("NoSuchEntityException"):
                    iam.detach_role_policy(RoleName=role["RoleName"], PolicyArn=arn)
            for grp in pg.get("PolicyGroups", []):
                with _ignore("NoSuchEntityException"):
                    iam.detach_group_policy(GroupName=grp["GroupName"], PolicyArn=arn)
            for usr in pg.get("PolicyUsers", []):
                with _ignore("NoSuchEntityException"):
                    iam.detach_user_policy(UserName=usr["UserName"], PolicyArn=arn)
        iam.delete_policy(PolicyArn=arn)
        log.info("  deleted policy %s", name)
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchEntityException":
            log.info("  policy %s: not found (already deleted)", name)
        else:
            log.warning("  policy %s: %s", name, e)


def del_iam_group(name: str, iam, dry: bool):
    log.info("IAM group: %s", name)
    if dry:
        return
    with _ignore("NoSuchEntityException"):
        for pg in iam.get_paginator("list_attached_group_policies").paginate(GroupName=name):
            for p in pg["AttachedPolicies"]:
                iam.detach_group_policy(GroupName=name, PolicyArn=p["PolicyArn"])
        for pg in iam.get_paginator("list_group_policies").paginate(GroupName=name):
            for p in pg["PolicyNames"]:
                iam.delete_group_policy(GroupName=name, PolicyName=p)
        # Remove all users from group
        g = iam.get_group(GroupName=name)
        for u in g["Users"]:
            iam.remove_user_from_group(GroupName=name, UserName=u["UserName"])
        iam.delete_group(GroupName=name)
        log.info("  deleted group %s", name)


def del_codebuild(name: str, cb, dry: bool):
    log.info("CodeBuild: %s", name)
    if dry:
        return
    with _ignore("ResourceNotFoundException"):
        cb.delete_project(name=name)
        log.info("  deleted codebuild %s", name)


def del_firehose(name: str, fh, dry: bool):
    log.info("Firehose: %s", name)
    if dry:
        return
    with _ignore("ResourceNotFoundException"):
        fh.delete_delivery_stream(DeliveryStreamName=name)
        log.info("  deleted firehose %s", name)


def del_log_group(name: str, logs, dry: bool):
    log.info("CW log group: %s", name)
    if dry:
        return
    with _ignore("ResourceNotFoundException"):
        logs.delete_log_group(logGroupName=name)
        log.info("  deleted log group %s", name)


def del_cw_alarm(name: str, cw, dry: bool):
    log.info("CW alarm: %s", name)
    if dry:
        return
    with _ignore("ResourceNotFoundException"):
        cw.delete_alarms(AlarmNames=[name])
        log.info("  deleted alarm %s", name)


def del_cw_rule(name: str, events, dry: bool):
    log.info("Events rule: %s", name)
    if dry:
        return
    with _ignore("ResourceNotFoundException"):
        # Phải remove targets trước
        targets = events.list_targets_by_rule(Rule=name).get("Targets", [])
        if targets:
            events.remove_targets(Rule=name, Ids=[t["Id"] for t in targets])
        events.delete_rule(Name=name)
        log.info("  deleted rule %s", name)


def del_ssm(name: str, ssm, dry: bool):
    log.info("SSM param: %s", name)
    if dry:
        return
    with _ignore("ParameterNotFound"):
        ssm.delete_parameter(Name=name)
        log.info("  deleted ssm %s", name)


def del_secret(name: str, sm, dry: bool):
    log.info("Secret: %s", name)
    if dry:
        return
    with _ignore("ResourceNotFoundException"):
        sm.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
        log.info("  deleted secret %s", name)


def del_kms_alias(name: str, kms, dry: bool):
    # name phải bắt đầu bằng "alias/"
    if not name.startswith("alias/"):
        name = f"alias/{name}"
    log.info("KMS alias: %s", name)
    if dry:
        return
    with _ignore("NotFoundException"):
        kms.delete_alias(AliasName=name)
        log.info("  deleted alias %s", name)


def del_sns(name: str, sns, dry: bool):
    log.info("SNS topic: %s", name)
    if dry:
        return
    # Tìm ARN theo name
    try:
        pgs = sns.get_paginator("list_topics")
        for pg in pgs.paginate():
            for t in pg["Topics"]:
                arn = t["TopicArn"]
                if arn.split(":")[-1] == name:
                    sns.delete_topic(TopicArn=arn)
                    log.info("  deleted topic %s", arn)
                    return
        log.info("  SNS topic not found: %s", name)
    except ClientError as e:
        log.warning("  SNS %s: %s", name, e)


def del_sqs(name: str, sqs, dry: bool):
    log.info("SQS queue: %s", name)
    if dry:
        return
    try:
        url = sqs.get_queue_url(QueueName=name)["QueueUrl"]
        sqs.delete_queue(QueueUrl=url)
        log.info("  deleted queue %s", name)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("AWS.SimpleQueueService.NonExistentQueue",):
            pass
        else:
            log.warning("  SQS %s: %s", name, e)


def del_dynamodb(name: str, ddb, dry: bool):
    log.info("DynamoDB table: %s", name)
    if dry:
        return
    with _ignore("ResourceNotFoundException"):
        ddb.delete_table(TableName=name)
        # Đợi xóa xong (non-blocking — chỉ log)
        log.info("  deleted table %s (may take a moment)", name)


def del_api_gw(name: str, apigw, dry: bool):
    log.info("API Gateway: %s", name)
    if dry:
        return
    try:
        apis = apigw.get_rest_apis()["items"]
        for api in apis:
            if api["name"] == name:
                apigw.delete_rest_api(restApiId=api["id"])
                log.info("  deleted API %s (%s)", name, api["id"])
                return
        log.info("  API GW not found: %s", name)
    except ClientError as e:
        log.warning("  API GW %s: %s", name, e)


def del_efs(creation_token: str, efs, dry: bool):
    log.info("EFS: %s", creation_token)
    if dry:
        return
    try:
        fss = efs.describe_file_systems(CreationToken=creation_token)["FileSystems"]
        for fs in fss:
            # Xóa mount targets trước
            mts = efs.describe_mount_targets(FileSystemId=fs["FileSystemId"])["MountTargets"]
            for mt in mts:
                efs.delete_mount_target(MountTargetId=mt["MountTargetId"])
            time.sleep(2)
            efs.delete_file_system(FileSystemId=fs["FileSystemId"])
            log.info("  deleted EFS %s", fs["FileSystemId"])
    except ClientError as e:
        log.warning("  EFS %s: %s", creation_token, e)


def del_backup_vault(name: str, backup, dry: bool):
    log.info("Backup vault: %s", name)
    if dry:
        return
    with _ignore("ResourceNotFoundException"):
        # Xóa recovery points trước
        rps = backup.list_recovery_points_by_backup_vault(BackupVaultName=name).get("RecoveryPoints", [])
        for rp in rps:
            backup.delete_recovery_point(
                BackupVaultName=name,
                RecoveryPointArn=rp["RecoveryPointArn"]
            )
        backup.delete_backup_vault(BackupVaultName=name)
        log.info("  deleted vault %s", name)


def del_backup_plan(name: str, backup, dry: bool):
    log.info("Backup plan: %s", name)
    if dry:
        return
    try:
        plans = backup.list_backup_plans()["BackupPlansList"]
        for p in plans:
            if p["BackupPlanName"] == name:
                backup.delete_backup_plan(BackupPlanId=p["BackupPlanId"])
                log.info("  deleted plan %s", name)
                return
    except ClientError as e:
        log.warning("  Backup plan %s: %s", name, e)


def del_route53_zone(name: str, r53, dry: bool):
    log.info("Route53 zone: %s", name)
    if dry:
        return
    try:
        if not name.endswith("."):
            name += "."
        zones = r53.list_hosted_zones_by_name(DNSName=name)["HostedZones"]
        for z in zones:
            if z["Name"] == name:
                zid = z["Id"].split("/")[-1]
                # Xóa non-SOA/NS records trước
                rrsets = r53.list_resource_record_sets(HostedZoneId=zid)["ResourceRecordSets"]
                changes = [{"Action": "DELETE", "ResourceRecordSet": rr}
                           for rr in rrsets if rr["Type"] not in ("SOA", "NS")]
                if changes:
                    r53.change_resource_record_sets(
                        HostedZoneId=zid, ChangeBatch={"Changes": changes})
                r53.delete_hosted_zone(Id=zid)
                log.info("  deleted zone %s", name)
                return
    except ClientError as e:
        log.warning("  Route53 %s: %s", name, e)


# ── VPC cleanup (dependency-ordered) ────────────────────────────────────────

def del_vpc_by_name(name: str, ec2, dry: bool):
    """Tìm VPC có Name tag = name, xóa toàn bộ dependencies rồi xóa VPC."""
    log.info("VPC (name=%s): tìm kiếm...", name)
    try:
        vpcs = ec2.describe_vpcs(
            Filters=[{"Name": "tag:Name", "Values": [name]},
                     {"Name": "isDefault", "Values": ["false"]}]
        )["Vpcs"]
        if not vpcs:
            log.info("  VPC %s: không tìm thấy hoặc là default", name)
            return
        for vpc in vpcs:
            _del_vpc(vpc["VpcId"], ec2, dry)
    except ClientError as e:
        log.warning("  VPC %s: %s", name, e)


def _del_vpc(vpc_id: str, ec2, dry: bool):
    log.info("  VPC %s", vpc_id)
    if dry:
        return
    try:
        # 1. EC2 instances
        insts = ec2.describe_instances(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]},
                     {"Name": "instance-state-name", "Values": ["running", "stopped"]}]
        )["Reservations"]
        for r in insts:
            for i in r["Instances"]:
                ec2.terminate_instances(InstanceIds=[i["InstanceId"]])
                log.info("    terminated EC2 %s", i["InstanceId"])

        # 2. NAT gateways
        nats = ec2.describe_nat_gateways(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]},
                     {"Name": "state", "Values": ["available"]}]
        )["NatGateways"]
        for nat in nats:
            ec2.delete_nat_gateway(NatGatewayId=nat["NatGatewayId"])
            log.info("    deleted NAT %s", nat["NatGatewayId"])
        if nats:
            time.sleep(5)

        # 3. Internet gateways
        igws = ec2.describe_internet_gateways(
            Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]
        )["InternetGateways"]
        for igw in igws:
            ec2.detach_internet_gateway(InternetGatewayId=igw["InternetGatewayId"], VpcId=vpc_id)
            ec2.delete_internet_gateway(InternetGatewayId=igw["InternetGatewayId"])
            log.info("    deleted IGW %s", igw["InternetGatewayId"])

        # 4. Egress-only IGW
        eigws = ec2.describe_egress_only_internet_gateways(
            Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]
        )["EgressOnlyInternetGateways"]
        for eigw in eigws:
            ec2.delete_egress_only_internet_gateway(
                EgressOnlyInternetGatewayId=eigw["EgressOnlyInternetGatewayId"])

        # 5. VPC endpoints
        eps = ec2.describe_vpc_endpoints(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]},
                     {"Name": "vpc-endpoint-state", "Values": ["available", "pending"]}]
        )["VpcEndpoints"]
        if eps:
            ec2.delete_vpc_endpoints(VpcEndpointIds=[e["VpcEndpointId"] for e in eps])
            log.info("    deleted %d VPC endpoints", len(eps))

        # 6. Security groups (không xóa default)
        sgs = ec2.describe_security_groups(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        )["SecurityGroups"]
        for sg in sgs:
            if sg["GroupName"] == "default":
                continue
            # Revoke ingress/egress rules trước (tránh dependency)
            if sg.get("IpPermissions"):
                ec2.revoke_security_group_ingress(
                    GroupId=sg["GroupId"], IpPermissions=sg["IpPermissions"])
            if sg.get("IpPermissionsEgress"):
                ec2.revoke_security_group_egress(
                    GroupId=sg["GroupId"], IpPermissions=sg["IpPermissionsEgress"])
        for sg in sgs:
            if sg["GroupName"] == "default":
                continue
            with _ignore("InvalidGroup.NotFound", "DependencyViolation"):
                ec2.delete_security_group(GroupId=sg["GroupId"])
                log.info("    deleted SG %s", sg["GroupId"])

        # 7. Route tables (không xóa main)
        rts = ec2.describe_route_tables(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        )["RouteTables"]
        for rt in rts:
            main = any(a.get("Main") for a in rt.get("Associations", []))
            if main:
                continue
            for assoc in rt.get("Associations", []):
                if not assoc.get("Main"):
                    with _ignore("InvalidAssociationID.NotFound"):
                        ec2.disassociate_route_table(
                            AssociationId=assoc["RouteTableAssociationId"])
            with _ignore("InvalidRouteTableID.NotFound", "DependencyViolation"):
                ec2.delete_route_table(RouteTableId=rt["RouteTableId"])
                log.info("    deleted RT %s", rt["RouteTableId"])

        # 8. Subnets
        subnets = ec2.describe_subnets(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        )["Subnets"]
        for sn in subnets:
            with _ignore("InvalidSubnetID.NotFound", "DependencyViolation"):
                ec2.delete_subnet(SubnetId=sn["SubnetId"])
                log.info("    deleted subnet %s", sn["SubnetId"])

        # 9. DHCP options (bỏ association nhưng không xóa default)
        dhcp = ec2.describe_vpc_attribute(VpcId=vpc_id, Attribute="enableDnsSupport")
        # Associate về default DHCP trước khi xóa VPC
        ec2.associate_dhcp_options(DhcpOptionsId="default", VpcId=vpc_id)

        # 10. Xóa VPC
        ec2.delete_vpc(VpcId=vpc_id)
        log.info("  deleted VPC %s", vpc_id)

    except ClientError as e:
        log.warning("  VPC %s cleanup lỗi: %s", vpc_id, e)


# ── Main sweep ───────────────────────────────────────────────────────────────

def sweep(resources: list[tuple[str, str, str | None]], region: str, dry: bool):
    session = boto3.Session(region_name=region)

    clients = {
        "s3":       session.client("s3"),
        "lam":      session.client("lambda"),
        "iam":      session.client("iam"),
        "cb":       session.client("codebuild"),
        "fh":       session.client("firehose"),
        "logs":     session.client("logs"),
        "cw":       session.client("cloudwatch"),
        "events":   session.client("events"),
        "ssm":      session.client("ssm"),
        "sm":       session.client("secretsmanager"),
        "kms":      session.client("kms"),
        "sns":      session.client("sns"),
        "sqs":      session.client("sqs"),
        "ddb":      session.client("dynamodb"),
        "apigw":    session.client("apigateway"),
        "efs":      session.client("efs"),
        "backup":   session.client("backup"),
        "r53":      session.client("route53"),
        "ec2":      session.client("ec2"),
    }

    # Phân loại theo type
    by_type: dict[str, list[str]] = {}
    for rtype, _tf, aws_name in resources:
        by_type.setdefault(rtype, []).append(aws_name or _tf)

    def names(t): return by_type.get(t, [])

    # ── Thứ tự xóa theo dependency ──────────────────────────────────────────

    # 1. Lambda (không dependency downstream)
    for n in names("aws_lambda_function"):    del_lambda(n, clients["lam"], dry)
    # aws_lambda_alias/permission xóa cùng function → bỏ qua

    # 2. API Gateway
    for n in names("aws_api_gateway_rest_api"): del_api_gw(n, clients["apigw"], dry)

    # 3. CloudWatch events/rules
    for n in names("aws_cloudwatch_event_rule"):    del_cw_rule(n, clients["events"], dry)
    for n in names("aws_cloudwatch_metric_alarm"):  del_cw_alarm(n, clients["cw"], dry)
    for n in names("aws_cloudwatch_composite_alarm"): del_cw_alarm(n, clients["cw"], dry)

    # 4. CodeBuild
    for n in names("aws_codebuild_project"):   del_codebuild(n, clients["cb"], dry)

    # 5. Kinesis Firehose
    for n in names("aws_kinesis_firehose_delivery_stream"): del_firehose(n, clients["fh"], dry)

    # 6. SNS
    for n in names("aws_sns_topic"):           del_sns(n, clients["sns"], dry)

    # 7. SQS
    for n in names("aws_sqs_queue"):           del_sqs(n, clients["sqs"], dry)

    # 8. DynamoDB
    for n in names("aws_dynamodb_table"):      del_dynamodb(n, clients["ddb"], dry)

    # 9. EFS
    for n in names("aws_efs_file_system"):     del_efs(n, clients["efs"], dry)

    # 10. Backup (plan → vault)
    for n in names("aws_backup_plan"):         del_backup_plan(n, clients["backup"], dry)
    for n in names("aws_backup_vault"):        del_backup_vault(n, clients["backup"], dry)

    # 11. Route53 (records xóa trong zone deletion)
    for n in names("aws_route53_zone"):        del_route53_zone(n, clients["r53"], dry)

    # 12. S3 (sau khi resource phụ thuộc đã xóa)
    for n in names("aws_s3_bucket"):           del_s3(n, clients["s3"], dry)

    # 13. SSM / Secrets / KMS alias
    for n in names("aws_ssm_parameter"):       del_ssm(n, clients["ssm"], dry)
    for n in names("aws_secretsmanager_secret"): del_secret(n, clients["sm"], dry)
    for n in names("aws_kms_alias"):           del_kms_alias(n, clients["kms"], dry)

    # 14. CloudWatch log groups (sau khi resource emit log xóa xong)
    for n in names("aws_cloudwatch_log_group"): del_log_group(n, clients["logs"], dry)

    # 15. IAM (sau khi resource dùng role xóa xong)
    for n in names("aws_iam_group"):           del_iam_group(n, clients["iam"], dry)
    for n in names("aws_iam_role"):            del_iam_role(n, clients["iam"], dry)
    for n in names("aws_iam_policy"):          del_iam_policy(n, clients["iam"], dry)

    # 16. Networking / VPC (cuối cùng — ENI phải release trước)
    vpc_names = names("aws_vpc")
    if vpc_names:
        log.info("VPC cleanup: tìm theo Name tag (tf_name)")
        for n in vpc_names:
            del_vpc_by_name(n, clients["ec2"], dry)

    log.info("Sweep %s.", "DRY-RUN done" if dry else "DONE")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", required=True,
                    help="Path tới results JSON (evaluate.py output)")
    ap.add_argument("--rows", nargs="+", type=int, default=None,
                    help="Chỉ xử lý các row index này (mặc định: tất cả)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Liệt kê resource sẽ xóa, KHÔNG xóa thật")
    ap.add_argument("--region", default="us-east-1",
                    help="AWS region (mặc định: us-east-1)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.dry_run:
        log.info("=== DRY-RUN mode — không có gì bị xóa ===")

    resources = collect_resources(args.results, args.rows)
    log.info("Tìm thấy %d unique resources cần xóa (từ %s%s)",
             len(resources), args.results,
             f" rows={args.rows}" if args.rows else "")

    if not resources:
        log.info("Không có resource nào. Thoát.")
        sys.exit(0)

    sweep(resources, args.region, args.dry_run)


# ── Public API (gọi từ evaluate.py) ─────────────────────────────────────────

def cleanup_row(
    generated_code: str,
    region: str = "us-east-1",
    dry: bool = False,
    row_idx: int | None = None,
) -> None:
    """Xóa AWS resources của 1 row ngay sau khi pipeline hoàn tất.

    Thiết kế: chạy SAU auto_destroy của A5, đóng vai trò safety net. Idempotent —
    resource đã xóa sẽ trả NotFound và bị bỏ qua. Nuốt mọi exception để không
    làm fail row result.
    """
    if not generated_code or not generated_code.strip():
        return
    label = f"[cleanup row={row_idx}]" if row_idx is not None else "[cleanup]"
    try:
        resources = parse_resources(generated_code)
        if not resources:
            return
        log.info("%s sweep %d resources", label, len(resources))
        sweep(resources, region, dry)
        log.info("%s done", label)
    except Exception as e:
        log.warning("%s lỗi (ignored): %s", label, e)


if __name__ == "__main__":
    main()
