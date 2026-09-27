"""Journal metadata fixtures: no paid API, publisher login, or PDF requests."""

import copy
from datetime import date, timedelta
import unittest
from unittest.mock import Mock, patch

import requests

from daily_digest.journals import collect_journals, normalize_doi


TODAY = date(2026, 9, 27)
ISSN = "2377-3766"
DOI = "10.1109/LRA.2026.1234567"
NAME = "IEEE Robotics and Automation Letters"


def configuration(**settings):
    return {"journals": dict(enabled=True, watchlist=[{"name": NAME, "issns": [ISSN]}],
                             abstract_enrichment_limit=0, **settings)}


def article(doi=DOI, **changes):
    result = {
        "DOI": doi, "type": "journal-article", "ISSN": [ISSN],
        "title": ["A tendon-driven dexterous hand"],
        "abstract": "<jats:p>We calibrate <jats:italic>tendon</jats:italic> displacement.</jats:p>",
        "author": [{"given": "Example", "family": "Author", "affiliation": [{"name": "Example Robotics Lab"}]}],
        "published-online": {"date-parts": [[2026, 9, 23]]},
        "published-print": {"date-parts": [[2027, 1]]},
        "indexed": {"date-time": "2026-09-27T07:00:00Z"},
        "URL": "https://untrusted.example/article.pdf",
    }
    result.update(changes)
    return result


def response(items=None, *, status=200, cursor="next", data=None, headers=None):
    if data is None:
        data = {"status": "ok", "message": {"items": items or [], "next-cursor": cursor}}
    return Mock(status_code=status, content=b"{}", headers=headers or {}, json=Mock(return_value=data))


class DOISafetyTests(unittest.TestCase):
    def test_identifiers_and_canonical_urls_normalize_to_lowercase(self):
        for value in (DOI, " https://doi.org/" + DOI + " ", "HTTP://DX.DOI.ORG/" + DOI):
            self.assertEqual(normalize_doi(value), DOI.lower())

    def test_malformed_dois_and_uri_credentials_or_query_are_rejected(self):
        for value in (None, 123, "arxiv:2609.12345", "10.12/paper", "10.１２３４/paper",
                      "https://evil.example/10.1109/paper", "https://user:pass@doi.org/10.1109/paper",
                      "10.1109/paper?key=secret", "10.1109/paper#fragment", "10.1109/user@host",
                      "10.1109/line\nbreak", "10.1109/trailing\n", "10.1109/<script>"):
            with self.subTest(value=value):
                self.assertEqual(normalize_doi(value), "")


