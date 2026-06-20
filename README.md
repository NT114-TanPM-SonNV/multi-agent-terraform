# A Multi-Agent Framework for Secure and Deployable AWS Terraform Code Generation

![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![Terraform](https://img.shields.io/badge/Terraform-1.5%2B-623CE4)
![LangGraph](https://img.shields.io/badge/LangGraph-enabled-111827)
![AWS](https://img.shields.io/badge/AWS-deployable-FF9900)
![Checkov](https://img.shields.io/badge/Checkov-security-1E88E5)

## Table of Contents

- [Flow](#flow)
- [What this repo contains](#what-this-repo-contains)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [Shared State](#shared-state)
- [Notes](#notes)
- [Troubleshooting](#troubleshooting)

This project provides a 5-agent LangGraph pipeline for generating deployable AWS Terraform:

- **Architecture Agent**: converts the user prompt into an infrastructure plan
- **Security Agent**: selects Checkov checks for the plan
- **Engineering Agent**: generates Terraform HCL
- **Validation Agent**: validates the generated configuration with Terraform CLI and Checkov
- **Deployment Agent**: applies the plan to AWS and performs cleanup

## Flow
<img width="1999" height="917" alt="Screenshot 2026-06-20 212440" src="https://github.com/user-attachments/assets/9016df14-c922-448d-a645-8ea1c593d98e" />


```text
User prompt -> Architecture Agent -> Security Agent -> Engineering Agent -> Validation Agent -> Deployment Agent -> End
```

Retry and back-routing are handled by the graph:

```text
Validation Agent SYNTAX / LOGIC -> Engineering Agent
Validation Agent MISSING_RESOURCE -> Architecture Agent
Validation Agent SECURITY -> Engineering Agent
Validation Agent INFRASTRUCTURE / UNKNOWN / budget exceeded -> requires_human

Deployment Agent LOGIC -> Engineering Agent
Deployment Agent MISSING_RESOURCE -> Architecture Agent
Deployment Agent INFRASTRUCTURE / UNKNOWN / budget exceeded -> requires_human
```

## What this repo contains

- `agents/`: implementations of the 5 agents
- `core/`: shared state, retry control, Terraform helpers, parsing, metrics
- `prompts/`: prompts for each agent
- `graph.py`: LangGraph orchestration
- `dataset/`: input cases

## Requirements

- Python 3.11+
- Terraform 1.5+
- Checkov
- AWS credentials
- LLM API key and provider configuration

## Quick Start

### 1. Create a virtual environment

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

### 2. Install dependencies

```powershell
pip install -r requirements.txt
```

### 3. Configure environment variables

Create a `.env` file with at least:

```ini
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=your_key_here
DEEPSEEK_MODEL=deepseek-v4-pro

AWS_ACCESS_KEY_ID=your_access_key
AWS_SECRET_ACCESS_KEY=your_secret_key
AWS_DEFAULT_REGION=us-east-1

TF_PLAN_TIMEOUT=300
```

### 4. Run the pipeline

```powershell
python graph.py "Create an S3 bucket with versioning and encryption enabled."
```

### 5. Run benchmark scripts

```powershell
python baseline.py --csv dataset/data-dev.csv --out results/baseline.json --workers 5
python eval.py --csv dataset/data-dev.csv --no-deploy --out results.json --workers 5
python score.py results.json --csv dataset/data-dev.csv --checkov --llm-judge
```


## Shared State

The pipeline passes a shared `AgentState` through all agents. It stores:

- user prompt
- infrastructure plan
- security profile
- generated code
- validation feedback
- deployment result
- retry counters
- routing history

## Notes

- `requires_human` is the terminal stop node for non-recoverable cases.
- Retry budgets are managed in `core/retry_control.py`.
- Validation and deployment may route back to Architecture or Engineering depending on the error type.

## Troubleshooting

- `terraform not found`: install Terraform and ensure it is on `PATH`
- `checkov not found`: install Checkov from `requirements.txt`
- `AWS credentials not found`: set `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_DEFAULT_REGION`
- `LLM call failed`: verify `DEEPSEEK_API_KEY` and model settings in `.env`
