"""Publisher-deposited journal metadata; never fetch article bodies or PDFs.

Crossref indexes are used to discover *metadata changes*, not publication dates.
Online/print dates remain separate, including their original precision. See:
https://www.crossref.org/documentation/retrieve-metadata/rest-api/tips-for-using-the-crossref-rest-api/
https://help.openalex.org/data/works/attributes/
"""

from __future__ import annotations

import calendar
import html
import re
import time
from datetime import date, timedelta
from urllib.parse import quote

import requests


DEFAULT_WATCHLIST = [
    {"name": "IEEE Transactions on Robotics", "issns": ["1552-3098"]},
    {"name": "IEEE Robotics and Automation Letters", "issns": ["2377-3766"]},
    {"name": "The International Journal of Robotics Research", "issns": ["0278-3649", "1741-3176"]},
    {"name": "Science Robotics", "issns": ["2470-9476"]},
    {"name": "Soft Robotics", "issns": ["2169-5172", "2169-5180"]},
]

_HAND_TERMS = (
    "dexterous", "in-hand", "in hand", "multi-finger", "multifinger", "robotic hand",
    "robot hand", "anthropomorphic hand", "tendon", "underactuat", "gripper", "grasp",
    "fingertip", "tactile", "haptic", "end-effector", "end effector", "soft actuator",
    "robot skin", "robotic skin", "series elastic", "impedance control", "force control",
    "contact-rich", "contact rich",
)
_HEADERS = {"User-Agent": "DexterousPaperDigest/1.0 (journal metadata only)", "Accept": "application/json"}


class JournalSourceError(Exception):
    """A bounded, sanitized error safe to include in the source status note."""


def normalize_doi(value: object) -> str:
    """Accept DOI identifiers or canonical DOI URLs, never arbitrary article URLs."""
    if not isinstance(value, str) or any(ord(character) < 32 or ord(character) == 127 for character in value):
        return ""
    value = value.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
            break
    if len(value) > 512 or not re.fullmatch(r"10\.[0-9]{4,9}/[!-~]+", value, re.ASCII):
        return ""
    # These characters introduce credentials, URI query/fragment ambiguity, or
    # unsafe markup. Public DOI links are generated from the identifier only.
    if any(character in value for character in '@?#<>"\'`\\'):
        return ""
    return value.lower()


def _issn(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().upper()
    if not re.fullmatch(r"[0-9]{4}-[0-9]{3}[0-9X]", value, re.ASCII):
        return None
    digits = value.replace("-", "")
    total = sum(int(number) * (8 - index) for index, number in enumerate(digits[:7]))
    check = 10 if digits[-1] == "X" else int(digits[-1])
    return value if (total + check) % 11 == 0 else None


def _text(value: object, limit: int = 24000) -> str:
    if not isinstance(value, str):
        return ""
    # Crossref abstracts commonly contain JATS tags. Keep paragraph separation.
    value = re.sub(r"</(?:[\w-]+:)?(?:p|title|sec)>|<br\s*/?>", " ", value, flags=re.I)
    value = html.unescape(re.sub(r"<[^>]*>", "", value))
    value = "".join(character for character in value if character >= " " or character in "\n\t")
    return " ".join(value.split())[:limit]


def _date_parts(value: object) -> tuple[str, str] | None:
    """Return an ISO partial date without inventing an unavailable day/month."""
    if not isinstance(value, dict):
        return None
    parts = value.get("date-parts")
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], list):
        return None
    numbers = parts[0]
    if not 1 <= len(numbers) <= 3 or any(type(number) is not int for number in numbers):
        return None
    year = numbers[0]
    if not 1000 <= year <= 9999:
        return None
    if len(numbers) == 1:
        return str(year), "year"
    month = numbers[1]
    if not 1 <= month <= 12:
        return None
    if len(numbers) == 2:
        return f"{year:04}-{month:02}", "month"
    try:
        day = date(year, month, numbers[2])
    except ValueError:
        return None
    return day.isoformat(), "day"


