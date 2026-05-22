from typing import TypedDict


class AgentState(TypedDict):
    # Input — không thay đổi trong suốt pipeline
    prompt: str

    # Cấu hình môi trường — đọc từ env vars khi khởi tạo graph
    floci_endpoint: str
    terraform_plan_timeout: int

    # Agent 1
    infrastructure_plan: dict
    # {
    #   "resources": list[{
    #     "type": str,                        # exact Terraform resource type
    #     "name": str,                        # snake_case identifier
    #     "attrs": dict[str, any],            # flat HCL attributes (exact TF attr names + values)
    #     "refs":  dict[str, str | list],     # cross-resource refs: attr → "type.name.field"
    #   }],
    #   "data_sources": list[{"type": str, "name": str, "filters": dict[str, str]}],
    #   "dependencies": dict[str, list[str]], # graph-level: "type.name" → ["type.name"]
    # }

    # Agent 2
    # {"aws_db_instance.main": {"storage_encrypted": True, "deletion_protection": True}, ...}
    security_constraints: dict
    # {"aws_db_instance.main": {"storage_encrypted": "CKV_AWS_17"}, ...}
    # Chỉ chứa attrs có ckv_id non-null — A4 dùng để chạy checkov --check <ids>
    security_ckv_ids: dict

    # Agent 3
    generated_code: str

    # Agent 4
    validation_result: dict

    # Retry counters — tách biệt theo loop type
    arch_retry_count: int
    sec_retry_count: int
    eng_retry_count: int
    total_retry_count: int
    error_history: list

    # Agent 5
    deployment_result: dict
    deploy_retry_count: int

    # Audit log
    routing_log: list
