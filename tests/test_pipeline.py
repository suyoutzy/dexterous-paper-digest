"""Regression checks for relevance, source identity, and durable delivery state.

All network/model calls are mocked. Temporary artifact directories simulate
GitHub jobs; no live messages or API calls are made.
"""

import copy
import json
import os
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from daily_digest.delivery import DeliveryError, DeliveryUncertainError
from daily_digest.pipeline import (
    PipelineError,
    candidates,
    collect,
    deliver,
    final_papers,
    ranked_papers,
    relevance,
)
from daily_digest.state import (
    StateError,
    acknowledge_all,
    load_json,
    publish,
    resume_pending,
    write_json,
)


TODAY = date(2026, 9, 27)
CONFIG = {
    "recent_days": 7,
    "fallback_days": 30,
    "shortlist_limit": 30,
    "target_count": 5,
    "minimum_score": 65,
}


def fresh_state():
    return {"version": 1, "sent_ids": [], "last_sent_date": ""}


def paper(identifier="2609.00001", published="2026-09-25", title="Dexterous hand calibration"):
    return {
        "paper_id": f"arxiv:{identifier}",
        "arxiv_id": identifier,
        "title": title,
        "abstract": "Calibration of a tendon-driven hand for multi-finger grasping.",
        "publish_date": published,
        "arxiv_url": f"https://arxiv.org/abs/{identifier}",
        "pdf_url": f"https://arxiv.org/pdf/{identifier}",
        "code_url": "https://github.com/example/hand",
        "authors": "Researcher A, Researcher B",
        "ranking_score": 88,
        "reading_depth": "abstract_only",
        "enrichment_note": "仅依据标题和摘要；未读取正文。",
        "is_recent": True,
    }


def card(index):
    return {
        "msg_type": "interactive",
        "card": {"elements": [{"tag": "div", "text": {"tag": "plain_text", "content": f"card {index}"}}]},
    }


def pending(cards=2, status="inflight", run_id="123-1"):
    return {
        "version": 1,
        "batch_id": "test-batch-not-real",
        "date": TODAY.isoformat(),
        "status": status,
        "attempt_run_id": run_id,
        "paper_ids": ["arxiv:2609.00001", "arxiv:2609.00002"],
        "acknowledged_cards": [],
        "cards": [card(index) for index in range(cards)],
        "report": "# 灵巧手论文日报\n\n仅依据保存的标题和摘要。\n",
        "usage": {},
    }


def summary_row(identifier="arxiv:2609.00001"):
    return {
        "paper_id": identifier,
        "priority_score": 88,
        "title_zh": "灵巧手机构标定",
        "summary": "结合腱绳机构与标定方法。",
        "why_for_you": "适合建立灵巧末端设计与控制基础。",
        "learning_action": "先检查机构参数并建立仿真，需要论文指定的传感器。",
        "evidence": "所提供摘要支持标定方法；未核实全文实验。",
    }


