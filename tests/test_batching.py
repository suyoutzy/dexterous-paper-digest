"""Bounded model requests preserve all candidates and their source identity."""

import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from daily_digest.pipeline import PipelineError, input_groups, prepare
from daily_digest.state import load_json, write_json


def payload_length(context, group):
    return len(json.dumps(dict(context, candidates=group), ensure_ascii=False))


class InputGroupsTests(unittest.TestCase):
    def test_exact_character_budget_accepts_boundary_and_splits_one_character_below(self):
        context = {"profile": "灵巧手控制研究"}
        items = [{"paper_id": str(index), "evidence": "触觉" * 20} for index in range(1, 4)]
        limit = payload_length(context, items[:2])
        self.assertEqual(input_groups(items, context, limit, 5), [items[:2], items[2:]])
        self.assertEqual(input_groups(items, context, limit - 1, 5), [[item] for item in items])

    def test_count_limit_preserves_every_item_and_original_order(self):
        items = [{"paper_id": str(index), "abstract": "method"} for index in range(11)]
        original = copy.deepcopy(items)
        groups = input_groups(items, {"profile": "Aero Hand"}, 24_000, 5)
        self.assertEqual([len(group) for group in groups], [5, 5, 1])
        self.assertEqual([item for group in groups for item in group], items)
        self.assertEqual(items, original)

    def test_large_abstract_budget_splits_items_without_truncating_either(self):
        items = [{"paper_id": str(index), "abstract": "触" * 18_000} for index in range(3)]
        context = {"profile": "灵巧手设计与控制"}
        groups = input_groups(items, context, 24_000, 2)
        self.assertEqual([len(group) for group in groups], [1, 1, 1])
        self.assertEqual([item for group in groups for item in group], items)
        self.assertTrue(all(payload_length(context, group) <= 24_000 for group in groups))

    def test_atomic_item_that_cannot_fit_is_rejected_instead_of_dropped_or_truncated(self):
        items = [{"paper_id": "small", "abstract": "ok"}, {"paper_id": "large", "abstract": "触" * 1000}]
        original = copy.deepcopy(items)
        with self.assertRaises(PipelineError):
            input_groups(items, {"profile": "Aero Hand"}, 200, 5)
        self.assertEqual(items, original)

    def test_empty_candidates_and_invalid_limits(self):
        self.assertEqual(input_groups([], {"profile": "Aero Hand"}, 24_000, 5), [])
        for budget, count in ((0, 5), (-1, 5), (24_000, 0), (24_000, -1)):
            with self.subTest(budget=budget, count=count), self.assertRaises(PipelineError):
                input_groups([], {}, budget, count)


def source_papers():
    return {
        f"arxiv:2609.{index:05d}": {
            "paper_id": f"arxiv:2609.{index:05d}", "arxiv_id": f"2609.{index:05d}",
            "title": f"Dexterous hand control {index}",
            "abstract": "A tendon-driven hand performs controlled multi-finger grasping.",
            "authors": f"Example Author {index}, Another Author",
            "affiliations": [f"Source-reported Robotics Lab {index}"],
            "journal_ref": f"Source-reported venue note {index}",
            "doi": f"10.0000/mock-paper-{index}",
            "publish_date": "2026-09-25", "date_basis": "initial_publication",
            "arxiv_url": f"https://arxiv.org/abs/2609.{index:05d}",
        }
        for index in range(1, 12)
    }


