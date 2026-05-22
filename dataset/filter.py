"""Dataset filter — FLOCI_SUPPORTED resource type set + compatibility helpers."""
import re

# Resource types supported by Floci (LocalStack-compatible).
# Derived from LocalStack community + Pro coverage. Used by graph.py floci_check_node.
FLOCI_SUPPORTED: frozenset[str] = frozenset({
    # Compute
    "aws_instance", "aws_launch_template", "aws_launch_configuration",
    "aws_autoscaling_group", "aws_autoscaling_policy",
    # VPC / Networking
    "aws_vpc", "aws_subnet", "aws_internet_gateway", "aws_nat_gateway",
    "aws_route_table", "aws_route_table_association", "aws_route",
    "aws_security_group", "aws_security_group_rule",
    "aws_network_acl", "aws_network_acl_rule",
    "aws_vpc_endpoint", "aws_vpc_peering_connection",
    "aws_eip", "aws_eip_association",
    "aws_network_interface",
    # Load Balancing
    "aws_lb", "aws_alb", "aws_lb_listener", "aws_alb_listener",
    "aws_lb_listener_rule", "aws_alb_listener_rule",
    "aws_lb_target_group", "aws_alb_target_group",
    "aws_lb_target_group_attachment", "aws_alb_target_group_attachment",
    "aws_elb",
    # Storage
    "aws_s3_bucket", "aws_s3_bucket_acl", "aws_s3_bucket_cors_configuration",
    "aws_s3_bucket_lifecycle_configuration", "aws_s3_bucket_logging",
    "aws_s3_bucket_notification", "aws_s3_bucket_object",
    "aws_s3_bucket_ownership_controls", "aws_s3_bucket_policy",
    "aws_s3_bucket_public_access_block", "aws_s3_bucket_replication_configuration",
    "aws_s3_bucket_server_side_encryption_configuration",
    "aws_s3_bucket_versioning", "aws_s3_bucket_website_configuration",
    "aws_s3_object",
    # Database
    "aws_db_instance", "aws_db_subnet_group", "aws_db_parameter_group",
    "aws_db_option_group",
    "aws_rds_cluster", "aws_rds_cluster_instance", "aws_rds_cluster_parameter_group",
    "aws_elasticache_cluster", "aws_elasticache_replication_group",
    "aws_elasticache_subnet_group", "aws_elasticache_parameter_group",
    "aws_dynamodb_table", "aws_dynamodb_table_item",
    # IAM
    "aws_iam_role", "aws_iam_role_policy", "aws_iam_role_policy_attachment",
    "aws_iam_policy", "aws_iam_user", "aws_iam_user_policy",
    "aws_iam_user_policy_attachment", "aws_iam_group", "aws_iam_group_policy",
    "aws_iam_group_membership", "aws_iam_instance_profile",
    "aws_iam_access_key", "aws_iam_account_password_policy",
    # Lambda
    "aws_lambda_function", "aws_lambda_event_source_mapping",
    "aws_lambda_permission", "aws_lambda_layer_version",
    # API Gateway
    "aws_api_gateway_rest_api", "aws_api_gateway_resource",
    "aws_api_gateway_method", "aws_api_gateway_integration",
    "aws_api_gateway_deployment", "aws_api_gateway_stage",
    "aws_api_gateway_authorizer", "aws_api_gateway_usage_plan",
    "aws_api_gateway_api_key",
    "aws_apigatewayv2_api", "aws_apigatewayv2_integration",
    "aws_apigatewayv2_route", "aws_apigatewayv2_stage",
    "aws_apigatewayv2_deployment",
    # Messaging
    "aws_sqs_queue", "aws_sqs_queue_policy",
    "aws_sns_topic", "aws_sns_topic_policy", "aws_sns_topic_subscription",
    # Container
    "aws_ecs_cluster", "aws_ecs_service", "aws_ecs_task_definition",
    "aws_ecr_repository", "aws_ecr_repository_policy",
    # KMS / Secrets
    "aws_kms_key", "aws_kms_alias",
    "aws_secretsmanager_secret", "aws_secretsmanager_secret_version",
    # CloudWatch
    "aws_cloudwatch_log_group", "aws_cloudwatch_log_stream",
    "aws_cloudwatch_metric_alarm", "aws_cloudwatch_event_rule",
    "aws_cloudwatch_event_target",
    # DNS
    "aws_route53_zone", "aws_route53_record",
    # CDN / Certificate
    "aws_cloudfront_distribution", "aws_cloudfront_origin_access_identity",
    "aws_acm_certificate", "aws_acm_certificate_validation",
    # Elastic Beanstalk
    "aws_elastic_beanstalk_application", "aws_elastic_beanstalk_environment",
    # Kinesis
    "aws_kinesis_stream", "aws_kinesis_firehose_delivery_stream",
    # Step Functions
    "aws_sfn_state_machine",
    # CodeBuild / CodePipeline
    "aws_codebuild_project", "aws_codepipeline", "aws_codecommit_repository",
    # SSM
    "aws_ssm_parameter",
    # ElasticSearch / OpenSearch
    "aws_elasticsearch_domain", "aws_opensearch_domain",
    # MSK
    "aws_msk_cluster", "aws_msk_configuration",
    # EFS
    "aws_efs_file_system", "aws_efs_mount_target", "aws_efs_access_point",
    # FSx
    "aws_fsx_lustre_file_system", "aws_fsx_windows_file_system",
    # Glue
    "aws_glue_catalog_database", "aws_glue_catalog_table", "aws_glue_job",
    # Athena
    "aws_athena_database", "aws_athena_workgroup",
    # WAF (v1 only — v2 not fully supported)
    "aws_waf_web_acl", "aws_waf_rule",
    # Data Sources (commonly used)
    "aws_caller_identity", "aws_partition",
})


def parse_resources(resource_field: str) -> list[str]:
    """Parse the 'Resource' CSV column (comma-separated types) into a list."""
    if not resource_field:
        return []
    return [r.strip() for r in resource_field.split(",") if r.strip()]


def is_floci_compatible(resource_field: str) -> bool:
    """Return True if ALL resource types in the field are in FLOCI_SUPPORTED."""
    types = parse_resources(resource_field)
    if not types:
        return True
    return all(t in FLOCI_SUPPORTED for t in types)
