import copy
import json
import unittest

from daily_digest.delivery import build_cards, MAX_CARD_BYTES
from daily_digest.pipeline import final_papers, PipelineError, report, fit_summary_input
from test_pipeline import paper, CONFIG
from v4_fixtures import v4_row, evidence_text


class ContentV4Tests(unittest.TestCase):
    def test_number_rejection_identifies_the_result_missing_values_and_cited_blocks(self):
        source = dict(self.source(), reading_text=(
            "[E1] Object Flipping success rose from 14% to 86%.\n"
            "[E2] Screwdriver Rotation progress rose from 26.0% to 95.3%."))
        row = v4_row()
        row["experiments"]["results"] = [
            {"text": "翻转成功率从14%提高到86%。", "source_ids": ["E1"]},
            {"text": "旋转进度达到95.3%，提高69.3个百分点。", "source_ids": ["E2"]},
        ]
        with self.assertRaises(PipelineError) as raised:
            final_papers({"papers": [row]}, [source], CONFIG, require_v4=True)
        message = str(raised.exception)
        for detail in ("result 2", "69.3", "E2"):
            self.assertIn(detail, message)
        row["experiments"]["results"][1]["text"] = "旋转进度从26%提高到95.3%。"
        selected = final_papers({"papers": [row]}, [source], CONFIG, require_v4=True)[0]
        self.assertEqual(selected["experiments"]["results"][1]["text"], "旋转进度从26%提高到95.3%。")

    def test_source_segment_ids_ground_results_without_requiring_model_to_retype_the_pdf(self):
        source = fit_summary_input(dict(self.source(), reading_text=evidence_text()), {"required_count": 1}, 100_000)
        self.assertIn("[E1]", source["reading_text"])
        row = v4_row()
        row["experiments"]["results"][0].pop("quote")
        row["experiments"]["results"][0]["source_ids"] = ["E1"]
        selected = final_papers({"papers": [row]}, [source], CONFIG, require_v4=True)[0]
        self.assertIn("3.9", selected["experiments"]["results"][0]["text"])
        row["experiments"]["results"][0]["source_ids"] = ["E9999"]
        with self.assertRaisesRegex(PipelineError, "evidence"):
            final_papers({"papers": [row]}, [source], CONFIG, require_v4=True)
        row["experiments"]["results"][0].update(source_ids=["E1"], text="误差0.01 mm。")
        with self.assertRaisesRegex(PipelineError, "evidence"):
            final_papers({"papers": [row]}, [source], CONFIG, require_v4=True)
    def source(self, full=True):
        return dict(paper(), reading_text=evidence_text(), reading_depth="full_text" if full else "abstract_only",
            enrichment_note="论文全文（可提取文本；图像与公式未独立核验）。" if full else "摘要及公开页面，未获取全文。",
            reading_url="https://arxiv.org/pdf/2609.00001")

    def selected(self, row=None):
        return final_papers({"papers": [row or v4_row()]}, [self.source()], CONFIG)[0]

    def test_new_structure_survives_and_uses_server_supplied_reading_basis(self):
        row = v4_row()
        row["reading_basis"] = "完整实验核验"
        selected = self.selected(row)
        self.assertEqual(selected["content_version"], 4)
        self.assertEqual(selected["work_modules"], [1, 3])
        self.assertEqual(selected["reading_depth_code"], "full_text")
        self.assertEqual(selected["reading_basis"], self.source()["enrichment_note"])
        self.assertNotIn("reading_text", selected)
        self.assertNotIn("quote", json.dumps(selected["experiments"]))

    def test_all_four_sections_and_named_modules_appear_in_archive_and_card(self):
        selected = self.selected()
        digest = {"date": "2026-10-16", "papers": [selected]}
        archive = report(digest)
        card = json.dumps(build_cards(digest), ensure_ascii=False)
        for rendered in (archive, card):
            for heading in ("研究问题与核心方法", "实验结果与局限", "关键术语", "科研启发与阅读建议"):
                self.assertIn(heading, rendered)
            self.assertIn("工作模块：机器人本体与硬件、运动规划与控制", rendered)
            self.assertIn("阅读依据：", rendered)
            self.assertIn("选择性阅读", rendered)
            self.assertIn("基于证据分析", rendered)
            self.assertNotIn("为什么适合你", rendered)
            self.assertNotIn("可以学什么", rendered)
        self.assertEqual(archive.count("### "), 4)

    def test_result_without_matching_source_quote_is_rejected(self):
        row = v4_row()
        row["experiments"]["results"][0]["quote"] = "Success was 99.9 percent."
        with self.assertRaisesRegex(PipelineError, "evidence"):
            self.selected(row)

    def test_invented_result_number_cannot_borrow_an_unrelated_source_quote(self):
        row = v4_row()
        row["experiments"]["results"][0]["text"] = "误差只有0.01 mm。"
        with self.assertRaisesRegex(PipelineError, "evidence"):
            self.selected(row)

    def test_author_limit_requires_quote_but_analysis_is_labelled_separately(self):
        row = v4_row()
        row["experiments"]["limitations"][0].update(kind="author", quote="")
        with self.assertRaisesRegex(PipelineError, "evidence"):
            self.selected(row)
        row["experiments"]["limitations"][0].update(text="实验局限于一个任务。", quote="Testing was limited to one task.")
        self.assertIn("作者指出", report({"date": "2026-10-16", "papers": [self.selected(row)]}))

    def test_multiple_modules_and_invalid_types_are_checked(self):
        for modules in ([], [0], [7], [True], ["1"], [1, 1], 1):
            row = v4_row()
            row["work_modules"] = modules
            with self.subTest(modules=modules), self.assertRaisesRegex(PipelineError, "work module"):
                self.selected(row)

    def test_missing_sections_grade_and_term_roles_cannot_pass_validation(self):
        for field in ("research", "experiments", "terms", "insight", "reading"):
            row = v4_row()
            del row[field]
            with self.subTest(field=field), self.assertRaises(PipelineError):
                self.selected(row)
        row = v4_row()
        row["reading"]["level"] = "热门必读"
        with self.assertRaises(PipelineError):
            self.selected(row)
        row = v4_row()
        row["terms"][0]["role"] = ""
        with self.assertRaises(PipelineError):
            self.selected(row)

    def test_new_generation_cannot_silently_accept_a_legacy_model_response(self):
        from test_pipeline import summary_row
        with self.assertRaisesRegex(PipelineError, "content_version"):
            final_papers({"papers": [summary_row()]}, [self.source()], CONFIG, require_v4=True)

    def test_malformed_grade_or_limitation_kind_is_a_retryable_validation_error(self):
        for value in ([], {}):
            row = v4_row()
            row["reading"]["level"] = value
            with self.subTest(field="level", value=value), self.assertRaises(PipelineError):
                self.selected(row)
            row = v4_row()
            row["experiments"]["limitations"][0]["kind"] = value
            with self.subTest(field="kind", value=value), self.assertRaises(PipelineError):
                self.selected(row)

    def test_no_result_data_is_honestly_explained_instead_of_fabricated(self):
        row = v4_row()
        row["experiments"]["results"] = []
        selected = final_papers({"papers": [row]}, [self.source(False)], CONFIG)[0]
        archive = report({"date": "2026-10-16", "papers": [selected]})
        self.assertIn("未获取全文", archive)
        self.assertIn("未提供可核对的定量结果", archive)

    def test_large_v4_digest_splits_at_paper_boundaries_without_losing_sections(self):
        selected = self.selected()
        selected["research"]["problem"] = "机构设计需要考虑控制映射。" * 35
        papers = [dict(copy.deepcopy(selected), title_zh=f"论文{number}") for number in range(5)]
        cards = build_cards({"date": "2026-10-16", "papers": papers})
        self.assertGreater(len(cards), 1)
        for card in cards:
            self.assertLessEqual(len(json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode()), MAX_CARD_BYTES)
        rendered = json.dumps(cards, ensure_ascii=False)
        self.assertEqual(rendered.count("研究问题与核心方法"), 5)
        self.assertEqual(rendered.count("科研启发与阅读建议"), 5)

    def test_model_markdown_and_mentions_remain_inert_in_new_card_sections(self):
        selected = self.selected()
        selected["research"]["problem"] = "[fake](https://evil.example) <at id=all></at> **injected**"
        card = json.dumps(build_cards({"papers": [selected]}), ensure_ascii=False)
        self.assertNotIn("<at id=all>", card)
        self.assertNotIn("[fake](https://evil.example)", card)

    def test_model_title_cannot_embed_active_images_in_archive(self):
        row = v4_row()
        row["title_zh"] = "![image](https://evil.example/track)"
        rendered = report({"date": "2026-10-16", "papers": [self.selected(row)]})
        self.assertNotIn(row["title_zh"], rendered)
        self.assertIn("image", rendered)


if __name__ == "__main__":
    unittest.main()
