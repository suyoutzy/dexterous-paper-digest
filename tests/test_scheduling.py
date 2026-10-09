"""Scheduling and bounded validation retries, without live requests or delivery."""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from daily_digest.llm import LLMClient, LLMError, OffPeakSkip
from daily_digest.pipeline import (
    ModelValidationError, PipelineError, deliver, prepare, rank_metadata, ranked_papers,
    validated_model_batch,
)
from daily_digest.state import StateError, load_json, validate_state, write_json


BEIJING = timezone(timedelta(hours=8))


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = {
            "timezone": "Asia/Shanghai", "target_count": 5, "delivery_interval_days": 2,
            "recent_days": 7, "fallback_days": 30, "shortlist_limit": 30,
            "minimum_score": 65, "profile": "Aero Hand",
            "llm": {"base_url": "https://api.deepseek.com", "ranking_model": "deepseek-flash",
                "summary_model": "deepseek-flash", "reasoning_effort": "low", "timeout_seconds": 180,
                "api_style": "chat_completions", "max_input_chars": 24000,
                "ranking_batch_size": 5, "summary_batch_size": 5},
        }
        self.config_path = self.root / "config.yaml"
        self.state = {"version": 1, "sent_ids": [], "last_sent_date": "2026-10-09"}
        self.batch = {"version": 1, "status": "complete", "date": "2026-10-09",
            "batch_id": "saved-batch", "attempt_run_id": "old-1", "paper_ids": ["arxiv:2609.00001"],
            "cards": [{"msg_type": "interactive", "card": {}}], "acknowledged_cards": [],
            "report": "Saved preview", "usage": {}}
        self.output = self.root / "output"
        self.github_output = self.root / "github-output"
        self.summary = self.root / "summary"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "GITHUB_EVENT_NAME": "schedule", "GITHUB_RUN_ID": "test", "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_OUTPUT": str(self.github_output), "GITHUB_STEP_SUMMARY": str(self.summary),
            "LLM_API_KEY": "test-key-not-real",
        }))
        self.clock = self.stack.enter_context(patch("daily_digest.pipeline.datetime"))
        self.clock.now.return_value = datetime(2026, 10, 10, 8, tzinfo=BEIJING)
        self.stack.enter_context(patch("daily_digest.pipeline.ZoneInfo", return_value=BEIJING))
        self.peak = self.stack.enter_context(patch("daily_digest.pipeline.ensure_off_peak"))
        self.collect = self.stack.enter_context(patch("daily_digest.pipeline.collect", return_value=({}, 0)))
        self.journals = self.stack.enter_context(patch("daily_digest.pipeline.collect_journals", return_value=({}, 0, "")))
        self.client = Mock(usage={})
        self.llm = self.stack.enter_context(patch("daily_digest.pipeline.LLMClient", return_value=self.client))

    def run_prepare(self):
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        write_json(self.root / ".daily/state.json", self.state)
        write_json(self.root / ".daily/pending.json", self.batch)
        prepare(self.root, self.config_path, self.output, preview=False, retry_uncertain=False)

    def assert_skipped_before_network(self):
        self.collect.assert_not_called()
        self.journals.assert_not_called()
        self.llm.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertIn("should_send=false", self.github_output.read_text(encoding="utf-8"))
        self.assertIn("prepared=false", self.github_output.read_text(encoding="utf-8"))
        self.assertEqual(load_json(self.root / ".daily/state.json"), self.state)

    def test_cooldown_is_checked_before_sources_and_peak_guard(self):
        self.run_prepare()
        self.assert_skipped_before_network()
        self.peak.assert_not_called()
        self.assertIn("2026-10-11", self.summary.read_text(encoding="utf-8"))

    def test_peak_preflight_is_a_normal_skip_before_source_refresh(self):
        self.clock.now.return_value = datetime(2026, 10, 12, 10, tzinfo=BEIJING)
        self.peak.side_effect = OffPeakSkip("Peak hours; no request sent.")
        self.run_prepare()
        self.assert_skipped_before_network()
        self.peak.assert_called_once()
        self.assertIn("Peak hours", self.summary.read_text(encoding="utf-8"))

    def test_two_day_interval_handles_month_and_leap_year_boundaries(self):
        for previous, current in (("2026-09-30", datetime(2026, 10, 2, 8, tzinfo=BEIJING)),
                                  ("2028-02-28", datetime(2028, 3, 1, 8, tzinfo=BEIJING))):
            with self.subTest(previous=previous):
                self.state["last_sent_date"] = previous
                self.clock.now.return_value = current
                self.run_prepare()
                self.assertEqual(load_json(self.output / "pending.json")["date"], current.date().isoformat())
                self.assertEqual(load_json(self.output / "state.json"), self.state)
                self.assertIn("should_send=true", self.github_output.read_text(encoding="utf-8"))

    def test_delayed_success_resets_cooldown_from_confirmation_date(self):
        self.state.update(last_sent_date="2026-10-08", last_confirmed_date="2026-10-09")
        self.run_prepare()
        self.assert_skipped_before_network()

    def test_saved_failed_batch_resumes_without_model_calls_even_at_peak(self):
        self.batch.update(status="failed", date="2026-10-10", acknowledged_cards=[])
        self.clock.now.return_value = datetime(2026, 10, 12, 10, tzinfo=BEIJING)
        self.peak.side_effect = OffPeakSkip("Peak hours")
        self.run_prepare()
        self.peak.assert_not_called()
        self.collect.assert_not_called()
        self.llm.assert_not_called()
        saved = load_json(self.output / "pending.json")
        self.assertEqual(saved["date"], "2026-10-10")
        self.assertEqual(saved["attempt_run_id"], "test-1")
        with patch("daily_digest.pipeline.send_card") as send:
            deliver(self.output, self.root / "delivered")
        send.assert_called_once()
        confirmed = load_json(self.root / "delivered/state.json")
        self.assertEqual(confirmed["last_sent_date"], "2026-10-10")
        self.assertEqual(confirmed["last_confirmed_date"], "2026-10-12")

    def test_crossing_peak_during_generation_discards_partial_output_normally(self):
        self.clock.now.return_value = datetime(2026, 10, 12, 8, 59, tzinfo=BEIJING)
        self.config["journals"] = {"enabled": True}
        with patch("daily_digest.pipeline.rank_metadata", side_effect=OffPeakSkip("Peak hours")):
            self.run_prepare()
        self.assertFalse((self.output / "pending.json").exists())
        self.assertEqual(load_json(self.root / ".daily/state.json"), self.state)
        self.assertNotIn("should_send=true", self.github_output.read_text(encoding="utf-8"))
        self.assertIn("Peak hours", self.summary.read_text(encoding="utf-8"))

    def test_invalid_interval_and_future_confirmation_are_errors(self):
        for value in (0, True, "2", 366):
            self.config["delivery_interval_days"] = value
            with self.subTest(value=value), self.assertRaises(PipelineError):
                self.run_prepare()
        self.config["delivery_interval_days"] = 2
        self.state["last_confirmed_date"] = "2026-10-11"
        with self.assertRaises(StateError):
            self.run_prepare()

    def test_invalid_confirmation_date_is_rejected(self):
        for value in ("2026-02-30", "not-a-date", ["2026-10-09"]):
            with self.subTest(value=value), self.assertRaises(StateError):
                validate_state(dict(self.state, last_confirmed_date=value))


