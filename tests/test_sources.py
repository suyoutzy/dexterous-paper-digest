"""Official arXiv RSS fallback fixtures; all requests are mocked.

Schema reference: https://info.arxiv.org/help/rss_specifications.html
RSS announcement dates must not replace known original publication dates.
"""

import copy
from datetime import date
import unittest
from unittest.mock import Mock, patch
from xml.sax.saxutils import escape

import requests

from daily_digest.sources import collect_rss
from daily_digest.state import StateError


TODAY = date(2026, 9, 27)  # Sunday: an official empty feed is a normal result.
BUILT = "Sun, 27 Sep 2026 05:00:11 +0000"
ANNOUNCED = "Fri, 25 Sep 2026 00:00:00 -0400"
RSS_URL = "https://rss.arxiv.org/rss/cs.RO"


def item(link="https://arxiv.org/abs/2609.12345v1", announce="new", pub_date=ANNOUNCED,
         title="A tendon-driven dexterous hand", abstract="We calibrate tendon displacement for a robotic hand."):
    publication = "" if pub_date is None else f"<pubDate>{escape(pub_date)}</pubDate>"
    return f"""<item>
      <title>{escape(title)}</title><link>{escape(link)}</link>
      <description>{escape('arXiv:2609.12345v1 Announce Type: ' + announce + '\nAbstract: ' + abstract)}</description>
      <guid isPermaLink="false">oai:arXiv.org:2609.12345v1</guid>
      <category>cs.RO</category>{publication}
      <arxiv:announce_type>{escape(announce)}</arxiv:announce_type>
      <dc:creator>Example Author, Another Author</dc:creator>
    </item>"""


