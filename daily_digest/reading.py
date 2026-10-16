"""Read public fulltext only after selection; never persist raw papers in the DB."""

from __future__ import annotations

from html.parser import HTMLParser
from io import BytesIO
import ipaddress
import json
import re
import socket
import time
from urllib.parse import quote, urljoin, urlsplit

import requests

MAX_BYTES = 15_000_000
MAX_PAGES = 80


class ReadingUnavailable(ValueError):
    pass


def _public(url: str) -> None:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or any(c.isspace() for c in url) or "\\" in url):
            raise ReadingUnavailable("Not a public HTTPS paper URL.")
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ReadingUnavailable("Paper URL resolved to a nonpublic host.")
    except (ValueError, OSError) as exc:
        raise ReadingUnavailable("Paper URL is unavailable or not public.") from exc


def fetch_public(url: str, *, deadline: float | None = None) -> tuple[bytes, str, str]:
    """Validate every redirect; cap both declared and streamed response sizes."""
    deadline = deadline or time.monotonic() + 35
    for _ in range(5):
        _public(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReadingUnavailable("Paper access time budget exhausted.")
        try:
            response = requests.get(url, timeout=min(12, remaining), stream=True, allow_redirects=False,
                headers={"User-Agent": "DexterousPaperDigest/4.0 (public scholarly reading)",
                    "Accept": "text/html,application/pdf,application/json,application/xml;q=0.9"})
            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    url = urljoin(url, response.headers.get("Location", ""))
                    continue
                if response.status_code != 200:
                    raise ReadingUnavailable("Fulltext endpoint did not return a public document.")
                if int(response.headers.get("Content-Length", 0)) > MAX_BYTES:
                    raise ReadingUnavailable("Paper exceeds download size limit.")
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_BYTES or time.monotonic() > deadline:
                        raise ReadingUnavailable("Paper access resource limit exceeded.")
                    chunks.append(chunk)
                return b"".join(chunks), url, response.headers.get("Content-Type", "")
            finally:
                response.close()
        except (requests.RequestException, ValueError) as exc:
            raise ReadingUnavailable("Public paper request failed.") from exc
    raise ReadingUnavailable("Too many paper redirects.")


class _Article(HTMLParser):
    def __init__(self):
        super().__init__()
        self.article, self.all, self.links, self.headings = [], [], [], []
        self.depth = self.skip = 0
        self.heading = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "nav", "footer", "aside"}:
            self.skip += 1
        if tag in {"article", "main"}:
            self.depth += 1
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and self.depth:
            self.heading = []
        if tag == "meta" and attrs.get("name", "").lower() in {"citation_pdf_url", "citation_fulltext_html_url"}:
            self.links.append(attrs.get("content", ""))
        if tag in {"a", "link"}:
            href = attrs.get("href", "")
            if href and (attrs.get("type") == "application/pdf" or re.search(r"(?:\.pdf(?:[?#]|$)|/doi/pdf/)", href)):
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and self.heading is not None:
            self.headings.append(" ".join(self.heading))
            self.heading = None
        if tag in {"article", "main"}:
            self.depth = max(0, self.depth - 1)
        if tag in {"script", "style", "nav", "footer", "aside"}:
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if data.strip() and not self.skip:
            self.all.append(data.strip())
            if self.depth:
                self.article.append(data.strip())
                if self.heading is not None:
                    self.heading.append(data.strip())


def extract_html(raw: bytes) -> tuple[str, list[str]]:
    parser = _Article()
    parser.feed(raw.decode("utf-8", errors="replace"))
    headings = "\n".join(parser.headings)
    structured = (len(parser.headings) >= 3 and re.search(r"\bintroduction\b", headings, re.I)
        and re.search(r"\b(experiments?|results|evaluation)\b", headings, re.I))
    restricted = re.search(r"purchase this article|to read the full text|request[- ]a[- ]copy|sign in to access",
        " ".join(parser.all), re.I)
    return ("\n".join(parser.article) if structured and not restricted else ""), parser.links


def extract_pdf(raw: bytes) -> tuple[str, bool]:
    try:
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(raw), strict=False)
        if reader.is_encrypted:
            raise ReadingUnavailable("Encrypted PDF is not extracted.")
        count = len(reader.pages)
        pages = [(page.extract_text() or "") for page in reader.pages[:MAX_PAGES]]
        complete = count <= MAX_PAGES and all(len(page.strip()) >= 30 for page in pages)
        return "\n\n".join(pages), complete
    except Exception as exc:
        raise ReadingUnavailable("PDF text cannot be reliably extracted.") from exc


