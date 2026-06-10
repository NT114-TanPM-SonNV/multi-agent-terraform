"""LLM-judge metric for generated HCL adequacy."""
import json
import logging
import os

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_JUDGE_SYSTEM = """\
You are an expert Infrastructure-as-Code reviewer evaluating Terraform configurations.
You will be given a user intent and a generated Terraform HCL configuration.
Your task: judge whether the generated configuration correctly and adequately implements the intent.

Evaluation rubric:
1. CORRECTNESS — Does the HCL implement what the intent asks for?
   Check: Are the required AWS resource types present?
   Check: Are key attributes set to reasonable values matching the intent?
2. COMPLETENESS — Are all components requested by the intent present?
   Check: No critical resource is missing.
3. VALIDITY — Is the HCL structurally valid Terraform?
   Check: Proper block syntax, references look correct.

Scoring:
- Output 1 if the configuration is ADEQUATE: correctly implements the intent with no critical errors.
- Output 0 if the configuration is INADEQUATE: missing required resources, wrong resource types,
  critical attribute errors, or structurally broken.

Be strict but fair. Minor style differences (naming, tags) do NOT make a config inadequate.
Missing a required resource or using completely wrong types = inadequate.

Output ONLY a JSON object: {"score": 0} or {"score": 1}
No explanation, no markdown, no other text.\
"""

_JUDGE_USER = """\
INTENT:
{intent}

GENERATED TERRAFORM HCL:
```hcl
{hcl}
```

Output JSON only: {{"score": 0}} or {{"score": 1}}\
"""


def _make_judge_llm():
    """Create the judge model."""
    provider = os.environ.get("LLM_PROVIDER", "deepseek").lower()
    if provider == "deepseek":
        from langchain_openai import ChatOpenAI
        judge_model = os.environ.get("JUDGE_MODEL", "deepseek-chat")
        return ChatOpenAI(
            model=judge_model,
            max_tokens=64,       # chỉ cần {"score": 0/1}
            temperature=0.0,     # tất định cho reproducibility
            api_key=os.environ.get("DEEPSEEK_API_KEY"),
            base_url="https://api.deepseek.com/v1",
        )
    else:
        from langchain_nvidia_ai_endpoints import ChatNVIDIA
        judge_model = os.environ.get("NVIDIA_JUDGE_MODEL", "meta/llama-3.3-70b-instruct")
        return ChatNVIDIA(model=judge_model, max_tokens=64, temperature=0.0)


_judge_llm = None


def _get_judge():
    global _judge_llm
    if _judge_llm is None:
        _judge_llm = _make_judge_llm()
    return _judge_llm


def llm_judge_single(prompt: str, hcl: str, timeout: int = 60) -> int:
    """Judge one sample and return 0 or 1."""
    if not hcl or not hcl.strip():
        return 0

    messages = [
        {"role": "system", "content": _JUDGE_SYSTEM},
        {"role": "user",   "content": _JUDGE_USER.format(
            intent=prompt.strip()[:1000],
            hcl=hcl.strip()[:3000],
        )},
    ]

    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTE
    with ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_get_judge().invoke, messages)
        try:
            raw = future.result(timeout=timeout).content.strip()
        except FTE:
            logger.warning("LLM judge timeout")
            return 0
        except Exception as e:
            logger.warning("LLM judge error: %s", e)
            return 0

    try:
        import re
        m = re.search(r'"score"\s*:\s*([01])', raw)
        if m:
            return int(m.group(1))
        data = json.loads(raw)
        return int(bool(data.get("score", 0)))
    except Exception:
        # Fallback: tìm số 0 hoặc 1 trong response
        if "\"score\": 1" in raw or '"score":1' in raw:
            return 1
        return 0