class SelectionTests(unittest.TestCase):
    def test_generic_vla_is_excluded_without_a_hand_or_technical_grasp_contribution(self):
        generic = {
            "title": "A General Vision Language Action Policy for Robotic Manipulation",
            "abstract": "A pretrained transformer completes kitchen tasks with a single robot arm.",
            "code_url": "https://github.com/example/vla",
        }
        self.assertEqual(relevance(generic), 0)
        self.assertGreater(relevance(paper()), 0)

    def test_candidates_remove_sent_future_old_bad_dates_and_noncanonical_ids(self):
        valid = paper("2609.00001")
        sent = paper("2609.00002")
        future = paper("2609.00003", "2026-09-28")
        old = paper("2608.00004", "2026-08-26")
        malformed_date = paper("2609.00005", "not-a-date")
        bad_id = paper("2609.1")
        db = {p["paper_id"]: p for p in (valid, sent, future, old, malformed_date, bad_id)}
        db["openreview:fake"] = paper("2609.00006")
        state = fresh_state()
        state["sent_ids"] = [sent["paper_id"]]
        result = candidates(db, state, TODAY, CONFIG)
        self.assertEqual([p["paper_id"] for p in result], [valid["paper_id"]])

    def test_recent_seven_day_candidates_outrank_older_high_rule_scores(self):
        recent = paper("2609.00001", "2026-09-24", "Dexterous hand")
        recent["abstract"] = "A robotic hand design."
        recent["code_url"] = ""
        older = paper("2609.00002", "2026-09-10", "Dexterous hand in-hand manipulation using tactile impedance force control")
        db = {p["paper_id"]: p for p in (older, recent)}
        result = candidates(db, fresh_state(), TODAY, CONFIG)
        self.assertGreater(result[1]["rule_score"], result[0]["rule_score"])
        self.assertEqual(result[0]["paper_id"], recent["paper_id"])
        self.assertTrue(result[0]["is_recent"])
        self.assertFalse(result[1]["is_recent"])

    def test_canonical_paper_and_pdf_urls_replace_untrusted_database_fields(self):
        original = paper()
        original["arxiv_url"] = "https://evil.example/fake"
        original["pdf_url"] = "javascript:alert(1)"
        result = candidates({original["paper_id"]: original}, fresh_state(), TODAY, CONFIG)
        self.assertEqual(result[0]["arxiv_url"], "https://arxiv.org/abs/2609.00001")
        self.assertEqual(result[0]["pdf_url"], "https://arxiv.org/pdf/2609.00001")
        self.assertEqual(original["arxiv_url"], "https://evil.example/fake")

    def test_ranking_rejects_unknown_or_duplicate_ids_and_nonfinite_scores(self):
        source = [paper()]
        for rows in (
            [{"paper_id": "arxiv:2609.99999", "score": 90}],
            [{"paper_id": source[0]["paper_id"], "score": 90}] * 2,
            [{"paper_id": source[0]["paper_id"], "score": float("nan")}],
            [{"paper_id": source[0]["paper_id"], "score": True}],
        ):
            with self.subTest(rows=rows), self.assertRaises(PipelineError):
                ranked_papers({"rankings": rows}, source, 65)

    def test_ranking_must_cover_every_candidate_instead_of_silently_omitting_one(self):
        sources = [paper("2609.00001"), paper("2609.00002")]
        result = {"rankings": [{"paper_id": sources[0]["paper_id"], "score": 85}]}
        with self.assertRaises(PipelineError):
            ranked_papers(result, sources, 65)

    @patch("daily_digest.pipeline.requests.get")
    def test_http_200_arxiv_error_or_non_atom_response_is_not_an_empty_day(self, get):
        collect_config = dict(CONFIG, keywords=["dexterous hand"], arxiv_max_results=200)
        error_feed = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
            <id>http://arxiv.org/api/errors#incorrect_id_format</id>
            <title>Error</title><summary>invalid search</summary>
            </entry></feed>"""
        for content in (error_feed.encode(), b"<html><body>Unavailable</body></html>"):
            with self.subTest(content=content):
                get.reset_mock()
                get.return_value = Mock(status_code=200, content=content)
                with self.assertRaises(PipelineError):
                    collect({}, TODAY, collect_config)
                get.assert_called_once()

    def test_final_selection_rejects_unknown_ids_and_uses_only_source_links_and_reading_depth(self):
        source = paper()
        with self.assertRaises(PipelineError):
            final_papers({"papers": [summary_row("arxiv:2609.99999")]}, [source], CONFIG)
        row = summary_row()
        row.update({
            "arxiv_url": "https://evil.example/model-link",
            "pdf_url": "https://evil.example/model-pdf",
            "code_url": "https://evil.example/model-code",
            "reading_depth": "完整精读全文",
        })
        result = final_papers({"papers": [row]}, [source], CONFIG)[0]
        for field in ("arxiv_url", "pdf_url", "code_url"):
            self.assertEqual(result[field], source[field])
        self.assertEqual(result["reading_depth"], source["enrichment_note"])
        self.assertEqual(result["reading_depth_code"], source["reading_depth"])

    def test_final_selection_does_not_fill_five_slots_with_below_threshold_papers(self):
        source = paper()
        source["ranking_score"] = 64
        row = summary_row()
        row["priority_score"] = 100  # Model prose cannot overwrite the settled ranking.
        self.assertEqual(final_papers({"papers": [row]}, [source], CONFIG), [])

    def test_summary_preserves_ranked_scores_and_cannot_omit_selected_papers(self):
        source = paper()
        row = summary_row()
        row["priority_score"] = 1
        result = final_papers({"papers": [row]}, [source], CONFIG)
        self.assertEqual(result[0]["priority_score"], source["ranking_score"])
        self.assertEqual(result[0]["authors"], source["authors"])
        self.assertEqual(result[0]["reading_depth_code"], "abstract_only")
        with self.assertRaises(PipelineError):
            final_papers({"papers": []}, [source], CONFIG)

    @patch("daily_digest.pipeline.requests.get")
    def test_arxiv_metadata_preserves_author_and_bibliographic_fields_without_verifying_authority(self, get):
        get.return_value = Mock(status_code=200, content=b"""
            <feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
              <entry><id>http://arxiv.org/abs/2609.00001v2</id>
                <title>Dexterous hand calibration</title><summary>Tendon driven hand control.</summary>
                <published>2026-09-25T00:00:00Z</published>
                <author><name>Researcher A</name><arxiv:affiliation>Example Lab</arxiv:affiliation></author>
                <arxiv:journal_ref>Example Journal 2026</arxiv:journal_ref>
                <arxiv:doi>10.1234/example</arxiv:doi>
              </entry>
            </feed>""")
        db, count = collect({}, TODAY, dict(CONFIG, keywords=["dexterous hand"], arxiv_max_results=80))
        self.assertEqual(count, 1)
        result = db["arxiv:2609.00001"]
        self.assertEqual(result["authors"], "Researcher A")
        self.assertEqual(result["affiliations"], ["Example Lab"])
        self.assertEqual(result["journal_ref"], "Example Journal 2026")
        self.assertEqual(result["doi"], "10.1234/example")
        self.assertNotIn("author_authority", result)
        get.assert_called_once()


class DurableDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.output = self.root / "output"
        self.source.mkdir()
        write_json(self.source / "state.json", fresh_state())
        write_json(self.source / "pending.json", pending())
        self.environment = patch.dict(os.environ, {
            "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
            "FEISHU_WEBHOOK": "test-webhook-not-real", "FEISHU_SIGN_SECRET": "test-secret-not-real",
        })
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_acknowledge_all_cannot_mark_any_paper_sent_while_a_card_is_missing(self):
        state = fresh_state()
        batch = pending()
        batch["acknowledged_cards"] = [0]
        before = copy.deepcopy(state)
        with self.assertRaises(StateError):
            acknowledge_all(state, batch)
        self.assertEqual(state, before)
        self.assertEqual(batch["status"], "inflight")

    @patch("daily_digest.pipeline.send_card")
    def test_delivery_checkpoints_each_ack_and_marks_papers_only_after_every_ack(self, send):
        count = 0

        def inspect_checkpoint(*_args):
            nonlocal count
            saved = load_json(self.output / "pending.json")
            state = load_json(self.output / "state.json")
            self.assertEqual(saved["acknowledged_cards"], list(range(count)))
            self.assertEqual(state["sent_ids"], [])
            self.assertEqual(state["last_sent_date"], "")
            count += 1

        send.side_effect = inspect_checkpoint
        deliver(self.source, self.output)
        self.assertEqual(send.call_count, 2)
        result = load_json(self.output / "pending.json")
        state = load_json(self.output / "state.json")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["acknowledged_cards"], [0, 1])
        self.assertEqual(state["sent_ids"], result["paper_ids"])
        self.assertEqual(state["last_sent_date"], TODAY.isoformat())

    @patch("daily_digest.pipeline.send_card", side_effect=[None, DeliveryUncertainError("ambiguous transport")])
    def test_uncertain_delivery_persists_partial_ack_but_never_marks_papers_sent(self, send):
        with self.assertRaises(DeliveryUncertainError):
            deliver(self.source, self.output)
        result = load_json(self.output / "pending.json")
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["acknowledged_cards"], [0])
        self.assertEqual(load_json(self.output / "state.json"), fresh_state())
        self.assertEqual(send.call_count, 2)

    @patch("daily_digest.pipeline.send_card", side_effect=[None, DeliveryError("explicit rejection")])
    def test_explicit_rejection_persists_failed_status_without_losing_prior_ack(self, send):
        with self.assertRaises(DeliveryError):
            deliver(self.source, self.output)
        result = load_json(self.output / "pending.json")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["acknowledged_cards"], [0])
        self.assertEqual(load_json(self.output / "state.json"), fresh_state())

    @patch("daily_digest.pipeline.send_card")
    def test_resuming_partial_batch_skips_already_acknowledged_card(self, send):
        batch = pending()
        batch["acknowledged_cards"] = [0]
        write_json(self.source / "pending.json", batch)
        deliver(self.source, self.output)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[2], batch["cards"][1])
        self.assertEqual(load_json(self.output / "pending.json")["status"], "complete")

    def test_new_attempt_of_same_github_run_cannot_automatically_replay_uncertain_or_inflight_batch(self):
        for status in ("uncertain", "inflight"):
            batch = pending(status=status, run_id="123-1")
            with self.subTest(status=status), self.assertRaises(StateError):
                resume_pending(batch, "123-2")
            self.assertEqual(batch["status"], status)
        resumed = resume_pending(pending(status="uncertain"), "123-2", retry_uncertain=True)
        self.assertEqual(resumed["status"], "inflight")
        self.assertEqual(resumed["attempt_run_id"], "123-2")

    def test_same_attempt_also_requires_explicit_opt_in_to_replay_an_unconfirmed_batch(self):
        for status in ("uncertain", "inflight"):
            batch = pending(status=status, run_id="123-1")
            with self.subTest(status=status), self.assertRaises(StateError):
                resume_pending(batch, "123-1")

    @patch("daily_digest.pipeline.send_card")
    def test_delivery_requires_the_exact_persisted_run_attempt(self, send):
        with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "2"}):
            with self.assertRaises(StateError):
                deliver(self.source, self.output)
        send.assert_not_called()

    def test_completed_batch_cannot_be_replayed(self):
        with self.assertRaises(StateError):
            resume_pending(pending(status="complete"), "123-2", retry_uncertain=True)


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.source = self.base / "artifact"
        self.root = self.base / "checkout"
        self.source.mkdir()
        self.root.mkdir()
        write_json(self.source / "state.json", fresh_state())
        write_json(self.source / "pending.json", pending())
        write_json(self.source / "papers_db.json", {"arxiv:2609.00001": paper()})

    def tearDown(self):
        self.temporary.cleanup()

    def test_publisher_copies_only_named_data_and_does_not_replace_or_execute_code(self):
        script = self.root / "daily_digest" / "pipeline.py"
        script.parent.mkdir()
        script.write_text("# trusted original implementation\n", encoding="utf-8")
        malicious = self.source / "daily_digest" / "pipeline.py"
        malicious.parent.mkdir()
        malicious.write_text("raise RuntimeError('Artifact code must never run')\n", encoding="utf-8")
        workflow = self.source / ".github" / "workflows" / "injected.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text("name: untrusted artifact\n", encoding="utf-8")
        (self.source / "run.ps1").write_text("throw 'Artifact code must never run'\n", encoding="utf-8")
        (self.source / "preview.md").write_text("Untrusted alternate report", encoding="utf-8")
        publish(self.source, self.root, "plan", "123-1")
        self.assertEqual(script.read_text(encoding="utf-8"), "# trusted original implementation\n")
        self.assertFalse((self.root / ".github").exists())
        self.assertFalse((self.root / "run.ps1").exists())
        self.assertFalse((self.root / "preview.md").exists())
        created = {path.relative_to(self.root).as_posix() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(created, {
            "daily_digest/pipeline.py", "docs/papers_db.json", ".daily/state.json", ".daily/pending.json",
        })

    def test_artifact_from_another_attempt_is_rejected_before_any_publishing(self):
        with self.assertRaises(StateError):
            publish(self.source, self.root, "plan", "123-2")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_complete_result_requires_all_acks_and_matching_saved_intent(self):
        publish(self.source, self.root, "plan", "123-1")
        batch = pending(status="complete")
        batch["acknowledged_cards"] = [0]
        write_json(self.source / "pending.json", batch)
        with self.assertRaises(StateError):
            publish(self.source, self.root, "result", "123-1")
        self.assertFalse((self.root / "docs/digests").exists())
        batch["acknowledged_cards"] = [0, 1]
        batch["batch_id"] = "another-batch"
        write_json(self.source / "pending.json", batch)
        with self.assertRaises(StateError):
            publish(self.source, self.root, "result", "123-1")

    def test_complete_result_archives_only_report_embedded_in_confirmed_batch(self):
        publish(self.source, self.root, "plan", "123-1")
        batch = pending()
        state = fresh_state()
        batch["acknowledged_cards"] = [0, 1]
        acknowledge_all(state, batch)
        write_json(self.source / "pending.json", batch)
        write_json(self.source / "state.json", state)
        (self.source / "preview.md").write_text("Untrusted alternate report", encoding="utf-8")
        publish(self.source, self.root, "result", "123-1")
        archive = self.root / "docs/digests/2026-09-27.md"
        self.assertEqual(archive.read_text(encoding="utf-8"), batch["report"])
        self.assertEqual(load_json(self.root / ".daily/state.json"), state)
        self.assertFalse((self.root / "preview.md").exists())


if __name__ == "__main__":
    unittest.main()