@patch("daily_digest.journals.time.sleep")
@patch("daily_digest.journals.requests.get")
class JournalMetadataTests(unittest.TestCase):
    def test_disabled_source_does_not_make_requests_or_modify_cache(self, get, sleep):
        cache = {"arxiv:2609.12345": {"title": "A saved hand paper"}}
        result, count, note = collect_journals(cache, TODAY, {})
        self.assertEqual(result, cache)
        self.assertEqual(count, 0)
        get.assert_not_called()
        self.assertIn("未启用", note)

    def test_online_date_exact_allowlist_and_canonical_doi_link(self, get, sleep):
        get.return_value = response([article()])
        result, count, note = collect_journals({}, TODAY, configuration())
        self.assertEqual(count, 1)
        paper = result["doi:" + DOI.lower()]
        self.assertEqual(paper["paper_url"], "https://doi.org/" + DOI.lower())
        self.assertEqual(paper["publish_date"], "2026-09-23")
        self.assertEqual(paper["date_precision"], "day")
        self.assertEqual(paper["date_basis"], "online_publication")
        self.assertEqual(paper["print_publication_date"], "2027-01")
        self.assertEqual(paper["metadata_indexed_at"], "2026-09-27T07:00:00Z")
        self.assertEqual(paper["abstract"], "We calibrate tendon displacement.")
        self.assertEqual(paper["authors"], "Example Author")
        self.assertEqual(paper["affiliations"], ["Example Robotics Lab"])
        self.assertEqual(paper["source"], "crossref")
        self.assertEqual(paper["publication_status"], "journal_article")
        self.assertEqual(paper["first_seen_date"], TODAY.isoformat())
        self.assertEqual(get.call_args.args[0], f"https://api.crossref.org/journals/{ISSN}/works")
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertIn("from-index-date:", get.call_args.kwargs["params"]["filter"])
        self.assertNotIn("from-pub-date", get.call_args.kwargs["params"]["filter"])

    def test_wrong_journal_or_non_article_or_unsafe_doi_is_not_accepted(self, get, sleep):
        get.return_value = response([
            article(ISSN=["2470-9476"]), article(type="proceedings-article"),
            article(ISSN=[]), article(doi="10.1109/paper?key=secret"), article(title=[]),
        ])
        result, count, _ = collect_journals({}, TODAY, configuration())
        self.assertEqual(result, {})
        self.assertEqual(count, 0)

    def test_secondary_allowed_issn_matches_but_only_primary_is_queried(self, get, sleep):
        config = configuration()
        config["journals"]["watchlist"] = [{"name": "IJRR", "issns": ["0278-3649", "1741-3176"]}]
        get.return_value = response([article(ISSN=["1741-3176"])])
        _, count, _ = collect_journals({}, TODAY, config)
        self.assertEqual(count, 1)
        self.assertIn("/journals/0278-3649/works", get.call_args.args[0])
        self.assertEqual(get.call_count, 1)

    def test_partial_publication_dates_are_not_fabricated(self, get, sleep):
        for parts, expected, precision in (([2026, 9], "2026-09", "month"), ([2026], "2026", "year")):
            with self.subTest(parts=parts):
                get.return_value = response([article(**{"published-online": {"date-parts": [parts]}})])
                result, _, _ = collect_journals({}, TODAY, configuration())
                self.assertEqual(result["doi:" + DOI.lower()]["publish_date"], expected)
                self.assertEqual(result["doi:" + DOI.lower()]["date_precision"], precision)

    def test_index_date_never_becomes_unknown_publication_date(self, get, sleep):
        get.return_value = response([article(**{"published-online": {}, "published-print": {}, "abstract": ""})])
        result, _, _ = collect_journals({}, TODAY, configuration())
        paper = result["doi:" + DOI.lower()]
        self.assertEqual(paper["publish_date"], "")
        self.assertEqual(paper["date_precision"], "unknown")
        self.assertEqual(paper["abstract"], "")

    def test_invalid_online_day_uses_actual_print_date_instead(self, get, sleep):
        get.return_value = response([article(**{"published-online": {"date-parts": [[2026, 2, 30]]}})])
        result, _, _ = collect_journals({}, TODAY, configuration())
        self.assertEqual(result["doi:" + DOI.lower()]["publish_date"], "2027-01")
        self.assertEqual(result["doi:" + DOI.lower()]["date_basis"], "print_publication")

    def test_cached_summary_and_original_date_survive_missing_deposit_fields(self, get, sleep):
        key = "doi:" + DOI.lower()
        cache = {key: {"source": "crossref", "abstract": "Previously available abstract.",
                       "publish_date": "2026-08-01", "date_basis": "online_publication", "date_precision": "day",
                       "first_seen_date": "2026-08-02", "code_url": "https://github.com/example/hand"}}
        before = copy.deepcopy(cache)
        get.return_value = response([article(**{"published-online": {}, "published-print": {}, "abstract": ""})])
        result, _, _ = collect_journals(cache, TODAY, configuration())
        self.assertEqual(cache, before)
        for field in ("abstract", "publish_date", "date_precision", "date_basis", "first_seen_date", "code_url"):
            self.assertEqual(result[key][field], before[key][field])
        self.assertEqual(result[key]["last_seen_date"], TODAY.isoformat())

    def test_old_article_indexed_today_keeps_old_publication_date(self, get, sleep):
        get.return_value = response([article(**{"published-online": {"date-parts": [[2021, 1, 3]]}})])
        result, _, _ = collect_journals({}, TODAY, configuration())
        self.assertEqual(result["doi:" + DOI.lower()]["publish_date"], "2021-01-03")

    def test_daily_overlap_and_weekly_backfill_query_metadata_dates(self, get, sleep):
        cache = {"doi:10.1109/old": {"source": "crossref", "publish_date": "2020-01-01"}}
        get.return_value = response([])
        for day, days in ((date(2026, 9, 28), 7), (TODAY, 90)):
            with self.subTest(day=day):
                collect_journals(cache, day, configuration())
                self.assertIn("from-index-date:" + (day - timedelta(days=days)).isoformat(),
                              get.call_args.kwargs["params"]["filter"])

    def test_pagination_keeps_all_pages_and_deduplicates_doi(self, get, sleep):
        get.side_effect = [response([article()], cursor="second"),
                           response([article(title=["Updated tendon hand title"])], cursor="third"), response([])]
        result, count, _ = collect_journals({}, TODAY, configuration(page_size=1))
        self.assertEqual(count, 1)
        self.assertEqual(result["doi:" + DOI.lower()]["title"], "Updated tendon hand title")
        self.assertEqual([call.kwargs["params"]["cursor"] for call in get.call_args_list], ["*", "second", "third"])

    def test_pagination_cap_and_repeated_cursor_report_incomplete_coverage(self, get, sleep):
        for max_pages, cursor in ((1, "second"), (10, "*")):
            with self.subTest(max_pages=max_pages, cursor=cursor):
                get.reset_mock()
                get.return_value = response([article()], cursor=cursor)
                _, count, note = collect_journals({}, TODAY, configuration(page_size=1, max_pages=max_pages))
                self.assertEqual(count, 1)
                self.assertEqual(get.call_count, 1)
                self.assertIn("可能不完整", note)

    def test_one_journal_failure_does_not_drop_other_journal_data(self, get, sleep):
        config = configuration()
        config["journals"]["watchlist"].append({"name": "Science Robotics", "issns": ["2470-9476"]})
        get.side_effect = [response([article()]), response(status=403)]
        result, count, note = collect_journals({}, TODAY, config)
        self.assertEqual(count, 1)
        self.assertIn("doi:" + DOI.lower(), result)
        self.assertIn("1/2", note)
        self.assertIn("HTTP_403", note)
        self.assertEqual(get.call_count, 2)

    def test_bounded_retries_respect_retry_after_without_unbounded_sleep(self, get, sleep):
        get.side_effect = [response(status=429, headers={"Retry-After": "600"}),
                           response(status=503), response([article()])]
        _, count, _ = collect_journals({}, TODAY, configuration())
        self.assertEqual(count, 1)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [30, 2])

    def test_network_failure_and_bad_schema_preserve_cache_and_report_failure(self, get, sleep):
        cache = {"arxiv:2609.1": {"title": "Saved paper"}}
        for failure in (requests.ConnectionError("DO NOT EXPOSE URL OR TOKEN"), response(data={"message": []})):
            with self.subTest(failure=failure):
                get.reset_mock()
                get.side_effect = failure if isinstance(failure, Exception) else None
                get.return_value = failure
                result, count, note = collect_journals(cache, TODAY, configuration())
                self.assertEqual(result, cache)
                self.assertEqual(count, 0)
                self.assertIn("来源失败", note)
                self.assertNotIn("TOKEN", note)
                self.assertLessEqual(get.call_count, 3)

    def test_invalid_issn_is_not_queried(self, get, sleep):
        config = configuration()
        config["journals"]["watchlist"][0]["issns"] = ["2377-3760"]
        _, count, note = collect_journals({}, TODAY, config)
        self.assertEqual(count, 0)
        self.assertIn("watchlist_invalid", note)
        get.assert_not_called()

    def test_publication_supplement_finds_fresh_paper_beyond_old_index_updates(self, get, sleep):
        config = configuration(fresh_publication_supplement=True)
        config["fallback_days"] = 30
        get.side_effect = [
            response([article("10.1109/old", **{"published-online": {"date-parts": [[2021, 1, 3]]}})]),
            response([article("10.1109/new", **{"published-online": {"date-parts": [[2026, 9, 25]]}, "indexed": {}})]),
        ]
        result, count, note = collect_journals({}, TODAY, config)
        self.assertEqual(count, 2)
        self.assertEqual(result["doi:10.1109/old"]["publish_date"], "2021-01-03")
        self.assertEqual(result["doi:10.1109/new"]["publish_date"], "2026-09-25")
        self.assertEqual(result["doi:10.1109/new"]["date_basis"], "online_publication")
        self.assertNotIn("metadata_indexed_at", result["doi:10.1109/new"])
        indexed, published = [call.kwargs["params"] for call in get.call_args_list]
        self.assertIn("from-index-date:", indexed["filter"])
        self.assertEqual(indexed["sort"], "indexed")
        self.assertEqual(indexed["order"], "desc")
        self.assertEqual(published["filter"], "type:journal-article,from-pub-date:2026-08-28,until-pub-date:2026-09-27")
        self.assertNotIn("sort", published)
        self.assertNotIn("order", published)
        self.assertEqual(published["cursor"], "*")
        self.assertIn("发表日期补查 1/1", note)
        self.assertIn("取得 1 条记录", note)

    def test_supplement_deduplicates_doi_and_preserves_available_abstract(self, get, sleep):
        get.side_effect = [response([article()]), response([article(doi=DOI.lower(), abstract="")])]
        result, count, note = collect_journals({}, TODAY, configuration(fresh_publication_supplement=True))
        self.assertEqual(count, 1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result["doi:" + DOI.lower()]["abstract"], "We calibrate tendon displacement.")
        self.assertIn("按 DOI 去重", note)

    def test_primary_page_cap_does_not_prevent_publication_supplement(self, get, sleep):
        get.side_effect = [
            response([article("10.1109/old", **{"published-online": {"date-parts": [[2021, 1, 3]]}})], cursor="more-old"),
            response([article("10.1109/fresh")], cursor="more-new"),
        ]
        result, count, note = collect_journals({}, TODAY, configuration(
            fresh_publication_supplement=True, page_size=1, max_pages=1))
        self.assertEqual(count, 2)
        self.assertIn("doi:10.1109/fresh", result)
        self.assertEqual(get.call_count, 2)
        self.assertIn("分页受限，可能不完整", note)
        self.assertIn("（发表日期补查）", note)

    def test_indexed_and_publication_queries_fail_independently(self, get, sleep):
        config = configuration(fresh_publication_supplement=True)
        for indexed, published, expected_main, expected_supplement in (
            (response(status=403), response([article()]), "Crossref 0/1", "发表日期补查 1/1"),
            (response([article()]), response(status=403), "Crossref 1/1", "发表日期补查 0/1"),
        ):
            with self.subTest(expected_main=expected_main):
                get.reset_mock()
                get.side_effect = [indexed, published]
                result, count, note = collect_journals({}, TODAY, config)
                self.assertEqual(count, 1)
                self.assertIn("doi:" + DOI.lower(), result)
                self.assertIn(expected_main, note)
                self.assertIn(expected_supplement, note)
                self.assertIn("HTTP_403", note)
                self.assertEqual(get.call_count, 2)

    def test_supplement_pagination_reuses_filter_and_updates_cursor(self, get, sleep):
        config = configuration(fresh_publication_supplement=True, page_size=1)
        config["fallback_days"] = 10
        get.side_effect = [response([]), response([article("10.1109/new1")], cursor="second-new"),
                           response([article("10.1109/new2")], cursor="third-new"), response([])]
        _, count, note = collect_journals({}, TODAY, config)
        self.assertEqual(count, 2)
        pages = [call.kwargs["params"] for call in get.call_args_list[1:]]
        self.assertEqual([page["cursor"] for page in pages], ["*", "second-new", "third-new"])
        self.assertTrue(all(page["filter"] == "type:journal-article,from-pub-date:2026-09-17,until-pub-date:2026-09-27" for page in pages))
        self.assertTrue(all("sort" not in page and "order" not in page and page["rows"] == 1 for page in pages))
        self.assertIn("最近 10 天发表日期补查", note)
        self.assertNotIn("可能不完整", note)

    def test_openalex_can_fill_recent_related_abstract_without_article_download(self, get, sleep):
        config = configuration()
        config["journals"]["abstract_enrichment_limit"] = 20
        get.side_effect = [response([article(abstract="")]), response(data={
            "doi": "https://doi.org/" + DOI.lower(),
            "abstract_inverted_index": {"A": [0], "dexterous": [1], "hand": [2], "uses": [3], "tendon": [4], "control.": [5]},
        })]
        result, _, note = collect_journals({}, TODAY, config)
        self.assertEqual(result["doi:" + DOI.lower()]["abstract"], "A dexterous hand uses tendon control.")
        self.assertEqual(result["doi:" + DOI.lower()]["abstract_source"], "openalex")
        self.assertIn("补齐 1", note)
        for call in get.call_args_list:
            self.assertNotIn("article.pdf", call.args[0])
            self.assertTrue(call.args[0].startswith(("https://api.crossref.org/", "https://api.openalex.org/")))

    def test_missing_or_wrong_doi_or_invalid_abstract_index_is_best_effort(self, get, sleep):
        config = configuration()
        config["journals"]["abstract_enrichment_limit"] = 1
        for data in ({"doi": "https://doi.org/10.1109/different", "abstract_inverted_index": {"Wrong": [0]}},
                     {"doi": DOI, "abstract_inverted_index": None},
                     {"doi": DOI, "abstract_inverted_index": {"Words": [0], "missing": [2]}},
                     {"doi": DOI, "abstract_inverted_index": {"Duplicate": [0], "position": [0]}}):
            with self.subTest(data=data):
                get.side_effect = [response([article(abstract="")]), response(data=data)]
                result, _, note = collect_journals({}, TODAY, config)
                self.assertEqual(result["doi:" + DOI.lower()]["abstract"], "")
                self.assertIn("1 篇未获得摘要", note)

    def test_abstract_enrichment_is_limited_to_recent_related_records(self, get, sleep):
        config = configuration()
        config["journals"]["abstract_enrichment_limit"] = 1
        get.side_effect = [response([
            article(abstract=""),
            article("10.1109/unrelated", title=["Quadruped locomotion in rough terrain"], abstract=""),
            article("10.1109/old", abstract="", **{"published-online": {"date-parts": [[2021, 1, 3]]}}),
        ]), response(status=404)]
        _, count, _ = collect_journals({}, TODAY, config)
        self.assertEqual(count, 3)
        self.assertEqual(get.call_count, 2)

    def test_repeated_openalex_network_failures_stop_enrichment_early(self, get, sleep):
        config = configuration()
        config["journals"]["abstract_enrichment_limit"] = 20
        get.side_effect = [response([article(f"10.1109/hand{index}", abstract="") for index in range(8)])] + [
            requests.Timeout("DO NOT EXPOSE") for _ in range(3)]
        result, count, note = collect_journals({}, TODAY, config)
        self.assertEqual(count, 8)
        self.assertEqual(len(result), 8)
        self.assertEqual(get.call_count, 4)
        self.assertIn("3 篇未获得摘要", note)
        self.assertNotIn("DO NOT EXPOSE", note)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
