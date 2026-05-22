"""Wrapper cho Terraform CLI, Checkov, và Floci — dùng chung cho toàn pipeline.

_TF_ENV đảm bảo mọi subprocess đều dùng plugin cache — tránh download
provider hàng trăm lần khi chạy benchmark.
"""
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Set

# Cache provider giữa các lần gọi terraform — đặt ngoài thư mục tmp
# để tồn tại xuyên suốt toàn bộ benchmark
_TF_CACHE_DIR = Path(__file__).parent.parent / ".tf_plugin_cache"
_TF_CACHE_DIR.mkdir(exist_ok=True)

# Env dùng chung cho mọi subprocess terraform — inject cache dir.
# MAY_BREAK_DEPENDENCY_LOCK_FILE: cho phép dùng plugin cache khi init trong
# thư mục chưa có .terraform.lock.hcl (tránh lỗi checksum mismatch ngẫu nhiên).
_TF_ENV = {
    **os.environ,
    "TF_PLUGIN_CACHE_DIR": str(_TF_CACHE_DIR),
    "TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE": "true",
}

_REQUIRED_TOOLS = ("checkov", "terraform", "opa")


def substitute_endpoint(hcl: str, endpoint: str) -> str:
    """Thay localhost:4566 trong provider block bằng floci_endpoint thực.

    Cần thiết khi floci_endpoint khác http://localhost:4566 (vd: port khác,
    remote host). Không thay nếu endpoint rỗng.
    """
    if not endpoint:
        return hcl
    return re.sub(r"https?://(?:localhost|127\.0\.0\.1):4566", endpoint.rstrip("/"), hcl)


def check_required_tools() -> None:
    """Kiểm tra các công cụ bắt buộc có trong PATH không.

    Gọi một lần lúc startup để fail fast thay vì crash giữa benchmark.
    """
    missing = [t for t in _REQUIRED_TOOLS if not shutil.which(t)]
    if missing:
        raise RuntimeError(f"Công cụ chưa được cài: {', '.join(missing)}")


def run_terraform(cmd: list[str], cwd: str | Path, timeout: int) -> subprocess.CompletedProcess:
    """Chạy lệnh terraform với plugin cache và timeout tường minh.

    Không bắt TimeoutExpired ở đây — để agent gọi tự xử lý
    vì mỗi agent có cách route khác nhau khi timeout.
    """
    # Timeout với Popen + wait để ensure cleanup (subprocess.run timeout sometimes hangs)
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_TF_ENV,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    except subprocess.TimeoutExpired:
        proc.kill()  # Forcefully terminate if timeout
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass  # Already dead
        raise


# ── Floci (LocalStack) support ────────────────────────────────────────────────

