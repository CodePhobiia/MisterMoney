"""
V3 Route Prompts
Prompt templates for each route type
"""

from .dossier_challenge_v1 import DOSSIER_CHALLENGE_SYSTEM, build_dossier_challenge_prompt
from .dossier_v1 import DOSSIER_SYSTEM, build_dossier_synthesis_prompt
from .rule_heavy_v1 import RULE_HEAVY_SYSTEM, build_rule_heavy_prompt
from .rule_judge_v1 import RULE_JUDGE_SYSTEM, build_rule_judge_prompt
from .simple_blind_v1 import SIMPLE_BLIND_SYSTEM, build_simple_blind_prompt
from .simple_judge_v1 import SIMPLE_JUDGE_SYSTEM, build_simple_judge_prompt

__all__ = [
    "SIMPLE_BLIND_SYSTEM",
    "build_simple_blind_prompt",
    "SIMPLE_JUDGE_SYSTEM",
    "build_simple_judge_prompt",
    "RULE_HEAVY_SYSTEM",
    "build_rule_heavy_prompt",
    "RULE_JUDGE_SYSTEM",
    "build_rule_judge_prompt",
    "DOSSIER_SYSTEM",
    "build_dossier_synthesis_prompt",
    "DOSSIER_CHALLENGE_SYSTEM",
    "build_dossier_challenge_prompt",
]
