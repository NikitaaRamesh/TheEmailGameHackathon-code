import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.orchestrator import (
    LeaderboardPosition,
    MatchMetrics,
    OrchestratorError,
    OrchestratorState,
    TerminalTelemetry,
    audit_latest_match,
    audit_latest_match_with_retry,
    hold_if_top_ranked,
    is_degraded,
    parse_terminal_telemetry,
    percentile_95,
    should_refactor,
    validate_candidate_source,
)


class OrchestratorTests(unittest.TestCase):
    def test_terminal_telemetry_extracts_operational_signals(self):
        output = """
▶ Watch your match
https://play.theemailgame.com/watch?agent=nikitaa_r_r&token=abc.def
[nikitaa_r_r] IN GAME - Round 1
[nikitaa_r_r] IN GAME - Round 2
[nikitaa_r_r] request failed for peer; retrying once
[nikitaa_r_r] authorization resolution failed closed: resolved 1 of 2 entries
[nikitaa_r_r] Budget: $29.81 left of $30.00 (1% used)
"""
        telemetry = parse_terminal_telemetry(output, 0, stop_requested=True)
        self.assertEqual("abc.def", telemetry.watch_token)
        self.assertEqual([1, 2], telemetry.rounds_seen)
        self.assertEqual(1, len(telemetry.transport_failures))
        self.assertEqual(1, len(telemetry.authorization_failures))
        self.assertEqual(29.81, telemetry.budget_left)
        self.assertTrue(telemetry.stop_requested)

    def test_degradation_prioritizes_penalties_then_score(self):
        state = OrchestratorState(best_score=10, best_penalties=0, best_rank=1)
        clean_drop = self.metrics(score=9)
        penalty = self.metrics(score=12, penalties=1)
        rank_drop = self.metrics(score=10, rank=2)
        self.assertTrue(is_degraded(clean_drop, state, score_drop=0))
        self.assertFalse(is_degraded(clean_drop, state, score_drop=1))
        self.assertTrue(is_degraded(penalty, state, score_drop=99))
        self.assertTrue(is_degraded(rank_drop, state, score_drop=0))

    def test_percentile_95_handles_small_samples(self):
        self.assertEqual(2.0, percentile_95([1.0, 2.0]))

    def test_infrastructure_failure_never_triggers_refactor(self):
        metrics = self.metrics(score=0, signed=0, infrastructure_failure=True)
        self.assertFalse(should_refactor(metrics, minimum_score=10))

    def test_low_clean_score_triggers_refactor(self):
        metrics = self.metrics(score=8, signed=6)
        self.assertTrue(should_refactor(metrics, minimum_score=10))

    def test_top_rank_hold_releases_immediately_when_rank_drops(self):
        state = OrchestratorState(last_match_id="arena_new")
        positions = [self.position(rank=2), self.position(rank=3)]
        clock = [1000.0]

        def advance(seconds):
            clock[0] += seconds

        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.orchestrator.fetch_leaderboard_position", side_effect=positions
        ), patch("scripts.orchestrator.time.time", side_effect=lambda: clock[0]), patch(
            "scripts.orchestrator.time.sleep", side_effect=advance
        ) as sleep:
            result = hold_if_top_ranked(
                "https://example.test",
                "agent",
                state,
                Path(directory) / "state.json",
                Path(directory) / "events.jsonl",
                maximum_rank=2,
                hold_seconds=480,
                poll_seconds=30,
                timeout=1,
            )

        self.assertEqual(3, result.rank)
        self.assertEqual(1, sleep.call_count)
        self.assertIsNone(state.top_rank_hold_until)

    def test_completed_top_rank_hold_is_not_repeated_for_same_match(self):
        state = OrchestratorState(
            last_match_id="arena_new",
            top_rank_hold_match_id="arena_new",
            top_rank_hold_until=999.0,
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.orchestrator.fetch_leaderboard_position", return_value=self.position(rank=1)
        ), patch("scripts.orchestrator.time.time", return_value=1000.0), patch(
            "scripts.orchestrator.time.sleep"
        ) as sleep:
            hold_if_top_ranked(
                "https://example.test",
                "agent",
                state,
                Path(directory) / "state.json",
                Path(directory) / "events.jsonl",
                maximum_rank=2,
                hold_seconds=480,
                poll_seconds=30,
                timeout=1,
            )

        sleep.assert_not_called()

    def test_candidate_must_keep_custom_agent_base(self):
        original = self.agent_source("class CustomAgent(BaseAgent):\n    pass\n")
        invalid = self.agent_source("class CustomAgent:\n    pass\n")
        with self.assertRaisesRegex(OrchestratorError, "subclasses BaseAgent"):
            validate_candidate_source(invalid, original)

    def test_candidate_cannot_add_imports(self):
        original = self.agent_source("class CustomAgent(BaseAgent):\n    pass\n")
        invalid = "import subprocess\n" + original
        with self.assertRaisesRegex(OrchestratorError, "outside CustomAgent"):
            validate_candidate_source(invalid, original)

    def test_candidate_cannot_change_module_level_code(self):
        original = self.agent_source("class CustomAgent(BaseAgent):\n    pass\n")
        invalid = original.replace("VALUE_0 = 0", "VALUE_0 = 99")
        with self.assertRaisesRegex(OrchestratorError, "outside CustomAgent"):
            validate_candidate_source(invalid, original)

    def test_authenticated_audit_detects_transport_failure(self):
        history = {
            "matches": [
                {
                    "game_id": "arena_new",
                    "your_score": 8,
                    "rank": 2,
                    "field": 4,
                    "result": "loss",
                    "rounds": 3,
                    "breakdown": {"collected": 4, "signed": 4, "penalties": 0},
                    "abandoned": False,
                }
            ]
        }
        detail = {
            "match": {
                "rounds": [
                    {
                        "round_number": number,
                        "your_score": score,
                        "performance": {
                            "supposed_to_request_from": [],
                            "successfully_submitted_for": [],
                            "authorized_to_sign_for": [],
                            "successfully_signed_for": [],
                            "unauthorized_signing_penalties": 0,
                        },
                    }
                    for number, score in ((1, 4), (2, 4), (3, 0))
                ]
            }
        }
        terminal = TerminalTelemetry(
            exit_code=0,
            watch_token="token",
            rounds_seen=[1, 2, 3],
            transport_failures=["request failed"],
            authorization_failures=[],
            budget_left=29.81,
            budget_cap=30.0,
        )
        with patch("scripts.orchestrator.authenticated_get", side_effect=[history, detail]):
            metrics, audit = audit_latest_match(
                "https://example.test", "agent", "token", "arena_old", terminal
            )
        self.assertTrue(metrics.infrastructure_failure)
        self.assertEqual([4, 4, 0], metrics.round_scores)
        self.assertEqual(3, len(audit["rounds"]))

    def test_authenticated_audit_retries_delayed_persistence(self):
        expected = self.metrics()
        with patch(
            "scripts.orchestrator.audit_latest_match",
            side_effect=[OrchestratorError("not ready"), (expected, {"rounds": []})],
        ) as mocked, patch("scripts.orchestrator.time.sleep"):
            metrics, _ = audit_latest_match_with_retry(
                "https://example.test",
                "agent",
                "token",
                "old",
                TerminalTelemetry(0, "token", [1, 2, 3], [], [], 29.0, 30.0),
                attempts=2,
                retry_seconds=0.0,
            )
        self.assertEqual(expected, metrics)
        self.assertEqual(2, mocked.call_count)

    @staticmethod
    def metrics(**overrides):
        values = {
            "game_id": "arena",
            "score": 10,
            "rank": 1,
            "field": 4,
            "result": "win",
            "rounds": 3,
            "collected": 6,
            "signed": 6,
            "penalties": 0,
            "abandoned": False,
        }
        values.update(overrides)
        return MatchMetrics(**values)

    @staticmethod
    def position(rank):
        return LeaderboardPosition(rank=rank, elo=2000, idle_days=0.0, decay=0, phase="running")

    @staticmethod
    def agent_source(class_source):
        padding = "\n".join(f"VALUE_{index} = {index}" for index in range(80))
        return f"from src.base_agent import BaseAgent\n{padding}\n{class_source}"


if __name__ == "__main__":
    unittest.main()