# Các Terraform resource type mà Floci community hỗ trợ.
# Dùng bởi graph.py (floci_check_node) để gate trước khi chạy pipeline.
FLOCI_SUPPORTED: Set[str] = {
    # S3
    "aws_s3_bucket", "aws_s3_object", "aws_s3_bucket_object",
    "aws_s3_bucket_versioning", "aws_s3_bucket_server_side_encryption_configuration",
    "aws_s3_bucket_public_access_block", "aws_s3_bucket_policy",
    "aws_s3_bucket_acl", "aws_s3_bucket_logging", "aws_s3_bucket_metric",
    "aws_s3_bucket_notification", "aws_s3_bucket_object_lock_configuration",
    "aws_s3_bucket_request_payment_configuration", "aws_s3_bucket_inventory",
    "aws_s3_bucket_cors_configuration", "aws_s3_bucket_website_configuration",
    "aws_s3_bucket_lifecycle_configuration", "aws_s3_bucket_accelerate_configuration",
    "aws_s3_bucket_analytics_configuration", "aws_s3_bucket_ownership_controls",
    "aws_s3_bucket_intelligent_tiering_configuration",
    # EC2 / VPC / Networking
    "aws_instance", "aws_security_group", "aws_vpc", "aws_subnet",
    "aws_internet_gateway", "aws_route_table", "aws_route_table_association",
    "aws_eip", "aws_nat_gateway", "aws_key_pair", "aws_network_interface",
    "aws_ami", "aws_launch_template", "aws_placement_group", "aws_ec2_fleet",
    "aws_egress_only_internet_gateway", "aws_network_acl", "aws_default_network_acl",
    "aws_vpc_dhcp_options", "aws_vpc_dhcp_options_association",
    "aws_vpc_peering_connection",
    "aws_vpc_security_group_egress_rule", "aws_vpc_security_group_ingress_rule",
    # IAM
    "aws_iam_role", "aws_iam_policy", "aws_iam_role_policy",
    "aws_iam_role_policy_attachment", "aws_iam_instance_profile",
    "aws_iam_policy_document", "aws_iam_user", "aws_iam_group",
    "aws_iam_group_membership", "aws_iam_group_policy",
    "aws_iam_group_policy_attachment", "aws_iam_user_ssh_key",
    "aws_iam_virtual_mfa_device",
    # RDS
    "aws_db_instance", "aws_db_subnet_group", "aws_db_parameter_group",
    "aws_db_snapshot", "aws_db_option_group", "aws_rds_cluster",
    "aws_rds_cluster_instance", "aws_rds_cluster_parameter_group",
    "aws_db_proxy", "aws_db_proxy_default_target_group",
    # DynamoDB
    "aws_dynamodb_table", "aws_dynamodb_global_table",
    "aws_dynamodb_contributor_insights", "aws_dynamodb_table_item",
    "aws_dynamodb_table_replica", "aws_dynamodb_kinesis_streaming_destination",
    # Lambda
    "aws_lambda_function", "aws_lambda_permission",
    "aws_lambda_event_source_mapping", "aws_lambda_function_url",
    "aws_lambda_invocation", "aws_lambda_alias", "aws_lambda_layer_version",
    # Messaging / Streaming
    "aws_sqs_queue", "aws_sqs_queue_policy",
    "aws_sns_topic", "aws_sns_topic_subscription", "aws_sns_topic_policy",
    "aws_kinesis_stream", "aws_kinesis_stream_consumer",
    "aws_kinesis_firehose_delivery_stream",
    # KMS / Secrets
    "aws_kms_key", "aws_kms_alias",
    "aws_secretsmanager_secret", "aws_secretsmanager_secret_version",
    # CloudWatch / Events
    "aws_cloudwatch_log_group", "aws_cloudwatch_log_stream",
    "aws_cloudwatch_log_resource_policy",
    "aws_cloudwatch_metric_alarm", "aws_cloudwatch_composite_alarm",
    "aws_cloudwatch_event_rule", "aws_cloudwatch_event_target",
    "aws_scheduler_schedule",
    # Route53 / ACM
    "aws_route53_zone", "aws_route53_record", "aws_route53_health_check",
    "aws_route53_query_log", "aws_route53_zone_association",
    "aws_route53_traffic_policy", "aws_route53_traffic_policy_instance",
    "aws_acm_certificate", "aws_acm_certificate_validation",
    # Load Balancing / Auto Scaling
    "aws_lb", "aws_lb_listener", "aws_lb_target_group",
    "aws_lb_listener_rule", "aws_lb_target_group_attachment", "aws_elb",
    "aws_autoscaling_group", "aws_launch_configuration",
    # Containers
    "aws_ecs_cluster", "aws_ecs_task_definition", "aws_ecs_service",
    "aws_ecr_repository", "aws_ecr_lifecycle_policy", "aws_ecr_image",
    "aws_eks_cluster", "aws_eks_node_group", "aws_eks_fargate_profile",
    "aws_eks_addon", "aws_eks_cluster_auth", "aws_eks_access_entry",
    "aws_eks_access_policy_association", "aws_eks_identity_provider_config",
    "aws_eks_pod_identity_association",
    # ElastiCache / Kafka
    "aws_elasticache_cluster", "aws_elasticache_replication_group",
    "aws_elasticache_subnet_group", "aws_elasticache_user",
    "aws_elasticache_user_group", "aws_elasticache_user_group_association",
    "aws_msk_cluster", "aws_msk_configuration", "aws_msk_serverless_cluster",
    "aws_mskconnect_connector", "aws_mskconnect_custom_plugin",
    # Cognito / API Gateway
    "aws_cognito_user_pool", "aws_cognito_user_pool_client",
    "aws_cognito_user_pool_domain",
    "aws_api_gateway_rest_api", "aws_api_gateway_resource",
    "aws_api_gateway_method", "aws_api_gateway_integration",
    "aws_api_gateway_deployment", "aws_api_gateway_stage",
    "aws_api_gateway_authorizer", "aws_api_gateway_method_response",
    "aws_api_gateway_integration_response",
    "aws_apigatewayv2_api", "aws_apigatewayv2_stage",
    "aws_apigatewayv2_integration", "aws_apigatewayv2_route",
    # SSM / Step Functions / Misc
    "aws_ssm_parameter", "aws_ssm_document",
    "aws_sfn_state_machine",
    "aws_cloudformation_stack",
    "aws_codebuild_project",
    "aws_codedeploy_app", "aws_codedeploy_deployment_group",
    "aws_backup_vault", "aws_backup_plan", "aws_backup_selection",
    "aws_glue_catalog_database", "aws_glue_crawler", "aws_glue_job",
    "aws_athena_workgroup", "aws_athena_database",
    "aws_elasticsearch_domain", "aws_opensearch_domain",
    "aws_appconfig_application", "aws_appconfig_environment",
    "aws_ses_email_identity", "aws_ses_domain_identity",
    "aws_pipes_pipe",
    "aws_transfer_server", "aws_transfer_user",
    "aws_bedrock_model_invocation_logging_configuration",
    # Terraform built-ins (không cần AWS)
    "archive_file",
    "random_string", "random_id", "random_password", "random_uuid", "random_integer",
    "null_resource",
    "local_file",
    # AWS data sources (luôn available)
    "aws_availability_zones", "aws_region", "aws_caller_identity",
    "aws_eks_cluster_auth", "aws_partition",
}


