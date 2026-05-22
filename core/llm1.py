"""Khởi tạo LLM dùng chung cho toàn bộ pipeline.

Mọi agent đều gọi call_llm() thay vì gọi llm.invoke() trực tiếp
để đảm bảo retry logic được áp dụng nhất quán.
"""
import atexit
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

from dotenv import load_dotenv
from langchain_nvidia_ai_endpoints import ChatNVIDIA
from tenacity import retry, stop_after_attempt, wait_exponential

load_dotenv()

_MODEL   = os.environ.get("NVIDIA_MODEL", "meta/llama-3.3-70b-instruct")
_TIMEOUT = int(os.environ.get("LLM_TIMEOUT",  "60"))   # giây
_RETRIES    = int(os.environ.get("LLM_RETRIES",    "3"))    # số lần retry; đặt 1 khi eval
_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "4096"))  # tăng nếu hard sample bị truncate
_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0"))
# Parse retry: trong-agent retry khi JSON malformed/truncated — tránh một parse error gết mẫu
_PARSE_RETRIES = int(os.environ.get("AGENT_PARSE_RETRIES", "2"))

# Instance dùng chung — khởi tạo một lần khi import
llm = ChatNVIDIA(model=_MODEL, max_tokens=_MAX_TOKENS, temperature=_TEMPERATURE)

# Thread pool dùng chung để enforce timeout — tránh tạo mới mỗi lần gọi.
# atexit shutdown tránh thread leak khi process exit (vd: long-running benchmark).
_executor = ThreadPoolExecutor(max_workers=4)
atexit.register(_executor.shutdown, wait=False)


@retry(
    stop=stop_after_attempt(_RETRIES),
    wait=wait_exponential(multiplier=1, min=4, max=60),
    reraise=True,
)
def call_llm(messages: list) -> str:
    """Gọi LLM với timeout cứng và tự động retry khi gặp lỗi 429 hoặc 5xx.

    Dùng ThreadPoolExecutor để enforce timeout vì ChatNVIDIA không hỗ trợ
    timeout trực tiếp — future.result(timeout=N) hủy chờ sau N giây.
    """
    future = _executor.submit(llm.invoke, messages)
    try:
        return future.result(timeout=_TIMEOUT).content
    except FuturesTimeoutError:
        raise TimeoutError(f"LLM call timed out after {_TIMEOUT}s")


@retry(
    stop=stop_after_attempt(_RETRIES),
    wait=wait_exponential(multiplier=1, min=4, max=60),
    reraise=True,
)
def call_llm_raw(bound_llm, messages: list):
    """Gọi LLM bound với tools, trả AIMessage (không chuyển thành string).

    Dùng cho agent có tool loop — response.tool_calls chứa danh sách tool calls.
    bound_llm thường là llm.bind_tools(_TOOLS).
    """
    future = _executor.submit(bound_llm.invoke, messages)
    try:
        return future.result(timeout=_TIMEOUT)
    except FuturesTimeoutError:
        raise TimeoutError(f"LLM call timed out after {_TIMEOUT}s")


def call_llm_with_parse_retry(
    messages: list,
    parse_fn,  # hàm parse: parse_fn(raw_text) → dict hoặc raise
) -> tuple[str, dict]:
    """Gọi LLM + retry parse nếu fail.

    Trả về (raw_response, parsed_dict). Nếu parse fail ở lần cuối, raise exception.
    Dùng bởi agent nodes để retry LLM+parse và recover từ JSON truncation.

    Args:
        messages: LangChain message list
        parse_fn: callable(raw_text) → dict, raise nếu parse fail

    Returns:
        (raw_response: str, parsed_dict: dict)

    Raises:
        TimeoutError: LLM timeout (không retry parse)
        ValueError/TypeError/etc: parse error sau AGENT_PARSE_RETRIES lần
    """
    last_error = None
    for attempt in range(1, _PARSE_RETRIES + 1):
        try:
            raw = call_llm(messages)
            parsed = parse_fn(raw)
            return (raw, parsed)
        except TimeoutError:
            raise  # TimeoutError không retry — escalate ngay
        except (ValueError, KeyError, TypeError, AssertionError) as e:
            last_error = e
            if attempt < _PARSE_RETRIES:
                import logging
                logging.getLogger(__name__).warning(
                    "Parse error attempt %d/%d: %s, retrying...",
                    attempt, _PARSE_RETRIES, e
                )
                continue
            raise  # Cuối cùng, raise
    if last_error:
        raise last_error