def _publication_date(item: dict) -> tuple[str, str, str]:
    for field, basis in (("published-online", "online_publication"),
                         ("published-print", "print_publication"),
                         ("published", "publication_unspecified"),
                         ("issued", "publication_unspecified")):
        parsed = _date_parts(item.get(field))
        if parsed:
            return parsed[0], parsed[1], basis
    return "", "unknown", "unknown"


def _date_interval(value: str) -> tuple[date, date] | None:
    try:
        parts = [int(part) for part in value.split("-")]
        if len(parts) == 3:
            exact = date(*parts)
            return exact, exact
        if len(parts) == 2:
            year, month = parts
            return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
        if len(parts) == 1:
            return date(parts[0], 1, 1), date(parts[0], 12, 31)
    except (ValueError, TypeError):
        pass
    return None


def _get_json(url: str, params: dict | None, timeout: int, attempts: int = 3) -> dict:
    for attempt in range(attempts):
        try:
            response = requests.get(url, params=params, headers=_HEADERS,
                                    timeout=(min(10, timeout), timeout), allow_redirects=False)
        except requests.RequestException:
            if attempt < attempts - 1:
                time.sleep(2 ** attempt)
                continue
            raise JournalSourceError("network_unavailable") from None
        if response.status_code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
            try:
                wait = float(response.headers.get("Retry-After", ""))
                wait = min(30, max(0, wait))
            except (ValueError, TypeError):
                wait = 2 ** attempt
            time.sleep(wait)
            continue
        if response.status_code != 200:
            raise JournalSourceError(f"HTTP_{response.status_code}")
        if isinstance(response.content, bytes) and len(response.content) > 8_000_000:
            raise JournalSourceError("response_too_large")
        try:
            data = response.json()
        except (ValueError, requests.exceptions.JSONDecodeError):
            raise JournalSourceError("invalid_json") from None
        if not isinstance(data, dict):
            raise JournalSourceError("invalid_schema")
        return data
    raise JournalSourceError("request_failed")


def _record(item: object, allowed_issns: set[str], journal_name: str,
            today: date, old: dict) -> dict | None:
    if not isinstance(item, dict) or item.get("type") != "journal-article":
        return None
    supplied = item.get("ISSN", [])
    if not isinstance(supplied, list) or not allowed_issns.intersection(
            candidate for candidate in (_issn(value) for value in supplied) if candidate):
        return None
    doi = normalize_doi(item.get("DOI"))
    titles = item.get("title", [])
    title = _text(titles[0], 2000) if isinstance(titles, list) and titles else ""
    if not doi or not title:
        return None
    names, affiliations = [], []
    for author in item.get("author", []) if isinstance(item.get("author"), list) else []:
        if not isinstance(author, dict):
            continue
        name = " ".join(filter(None, (_text(author.get("given"), 200), _text(author.get("family"), 200))))
        name = name or _text(author.get("name"), 400)
        if name:
            names.append(name)
        for affiliation in author.get("affiliation", []) if isinstance(author.get("affiliation"), list) else []:
            if isinstance(affiliation, dict):
                text = _text(affiliation.get("name"), 500)
                if text and text not in affiliations:
                    affiliations.append(text)
    publish_date, precision, basis = _publication_date(item)
    key = f"doi:{doi}"
    abstract = _text(item.get("abstract"))
    result = dict(old, paper_id=key, title=title, abstract=abstract or old.get("abstract", ""),
                  authors=", ".join(names) or old.get("authors", ""), affiliations=affiliations,
                  doi=doi, publish_date=publish_date, date_precision=precision, date_basis=basis,
                  journal_ref=journal_name, journal_name=journal_name,
                  publication_status="journal_article", paper_url="https://doi.org/" + quote(doi, safe="/();:-._+"),
                  source="crossref", first_seen_date=old.get("first_seen_date", today.isoformat()),
                  last_seen_date=today.isoformat())
    if not publish_date and old.get("publish_date"):
        for field in ("publish_date", "date_precision", "date_basis"):
            result[field] = old.get(field, result[field])
    if abstract:
        result["abstract_source"] = "crossref"
    online, printed = _date_parts(item.get("published-online")), _date_parts(item.get("published-print"))
    if online:
        result["online_publication_date"] = online[0]
    if printed:
        result["print_publication_date"] = printed[0]
    indexed = item.get("indexed", {})
    if isinstance(indexed, dict) and isinstance(indexed.get("date-time"), str):
        # Retained for synchronization provenance only; never used as publish_date.
        result["metadata_indexed_at"] = _text(indexed["date-time"], 100)
    return result


