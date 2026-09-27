import base64
import hashlib
import hmac
import json
import traceback
import unittest
from unittest.mock import Mock, patch

import requests

from daily_digest.delivery import (
    DeliveryError,
    DeliveryUncertainError,
    MAX_CARD_BYTES,
    MAX_MESSAGE_BYTES,
    build_cards,
    send_card,
)


WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/test-token-not-real"
SECRET = "test-sign-secret-not-real"


def sample_paper(number=1):
    return {
        "source": "arxiv",
        "title": f"Dexterous manipulation {number}",
        "title_zh": f"灵巧手触觉控制方法 {number}",
        "summary": "以触觉估计物体接触状态，并闭环调节抓取力。",
        "why_for_you": "有助于理解末端感知与控制的联系。",
        "learning_action": "先复现实验中的接触状态估计，再比较控制效果。",
        "evidence": ["摘要报告真实机器人实验。"],
        "reading_depth": "摘要初筛",
        "arxiv_url": f"https://arxiv.org/abs/2609.0000{number}",
        "pdf_url": f"https://arxiv.org/pdf/2609.0000{number}",
        "code_url": "https://github.com/example/research",
    }


def sample_card():
    return build_cards({"date": "2026-09-27", "papers": [sample_paper()]})[0]


def byte_count(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class BuildCardsTests(unittest.TestCase):
    def test_card_uses_concise_note_and_checked_archive_link(self):
        archive = "https://github.com/example/dexterous-paper-digest/blob/main/docs/digests/2026-09-27.md"
        card = build_cards({"papers": [sample_paper()], "note": "Detailed source diagnostics",
            "card_note": "标题摘要筛选，其他期刊见归档。", "archive_url": archive})[0]
        rendered = json.dumps(card, ensure_ascii=False)
        self.assertIn("标题摘要筛选", rendered)
        self.assertNotIn("Detailed source diagnostics", rendered)
        self.assertIn(archive, rendered)
        elements = card["card"]["elements"]
        self.assertEqual(elements[0]["text"]["content"], "今日精选 1 篇。")
        self.assertEqual(elements[-1]["tag"], "note")
        self.assertIn("标题摘要筛选", elements[-1]["elements"][0]["content"])
        unsafe = build_cards({"papers": [], "archive_url": "http://127.0.0.1/private"})[0]
        self.assertNotIn("127.0.0.1", json.dumps(unsafe))

    def test_every_paper_has_its_recorded_arxiv_or_full_journal_source(self):
        journals = (
            ("IEEE Transactions on Robotics", "T-RO"),
            ("IEEE Robotics and Automation Letters", "RA-L"),
            ("The International Journal of Robotics Research", "IJRR"),
            ("Science Robotics", "Sci. Robot."),
            ("Soft Robotics", "Soft Robot."),
        )
        arxiv_paper = sample_paper()
        # A self-reported journal reference does not turn an arXiv record into
        # an independently retrieved journal record.
        arxiv_paper["journal_ref"] = "Science Robotics 2026"
        papers = [arxiv_paper]
        for index, (journal, _abbreviation) in enumerate(journals, start=2):
            paper = sample_paper(index)
            paper.update(source="crossref", journal_name=journal, journal_ref=journal)
            papers.append(paper)
        cards = build_cards({"papers": papers})
        source_elements = [element for card in cards for element in card["card"]["elements"]
                           if element.get("text", {}).get("content", "").startswith("来源：")]
        self.assertEqual([element["text"]["content"] for element in source_elements],
                         ["来源：arXiv"] + [f"来源：{name}（{abbr}）" for name, abbr in journals])
        self.assertTrue(all(element["text"]["tag"] == "plain_text" for element in source_elements))

    def test_unknown_journal_is_not_mislabelled_as_arxiv_or_given_an_invented_abbreviation(self):
        for journal, expected in (
            ("Example Research Journal", "来源：Example Research Journal"),
            ("", "来源：期刊（Crossref 登记，未提供刊名）"),
        ):
            with self.subTest(journal=journal):
                paper = sample_paper()
                paper.update(source="crossref", journal_name=journal)
                card = build_cards({"papers": [paper]})[0]
                source = next(element for element in card["card"]["elements"]
                              if element.get("text", {}).get("content", "").startswith("来源："))
                self.assertEqual(source["text"]["content"], expected)

    def test_five_normal_papers_fit_one_card_with_expected_links_and_details(self):
        papers = [sample_paper(number) for number in range(1, 6)]
        cards = build_cards({"date": "2026-09-27", "papers": papers, "model": "deepseek-flash"})
        self.assertEqual(len(cards), 1)
        self.assertLessEqual(byte_count(cards[0]), MAX_CARD_BYTES)
        rendered = json.dumps(cards[0], ensure_ascii=False)
        for paper in papers:
            for field in ("title", "title_zh", "summary", "why_for_you", "learning_action", "reading_depth", "arxiv_url", "pdf_url", "code_url"):
                self.assertIn(paper[field], rendered)
        self.assertIn("2026-09-27", rendered)
        self.assertIn("deepseek-flash", rendered)

    def test_utf8_size_splits_at_paper_boundaries_and_preserves_numbering(self):
        papers = []
        for number in range(1, 6):
            paper = sample_paper(number)
            for field in ("summary", "why_for_you", "learning_action", "evidence", "reading_depth"):
                paper[field] = "灵" * 1_500
            papers.append(paper)
        cards = build_cards({"date": "2026-09-27", "papers": papers})
        self.assertGreater(len(cards), 1)
        all_titles = []
        for index, card in enumerate(cards, start=1):
            self.assertLessEqual(byte_count(card), MAX_CARD_BYTES)
            self.assertIn(f"第 {index}/{len(cards)} 条", card["card"]["header"]["title"]["content"])
            for element in card["card"]["elements"]:
                if element.get("text", {}).get("content", "").split("\n")[0].endswith(tuple(str(number) for number in range(1, 6))):
                    all_titles.append(element["text"]["content"].split("\n")[0])
        self.assertEqual(len(all_titles), 5)
        self.assertEqual([title.split(".")[0] for title in all_titles], ["1", "2", "3", "4", "5"])

    def test_split_digest_has_source_notes_and_archive_only_at_the_very_end(self):
        papers = []
        for number in range(1, 6):
            paper = sample_paper(number)
            for field in ("summary", "why_for_you", "learning_action", "evidence", "reading_depth"):
                paper[field] = "灵" * 1_500
            papers.append(paper)
        note = "来源：arXiv 与五本期刊。未读取正文。" + "来源说明" * 130
        archive = "https://github.com/example/dexterous-paper-digest/blob/main/docs/digests/2026-09-27.md"
        cards = build_cards({"papers": papers, "card_note": note, "archive_url": archive})
        self.assertGreater(len(cards), 1)
        for card in cards:
            self.assertLessEqual(byte_count(card), MAX_CARD_BYTES)
        for card in cards[:-1]:
            rendered = json.dumps(card, ensure_ascii=False)
            self.assertNotIn("未读取正文", rendered)
            self.assertNotIn("按课题关联", rendered)
            self.assertNotIn(archive, rendered)
        final_elements = cards[-1]["card"]["elements"]
        self.assertEqual(final_elements[-1]["tag"], "note")
        self.assertEqual(final_elements[-1]["elements"][0]["tag"], "plain_text")
        self.assertIn("未读取正文", final_elements[-1]["elements"][0]["content"])
        self.assertIn(archive, json.dumps(final_elements[-2]))

    def test_unsafe_links_are_not_buttons_and_text_cannot_generate_links_or_mentions(self):
        paper = sample_paper()
        paper.update({
            "summary": "[invented](https://evil.example/) <at id=all></at>",
            "arxiv_url": "javascript:alert(1)",
            "pdf_url": "https://user:secret@arxiv.org/pdf/1234",
            "code_url": "http://127.0.0.1/secrets",
        })
        card = build_cards({"papers": [paper]})[0]
        self.assertFalse(any(element.get("tag") == "action" for element in card["card"]["elements"]))
        summary = next(element for element in card["card"]["elements"] if element.get("text", {}).get("content", "").startswith("核心方法："))
        self.assertEqual(summary["text"]["tag"], "plain_text")

    def test_missing_links_are_not_inferred_from_an_arxiv_id(self):
        paper = {"title": "Paper", "arxiv_id": "2609.12345"}
        card = build_cards({"papers": [paper]})[0]
        self.assertFalse(any(element.get("tag") == "action" for element in card["card"]["elements"]))

    def test_empty_day_is_a_readable_single_card(self):
        cards = build_cards({"date": "2026-09-27", "papers": [], "note": "本次没有符合筛选条件的论文。"})
        self.assertEqual(len(cards), 1)
        self.assertIn("没有适合的新论文", json.dumps(cards[0], ensure_ascii=False))


class SendCardTests(unittest.TestCase):
    @patch("daily_digest.delivery.time.time", return_value=1599360473)
    @patch("daily_digest.delivery.requests.post")
    def test_official_signature_empty_message_and_utf8_transport(self, post, _time):
        post.return_value = Mock(status_code=200)
        post.return_value.json.return_value = {"code": 0, "msg": "success"}
        card = sample_card()
        original = json.dumps(card, ensure_ascii=False)
        send_card(WEBHOOK, SECRET, card)
        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(args, (WEBHOOK,))
        payload = json.loads(kwargs["data"].decode("utf-8"))
        expected = base64.b64encode(hmac.new(
            f"1599360473\n{SECRET}".encode(), b"", hashlib.sha256
        ).digest()).decode()
        self.assertEqual(payload["timestamp"], "1599360473")
        self.assertEqual(payload["sign"], expected)
        self.assertNotIn(SECRET, kwargs["data"].decode("utf-8"))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertLessEqual(len(kwargs["data"]), MAX_MESSAGE_BYTES)
        self.assertEqual(json.dumps(card, ensure_ascii=False), original)

    @patch("daily_digest.delivery.requests.post")
    def test_http_200_nonzero_code_is_failure_without_exposing_response(self, post):
        post.return_value = Mock(status_code=200)
        post.return_value.json.return_value = {"code": 19021, "msg": f"{WEBHOOK} {SECRET}"}
        with self.assertRaises(DeliveryError) as raised:
            send_card(WEBHOOK, SECRET, sample_card())
        self.assertNotIsInstance(raised.exception, DeliveryUncertainError)
        self.assertIn("19021", str(raised.exception))
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertNotIn(WEBHOOK, str(raised.exception))

    @patch("daily_digest.delivery.requests.post")
    def test_timeout_is_uncertain_not_retried_and_traceback_is_sanitized(self, post):
        post.side_effect = requests.ReadTimeout(f"request {WEBHOOK}; signature secret={SECRET}")
        try:
            send_card(WEBHOOK, SECRET, sample_card())
        except DeliveryUncertainError as error:
            rendered = "".join(traceback.format_exception(error))
            self.assertNotIn(WEBHOOK, rendered)
            self.assertNotIn(SECRET, rendered)
            self.assertIn("do not automatically retry", rendered)
        else:
            self.fail("An ambiguous timeout must not be treated as success.")
        post.assert_called_once()

    @patch("daily_digest.delivery.requests.post")
    def test_transport_exception_is_also_uncertain_and_sanitized(self, post):
        post.side_effect = requests.ConnectionError(f"proxy failure: {WEBHOOK} {SECRET}")
        with self.assertRaises(DeliveryUncertainError) as raised:
            send_card(WEBHOOK, SECRET, sample_card())
        self.assertNotIn(WEBHOOK, str(raised.exception))
        self.assertNotIn(SECRET, str(raised.exception))
        post.assert_called_once()

    @patch("daily_digest.delivery.requests.post")
    def test_redirect_is_rejected_without_forwarding_signature(self, post):
        post.return_value = Mock(status_code=302)
        with self.assertRaises(DeliveryError) as raised:
            send_card(WEBHOOK, SECRET, sample_card())
        self.assertNotIsInstance(raised.exception, DeliveryUncertainError)
        post.assert_called_once()
        self.assertFalse(post.call_args.kwargs["allow_redirects"])

    @patch("daily_digest.delivery.requests.post")
    def test_server_failure_invalid_json_and_missing_code_are_uncertain(self, post):
        for status, result in ((503, None), (200, {"msg": "ok"}), (200, {"code": False}), (200, {"code": "0"})):
            with self.subTest(status=status, result=result):
                post.reset_mock()
                post.return_value = Mock(status_code=status)
                post.return_value.json.return_value = result
                with self.assertRaises(DeliveryUncertainError):
                    send_card(WEBHOOK, SECRET, sample_card())
                post.assert_called_once()
        post.return_value = Mock(status_code=200)
        post.return_value.json.side_effect = ValueError(f"broken {SECRET}")
        with self.assertRaises(DeliveryUncertainError) as raised:
            send_card(WEBHOOK, SECRET, sample_card())
        self.assertNotIn(SECRET, str(raised.exception))

    @patch("daily_digest.delivery.requests.post")
    def test_untrusted_webhook_or_missing_secret_prevents_any_network_call(self, post):
        for webhook in (
            "http://open.feishu.cn/open-apis/bot/v2/hook/token",
            "https://open.feishu.cn.evil.example/open-apis/bot/v2/hook/token",
            "https://user:secret@open.feishu.cn/open-apis/bot/v2/hook/token",
            WEBHOOK + "?send=elsewhere",
            "https://open.feishu.cn/open-apis/bot/v2/hook/token/extra",
            "https://evil.example/open-apis/bot/v2/hook/token",
        ):
            with self.subTest(webhook=webhook), self.assertRaises(DeliveryError):
                send_card(webhook, SECRET, sample_card())
        with self.assertRaises(DeliveryError):
            send_card(WEBHOOK, "", sample_card())
        post.assert_not_called()

    @patch("daily_digest.delivery.requests.post")
    def test_oversized_signed_utf8_payload_prevents_any_network_call(self, post):
        card = {"msg_type": "interactive", "card": {"elements": [{"text": "灵" * 10_000}]}}
        with self.assertRaises(DeliveryError):
            send_card(WEBHOOK, SECRET, card)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
