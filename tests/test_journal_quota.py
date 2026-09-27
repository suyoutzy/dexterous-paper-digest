"""The extra journal recommendation is distinct from the four general choices."""

import copy
from datetime import date, timedelta
import unittest

from daily_digest.pipeline import candidates, select_recommendations


TODAY = date(2026, 9, 27)
CONFIG = {
    "fallback_days": 30,
    "recent_days": 7,
    "shortlist_limit": 30,
    "journals": {"shortlist_slots": 10},
}


def arxiv_paper(number, *, days_old=2, score=90):
    identifier = f"2609.{number:05d}"
    return {
        "paper_id": f"arxiv:{identifier}",
        "arxiv_id": identifier,
        "source": "arxiv",
        "title": f"Dexterous hand tactile control and tendon calibration study {number}",
        "abstract": "A multi-finger hand uses tactile sensing for tendon force control.",
        "authors": f"Example Author {number}, Another Author",
        "publish_date": (TODAY - timedelta(days=days_old)).isoformat(),
        "ranking_score": score,
    }


def journal_paper(number, *, days_old=2, score=80):
    paper = arxiv_paper(number, days_old=days_old, score=score)
    paper.pop("arxiv_id")
    doi = f"10.1234/journal-{number}"
    paper.update(
        paper_id=f"doi:{doi}", doi=doi, source="crossref",
        journal_name="IEEE Robotics and Automation Letters",
    )
    return paper


def ids(papers):
    return [paper["paper_id"] for paper in papers]


class JournalQuotaTests(unittest.TestCase):
    def test_four_best_general_papers_then_best_remaining_journal(self):
        general = [arxiv_paper(number, score=100 - number) for number in range(1, 5)]
        other_arxiv = arxiv_paper(5, score=95)
        journal = journal_paper(6, score=90)
        lower_journal = journal_paper(7, score=85)
        ranked = general + [other_arxiv, journal, lower_journal]
        before = copy.deepcopy(ranked)
        selected, filled = select_recommendations(ranked, 5)
        self.assertTrue(filled)
        self.assertEqual(ids(selected), ids(general + [journal]))
        self.assertNotIn(other_arxiv["paper_id"], ids(selected))
        self.assertEqual(ranked, before)

    def test_a_journal_in_the_top_four_still_requires_another_journal(self):
        first_journal = journal_paper(1, score=99)
        general = [first_journal] + [arxiv_paper(number) for number in range(2, 5)]
        extra_journal = journal_paper(6, score=85)
        selected, filled = select_recommendations(general + [arxiv_paper(5), extra_journal], 5)
        self.assertTrue(filled)
        self.assertEqual(ids(selected), ids(general + [extra_journal]))
        self.assertEqual(sum(paper["source"] == "crossref" for paper in selected), 2)

    def test_all_journal_general_choices_still_need_a_distinct_fifth_journal(self):
        ranked = [journal_paper(number, score=100 - number) for number in range(1, 7)]
        selected, filled = select_recommendations(ranked, 5)
        self.assertTrue(filled)
        self.assertEqual(ids(selected), ids(ranked[:5]))
        self.assertEqual(len(set(ids(selected))), 5)

    def test_missing_extra_journal_leaves_four_instead_of_filling_with_arxiv(self):
        ranked = [arxiv_paper(number) for number in range(1, 7)]
        selected, filled = select_recommendations(ranked, 5)
        self.assertFalse(filled)
        self.assertEqual(ids(selected), ids(ranked[:4]))

    def test_arxiv_self_reported_journal_and_doi_cannot_fill_the_extra_slot(self):
        general = [arxiv_paper(number) for number in range(1, 5)]
        preprint = arxiv_paper(5)
        preprint.update(doi="10.1234/preprint", journal_ref="Science Robotics 2026",
                        journal_name="Science Robotics")
        selected, filled = select_recommendations(general + [preprint], 5)
        self.assertFalse(filled)
        self.assertEqual(ids(selected), ids(general))

    def test_extra_skips_a_journal_version_with_the_same_normalized_doi(self):
        general = [arxiv_paper(number) for number in range(1, 5)]
        general[0]["doi"] = "https://doi.org/10.1234/SHARED-WORK"
        duplicate = journal_paper(5)
        duplicate.update(paper_id="doi:10.1234/shared-work", doi="10.1234/shared-work")
        distinct = journal_paper(6)
        selected, filled = select_recommendations(general + [duplicate, distinct], 5)
        self.assertTrue(filled)
        self.assertEqual(ids(selected), ids(general + [distinct]))

    def test_extra_skips_same_title_and_first_author_even_with_a_different_doi(self):
        general = [arxiv_paper(number) for number in range(1, 5)]
        duplicate = journal_paper(5)
        duplicate.update(title=general[0]["title"].upper() + "!",
                         authors="EXAMPLE AUTHOR 1, Different Coauthor")
        distinct = journal_paper(6)
        selected, filled = select_recommendations(general + [duplicate, distinct], 5)
        self.assertTrue(filled)
        self.assertEqual(ids(selected), ids(general + [distinct]))

    def test_noncanonical_or_missing_doi_does_not_count_as_an_extra_journal(self):
        general = [arxiv_paper(number) for number in range(1, 5)]
        for changes in (
            {"paper_id": "unverified-article-id"},
            {"doi": "not-a-doi"},
            {"doi": ""},
            {"source": "arxiv"},
        ):
            with self.subTest(changes=changes):
                unverified = journal_paper(5)
                unverified.update(changes)
                selected, filled = select_recommendations(general + [unverified], 5)
                self.assertFalse(filled)
                self.assertEqual(ids(selected), ids(general))

    def test_short_and_empty_rankings_do_not_duplicate_papers(self):
        for ranked in ([], [arxiv_paper(1)], [journal_paper(1)]):
            with self.subTest(count=len(ranked)):
                selected, filled = select_recommendations(ranked, 5)
                self.assertFalse(filled)
                self.assertEqual(selected, ranked)


