"""Publication metadata cannot turn re-indexed old work into new papers."""

from datetime import date
import unittest

from daily_digest.pipeline import candidates, concise_text, date_span, report


CONFIG = {"fallback_days": 30, "recent_days": 7, "shortlist_limit": 30}
TODAY = date(2026, 9, 27)


def journal(doi="10.1109/example.2026.1", published="2026-09-25"):
    return {"paper_id": "doi:" + doi, "doi": doi, "source": "crossref",
        "title": "A calibrated tendon-driven dexterous robotic hand",
        "authors": "Example Researcher, Second Researcher", "abstract": "",
        "publish_date": published, "journal_name": "IEEE Transactions on Robotics",
        "paper_url": "https://untrusted.example/wrong", "date_precision": "day"}


class JournalSelectionTests(unittest.TestCase):
    def pick(self, papers, sent=()):
        return candidates({p["paper_id"]: p for p in papers}, {"sent_ids": list(sent)}, TODAY, CONFIG)

    def test_long_summary_keeps_complete_sentences_instead_of_cutting_a_result_number(self):
        text = "提出基于电流的标定方法。" + "仿真模型用于检查控制映射。" + "作者报告达到95.0%的成功率。"
        limit = text.index("95.0") + 3
        shortened = concise_text(text, limit)
        self.assertTrue(shortened.endswith("。"))
        self.assertNotIn("达到95", shortened)
        self.assertEqual(concise_text("较长标题没有句号", 5), "较长标题…")

    def test_recent_title_only_journal_metadata_can_be_considered_with_canonical_doi_link(self):
        p = journal()
        result = self.pick([p])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["abstract"], "")
        self.assertEqual(result[0]["paper_url"], "https://doi.org/10.1109/example.2026.1")
        self.assertEqual(result[0]["pdf_url"], "")

    def test_indexing_date_never_makes_an_old_publication_recent(self):
        p = journal(published="2020-01-01")
        p["last_seen_date"] = "2026-09-27"
        p["indexed"] = "2026-09-27"
        self.assertEqual(self.pick([p]), [])

    def test_month_precision_is_preserved_without_claiming_recent_seven_days(self):
        p = journal(published="2026-09")
        result = self.pick([p])
        self.assertEqual(result[0]["publish_date"], "2026-09")
        self.assertFalse(result[0]["is_recent"])
        self.assertEqual(date_span("2026-02"), (date(2026, 2, 1), date(2026, 2, 28)))
        self.assertEqual(self.pick([journal(published="2026-10")]), [])
        self.assertEqual(self.pick([journal(published="2026")]), [])

    def test_journal_and_preprint_same_doi_prefer_journal_and_sent_preprint_excludes_both(self):
        p = journal()
        preprint = dict(p, paper_id="arxiv:2609.00001", source="arxiv", arxiv_id="2609.00001")
        self.assertEqual([x["paper_id"] for x in self.pick([preprint, p])], [p["paper_id"]])
        self.assertEqual(self.pick([preprint, p], [preprint["paper_id"]]), [])

    def test_missing_journal_abstract_can_use_explicitly_labelled_same_doi_preprint(self):
        p = journal()
        preprint = dict(p, paper_id="arxiv:2609.00001", source="arxiv", arxiv_id="2609.00001",
            abstract="We calibrate tendon-driven hand joints.")
        selected = self.pick([preprint, p])[0]
        self.assertEqual(selected["paper_id"], p["paper_id"])
        self.assertEqual(selected["abstract"], preprint["abstract"])
        self.assertEqual(selected["abstract_source"], "arxiv_same_doi")

    def test_exact_title_and_first_author_deduplicate_when_preprint_has_no_doi(self):
        p = journal()
        preprint = dict(p, paper_id="arxiv:2609.00001", doi="", source="arxiv", arxiv_id="2609.00001")
        self.assertEqual([x["paper_id"] for x in self.pick([preprint, p])], [p["paper_id"]])
        self.assertEqual(self.pick([preprint, p], [preprint["paper_id"]]), [])

    def test_unverified_or_noncanonical_journal_identity_does_not_enter_candidates(self):
        p = journal()
        p["paper_id"] = "doi:10.1109/different"
        self.assertEqual(self.pick([p]), [])
        p = journal()
        p["source"] = "unknown"
        self.assertEqual(self.pick([p]), [])

    def test_recent_preprint_volume_cannot_crowd_out_the_journal_candidate_pool(self):
        p = journal(published="2026-09-11")
        preprints = [dict(p, paper_id=f"arxiv:2609.{i:05d}", arxiv_id=f"2609.{i:05d}",
            source="arxiv", title=f"Distinct tendon-driven dexterous hand design number {i}",
            authors=f"Researcher {i}", doi="", publish_date="2026-09-25") for i in range(31)]
        selected = self.pick([*preprints, p])
        self.assertEqual(len(selected), 30)
        self.assertIn(p["paper_id"], [x["paper_id"] for x in selected])

    def test_journal_report_uses_doi_without_creating_a_pdf_url(self):
        p = self.pick([journal()])[0]
        p.update(title_zh="腱绳灵巧手", reading_depth="暂无摘要，仅依据标题", summary="暂无摘要",
            why_for_you="可了解标定", learning_action="先查看摘要", evidence="仅标题")
        rendered = report({"date": TODAY.isoformat(), "model": "deepseek-flash", "papers": [p]})
        self.assertIn("https://doi.org/10.1109/example.2026.1", rendered)
        self.assertNotIn("[PDF", rendered)
        self.assertIn("IEEE Transactions on Robotics", rendered)


if __name__ == "__main__":
    unittest.main()