def _matches(text: str, title: str) -> bool:
    words = set(re.findall(r"[a-z]{3,}", title.lower())) - {"the", "and", "for", "with", "from"}
    header = text[:4000].lower()
    return bool(words) and sum(word in header for word in words) / len(words) >= 0.6


def _usable(text: str, title: str) -> bool:
    return (len(text) >= 2500 and _matches(text, title)
        and bool(re.search(r"\bintroduction\b", text, re.I))
        and bool(re.search(r"\b(methods?|design|model)\b", text, re.I))
        and bool(re.search(r"\b(experiments?|results|evaluation|discussion)\b", text, re.I)))


def _excerpt(text: str, limit: int) -> str:
    # Sample across the document, rather than silently dropping later results.
    marker = "\n[全文节选，部分段落未提供]\n"
    width = max(1, (limit - 5 * len(marker)) // 6)
    return marker.join(text[int(i * (len(text) - width) / 5):int(i * (len(text) - width) / 5) + width]
        for i in range(6))[:limit]


def read_paper(paper: dict, *, max_chars: int = 90_000) -> dict:
    """Return an enriched COPY; failures downgrade this paper, not the whole run."""
    result = dict(paper)
    abstract = paper.get("abstract", "")
    result.update(reading_text=abstract, reading_depth="abstract_only" if abstract else "title_only",
        enrichment_note="摘要及来源公开页面，未获取全文。" if abstract else "仅标题与来源元数据，未获取摘要或全文。",
        reading_url=paper.get("paper_url") or paper.get("arxiv_url", ""))
    if abstract and paper.get("abstract_source") == "arxiv_same_doi":
        result["enrichment_note"] = "同 DOI arXiv 预印本摘要（可能与期刊版本不同），未获取期刊全文。"
    deadline, visited = time.monotonic() + 55, set()

    def document(url: str) -> bool:
        if not url or url in visited or len(visited) >= 10 or time.monotonic() > deadline:
            return False
        visited.add(url)
        try:
            raw, final_url, media_type = fetch_public(url, deadline=deadline)
            complete = True
            if raw.startswith(b"%PDF-"):
                text, complete = extract_pdf(raw)
                links = []
            elif "html" in media_type or raw.lstrip().startswith((b"<", b"<!")):
                text, links = extract_html(raw)
            else:
                return False
            if _usable(text, paper.get("title", "")):
                partial = not complete or len(text) > max_chars
                result.update(reading_text=_excerpt(text, max_chars) if len(text) > max_chars else text,
                    reading_depth="full_text_excerpt" if partial else "full_text",
                    enrichment_note=("论文全文节选（受长度或文本提取覆盖限制）；未提供全部正文。" if partial
                        else "论文全文（可提取文本；图像、公式及补充材料未独立核验）。"), reading_url=final_url)
                return True
            return any(document(urljoin(final_url, link)) for link in links[:3])
        except (ReadingUnavailable, TypeError, UnicodeError):
            return False

    arxiv_id = paper.get("arxiv_id", "")
    if isinstance(arxiv_id, str) and re.fullmatch(r"\d{4}\.\d{4,5}(?:v\d+)?", arxiv_id):
        if document(f"https://arxiv.org/html/{arxiv_id}") or document(f"https://arxiv.org/pdf/{arxiv_id}"):
            return result
    doi = paper.get("doi", "")
    if isinstance(doi, str) and re.fullmatch(r"10\.\d{4,9}/\S+", doi):
        encoded = quote(doi, safe="/().;:_-")
        try:
            raw, _, _ = fetch_public("https://api.crossref.org/works/" + encoded, deadline=deadline)
            metadata = json.loads(raw).get("message", {})
            for link in metadata.get("link", [])[:3]:
                if isinstance(link, dict) and document(link.get("URL", "")):
                    return result
        except (ReadingUnavailable, ValueError, TypeError, AttributeError):
            pass
        if document("https://doi.org/" + encoded):
            return result
        try:
            raw, _, _ = fetch_public("https://api.openalex.org/works/doi:" + encoded, deadline=deadline)
            metadata = json.loads(raw)
            locations = [metadata.get("best_oa_location"), *(metadata.get("locations") or [])[:4]]
            for location in locations:
                if isinstance(location, dict):
                    if document(location.get("pdf_url", "")) or document(location.get("landing_page_url", "")):
                        return result
        except (ReadingUnavailable, ValueError, TypeError, AttributeError):
            pass
    return result
