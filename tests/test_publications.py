"""Named venues enter the real selection/rendering paths without live services."""

import copy
import importlib
import importlib.util
import json
from datetime import date
from unittest import TestCase
from unittest.mock import Mock, patch

from daily_digest.delivery import build_cards
from daily_digest.pipeline import candidates, is_journal, report, select_recommendations
from daily_digest.reading import read_paper


TODAY = date(2026, 11, 1)
CONFIG = {"fallback_days": 30, "recent_days": 7, "shortlist_limit": 30}
RSS = {"name": "Robotics: Science and Systems", "abbreviation": "RSS",
    "provider": "openalex", "source_id": "S4306420803"}
CORL = {"name": "Conference on Robot Learning", "abbreviation": "CoRL", "provider": "pmlr"}
JMLR = {"name": "Journal of Machine Learning Research", "abbreviation": "JMLR",
    "provider": "openalex", "source_id": "S118988714", "issns": ["1533-7928", "1532-4435"]}


def conference():
    return {"paper_id": "pmlr:v305/hand26", "source": "pmlr", "venue_type": "conference",
        "venue_name": CORL["name"], "conference_name": CORL["name"], "venue_abbreviation": "CoRL",
        "title": "A calibrated dexterous robotic hand", "authors": "Example Author, Second Author",
        "abstract": "Tendon-driven dexterous hand calibration and tactile grasping.",
        "publish_date": "2026-10-28", "date_basis": "proceedings_publication",
        "paper_url": "https://proceedings.mlr.press/v305/hand26.html", "ranking_score": 90}


def work(source_id="S4306420803", doi="https://doi.org/10.15607/rss.2026.test"):
    return {"id": "https://openalex.org/W12345", "title": "A calibrated dexterous robotic hand",
        "doi": doi, "type": "conference-paper", "publication_date": "2026-10-28", "publication_year": 2026,
        "authorships": [{"author": {"id": "https://openalex.org/A1", "display_name": "Example Author"},
            "institutions": [{"display_name": "Example Lab"}]}],
        "primary_location": {"source": {"id": "https://openalex.org/" + source_id, "type": "conference"},
            "landing_page_url": "https://roboticsproceedings.org/rss26/hand.html", "pdf_url": None,
            "version": "publishedVersion", "is_published": True},
        "locations": [], "abstract_inverted_index": {"Dexterous": [0], "hand": [1], "calibration.": [2]}}


class PublicationSelectionTests(TestCase):
    def test_no_doi_conference_enters_general_ranking_but_not_extra_journal_slot(self):
        p = conference()
        result = candidates({p["paper_id"]: p}, {"sent_ids": []}, TODAY, CONFIG)
        self.assertEqual([p["paper_id"] for p in result], ["pmlr:v305/hand26"])
        self.assertFalse(is_journal(result[0]))
        self.assertEqual(select_recommendations(result, 5), (result, False))

    def test_openalex_journal_without_doi_can_fill_journal_slot_and_is_deduplicated(self):
        p = dict(conference(), paper_id="openalex:W12345", source="openalex", venue_type="journal",
            journal_name=JMLR["name"], venue_name=JMLR["name"], conference_name="",
            openalex_id="W12345", openalex_source_id="S118988714")
        p["paper_url"] = "https://openalex.org/W12345"
        result = candidates({p["paper_id"]: p}, {"sent_ids": []}, TODAY, CONFIG)
        self.assertEqual(len(result), 1)
        self.assertTrue(is_journal(result[0]))
        self.assertEqual(candidates({p["paper_id"]: p}, {"sent_ids": [p["paper_id"]]}, TODAY, CONFIG), [])

    def test_conference_and_already_sent_arxiv_version_are_not_recommended_twice(self):
        p = conference()
        a = dict(p, paper_id="arxiv:2610.00001", arxiv_id="2610.00001", source="arxiv")
        db = {p["paper_id"]: p, a["paper_id"]: a}
        self.assertEqual(candidates(db, {"sent_ids": [a["paper_id"]]}, TODAY, CONFIG), [])

    def test_explicit_arxiv_relationship_deduplicates_even_if_author_spelling_differs(self):
        p = dict(conference(), arxiv_id="2610.00001", authors="A. Researcher")
        a = dict(p, paper_id="arxiv:2610.00001", source="arxiv", authors="Alice Researcher")
        db = {p["paper_id"]: p, a["paper_id"]: a}
        self.assertEqual(candidates(db, {"sent_ids": [a["paper_id"]]}, TODAY, CONFIG), [])
        self.assertEqual(len(candidates(db, {"sent_ids": []}, TODAY, CONFIG)), 1)

    def test_conference_card_displays_the_actual_venue(self):
        p = dict(conference(), title_zh="灵巧手标定", work_module=3)
        rendered = json.dumps(build_cards({"papers": [p]}), ensure_ascii=False)
        self.assertIn("来源：Conference on Robot Learning（CoRL）", rendered)
        self.assertNotIn("来源未提供", rendered)

    def test_old_conference_is_not_admitted_by_extended_journal_window(self):
        p = dict(conference(), publish_date="2026-09-01")
        self.assertEqual(candidates({p["paper_id"]: p}, {"sent_ids": []}, TODAY, CONFIG, journal_days=90), [])

    def test_removed_journal_is_not_reintroduced_from_cached_metadata(self):
        p = dict(conference(), source="crossref", paper_id="doi:10.1234/soft", doi="10.1234/soft",
            journal_name="Soft Robotics", venue_type="journal")
        config = dict(CONFIG, journals={"watchlist": [JMLR]})
        self.assertEqual(candidates({p["paper_id"]: p}, {"sent_ids": []}, TODAY, config), [])