class ExtendedJournalCandidatesTests(unittest.TestCase):
    def make_candidates(self, papers, *, sent_ids=None, excluded=None, window=90):
        db = {paper["paper_id"]: paper for paper in papers}
        return candidates(db, {"sent_ids": sent_ids or []}, TODAY, CONFIG,
                          journal_days=window, exclude_ids=excluded)

    def test_extended_dates_apply_to_journals_while_arxiv_stays_within_thirty_days(self):
        fresh_arxiv = arxiv_paper(1, days_old=30)
        old_arxiv = arxiv_paper(2, days_old=31)
        older_journal = journal_paper(3, days_old=90)
        too_old_journal = journal_paper(4, days_old=91)
        selected = self.make_candidates([fresh_arxiv, old_arxiv, older_journal, too_old_journal])
        self.assertEqual(set(ids(selected)), {fresh_arxiv["paper_id"], older_journal["paper_id"]})
        self.assertFalse(next(p for p in selected if p["paper_id"] == older_journal["paper_id"])["is_recent"])

    def test_default_candidate_window_still_excludes_old_journals(self):
        older = journal_paper(1, days_old=31)
        db = {older["paper_id"]: older}
        self.assertEqual(candidates(db, {"sent_ids": []}, TODAY, CONFIG), [])
        self.assertEqual(ids(self.make_candidates([older])), [older["paper_id"]])

    def test_sent_and_already_evaluated_ids_are_excluded_during_extension(self):
        sent = journal_paper(1, days_old=60)
        evaluated = journal_paper(2, days_old=60)
        eligible = journal_paper(3, days_old=60)
        selected = self.make_candidates([sent, evaluated, eligible],
                                        sent_ids=[sent["paper_id"]], excluded={evaluated["paper_id"]})
        self.assertEqual(ids(selected), [eligible["paper_id"]])

    def test_previously_sent_preprint_excludes_matching_doi_journal_in_extension(self):
        sent = arxiv_paper(1, days_old=100)
        sent["doi"] = "https://doi.org/10.1234/SHARED"
        duplicate = journal_paper(2, days_old=60)
        duplicate.update(paper_id="doi:10.1234/shared", doi="10.1234/shared")
        self.assertEqual(self.make_candidates([sent, duplicate], sent_ids=[sent["paper_id"]]), [])

    def test_future_or_irrelevant_journals_are_not_admitted_by_extension(self):
        future = journal_paper(1, days_old=-1)
        irrelevant = journal_paper(2, days_old=60)
        irrelevant.update(title="General document classification benchmark",
                          abstract="A language model predicts categories in documents.")
        self.assertEqual(self.make_candidates([future, irrelevant]), [])

    def test_arxiv_journal_claim_does_not_receive_extended_journal_date_window(self):
        preprint = arxiv_paper(1, days_old=60)
        preprint.update(doi="10.1234/self-reported", journal_ref="Science Robotics 2026")
        self.assertEqual(self.make_candidates([preprint]), [])


if __name__ == "__main__":
    unittest.main()
