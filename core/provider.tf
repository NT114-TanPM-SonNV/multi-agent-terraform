# Provider config tĩnh cho Agent 3 — Floci (LocalStack-compatible) endpoints.
#
# URL dùng localhost:4566; dataset/evaluator._substitute_endpoint() tự thay bằng
# FLOCI_ENDPOINT thật lúc eval. Agent 3 chỉ prepend file này nguyên trạng.
#
# endpoints{} CHỈ liệt kê service thực sự xuất hiện trong dataset IaC-Eval đã filter
# (25 service / 318 mẫu — derive từ Resource column + reference HCL; xem
# docs/dataset_coverage.md, dataset/filter.py). Tên KEY là argument của AWS provider,
# KHÁC tên service Floci health (monitoring->cloudwatch, elasticloadbalancing->elb/elbv2,
# es->es/opensearch, kafka->kafka/kafkaconnect, cognito-idp->cognitoidp).
# Thêm service mới phải verify bằng terraform validate (key sai -> fail MỌI sample).

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region                      = "us-east-1"
  access_key                  = "test"
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true

  endpoints {
    ec2            = "http://localhost:4566"
    rds            = "http://localhost:4566"
    s3             = "http://localhost:4566"
    iam            = "http://localhost:4566"
    apigateway     = "http://localhost:4566"
    autoscaling    = "http://localhost:4566"
    backup         = "http://localhost:4566"
    cloudwatch     = "http://localhost:4566"
    codebuild      = "http://localhost:4566"
    cognitoidp     = "http://localhost:4566"
    dynamodb       = "http://localhost:4566"
    eks            = "http://localhost:4566"
    elasticache    = "http://localhost:4566"
    elb            = "http://localhost:4566"
    elbv2          = "http://localhost:4566"
    es             = "http://localhost:4566"
    events         = "http://localhost:4566"
    firehose       = "http://localhost:4566"
    kafka          = "http://localhost:4566"
    kafkaconnect   = "http://localhost:4566"
    kinesis        = "http://localhost:4566"
    kms            = "http://localhost:4566"
    lambda         = "http://localhost:4566"
    logs           = "http://localhost:4566"
    opensearch     = "http://localhost:4566"
    route53        = "http://localhost:4566"
    secretsmanager = "http://localhost:4566"
    sns            = "http://localhost:4566"
    sts            = "http://localhost:4566"
  }
}