class SourceAdapterTests(TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("daily_digest.publications"),
            "New conference and JMLR adapters must exist")
        self.module = importlib.import_module("daily_digest.publications")

    def test_openalex_checks_exact_source_and_uses_stable_identity_without_doi(self):
        with patch.object(self.module, "_get_json", return_value={"meta": {"count": 1}, "results": [work(doi=None)]}):
            db, count, note = self.module.collect_openalex({}, TODAY, RSS, {"max_pages": 1}, venue_type="conference")
        self.assertEqual(count, 1)
        self.assertIn("openalex:W12345", db)
        self.assertEqual(db["openalex:W12345"]["authors"], "Example Author")
        self.assertEqual(db["openalex:W12345"]["venue_type"], "conference")
        with patch.object(self.module, "_get_json", return_value={"meta": {"count": 1}, "results": [work("S999")]}):
            db, count, _ = self.module.collect_openalex({}, TODAY, RSS, {}, venue_type="conference")
        self.assertEqual(count, 0)

    def test_openalex_page_cap_reports_incomplete_coverage(self):
        with patch.object(self.module, "_get_json", return_value={"meta": {"count": 1000}, "results": [work()]}):
            _, _, note = self.module.collect_openalex({}, TODAY, RSS, {"max_pages": 1, "page_size": 1}, venue_type="conference")
        self.assertIn("分页受限", note)

    def test_openalex_missing_abstract_does_not_erase_previous_metadata(self):
        row = work(doi=None)
        row["abstract_inverted_index"] = None
        cache = {"openalex:W12345": {"abstract": "A previously available dexterous hand abstract."}}
        with patch.object(self.module, "_get_json", return_value={"meta": {"count": 1}, "results": [row]}):
            db, _, _ = self.module.collect_openalex(cache, TODAY, RSS, {}, venue_type="conference")
        self.assertEqual(db["openalex:W12345"]["abstract"], cache["openalex:W12345"]["abstract"])

    def test_pmlr_authors_from_volume_metadata_enable_preprint_deduplication(self):
        index = '<li><a href="v305">Volume 305</a> Proceedings of CoRL 2025</li>'
        feed = '''<rss><channel><item><title>A calibrated dexterous robotic hand</title>
            <description>Dexterous hand calibration.</description><pubDate>Wed, 28 Oct 2026 00:00:00 GMT</pubDate>
            <link>https://proceedings.mlr.press/v305/hand26.html</link></item></channel></rss>'''
        volume = '''<div class="paper"><p class="title">A calibrated dexterous robotic hand</p>
            <span class="authors">Example Author,&nbsp;Second Author</span>
            <a href="https://proceedings.mlr.press/v305/hand26.html">abs</a></div>'''
        with patch.object(self.module, "_get_text", side_effect=[index, feed, volume]):
            db, _, _ = self.module.collect_pmlr({}, TODAY, CORL, {})
        self.assertEqual(db["pmlr:v305/hand26"]["authors"], "Example Author, Second Author")

    def test_pmlr_uses_item_publication_date_not_feed_build_and_ignores_workshops(self):
        index = '<li><a href="v305">Volume 305</a> Proceedings of CoRL 2025</li>' + \
            '<li><a href="v999">Volume 999</a> CoRL Workshop 2026</li>'
        feed = '''<rss><channel><lastBuildDate>Sun, 01 Nov 2026 00:00:00 GMT</lastBuildDate>
            <item><title>A dexterous robotic hand</title><description>Tactile hand calibration.</description>
            <pubDate>Wed, 28 Oct 2026 00:00:00 GMT</pubDate><link>https://proceedings.mlr.press/v305/hand26.html</link></item>
            <item><title>Old dexterous hand</title><pubDate>2025</pubDate><link>https://proceedings.mlr.press/v305/old.html</link></item>
            </channel></rss>'''
        with patch.object(self.module, "_get_text", side_effect=[index, feed, ""]):
            db, count, note = self.module.collect_pmlr({}, TODAY, CORL, {})
        self.assertEqual(count, 1)
        self.assertEqual(db["pmlr:v305/hand26"]["publish_date"], "2026-10-28")
        self.assertEqual(db["pmlr:v305/hand26"]["conference_name"], CORL["name"])

    def test_openreview_only_accepts_exact_accepted_venue(self):
        content = {"title": {"value": "Dexterous hand calibration"}, "abstract": {"value": "Tactile hand."},
            "authors": {"value": ["Example Author"]}, "venueid": {"value": "ICLR.cc/2026/Conference"},
            "venue": {"value": "ICLR 2026 poster"}}
        good = {"id": "hand26", "pdate": 1793145600000, "odate": 1788220800000, "content": content}
        rejected = copy.deepcopy(good)
        rejected.update(id="rejected26")
        rejected["content"]["venueid"]["value"] = "ICLR.cc/2026/Conference/Rejected_Submission"
        with patch.object(self.module, "_get_json", return_value={"count": 2, "notes": [good, rejected]}):
            db, count, _ = self.module.collect_openreview({}, TODAY,
                {"name": "International Conference on Learning Representations", "abbreviation": "ICLR", "venue_prefix": "ICLR.cc"}, {})
        self.assertEqual(count, 1)
        self.assertEqual(list(db), ["openreview:hand26"])
        p = db["openreview:hand26"]
        self.assertEqual(p["date_basis"], "openreview_acceptance")
        p.update(title_zh="灵巧手标定", work_module=3, reading_depth="摘要", summary="标定方法",
            why_for_you="课题相关", learning_action="查看标定方法", evidence="公开摘要")
        self.assertIn("来源登记录用日期", json.dumps(build_cards({"papers": [p]}), ensure_ascii=False))
        self.assertIn("来源登记录用日期", report({"date": TODAY.isoformat(), "papers": [p]}))

    def test_jmlr_official_year_precision_is_preserved_instead_of_inventing_a_recent_day(self):
        collect = getattr(self.module, "collect_jmlr", None)
        self.assertTrue(callable(collect), "JMLR needs its current official feed rather than the stale OpenAlex source")
        feed = '''<rss><channel><item><title>A calibrated dexterous robotic hand</title>
            <link>http://jmlr.org/papers/v27/hand26.html</link><pubDate>2026</pubDate>
            <author>Example Author, Second Author</author><description>Dexterous hand learning.</description>
            </item></channel></rss>'''
        with patch.object(self.module, "_get_text", return_value=feed):
            db, count, _ = collect({}, TODAY, JMLR, {})
        self.assertEqual(count, 1)
        self.assertEqual(db["jmlr:v27/hand26"]["publish_date"], "2026")
        self.assertEqual(db["jmlr:v27/hand26"]["date_precision"], "year")
        self.assertTrue(is_journal(db["jmlr:v27/hand26"]))
        self.assertEqual(candidates(db, {"sent_ids": []}, TODAY, CONFIG), [])

    def test_malformed_nested_metadata_cannot_stop_later_healthy_conferences(self):
        bad, good = work(doi="10.15607/rss.bad"), work("S2", "10.1109/icra.good")
        bad["authorships"] = [{"author": "malformed"}]
        config = dict(CONFIG, conferences={"enabled": True, "watchlist": [RSS,
            dict(RSS, name="ICRA", source_id="S2")]})
        with patch.object(self.module, "_get_json", side_effect=[
            {"meta": {"count": 1}, "results": [bad]}, {"meta": {"count": 1}, "results": [good]}]):
            db, _, _ = self.module.collect_conferences({}, TODAY, config)
        self.assertIn("doi:10.1109/icra.good", db)
        with patch.object(self.module, "_get_json", return_value={"notes": [{"content": "malformed"}]}):
            db, count, _ = self.module.collect_openreview({}, TODAY, {"name": "ICLR", "venue_prefix": "ICLR.cc"}, {})
        self.assertEqual(count, 0)

    def test_one_conference_failure_preserves_cache_and_does_not_drop_other_venues(self):
        cache = {"old": {"title": "Existing paper"}}
        config = dict(CONFIG, conferences={"enabled": True, "watchlist": [RSS, CORL]})
        with patch.object(self.module, "collect_openalex", side_effect=self.module.JournalSourceError("HTTP_403")), \
                patch.object(self.module, "collect_pmlr", return_value=(dict(cache, new=conference()), 1, "PMLR refreshed")):
            db, count, note = self.module.collect_conferences(cache, TODAY, config)
        self.assertEqual(count, 1)
        self.assertIn("old", db)
        self.assertIn("new", db)
        self.assertIn("来源失败", note)


class PublicationReadingTests(TestCase):
    def test_selected_pmlr_record_follows_its_official_landing_page(self):
        article = '<article><h1>A calibrated dexterous robotic hand</h1><h2>Introduction</h2>' + \
            ('Dexterous hand calibration requires methods and models. ' * 60) + \
            '<h2>Methods</h2>Calibration.<h2>Experiments</h2>Results.</article>'
        with patch("daily_digest.reading.fetch_public", return_value=(article.encode(), conference()["paper_url"], "text/html")):
            result = read_paper(conference())
        self.assertEqual(result["reading_depth"], "full_text")
