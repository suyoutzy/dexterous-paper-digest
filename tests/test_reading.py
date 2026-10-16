"""Fulltext access must be bounded, attributable and honest about fallback."""
import json
import unittest
from unittest.mock import Mock, patch

from daily_digest import reading


def response(body, status=200, headers=None):
    raw = body.encode() if isinstance(body, str) else body
    result = Mock(status_code=status, headers=headers or {"Content-Type": "text/html"})
    result.iter_content.return_value = [raw]
    return result


def article():
    return "<html><nav>not evidence</nav><article><h1>Dexterous hand calibration</h1>" + \
        "<h2>Introduction</h2>" + ("This paper jointly designs a robotic hand and controller. " * 35) + \
        "<h2>Methods</h2>" + ("The method optimizes hand geometry. " * 35) + \
        "<h2>Experiments and results</h2>The error was 3.9 mm.<h2>Discussion</h2>Limited objects.</article></html>"


PUBLIC_DNS = [(2, 1, 6, "", ("93.184.216.34", 443))]


class ReadingTests(unittest.TestCase):
    @patch("daily_digest.reading.socket.getaddrinfo", return_value=PUBLIC_DNS)
    @patch("daily_digest.reading.requests.get")
    def test_long_preview_behind_access_notice_is_not_claimed_as_fulltext(self, get, _dns):
        preview = "<main><h1>Dexterous hand calibration</h1>" + (
            "Abstract preview: introduction calibration method experiments and results. " * 45
        ) + "Purchase this article to read the full text.</main>"
        get.side_effect = [response(preview), response(b"bad PDF")]
        result = reading.read_paper({"title": "Dexterous hand calibration", "arxiv_id": "2609.00001", "abstract": "Known abstract"})
        self.assertEqual(result["reading_depth"], "abstract_only")
        self.assertNotIn("论文全文", result["enrichment_note"])

    @patch("daily_digest.reading.socket.getaddrinfo", return_value=PUBLIC_DNS)
    @patch("daily_digest.reading.requests.get")
    def test_arxiv_full_html_is_read_without_downloading_other_candidates(self, get, _dns):
        get.return_value = response(article())
        source = {"arxiv_id": "2609.00001", "title": "Dexterous hand calibration", "abstract": "Short abstract"}
        result = reading.read_paper(source)
        self.assertEqual(result["reading_depth"], "full_text")
        self.assertIn("3.9 mm", result["reading_text"])
        self.assertNotIn("not evidence", result["reading_text"])
        self.assertEqual(result["reading_url"], "https://arxiv.org/html/2609.00001")
        self.assertEqual(get.call_count, 1)
        self.assertNotIn("reading_text", source)

    @patch("daily_digest.reading.socket.getaddrinfo", return_value=PUBLIC_DNS)
    @patch("daily_digest.reading.requests.get")
    def test_abstract_landing_page_and_failed_pdf_never_count_as_fulltext(self, get, _dns):
        get.side_effect = [response("<article>Abstract only</article>"), response(b"not a PDF")]
        result = reading.read_paper({"arxiv_id": "2609.00001", "title": "A hand", "abstract": "Actual abstract"})
        self.assertEqual(result["reading_depth"], "abstract_only")
        self.assertEqual(result["reading_text"], "Actual abstract")
        self.assertIn("未获取全文", result["enrichment_note"])

    @patch("daily_digest.reading.socket.getaddrinfo", return_value=PUBLIC_DNS)
    @patch("daily_digest.reading.requests.get")
    def test_long_or_wrong_paper_does_not_silently_claim_complete_reading(self, get, _dns):
        get.return_value = response(article())
        result = reading.read_paper({"arxiv_id": "2609.00001", "title": "Dexterous hand calibration"}, max_chars=1000)
        self.assertEqual(result["reading_depth"], "full_text_excerpt")
        self.assertLessEqual(len(result["reading_text"]), 1000)
        self.assertIn("节选", result["enrichment_note"])
        get.side_effect = [response(article()), response(b"bad pdf")]
        result = reading.read_paper({"arxiv_id": "2609.00002", "title": "Unrelated chemistry synthesis", "abstract": "Known abstract"})
        self.assertEqual(result["reading_depth"], "abstract_only")

    @patch("daily_digest.reading.socket.getaddrinfo", return_value=PUBLIC_DNS)
    @patch("daily_digest.reading.requests.get")
    def test_private_redirect_and_oversized_responses_are_rejected_before_parsing(self, get, _dns):
        get.return_value = response("", 302, {"Location": "http://127.0.0.1/admin"})
        with self.assertRaises(reading.ReadingUnavailable):
            reading.fetch_public("https://arxiv.org/pdf/2609.00001")
        self.assertEqual(get.call_count, 1)
        get.return_value = response("", headers={"Content-Length": "999999999"})
        with self.assertRaises(reading.ReadingUnavailable):
            reading.fetch_public("https://arxiv.org/pdf/2609.00001")

    @patch("daily_digest.reading.requests.get")
    def test_private_dns_is_rejected_without_any_http_request(self, get):
        with patch("daily_digest.reading.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.1", 443))]):
            with self.assertRaises(reading.ReadingUnavailable):
                reading.fetch_public("https://private.example/paper")
        get.assert_not_called()

    @patch("daily_digest.reading.socket.getaddrinfo", return_value=PUBLIC_DNS)
    @patch("daily_digest.reading.requests.get")
    def test_public_crossref_fulltext_link_is_followed(self, get, _dns):
        metadata = {"message": {"link": [{"URL": "https://publisher.example/article", "content-type": "text/html"}]}}
        get.side_effect = [response(json.dumps(metadata)), response(article())]
        result = reading.read_paper({"doi": "10.1234/hand", "title": "Dexterous hand calibration", "abstract": "Known abstract"})
        self.assertEqual(result["reading_depth"], "full_text")
        self.assertEqual(result["reading_url"], "https://publisher.example/article")

    def test_malformed_pdf_and_scanned_pdf_have_no_fabricated_text(self):
        with self.assertRaises(reading.ReadingUnavailable):
            reading.extract_pdf(b"%PDF-broken")

    def test_pdf_with_an_unreadable_page_is_reported_as_partial(self):
        pages = [Mock(), Mock()]
        pages[0].extract_text.return_value = "Readable methods and results. " * 20
        pages[1].extract_text.return_value = ""
        with patch("pypdf.PdfReader", return_value=Mock(is_encrypted=False, pages=pages)):
            text, complete = reading.extract_pdf(b"%PDF-fake")
        self.assertIn("methods and results", text)
        self.assertFalse(complete)

    def test_companion_preprint_abstract_is_not_claimed_as_the_journal_version(self):
        result = reading.read_paper({"abstract": "Companion abstract", "abstract_source": "arxiv_same_doi"})
        self.assertIn("预印本", result["enrichment_note"])
        self.assertIn("期刊", result["enrichment_note"])


if __name__ == "__main__":
    unittest.main()