def _reconstruct_abstract(index: object) -> str:
    if not isinstance(index, dict) or not index:
        return ""
    positions: dict[int, str] = {}
    for word, offsets in index.items():
        if not isinstance(word, str) or not isinstance(offsets, list) or not offsets:
            return ""
        for offset in offsets:
            if type(offset) is not int or not 0 <= offset < 10000 or offset in positions:
                return ""
            positions[offset] = word
    if not positions or set(positions) != set(range(len(positions))):
        return ""
    return _text(" ".join(positions[offset] for offset in range(len(positions))))


def _enrich_abstracts(merged: dict, keys: set[str], today: date, limit: int, timeout: int) -> tuple[int, int]:
    eligible = []
    for key in keys:
        paper = merged[key]
        interval = _date_interval(paper.get("publish_date", ""))
        title = paper.get("title", "").lower()
        if not paper.get("abstract") and interval and interval[1] >= today - timedelta(days=30) \
                and interval[0] <= today and any(term in title for term in _HAND_TERMS):
            eligible.append(key)
    # Stable priority: known recent publication dates before partial/older dates.
    eligible.sort(key=lambda key: (merged[key].get("publish_date", ""), key), reverse=True)
    enriched, unavailable, transport_failures = 0, 0, 0
    for key in eligible[:limit]:
        paper = merged[key]
        try:
            data = _get_json("https://api.openalex.org/works/doi:" + quote(paper["doi"], safe="/();:-._+"),
                             None, min(15, timeout), attempts=1)
            if normalize_doi(data.get("doi")) != paper["doi"]:
                raise JournalSourceError("DOI_mismatch")
            abstract = _reconstruct_abstract(data.get("abstract_inverted_index"))
            if not abstract:
                raise JournalSourceError("abstract_unavailable")
        except JournalSourceError as error:
            unavailable += 1
            if str(error) in {"network_unavailable", "HTTP_429", "HTTP_401", "HTTP_403",
                              "HTTP_500", "HTTP_502", "HTTP_503", "HTTP_504"}:
                transport_failures += 1
                if transport_failures >= 3:
                    break
            continue
        paper["abstract"] = abstract
        paper["abstract_source"] = "openalex"
        enriched += 1
        transport_failures = 0
    return enriched, unavailable


