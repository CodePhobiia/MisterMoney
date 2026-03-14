"""
Sports Event Checker
Fetches scores/results from sports APIs
"""

import re
from datetime import UTC, datetime

import structlog

from v3.evidence.entities import RuleGraph

from .base import SourceChecker, SourceCheckResult

log = structlog.get_logger()


class SportsChecker(SourceChecker):
    """
    Fetches sports scores and results

    For completed games: probability = 0 or 1
    For upcoming games: simple Elo/historical win rate estimation
    """

    # Using TheSportsDB free API
    BASE_URL = "https://www.thesportsdb.com/api/v1/json/3"
    TIMEOUT_SECONDS = 10

    def _extract_teams(self, source: str) -> tuple[str | None, str | None]:
        """
        Extract team names from source

        Looks for patterns like "TeamA vs TeamB" or "TeamA - TeamB"
        """
        if not source:
            return None, None

        # Try "vs" pattern
        vs_match = re.search(r'([a-zA-Z0-9\s]+)\s+vs\.?\s+([a-zA-Z0-9\s]+)', source, re.IGNORECASE)
        if vs_match:
            return vs_match.group(1).strip(), vs_match.group(2).strip()

        # Try dash pattern
        dash_match = re.search(r'([a-zA-Z0-9\s]+)\s*-\s*([a-zA-Z0-9\s]+)', source)
        if dash_match:
            return dash_match.group(1).strip(), dash_match.group(2).strip()

        return None, None

    def _parse_outcome(self, rule: RuleGraph) -> tuple[str | None, str]:
        """
        Parse expected outcome from rule

        Returns: (winning_team, outcome_type)
        outcome_type: 'win', 'score_over', 'score_under'
        """
        source_lower = rule.source_name.lower()

        # Check for win conditions
        if 'win' in source_lower or 'wins' in source_lower:
            team1, team2 = self._extract_teams(rule.source_name)
            if team1 and 'win' in team1.lower():
                return team1, 'win'
            if team2 and 'win' in team2.lower():
                return team2, 'win'
            return team1, 'win'  # Default to first team

        # Check for score thresholds
        if rule.threshold_num is not None:
            if rule.operator in ('>', '>='):
                return None, 'score_over'
            elif rule.operator in ('<', '<='):
                return None, 'score_under'

        return None, 'win'

    async def check(self, condition_id: str, rule: RuleGraph) -> SourceCheckResult:
        """
        Check sports event outcome

        For now, returns a mock probability based on simple heuristics.
        In production, would integrate with live sports APIs.
        """
        team1, team2 = self._extract_teams(rule.source_name)
        winning_team, outcome_type = self._parse_outcome(rule)

        if not team1 or not team2:
            log.warning("sports_no_teams",
                       source=rule.source_name,
                       condition_id=condition_id)
            return SourceCheckResult(
                condition_id=condition_id,
                source=rule.source_name,
                current_value="unknown",
                threshold="unknown",
                probability=0.5,
                confidence=0.0,
                raw_data={'error': 'Could not extract teams'},
                ttl_seconds=300
            )

        # For now, use a simple heuristic:
        # - If event is in the past (window_end passed): check if resolved
        # - If event is upcoming: use 50/50 baseline

        now = datetime.now(UTC)

        if rule.window_end and rule.window_end < now:
            # Event should be completed
            # In production, fetch actual results
            # For now, return low confidence
            log.info("sports_past_event",
                    teams=f"{team1} vs {team2}",
                    window_end=rule.window_end)

            return SourceCheckResult(
                condition_id=condition_id,
                source=rule.source_name,
                current_value="completed_unknown",
                threshold=winning_team or "unknown",
                probability=0.5,
                confidence=0.3,  # Low confidence without actual data
                raw_data={
                    'team1': team1,
                    'team2': team2,
                    'status': 'completed_unknown'
                },
                ttl_seconds=3600  # Cache longer for completed events
            )
        else:
            # Upcoming event - use baseline probability
            log.info("sports_upcoming_event",
                    teams=f"{team1} vs {team2}",
                    window_end=rule.window_end)

            # Simple home advantage heuristic
            # In production, would use Elo ratings, historical records, etc.
            probability = 0.5

            # If we can determine home team, give slight edge
            if 'home' in rule.source_name.lower():
                if team1 and 'home' in team1.lower():
                    probability = 0.55
                elif team2 and 'home' in team2.lower():
                    probability = 0.45

            return SourceCheckResult(
                condition_id=condition_id,
                source=rule.source_name,
                current_value="upcoming",
                threshold=winning_team or "unknown",
                probability=probability,
                confidence=0.5,  # Moderate confidence in baseline
                raw_data={
                    'team1': team1,
                    'team2': team2,
                    'status': 'upcoming',
                    'outcome_type': outcome_type
                },
                ttl_seconds=300  # Refresh every 5 minutes
            )