def feed(items="", built=BUILT, channel_date=ANNOUNCED):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0" xmlns:arxiv="http://arxiv.org/schemas/atom"
         xmlns:dc="http://purl.org/dc/elements/1.1/">
      <channel><title>cs.RO updates on arXiv.org</title>
        <link>https://rss.arxiv.org/rss/cs.RO</link>
        <description>Robotics announcements</description>
        <lastBuildDate>{escape(built)}</lastBuildDate>
        <pubDate>{escape(channel_date)}</pubDate>
        <skipDays><day>Saturday</day><day>Sunday</day></skipDays>
        {items}
      </channel>
    </rss>""".encode("utf-8")


def mocked_response(content, status=200):
    return Mock(status_code=status, content=content)


@patch("daily_digest.sources.requests.get")
class RSSSourceTests(unittest.TestCase):
    def test_valid_new_item_uses_canonical_id_and_marks_announcement_date(self, get):
        get.return_value = mocked_response(feed(item()))
        result, count = collect_rss({}, TODAY)
        self.assertEqual(count, 1)
        self.assertEqual(set(result), {"arxiv:2609.12345"})
        paper = result["arxiv:2609.12345"]
        self.assertEqual(paper["paper_id"], "arxiv:2609.12345")
        self.assertEqual(paper["arxiv_id"], "2609.12345")
        self.assertEqual(paper["arxiv_url"], "https://arxiv.org/abs/2609.12345")
        self.assertEqual(paper["publish_date"], "2026-09-25")
        self.assertEqual(paper["date_basis"], "rss_announcement")
        self.assertEqual(paper["abstract"], "We calibrate tendon displacement for a robotic hand.")
        self.assertEqual(paper["authors"], "Example Author, Another Author")
        get.assert_called_once()
        self.assertEqual(get.call_args.args, (RSS_URL,))
        self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_official_empty_weekend_feed_is_accepted_without_losing_cached_papers(self, get):
        original = {"arxiv:2609.12345": {"title": "Saved hand paper", "publish_date": "2026-09-22"}}
        before = copy.deepcopy(original)
        get.return_value = mocked_response(feed(built="Fri, 25 Sep 2026 05:00:11 +0000"))
        result, count = collect_rss(original, TODAY)
        self.assertEqual(count, 0)
        self.assertEqual(result, before)
        self.assertEqual(original, before)

    def test_stale_feed_is_rejected_instead_of_being_treated_as_no_new_papers(self, get):
        get.return_value = mocked_response(feed(item(), built="Thu, 17 Sep 2026 05:00:11 +0000"))
        with self.assertRaises(StateError):
            collect_rss({}, TODAY)

    def test_feed_without_valid_build_date_or_far_future_build_is_rejected(self, get):
        for built in ("", "not a date", "Wed, 30 Sep 2026 05:00:11 +0000"):
            with self.subTest(built=built):
                get.return_value = mocked_response(feed(item(), built=built))
                with self.assertRaises(StateError):
                    collect_rss({}, TODAY)

    def test_new_unknown_replacement_and_cross_list_announcements_are_ignored(self, get):
        for announce in ("replace", "cross", "replace-cross", ""):
            with self.subTest(announce=announce):
                get.return_value = mocked_response(feed(item(announce=announce)))
                result, count = collect_rss({}, TODAY)
                self.assertEqual(count, 0)
                self.assertEqual(result, {})

    def test_existing_placeholder_without_valid_original_date_does_not_make_replacement_recent(self, get):
        for original in (
            {"title": "An old paper with unknown date"},
            {"title": "An old paper", "publish_date": ""},
            {"title": "An old paper", "publish_date": "unknown"},
        ):
            with self.subTest(original=original):
                cache = {"arxiv:2609.12345": original}
                before = copy.deepcopy(cache)
                get.return_value = mocked_response(feed(item(announce="replace-cross")))
                result, count = collect_rss(cache, TODAY)
                self.assertEqual(count, 0)
                self.assertEqual(result, before)
                self.assertEqual(cache, before)

    def test_old_paper_refresh_preserves_initial_date_basis_and_other_cached_metadata(self, get):
        original = {
            "title": "Earlier paper title", "publish_date": "2026-06-12",
            "date_basis": "initial_publication", "code_url": "https://github.com/example/hand",
            "matched_categories": ["Dexterous"], "matched_keywords": ["tendon"],
        }
        cache = {"arxiv:2609.12345": original}
        before = copy.deepcopy(cache)
        get.return_value = mocked_response(feed(item(announce="replace-cross")))
        result, count = collect_rss(cache, TODAY)
        self.assertEqual(count, 1)
        updated = result["arxiv:2609.12345"]
        self.assertEqual(updated["publish_date"], "2026-06-12")
        self.assertEqual(updated["date_basis"], "initial_publication")
        self.assertEqual(updated["title"], "A tendon-driven dexterous hand")
        for field in ("code_url", "matched_categories", "matched_keywords"):
            self.assertEqual(updated[field], original[field])
        self.assertEqual(cache, before)

    def test_known_original_date_without_old_basis_is_labeled_initial_publication(self, get):
        cache = {"arxiv:2609.12345": {"title": "Earlier paper", "publish_date": "2026-06-12"}}
        get.return_value = mocked_response(feed(item(announce="replace")))
        result, count = collect_rss(cache, TODAY)
        self.assertEqual(count, 1)
        self.assertEqual(result["arxiv:2609.12345"]["publish_date"], "2026-06-12")
        self.assertEqual(result["arxiv:2609.12345"]["date_basis"], "initial_publication")

    def test_existing_announcement_date_basis_is_preserved_on_refresh(self, get):
        cache = {"arxiv:2609.12345": {
            "title": "Earlier announcement", "publish_date": "2026-09-21", "date_basis": "rss_announcement",
        }}
        get.return_value = mocked_response(feed(item(announce="replace")))
        result, count = collect_rss(cache, TODAY)
        self.assertEqual(count, 1)
        self.assertEqual(result["arxiv:2609.12345"]["publish_date"], "2026-09-21")
        self.assertEqual(result["arxiv:2609.12345"]["date_basis"], "rss_announcement")

    def test_noncanonical_ids_and_nonofficial_article_hosts_are_ignored(self, get):
        bad_links = (
            "https://arxiv.org/abs/2609.1", "https://arxiv.org/abs/2609.123456",
            "https://arxiv.org/abs/２６０９.１２３４５", "https://arxiv.org/abs/2609.12345/v1",
            "https://arxiv.org.evil.example/abs/2609.12345", "javascript:alert(1)",
            "https://evil.example/abs/2609.12345", "https://user:pass@arxiv.org/abs/2609.12345",
        )
        get.return_value = mocked_response(feed("".join(item(link=link) for link in bad_links)))
        result, count = collect_rss({}, TODAY)
        self.assertEqual(count, 0)
        self.assertEqual(result, {})

    def test_item_without_date_uses_channel_announcement_date(self, get):
        get.return_value = mocked_response(feed(item(pub_date=None)))
        result, count = collect_rss({}, TODAY)
        self.assertEqual(count, 1)
        self.assertEqual(result["arxiv:2609.12345"]["publish_date"], "2026-09-25")
        self.assertEqual(result["arxiv:2609.12345"]["date_basis"], "rss_announcement")

    def test_transport_failure_or_invalid_feed_never_becomes_an_empty_result(self, get):
        get.side_effect = requests.ConnectionError("public RSS unavailable")
        with self.assertRaises(StateError):
            collect_rss({}, TODAY)
        get.side_effect = None
        for response in (
            mocked_response(b"<html>Maintenance</html>"),
            mocked_response(b"not XML"),
            mocked_response(feed(), status=503),
        ):
            with self.subTest(response=response):
                get.return_value = response
                with self.assertRaises(StateError):
                    collect_rss({}, TODAY)


if __name__ == "__main__":
    unittest.main()