def collect_journals(db: dict, today: date, config: dict) -> tuple[dict, int, str]:
    """Merge bounded exact-journal metadata; isolated source errors are reported.

    The adapter retains journal metadata updates, including old publications.
    Relevance and candidate recency belong to the caller, so an indexed update
    cannot accidentally make an old article rank as newly published.
    """
    settings = config.get("journals", {})
    if not isinstance(settings, dict) or not settings.get("enabled", False):
        return dict(db), 0, "期刊来源未启用。"
    watchlist = settings.get("watchlist", DEFAULT_WATCHLIST)
    if not isinstance(watchlist, list):
        return dict(db), 0, "期刊来源失败：watchlist 配置无效。"
    timeout = min(60, max(1, int(settings.get("request_timeout", 25))))
    page_size = min(1000, max(1, int(settings.get("page_size", 100))))
    max_pages = min(20, max(1, int(settings.get("max_pages", 10))))
    overlap = min(90, max(1, int(settings.get("overlap_days", 7))))
    backfill = min(180, max(overlap, int(settings.get("backfill_days", 90))))
    known_journals = any(isinstance(paper, dict) and paper.get("source") == "crossref" for paper in db.values())
    days = backfill if today.weekday() == 6 or not known_journals else overlap
    since = today - timedelta(days=days)
    merged = {key: dict(value) if isinstance(value, dict) else value for key, value in db.items()}
    seen_keys: set[str] = set()
    success, attempted, failures, incomplete = 0, 0, [], []
    supplemental_success, supplemental_attempts, supplemental_keys = 0, 0, set()
    supplement = bool(settings.get("fresh_publication_supplement", False))
    publication_days = min(365, max(1, int(config.get("fallback_days", 30))))
    publication_since = today - timedelta(days=publication_days)
    for journal in watchlist:
        if not isinstance(journal, dict):
            failures.append("watchlist_invalid")
            continue
        name = _text(journal.get("name"), 200)
        values = journal.get("issns", [])
        allowed = [_issn(value) for value in values] if isinstance(values, list) else []
        if not name or not allowed or any(value is None for value in allowed):
            failures.append("watchlist_invalid")
            continue
        # Index updates can be dominated by reindexing of historical articles.
        # A separately bounded publication query protects current articles from
        # being crowded out by the main query's page cap. It supplements index
        # synchronization rather than replacing it (deposits can arrive late).
        for mode in ("indexed", "published") if supplement else ("indexed",):
            is_supplement = mode == "published"
            if is_supplement:
                supplemental_attempts += 1
                query_filter = (f"type:journal-article,from-pub-date:{publication_since.isoformat()},"
                                f"until-pub-date:{today.isoformat()}")
            else:
                attempted += 1
                query_filter = (f"type:journal-article,from-index-date:{since.isoformat()},"
                                f"until-index-date:{today.isoformat()}")
            label = name + ("（发表日期补查）" if is_supplement else "")
            cursor, cursors, completed = "*", set(), False
            try:
                for page in range(max_pages):
                    params = {"filter": query_filter, "rows": page_size, "cursor": cursor}
                    # Crossref's August 2026 cursor implementation rejects any
                    # publication-date sort. The caller sorts these records.
                    if not is_supplement:
                        params.update(sort="indexed", order="desc")
                    data = _get_json(f"https://api.crossref.org/journals/{allowed[0]}/works", params, timeout)
                    message = data.get("message")
                    if data.get("status") != "ok" or not isinstance(message, dict) or not isinstance(message.get("items"), list):
                        raise JournalSourceError("invalid_schema")
                    items = message["items"]
                    for item in items:
                        doi = normalize_doi(item.get("DOI")) if isinstance(item, dict) else None
                        old = merged.get(f"doi:{doi}", {}) if doi else {}
                        paper = _record(item, set(allowed), name, today, old if isinstance(old, dict) else {})
                        if paper:
                            merged[paper["paper_id"]] = paper
                            seen_keys.add(paper["paper_id"])
                            if is_supplement:
                                supplemental_keys.add(paper["paper_id"])
                    if len(items) < page_size:
                        completed = True
                        break
                    next_cursor = message.get("next-cursor")
                    if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor or next_cursor in cursors:
                        incomplete.append(label)
                        completed = True
                        break
                    cursors.add(cursor)
                    cursor = next_cursor
                if not completed:
                    incomplete.append(label)
                if is_supplement:
                    supplemental_success += 1
                else:
                    success += 1
            except JournalSourceError as error:
                failures.append(f"{label}({error})")
    limit = min(50, max(0, int(settings.get("abstract_enrichment_limit", 20))))
    enriched, unavailable = _enrich_abstracts(merged, seen_keys, today, limit, timeout) if limit else (0, 0)
    note = f"期刊：Crossref {success}/{attempted} 个来源刷新成功，回查最近 {days} 天元数据索引，获得 {len(seen_keys)} 条期刊记录；索引更新日期不作为发表日期。"
    if supplement:
        note += (f" 最近 {publication_days} 天发表日期补查 {supplemental_success}/{supplemental_attempts} 个来源成功，"
                 f"取得 {len(supplemental_keys)} 条记录，与索引查询按 DOI 去重。")
    if failures:
        note += " 来源失败：" + "、".join(failures) + "。"
    if incomplete:
        note += " 分页受限，可能不完整：" + "、".join(incomplete) + "。"
    if limit:
        note += f" OpenAlex 补齐 {enriched} 篇摘要，{unavailable} 篇未获得摘要。"
    return merged, len(seen_keys), note
