"""Load system prompt cho từng agent.

Mỗi agent có một file .py riêng chứa SYSTEM_PROMPT string constant.
API load_prompt(name) giữ nguyên để agents không cần sửa.
"""
import importlib


def load_prompt(name: str) -> str:
    """Trả về SYSTEM_PROMPT từ prompts/{name}.py."""
    mod = importlib.import_module(f"prompts.{name}")
    return mod.SYSTEM_PROMPT
