"""LLM-judge metric — đánh giá adequacy của generated HCL.

Theo MACOG (Khan et al. 2025): binary adequacy check per task.
- Judge model KHÁC với generator để tránh self-evaluation bias.
- Rubric-based: ẩn model identity, tránh position bias.
- Output: c_i ∈ {0, 1} (1 = adequate/correct, 0 = inadequate/incorrect)
- LLM-judge = 100 × mean(c_i) across M tasks.
"""
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
    """Tạo judge LLM — dùng deepseek-chat (non-reasoning, nhanh, rẻ)
    thay vì deepseek-v4-pro (generator) để tránh self-evaluation bias."""
    provider = os.environ.get("LLM_PROVIDER", "deepseek").lower()
    if provider == "deepseek":
        from langchain_openai import ChatOpenAI
        # Dùng deepseek-chat chứ KHÔNG phải v4-pro (generator)
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
    """Judge 1 row. Trả về 0 hoặc 1.

    Fallback = 0 nếu LLM lỗi (conservative — không inflate score).
    """
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

    # Parse {"score": 0/1}
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


def llm_judge_batch(prompts: list[str], hcls: list[str],
                    timeout_per: int = 60) -> list[int]:
    """Judge nhiều row. Trả về list[0|1]."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = [0] * len(prompts)
    with ThreadPoolExecutor(max_workers=4) as ex:
        future_to_idx = {
            ex.submit(llm_judge_single, p, h, timeout_per): i
            for i, (p, h) in enumerate(zip(prompts, hcls))
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                logger.warning("Judge row %d error: %s", idx, e)
    return results