class ValidationRetryTests(unittest.TestCase):
    def test_ranking_retries_only_bad_second_batch_and_does_not_restart_first(self):
        papers = [{"paper_id": f"arxiv:2609.{i:05d}"} for i in range(11)]
        config = {"minimum_score": 65, "profile": "Aero Hand", "llm": {
            "ranking_model": "deepseek-flash", "ranking_batch_size": 5, "max_input_chars": 24000}}
        client = Mock()
        attempts = 0

        def generated(_system, user, **_kwargs):
            nonlocal attempts
            attempts += 1
            rows = [{"paper_id": p["paper_id"], "score": 80} for p in json.loads(user)["candidates"]]
            if attempts == 2:
                rows[0]["paper_id"] = "unknown-id"
            return {"rankings": rows}

        client.generate_json.side_effect = generated
        ranked = rank_metadata(client, papers, config)
        self.assertEqual(len(ranked), 11)
        calls = client.generate_json.call_args_list
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[1], calls[2])
        self.assertNotEqual(calls[0], calls[1])
        self.assertNotEqual(calls[2], calls[3])

    def test_persistent_bad_output_stops_after_exactly_one_retry(self):
        client = Mock()
        client.generate_json.return_value = {"rankings": [{"paper_id": [], "score": 80}]}
        with self.assertRaises(ModelValidationError):
            validated_model_batch(client, "system", "input", 100,
                lambda result: ranked_papers(result, [{"paper_id": "known"}], 65))
        self.assertEqual(client.generate_json.call_count, 2)

    def test_invalid_json_is_retried_and_both_responses_are_counted(self):
        client = LLMClient("https://api.deepseek.com", "deepseek-flash", "test-key-not-real")

        def response(content):
            result = Mock(status_code=200, headers={"Content-Type": "application/json"})
            result.json.return_value = {"choices": [{"finish_reason": "stop", "message": {"content": content}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
            result.text = json.dumps(result.json.return_value)
            return result

        client.session.post = Mock(side_effect=[response("invalid JSON"), response('{"ok": true}')])
        result = validated_model_batch(client, "system", "input", 100, lambda answer: answer)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.usage["requests"], 2)
        self.assertEqual(client.usage["input_tokens"], 20)

    def test_auth_transport_and_peak_errors_are_not_retried_as_bad_output(self):
        for error in (LLMError("HTTP 401"), LLMError("transport failed"), OffPeakSkip("Peak hours")):
            client = Mock()
            client.generate_json.side_effect = error
            with self.subTest(error=error), self.assertRaises(type(error)):
                validated_model_batch(client, "system", "input", 100, lambda answer: answer)
            client.generate_json.assert_called_once()


if __name__ == "__main__":
    unittest.main()
