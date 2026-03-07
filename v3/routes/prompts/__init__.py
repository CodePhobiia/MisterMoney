"""
V3 Route Prompts
Prompt templates for each route type
"""

from .simple_blind_v1 import SIMPLE_BLIND_SYSTEM, build_simple_blind_prompt
from .simple_judge_v1 import SIMPLE_JUDGE_SYSTEM, build_simple_judge_prompt

__all__ = [
    "SIMPLE_BLIND_SYSTEM",
    "build_simple_blind_prompt",
    "SIMPLE_JUDGE_SYSTEM",
    "build_simple_judge_prompt",
]
