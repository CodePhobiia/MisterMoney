"""
Source Registry
Maps resolution_source URLs → checker function, classifies market type
"""

import re
from typing import Literal

import structlog

from .source_checkers.base import SourceChecker
from .source_checkers.coingecko import CoinGeckoChecker
from .source_checkers.economic import EconomicChecker
from .source_checkers.generic_api import GenericAPIChecker
from .source_checkers.sports import SportsChecker

log = structlog.get_logger()


class SourceRegistry:
    """Maps resolution_source URLs → checker function, classifies market type"""

    def __init__(self):
        """Initialize registry with known checkers"""
        self.checkers = {
            'coingecko': CoinGeckoChecker(),
            'sports': SportsChecker(),
            'economic': EconomicChecker(),
            'generic': GenericAPIChecker(),
        }

        # Known API domains → checker mapping
        self.domain_patterns = [
            (r'coingecko\.com', 'coingecko'),
            (r'coinmarketcap\.com', 'coingecko'),
            (r'espn\.com', 'sports'),
            (r'api-football\.com', 'sports'),
            (r'thesportsdb\.com', 'sports'),
            (r'bls\.gov', 'economic'),
            (r'fred\.stlouisfed\.org', 'economic'),
            (r'api\.stlouisfed\.org', 'economic'),
        ]

    def get_checker(self, resolution_source: str) -> SourceChecker | None:
        """
        Get checker for a resolution source URL

        Args:
            resolution_source: URL or identifier for resolution source

        Returns:
            SourceChecker instance if recognized, else None
        """
        if not resolution_source:
            return None

        source_lower = resolution_source.lower()

        # Match against known patterns
        for pattern, checker_name in self.domain_patterns:
            if re.search(pattern, source_lower):
                log.info("matched_checker",
                        source=resolution_source,
                        checker=checker_name)
                return self.checkers[checker_name]

        # Fallback to generic if it looks like a URL
        if source_lower.startswith(('http://', 'https://')):
            log.info("fallback_to_generic", source=resolution_source)
            return self.checkers['generic']

        log.warning("no_checker_found", source=resolution_source)
        return None

    def classify_market(
        self,
        question: str,
        rules: str,
        source: str
    ) -> Literal["numeric", "simple", "rule", "dossier"]:
        """
        Deterministic classification based on heuristics

        Classification logic:
        - numeric: source is known API (coingecko, bls.gov, espn scores), threshold in rules
        - simple: short rules, clear YES/NO, single verifiable event
        - rule: long rules (>500 chars), multiple clauses, "notwithstanding", "unless"
        - dossier: multiple sources, investigation needed, complex provenance

        Args:
            question: Market question text
            rules: Resolution rules text
            source: Resolution source URL/identifier

        Returns:
            Market type classification
        """
        question_lower = question.lower()
        rules_lower = rules.lower()
        source_lower = source.lower() if source else ""

        # Rule: long rules with complex language (check FIRST - takes precedence)
        rule_indicators = [
            'notwithstanding', 'unless', 'provided that', 'except',
            'in the event', 'subject to', 'pursuant to', 'whereby',
            'clarification:', 'edge case', 'interpretation'
        ]
        has_complex_language = any(ind in rules_lower for ind in rule_indicators)
        is_long_rules = len(rules) > 500

        if is_long_rules or has_complex_language:
            log.info("classified_rule",
                    question=question[:50],
                    rules_len=len(rules),
                    has_complex_language=has_complex_language)
            return "rule"

        # Numeric: known API + threshold indicators
        has_known_api = self.get_checker(source) is not None and source
        threshold_indicators = [
            'reach', 'above', 'below', 'exceed', 'higher than', 'lower than',
            'at least', 'more than', 'less than', '>', '<', '>=', '<=',
            'threshold', 'target', 'level'
        ]
        has_threshold = any(ind in question_lower or ind in rules_lower
                           for ind in threshold_indicators)

        # Numeric patterns
        numeric_patterns = [
            r'\$\d+',  # dollar amounts
            r'\d+%',   # percentages
            r'\d+\.\d+',  # decimals
            r'\d+\s*(points|pts|goals|runs)',  # sports scores
        ]
        has_numeric_pattern = any(re.search(pat, question_lower) or re.search(pat, rules_lower)
                                 for pat in numeric_patterns)

        if has_known_api and (has_threshold or has_numeric_pattern):
            log.info("classified_numeric",
                    question=question[:50],
                    has_api=has_known_api,
                    has_threshold=has_threshold)
            return "numeric"

        # Dossier: multiple sources or investigation needed
        dossier_indicators = [
            'multiple sources', 'investigation', 'credible reports',
            'will be determined', 'committee', 'panel', 'jury',
            'official announcement', 'verified by', 'confirmed by',
            'evidence', 'documentation', 'proof'
        ]
        has_dossier_indicator = any(ind in rules_lower for ind in dossier_indicators)
        has_multiple_sources = source and (
            ',' in source or ';' in source
            or ' and ' in source_lower
        )

        if has_dossier_indicator or has_multiple_sources:
            log.info("classified_dossier", question=question[:50])
            return "dossier"

        # Simple: default for short, clear markets
        log.info("classified_simple", question=question[:50])
        return "simple"