class PrepareBatchIntegrationTests(unittest.TestCase):
    def run_prepare(self, ranking_scores, summary_transform=None, sources=None, extended_sources=None):
        config = {
            "timezone": "Asia/Shanghai", "target_count": 5, "recent_days": 7,
            "fallback_days": 30, "shortlist_limit": 30,
            "minimum_score": 65, "arxiv_max_results": 80, "keywords": ["dexterous hand"],
            "profile": "灵巧末端设计与控制初学者，具备嵌入式基础，已经组装 Aero Hand。",
            "llm": {
                "base_url": "https://jojocode.com/v1", "api_style": "auto",
                "ranking_model": "gpt-5.6-sol", "summary_model": "gpt-5.6-sol",
                "reasoning_effort": "medium", "timeout_seconds": 180,
                "max_input_chars": 24_000, "ranking_batch_size": 5, "summary_batch_size": 5,
            },
        }
        requests = {"rank": [], "summary": [], "journal_windows": []}
        db = copy.deepcopy(source_papers() if sources is None else sources)
        source_lookup = dict(db, **(extended_sources or {}))
        config["journals"] = {"enabled": extended_sources is not None or any(p.get("source") == "crossref" for p in db.values()),
            "fallback_days": 90, "fallback_candidate_limit": 10}

        def collect_journal_metadata(current, _today, settings):
            requests["journal_windows"].append(settings["fallback_days"])
            extra = extended_sources or {} if settings["fallback_days"] == 90 else {}
            return dict(current, **extra), len(extra), "模拟期刊检索。"
        client = Mock()
        client.usage = {"input_tokens": 0, "output_tokens": 0}

        def generate_json(system, user, **_kwargs):
            payload = json.loads(user)
            group = payload["candidates"]
            self.assertLessEqual(len(user), config["llm"]["max_input_chars"])
            if "rankings" in system:
                requests["rank"].append(payload)
                self.assertEqual(payload["required_count"], len(group))
                self.assertLessEqual(len(group), 5)
                return {"rankings": [{
                    "paper_id": p["paper_id"],
                    "score": ranking_scores.get(p["paper_id"], 10), "reason": "摘要支持该评分。",
                } for p in group]}
            requests["summary"].append(payload)
            self.assertLessEqual(len(group), 5)
            self.assertEqual(payload["required_count"], len(group))
            # Reverse model output order to ensure summary cannot undo ranking.
            result = {"papers": [{
                "paper_id": p["paper_id"],
                "title_zh": "灵巧手标定与控制", "summary": "结合腱绳机构建立标定模型。",
                "why_for_you": "有助于理解机构与控制参数的联系。",
                "learning_action": "先在仿真中验证标定过程；硬件依赖以论文为准。",
                "evidence": "摘要描述结合腱绳机构开展抓取控制；未核实正文实验。",
            } for p in reversed(group)]}
            return summary_transform(result) if summary_transform else result

        client.generate_json.side_effect = generate_json

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.yaml"
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            write_json(root / "docs/papers_db.json", db)
            output = root / "prepared"
            with (
                patch.dict(os.environ, {
                    "LLM_API_KEY": "mock-key-not-real", "GITHUB_RUN_ID": "test-run",
                    "GITHUB_RUN_ATTEMPT": "1", "GITHUB_OUTPUT": "", "GITHUB_STEP_SUMMARY": "",
                }),
                patch("daily_digest.pipeline.datetime") as clock,
                patch("daily_digest.pipeline.ZoneInfo", return_value=timezone(timedelta(hours=8))),
                patch("daily_digest.pipeline.collect", return_value=(db, len(db))),
                patch("daily_digest.pipeline.collect_journals", side_effect=collect_journal_metadata),
                patch("daily_digest.pipeline.LLMClient", return_value=client),
                patch("daily_digest.pipeline.requests.get", side_effect=AssertionError(
                    "The abstract-only pipeline must not request PDFs or Hugging Face metadata."
                )) as external_get,
            ):
                clock.now.return_value = datetime(2026, 9, 27, 12, tzinfo=timezone(timedelta(hours=8)))
                prepare(root, config_path, output, preview=True, retry_uncertain=False)
                external_get.assert_not_called()
            result = load_json(output / "pending.json")
        rank_ids = [p["paper_id"] for request in requests["rank"] for p in request["candidates"]]
        self.assertEqual(len(rank_ids), len(set(rank_ids)), "No candidate may be scored twice during journal fallback.")
        if sources is None and extended_sources is None:
            self.assertEqual(rank_ids, list(db))
            self.assertEqual([len(request["candidates"]) for request in requests["rank"]], [5, 5, 1])
        for stage in ("rank", "summary"):
            for request in requests[stage]:
                for item in request["candidates"]:
                    source = source_lookup[item["paper_id"]]
                    for field in ("abstract", "authors", "affiliations", "journal_ref", "doi", "publish_date"):
                        self.assertEqual(item[field], source[field])
        self.assertIn("仅依据标题/摘要与元数据，未读取正文", result["report"])
        return result, requests

    def test_without_a_journal_only_best_four_are_summarized_and_slot_is_not_filled_by_preprint(self):
        ranking = {f"arxiv:2609.{index:05d}": score for index, score in enumerate((70, 95, 80, 99, 65, 90), start=1)}
        result, requests = self.run_prepare(ranking)
        self.assertEqual(result["paper_ids"], [f"arxiv:2609.{index:05d}" for index in (4, 2, 6, 3)])
        self.assertEqual(len(requests["summary"]), 1)
        summary_ids = [p["paper_id"] for request in requests["summary"] for p in request["candidates"]]
        self.assertEqual(summary_ids, result["paper_ids"])
        self.assertNotIn("arxiv:2609.00005", summary_ids)
        self.assertEqual(len(summary_ids), len(set(summary_ids)))
        self.assertIn("期刊保留名额暂缺", result["report"])

    def journal_record(self, doi, published="2026-09-25"):
        source = source_papers()["arxiv:2609.00001"]
        return dict(source, paper_id="doi:" + doi, doi=doi, arxiv_id="", source="crossref",
            title="Dexterous hand tactile calibration study " + doi,
            authors="Journal Author " + doi, journal_name="IEEE Robotics and Automation Letters",
            journal_ref="IEEE Robotics and Automation Letters", publish_date=published)

    def test_best_four_are_preserved_and_a_separate_journal_is_always_fifth(self):
        journal = self.journal_record("10.1109/quota.1")
        sources = dict(source_papers(), **{journal["paper_id"]: journal})
        ranking = {f"arxiv:2609.{i:05d}": score for i, score in enumerate((70, 95, 80, 99, 65, 90), 1)}
        ranking[journal["paper_id"]] = 72
        result, requests = self.run_prepare(ranking, sources=sources)
        self.assertEqual(result["paper_ids"], ["arxiv:2609.00004", "arxiv:2609.00002", "arxiv:2609.00006",
            "arxiv:2609.00003", journal["paper_id"]])
        self.assertEqual(requests["journal_windows"], [30])

    def test_missing_extra_journal_queries_ninety_days_then_scores_only_new_journal_candidates(self):
        first = self.journal_record("10.1109/quota.1")
        older = self.journal_record("10.1109/quota.2", "2026-07-15")
        sources = dict(source_papers(), **{first["paper_id"]: first})
        ranking = {"arxiv:2609.00001": 99, first["paper_id"]: 98, "arxiv:2609.00002": 97,
            "arxiv:2609.00003": 96, older["paper_id"]: 100}
        result, requests = self.run_prepare(ranking, sources=sources, extended_sources={older["paper_id"]: older})
        self.assertEqual(result["paper_ids"], ["arxiv:2609.00001", first["paper_id"], "arxiv:2609.00002",
            "arxiv:2609.00003", older["paper_id"]])
        self.assertEqual(requests["journal_windows"], [30, 90])
        self.assertEqual([p["paper_id"] for p in requests["rank"][-1]["candidates"]], [older["paper_id"]])
        self.assertIn("最近90天", result["report"])
        self.assertEqual([p["paper_id"] for p in requests["summary"][0]["candidates"]], result["paper_ids"])

    def test_fallback_cannot_add_journal_version_of_an_already_selected_preprint(self):
        duplicate = self.journal_record("10.0000/mock-paper-1", "2026-07-15")
        ranking = {f"arxiv:2609.{i:05d}": 95 - i for i in range(1, 7)}
        ranking[duplicate["paper_id"]] = 100
        result, requests = self.run_prepare(ranking, extended_sources={duplicate["paper_id"]: duplicate})
        self.assertEqual(len(result["paper_ids"]), 4)
        self.assertNotIn(duplicate["paper_id"], result["paper_ids"])
        self.assertEqual(requests["journal_windows"], [30, 90])

    def test_when_thirty_days_have_no_candidates_an_older_journal_can_still_be_recommended(self):
        older = self.journal_record("10.1109/quota.3", "2026-07-15")
        result, requests = self.run_prepare({older["paper_id"]: 86}, sources={},
            extended_sources={older["paper_id"]: older})
        self.assertEqual(result["paper_ids"], [older["paper_id"]])
        self.assertEqual(requests["journal_windows"], [30, 90])
        self.assertEqual(len(requests["rank"]), 1)

    def test_fallback_does_not_rescore_journal_version_of_an_evaluated_but_unselected_preprint(self):
        duplicate = self.journal_record("10.0000/mock-paper-6", "2026-07-15")
        fresh_work = self.journal_record("10.1109/quota.4", "2026-07-16")
        ranking = {f"arxiv:2609.{i:05d}": 96 - i for i in range(1, 7)}
        ranking.update({duplicate["paper_id"]: 100, fresh_work["paper_id"]: 86})
        result, requests = self.run_prepare(ranking, extended_sources={
            duplicate["paper_id"]: duplicate, fresh_work["paper_id"]: fresh_work})
        self.assertEqual(result["paper_ids"][-1], fresh_work["paper_id"])
        ranked_ids = [p["paper_id"] for request in requests["rank"] for p in request["candidates"]]
        self.assertNotIn(duplicate["paper_id"], ranked_ids)

    def test_month_precision_is_not_reported_as_definitely_older_than_seven_days(self):
        journal = self.journal_record("10.1109/quota.5", "2026-09")
        sources = dict(source_papers(), **{journal["paper_id"]: journal})
        ranking = {f"arxiv:2609.{i:05d}": 96 - i for i in range(1, 6)}
        ranking[journal["paper_id"]] = 80
        result, _requests = self.run_prepare(ranking, sources=sources)
        self.assertEqual(result["paper_ids"][-1], journal["paper_id"])
        footer = result["cards"][-1]["card"]["elements"][-1]["elements"][0]["content"]
        self.assertIn("未能确认为最近7天", footer)
        self.assertNotIn("超过7天", footer)

    def test_below_threshold_items_from_every_ranking_batch_do_not_fill_five_slots(self):
        ranking = {"arxiv:2609.00001": 80, "arxiv:2609.00011": 90}
        result, requests = self.run_prepare(ranking)
        self.assertEqual(result["paper_ids"], ["arxiv:2609.00011", "arxiv:2609.00001"])
        self.assertEqual(len(requests["summary"]), 1)
        self.assertIn("达到推荐门槛的论文有2篇", result["report"])

    def test_summary_cannot_omit_any_selected_paper(self):
        ranking = {"arxiv:2609.00001": 80, "arxiv:2609.00011": 90}

        def omit_paper(result):
            result["papers"] = result["papers"][:-1]
            return result

        with self.assertRaisesRegex(PipelineError, "invalid paper count"):
            self.run_prepare(ranking, summary_transform=omit_paper)

    def test_summary_must_supply_all_required_text_for_selected_papers(self):
        def omit_text(result):
            result["papers"][0]["summary"] = ""
            return result

        with self.assertRaisesRegex(PipelineError, "required text: summary"):
            self.run_prepare({"arxiv:2609.00001": 80}, summary_transform=omit_text)

    def test_author_and_publication_metadata_reaches_ranking_and_selected_summary(self):
        result, requests = self.run_prepare({"arxiv:2609.00011": 90})
        self.assertEqual(result["paper_ids"], ["arxiv:2609.00011"])
        candidate = requests["summary"][0]["candidates"][0]
        source = source_papers()[candidate["paper_id"]]
        for field in ("authors", "affiliations", "journal_ref", "doi"):
            self.assertEqual(candidate[field], source[field])


if __name__ == "__main__":
    unittest.main()
