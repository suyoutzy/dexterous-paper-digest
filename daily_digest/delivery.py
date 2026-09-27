"""Build readable Feishu cards and send each card exactly once.

Feishu's signature uses ``timestamp + '\\n' + secret`` as the HMAC key and
an empty message. See https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot.
An uncertain transport result must be reconciled manually, not retried blindly.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import re
import time
from typing import Any
from urllib.parse import urlsplit

import requests

MAX_MESSAGE_BYTES = 20_000  # Conservative interpretation of the 20 KB API limit.
SIGNATURE_RESERVE_BYTES = 2_000
MAX_CARD_BYTES = MAX_MESSAGE_BYTES - SIGNATURE_RESERVE_BYTES
_WEBHOOK_HOSTS = {"open.feishu.cn", "open.larksuite.com"}
_WEBHOOK_PATH = re.compile(r"/open-apis/bot/v2/hook/[A-Za-z0-9-]{1,128}\Z")


class DeliveryError(RuntimeError):
    """The message was rejected, or local validation prevented sending."""


class DeliveryUncertainError(DeliveryError):
    """A request may have been delivered; do not automatically send it again."""


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _text(value: Any, limit: int, fallback: str = "") -> str:
    if value is None:
        rendered = fallback
    elif isinstance(value, str):
        rendered = value.strip() or fallback
    elif isinstance(value, (list, tuple)):
        rendered = "；".join(_text(item, limit) for item in value) or fallback
    elif isinstance(value, dict):
        rendered = "；".join(
            f"{_text(key, 60)}：{_text(item, limit)}"
            for key, item in value.items()
        ) or fallback
    else:
        rendered = str(value)
    # Dynamic prose is rendered as plain_text, so generated Markdown, @mentions,
    # and HTML-like syntax cannot become links or commands in the card.
    return rendered if len(rendered) <= limit else rendered[: limit - 1] + "…"


def _safe_link(value: Any) -> str | None:
    """Retain an existing public HTTP(S) URL; never infer or invent a link."""
    if not isinstance(value, str) or not value or len(value) > 2_048:
        return None
    if any(character.isspace() or ord(character) < 32 for character in value):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
        or "\\" in value
    ):
        return None
    host = host.lower().rstrip(".")
    if host == "localhost" or "." not in host or host.endswith((".local", ".internal")):
        return None
    try:
        if not ipaddress.ip_address(host).is_global:
            return None
    except ValueError:
        pass
    return value


def _plain(content: str) -> dict:
    return {"tag": "div", "text": {"tag": "plain_text", "content": content}}


def _paper_elements(paper: dict, number: int) -> list[dict]:
    title = _text(paper.get("title"), 220, "未提供标题")
    title_zh = _text(paper.get("title_zh"), 220)
    heading = f"{number}. {title_zh or title}"
    if title_zh and title_zh != title:
        heading += f"\n{title}"
    blocks = [
        _plain(heading),
        _plain(("公告日期：" if paper.get("date_basis") == "rss_announcement" else ("登记发表日期：" if paper.get("source") == "crossref" else "首次发布日期："))
            + _text(paper.get("publish_date"), 32, "来源未提供")
            + ("（仅精确到月）" if paper.get("date_precision") == "month" else "")
            + "；作者：" + _text(paper.get("authors"), 300, "来源未提供")),
        _plain("核心方法：" + _text(paper.get("summary"), 800, "未提供摘要。")),
        _plain("为什么适合你：" + _text(paper.get("why_for_you"), 550, "待核对课题关联。")),
        _plain("可以学什么：" + _text(paper.get("learning_action"), 400, "先核对方法与实验，再考虑复现。")),
        _plain("依据：" + _text(paper.get("evidence"), 360, "当前保存的摘要信息。")),
        _plain("阅读深度：" + _text(paper.get("reading_depth"), 160, "摘要初筛")),
    ]
    if paper.get("journal_ref"):
        label = "期刊登记（Crossref）：" if paper.get("source") == "crossref" else "发表登记（来源自报，未独立核实）："
        blocks.insert(2, _plain(label + _text(paper["journal_ref"], 220)))
    links = []
    seen = set()
    for label, fields in (
        ("论文", ("paper_url", "arxiv_url")),
        ("PDF", ("pdf_url",)),
        ("代码", ("code_url",)),
    ):
        url = next((url for field in fields if (url := _safe_link(paper.get(field)))), None)
        if url and url not in seen:
            seen.add(url)
            links.append({
                "tag": "button", "type": "default", "url": url,
                "text": {"tag": "plain_text", "content": label},
            })
    if links:
        blocks.append({"tag": "action", "actions": links})
    return blocks


def _card(date: str, paper_blocks: list[list[dict]], intro: str, part: str = "", model: str = "未登记", archive_url: str | None = None) -> dict:
    elements = [_plain(intro)]
    for index, blocks in enumerate(paper_blocks):
        if index:
            elements.append({"tag": "hr"})
        elements.extend(blocks)
    if archive_url:
        elements.append({"tag": "action", "actions": [{"tag": "button", "type": "default", "url": archive_url,
            "text": {"tag": "plain_text", "content": "日报归档 / 其他期刊候选"}}]})
    elements.append({
        "tag": "note", "elements": [{"tag": "plain_text", "content":
            "模型：" + model + " · 研究方向：灵巧末端设计与控制"}],
    })
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": f"灵巧手论文日报 · {date}{part}"},
            },
            "elements": elements,
        },
    }


def build_cards(digest: dict) -> list[dict]:
    """Prefer one <=18 KB card; split only between papers when necessary.

    The caller supplies validated database links. Only URLs actually present in
    the selected paper records become buttons. No model-generated URL is used.
    """
    if not isinstance(digest, dict):
        raise DeliveryError("Invalid digest: expected an object.")
    papers = digest.get("papers", [])
    if not isinstance(papers, list) or any(not isinstance(paper, dict) for paper in papers):
        raise DeliveryError("Invalid digest: expected a list of paper objects.")
    date = _text(digest.get("date"), 40, "日期未提供")
    model = _text(digest.get("model"), 80, "未登记")
    archive_url = _safe_link(digest.get("archive_url"))
    intro = f"今日精选 {len(papers)} 篇，按课题关联、摘要方法与学习价值推荐。"
    note = _text(digest.get("card_note", digest.get("note")), 600)
    if note:
        intro += "\n" + note
    if not papers:
        intro += "\n今天没有适合的新论文，后续继续检索。"
        return [_card(date, [], intro, model=model, archive_url=archive_url)]

    groups: list[list[list[dict]]] = []
    current: list[list[dict]] = []
    # Reserve room for the eventual split-card index before packing.
    placeholder = " · 第 999/999 条"
    for index, paper in enumerate(papers, start=1):
        blocks = _paper_elements(paper, index)
        candidate = _card(date, current + [blocks], intro, placeholder, model, archive_url)
        if len(_json_bytes(candidate)) > MAX_CARD_BYTES:
            if not current:
                raise DeliveryError("A paper card exceeds the configured message size limit.")
            groups.append(current)
            current = [blocks]
            if len(_json_bytes(_card(date, current, intro, placeholder, model, archive_url))) > MAX_CARD_BYTES:
                raise DeliveryError("A paper card exceeds the configured message size limit.")
        else:
            current.append(blocks)
    if current:
        groups.append(current)
    count = len(groups)
    result = [
        _card(date, blocks, intro, f" · 第 {index}/{count} 条" if count > 1 else "", model, archive_url)
        for index, blocks in enumerate(groups, start=1)
    ]
    if any(len(_json_bytes(card)) > MAX_CARD_BYTES for card in result):
        raise DeliveryError("A card exceeds the configured message size limit.")
    return result


def _validate_webhook(webhook: str) -> None:
    try:
        parsed = urlsplit(webhook)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname in _WEBHOOK_HOSTS
            and parsed.port in {None, 443}
            and parsed.username is None
            and parsed.password is None
            and not parsed.query and not parsed.fragment
            and _WEBHOOK_PATH.fullmatch(parsed.path) is not None
            and not any(character.isspace() for character in webhook)
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise DeliveryError("Invalid Feishu webhook: use an official HTTPS bot v2 hook URL.")


def send_card(webhook: str, sign_secret: str, card: dict) -> None:
    """Send once and validate the API result, without exposing any secret.

    Transport errors, 5xx responses, and malformed acknowledgements are marked
    uncertain because the message could have arrived. The caller must persist
    this state and reconcile it instead of automatically replaying the card.
    """
    _validate_webhook(webhook)
    if not isinstance(sign_secret, str) or not sign_secret.strip():
        raise DeliveryError("FEISHU_SIGN_SECRET must be configured.")
    if not isinstance(card, dict) or card.get("msg_type") != "interactive" or not isinstance(card.get("card"), dict):
        raise DeliveryError("Invalid Feishu card payload.")
    timestamp = str(int(time.time()))
    signature = base64.b64encode(hmac.new(
        f"{timestamp}\n{sign_secret}".encode("utf-8"),
        msg=b"", digestmod=hashlib.sha256,
    ).digest()).decode("ascii")
    payload = dict(card, timestamp=timestamp, sign=signature)
    try:
        body = _json_bytes(payload)
    except (TypeError, ValueError, OverflowError):
        raise DeliveryError("Invalid JSON in the Feishu card payload.") from None
    if len(body) > MAX_MESSAGE_BYTES:
        raise DeliveryError("The signed Feishu message exceeds the 20 KB size limit.")
    try:
        response = requests.post(
            webhook, data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=(10, 45), allow_redirects=False,
        )
    except requests.RequestException:
        # Never include the underlying exception, which may contain the hook
        # token, request headers, or a proxy URL. Suppress exception chaining.
        raise DeliveryUncertainError(
            "Feishu delivery could not be confirmed after a network error; do not automatically retry."
        ) from None
    if response.status_code >= 500:
        raise DeliveryUncertainError(
            f"Feishu delivery could not be confirmed (HTTP {response.status_code}); do not automatically retry."
        )
    if response.status_code != 200:
        raise DeliveryError(f"Feishu rejected the request (HTTP {response.status_code}).")
    try:
        result = response.json()
    except (ValueError, requests.RequestException):
        raise DeliveryUncertainError(
            "Feishu returned an unreadable acknowledgement; do not automatically retry."
        ) from None
    if not isinstance(result, dict) or type(result.get("code")) is not int:
        raise DeliveryUncertainError(
            "Feishu acknowledgement is missing its numeric result code; do not automatically retry."
        )
    if result["code"] != 0:
        # Server messages are deliberately omitted: some APIs echo requests.
        raise DeliveryError(f"Feishu rejected the message (API code {result['code']}).")
