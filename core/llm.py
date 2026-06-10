"""Shared LLM client and retry wrapper for the pipeline."""
import atexit
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

from dotenv import load_dotenv
from langchain_core.language_models import BaseChatModel
from tenacity import retry, stop_after_attempt, wait_random_exponential

load_dotenv()

if os.environ.get("LLM_USE_PROXY", "").lower() not in ("1", "true", "yes"):
    for _proxy_key in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ):
        os.environ.pop(_proxy_key, None)

_TIMEOUT     = int(os.environ.get("LLM_TIMEOUT",      "120"))
_RETRIES     = int(os.environ.get("LLM_RETRIES",      "5"))
_MAX_TOKENS  = int(os.environ.get("LLM_MAX_TOKENS",   "4096"))
_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE","0"))

# Per-agent timeout override. Unset agents inherit LLM_TIMEOUT.
_TIMEOUT_PER_AGENT: dict[str, int] = {
    "architecture": int(os.environ.get("LLM_TIMEOUT_ARCHI", str(_TIMEOUT))),
}

# Per-agent max_tokens overrides.
MAX_TOKENS_PER_AGENT = {
    "architecture": int(os.environ.get("LLM_MAX_TOKENS_ARCHI", "2048")),
    "security":     int(os.environ.get("LLM_MAX_TOKENS_SECU",  "2048")),
    "engineering":  int(os.environ.get("LLM_MAX_TOKENS_ENGI",  "4096")),
    "validation":   int(os.environ.get("LLM_MAX_TOKENS_VAL",   "2048")),
    "deployment":   int(os.environ.get("LLM_MAX_TOKENS_DEPLOY","2048")),
}

# Default provider is DeepSeek; NVIDIA remains supported via env config.
_PROVIDER = os.environ.get("LLM_PROVIDER", "deepseek").lower()


def _make_llm(max_tokens: int) -> BaseChatModel:
    if _PROVIDER == "deepseek":
        from langchain_openai import ChatOpenAI
        _MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
        return ChatOpenAI(
            model=_MODEL,
            max_tokens=max_tokens,
            temperature=_TEMPERATURE,
            api_key=os.environ.get("DEEPSEEK_API_KEY"),
            base_url="https://api.deepseek.com/v1",
            timeout=_TIMEOUT,
            max_retries=0,
        )
    else:
        from langchain_nvidia_ai_endpoints import ChatNVIDIA
        _MODEL = os.environ.get("NVIDIA_MODEL", "meta/llama-3.3-70b-instruct")
        return ChatNVIDIA(model=_MODEL, max_tokens=max_tokens, temperature=_TEMPERATURE)


# One instance per agent, reused across calls.
_llm_registry: dict[str, BaseChatModel] = {
    agent: _make_llm(tokens)
    for agent, tokens in MAX_TOKENS_PER_AGENT.items()
}
# Default fallback model.
llm = _make_llm(_MAX_TOKENS)

# Shared thread pool for timeout enforcement.
_executor = ThreadPoolExecutor(max_workers=12)
atexit.register(_executor.shutdown, wait=False)


def _call_llm_with_model(model: BaseChatModel, messages: list, timeout: int = _TIMEOUT) -> str:
    future = _executor.submit(model.invoke, messages)
    try:
        return future.result(timeout=timeout).content
    except FuturesTimeoutError:
        raise TimeoutError(f"LLM call timed out after {timeout}s")


_RAW_DEBUG = os.environ.get("LLM_RAW", "").lower() in ("1", "true")


# Jittered backoff to avoid synchronized retries across workers.
@retry(
    stop=stop_after_attempt(_RETRIES),
    wait=wait_random_exponential(multiplier=1, max=30),
    reraise=True,
)
def call_llm(messages: list, agent: str | None = None) -> str:
    """Call the LLM with timeout and retry."""
    import logging
    logger = logging.getLogger(__name__)

    model = _llm_registry.get(agent, llm) if agent else llm
    timeout = _TIMEOUT_PER_AGENT.get(agent, _TIMEOUT) if agent else _TIMEOUT

    try:
        raw = _call_llm_with_model(model, messages, timeout=timeout)
    except Exception as e:
        logger.debug(f"LLM call error ({agent}): {type(e).__name__}: {str(e)[:200]}")
        raise

    if _RAW_DEBUG:
        print(f"\n{'─'*60}\n[LLM RAW — {agent or 'default'}]\n{raw}\n{'─'*60}\n")
    return raw