def check_floci_health(endpoint: str, timeout: int = 5) -> bool:
    """Kiểm tra Floci (LocalStack) đang chạy và reachable."""
    try:
        url = endpoint.rstrip("/") + "/_localstack/health"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


_CKV_HEADER = re.compile(r"^Check:\s+(CKV2?_AWS_\d+):")
_CKV_FAILED_RES = re.compile(r"^\s*FAILED for resource:\s+(\S+)", re.MULTILINE)


def run_checkov_on_hcl(hcl: str, timeout: int = 60,
                       check_ids: list[str] | None = None) -> dict:
    """Chạy Checkov trên HCL string, trả dict structured.

    check_ids: nếu truyền, chỉ chạy các CKV IDs đó (--check flag).
               None = chạy tất cả rules (dùng cho scan toàn bộ).

    Returns:
        {
          "failed_ckv_ids":      sorted list of CKV IDs failed,
          "failed_per_resource": list of (resource_addr, ckv_id),
          "passed_count":        int,
          "failed_count":        int,
          "total_checks":        int,
          "scan_seconds":        float,
        }
    """
    checkov_bin = os.environ.get("CHECKOV_BIN") or shutil.which("checkov")
    if not checkov_bin:
        raise RuntimeError("checkov not found — set CHECKOV_BIN in .env or add to PATH")

    cmd = [checkov_bin, "-d", ".", "--framework", "terraform", "--quiet", "--compact"]
    if check_ids:
        cmd += ["--check", ",".join(sorted(set(check_ids)))]

    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="checkov_") as tmpdir:
        (Path(tmpdir) / "main.tf").write_text(hcl)
        try:
            proc = subprocess.run(
                cmd,
                cwd=tmpdir,
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Checkov timeout after {timeout}s")
        out = proc.stdout + "\n" + proc.stderr

    elapsed = round(time.time() - t0, 2)

    passed = failed = 0
    m = re.search(r"Passed checks:\s*(\d+),\s*Failed checks:\s*(\d+)", out)
    if m:
        passed, failed = int(m.group(1)), int(m.group(2))

    failed_ids: set[str] = set()
    failed_pairs: list[tuple[str, str]] = []
    for block in re.split(r"\n(?=Check:\s+CKV)", out):
        m_id = _CKV_HEADER.match(block)
        if not m_id:
            continue
        ckv_id = m_id.group(1)
        for m_res in _CKV_FAILED_RES.finditer(block):
            failed_ids.add(ckv_id)
            failed_pairs.append((m_res.group(1), ckv_id))

    return {
        "failed_ckv_ids":      sorted(failed_ids),
        "failed_per_resource": failed_pairs,
        "passed_count":        passed,
        "failed_count":        failed,
        "total_checks":        passed + failed,
        "scan_seconds":        elapsed,
    }
