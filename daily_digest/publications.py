"""Bounded, exact-venue metadata from OpenAlex, PMLR and OpenReview."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from .delivery import _safe_link
from .journals import JournalSourceError, _get_json, _reconstruct_abstract, _text, normalize_doi
from .reading import ReadingUnavailable, fetch_public


def publication_url(paper: dict) -> str:
    """Recognize source identities, never a model-authored paper URL."""
    key, source = paper.get("paper_id", ""), paper.get("source")
    doi = normalize_doi(paper.get("doi", ""))
    if source == "crossref" and doi and key == "doi:" + doi:
        from urllib.parse import quote
        return "https://doi.org/" + quote(doi, safe="/:;()._-~")
    if source == "openalex" and re.fullmatch(r"W\d+", str(paper.get("openalex_id", ""))):
        if doi and key == "doi:" + doi:
            from urllib.parse import quote
            return "https://doi.org/" + quote(doi, safe="/:;()._-~")
        if key == "openalex:" + paper["openalex_id"]:
            return _safe_link(paper.get("paper_url")) or "https://openalex.org/" + paper["openalex_id"]
    if source == "pmlr" and re.fullmatch(r"pmlr:v\d+/[A-Za-z0-9_-]+", str(key)):
        return "https://proceedings.mlr.press/" + key[5:] + ".html"
    if source == "jmlr" and re.fullmatch(r"jmlr:v\d+/[A-Za-z0-9_-]+", str(key)):
        return "https://www.jmlr.org/papers/" + key[5:] + ".html"
    if source == "openreview" and re.fullmatch(r"openreview:[A-Za-z0-9_-]{1,100}", str(key)):
        return "https://openreview.net/forum?id=" + key.split(":", 1)[1]
    return ""


def is_journal_record(paper: dict) -> bool:
    return bool(publication_url(paper) and (
        paper.get("source") == "crossref" and paper.get("venue_type", "journal") == "journal"
        or paper.get("source") in {"openalex", "jmlr"} and paper.get("venue_type") == "journal"))


def _base(venue: dict, venue_type: str) -> dict:
    return {"venue_type": venue_type, "venue_name": venue["name"],
        "venue_abbreviation": venue.get("abbreviation", ""),
        "journal_name" if venue_type == "journal" else "conference_name": venue["name"],
        "publication_status": "journal_article" if venue_type == "journal" else "conference_paper"}


def _recent(value: str, today: date, days: int) -> bool:
    try:
        return today - timedelta(days=days) <= date.fromisoformat(value) <= today
    except (ValueError, TypeError):
        return False


def _limits(settings: dict) -> tuple[int, int, int]:
    return (min(10, max(1, int(settings.get("max_pages", 2)))),
        min(100, max(1, int(settings.get("page_size", 100)))),
        min(30, max(1, int(settings.get("request_timeout", 15)))))


def collect_openalex(db: dict, today: date, venue: dict, settings: dict, *, venue_type: str) -> tuple[dict, int, str]:
    source_id = venue.get("source_id", "")
    if not re.fullmatch(r"S\d+", str(source_id)):
        raise JournalSourceError("invalid_source_id")
    pages, size, timeout = _limits(settings)
    days = int(settings.get("fallback_days", 30))
    merged, seen, incomplete = dict(db), set(), False
    for page in range(1, pages + 1):
        data = _get_json("https://api.openalex.org/works", {
            "filter": f"primary_location.source.id:{source_id},from_publication_date:{today - timedelta(days=days)},to_publication_date:{today}",
            "search": "dexterous OR tactile OR gripper OR grasping OR tendon OR underactuated",
            "sort": "publication_date:desc", "per_page": size, "page": page,
        }, timeout)
        rows, meta = data.get("results"), data.get("meta", {})
        if not isinstance(rows, list) or not isinstance(meta, dict):
            raise JournalSourceError("invalid_schema")
        for row in rows:
            if not isinstance(row, dict):
                continue
            location = row.get("primary_location") or {}
            if not isinstance(location, dict):
                continue
            origin = location.get("source") or {}
            if not isinstance(origin, dict) or origin.get("id") != "https://openalex.org/" + source_id:
                continue
            if row.get("type") in {"preprint", "paratext", "dataset", "peer-review"}:
                continue
            identifier = str(row.get("id", "")).removeprefix("https://openalex.org/")
            title = _text(row.get("title"), 2000)
            published = row.get("publication_date", "")
            if not re.fullmatch(r"W\d+", identifier) or not title or not _recent(published, today, days):
                continue
            doi = normalize_doi(row.get("doi"))
            key = "doi:" + doi if doi else "openalex:" + identifier
            # Do not overwrite Crossref's publisher-provided journal metadata.
            if merged.get(key, {}).get("source") == "crossref":
                continue
            authors, affiliations = [], []
            authorships = row.get("authorships")
            for author in authorships if isinstance(authorships, list) else []:
                if not isinstance(author, dict):
                    continue
                author_info = author.get("author")
                name = author_info.get("display_name") if isinstance(author_info, dict) else None
                if isinstance(name, str) and name:
                    authors.append(name)
                institutions = author.get("institutions")
                for institution in institutions if isinstance(institutions, list) else []:
                    if isinstance(institution, dict) and isinstance(institution.get("display_name"), str):
                        affiliations.append(institution["display_name"])
            record = dict(merged.get(key, {}), **_base(venue, venue_type), paper_id=key, source="openalex",
                openalex_id=identifier, openalex_source_id=source_id, doi=doi, title=title,
                authors=", ".join(authors), affiliations=list(dict.fromkeys(affiliations)),
                abstract=_reconstruct_abstract(row.get("abstract_inverted_index")) or merged.get(key, {}).get("abstract", ""), abstract_source="openalex",
                publish_date=published, date_precision="day", date_basis="openalex_publication",
                first_seen_date=merged.get(key, {}).get("first_seen_date", today.isoformat()), last_seen_date=today.isoformat(),
                paper_url=_safe_link(location.get("landing_page_url")) or "https://openalex.org/" + identifier,
                pdf_url=_safe_link(location.get("pdf_url")) or "")
            record["paper_url"] = publication_url(record)
            locations = row.get("locations")
            for alternative in locations if isinstance(locations, list) else []:
                if not isinstance(alternative, dict):
                    continue
                landing = alternative.get("landing_page_url")
                match = re.fullmatch(r"https?://arxiv\.org/abs/(\d{4}\.\d{4,5})(?:v\d+)?", landing) if isinstance(landing, str) else None
                if match:
                    record.update(arxiv_id=match[1], arxiv_url="https://arxiv.org/abs/" + match[1])
                    break
            merged[key] = record
            seen.add(key)
        total = meta.get("count")
        more = isinstance(total, int) and total > page * size
        if not more and len(rows) < size:
            break
        if page == pages and (more or len(rows) == size):
            incomplete = True
    note = f"OpenAlex {venue.get('abbreviation') or venue['name']} 精确来源检索取得{len(seen)}条记录；日期为来源登记的发表日期。"
    if incomplete:
        note += "分页受限，可能不完整。"
    return merged, len(seen), note


def _get_text(url: str) -> str:
    try:
        raw, _, _ = fetch_public(url)
        return raw.decode("utf-8")
    except (ReadingUnavailable, UnicodeError):
        raise JournalSourceError("public_metadata_unavailable") from None


def collect_jmlr(db: dict, today: date, venue: dict, settings: dict) -> tuple[dict, int, str]:
    """The official RSS has year-only dates; retain that precision honestly."""
    try:
        feed = ET.fromstring(_get_text("https://www.jmlr.org/jmlr.xml"))
    except ET.ParseError:
        raise JournalSourceError("invalid_jmlr_feed") from None
    if feed.tag != "rss" or feed.find("channel") is None:
        raise JournalSourceError("invalid_jmlr_feed")
    merged, count, year_only = dict(db), 0, 0
    for item in feed.findall("channel/item"):
        link = item.findtext("link", "").strip()
        match = re.fullmatch(r"https?://(?:www\.)?jmlr\.org/papers/(v\d+/[A-Za-z0-9_-]+)\.html", link)
        title, abstract = _text(item.findtext("title", ""), 2000), _text(item.findtext("description", ""))
        if not match or not title:
            continue
        published = item.findtext("pubDate", "").strip()
        precision = "year"
        if re.fullmatch(r"\d{4}", published):
            if not today.year - 1 <= int(published) <= today.year:
                continue
            year_only += 1
        else:
            try:
                published = parsedate_to_datetime(published).date().isoformat()
            except (ValueError, TypeError):
                continue
            if not _recent(published, today, int(settings.get("fallback_days", 30))):
                continue
            precision = "day"
        key = "jmlr:" + match[1]
        old = merged.get(key, {})
        # An earlier source snapshot can supply finer, already recorded dates.
        if precision == "year" and old.get("date_precision") in {"day", "month"} and str(old.get("publish_date", "")).startswith(published):
            published, precision = old["publish_date"], old["date_precision"]
        merged[key] = dict(old, **_base(venue, "journal"), source="jmlr", paper_id=key,
            title=title, abstract=abstract or old.get("abstract", ""), abstract_source="jmlr",
            authors=_text(item.findtext("author", "")) or old.get("authors", ""),
            publish_date=published, date_precision=precision, date_basis="journal_publication",
            paper_url="https://www.jmlr.org/papers/" + match[1] + ".html",
            first_seen_date=old.get("first_seen_date", today.isoformat()), last_seen_date=today.isoformat())
        count += 1
    note = f"JMLR 官方RSS取得{count}条元数据，其中{year_only}条仅提供发表年份；年份不补成具体日期，无法确认近期的条目只保留元数据。"
    return merged, count, note


def collect_pmlr(db: dict, today: date, venue: dict, settings: dict, *, index: str | None = None) -> tuple[dict, int, str]:
    abbreviation = venue.get("abbreviation")
    if abbreviation not in {"CoRL", "ICML"}:
        raise JournalSourceError("invalid_pmlr_venue")
    index = _get_text("https://proceedings.mlr.press/") if index is None else index
    volumes = []
    for entry in re.findall(r"<li\b[^>]*>(.*?)</li>", index, re.S | re.I):
        label = _text(entry)
        if not re.search(rf"^(?:Volume \d+\s*)?(?:Proceedings of )?{abbreviation} (\d{{4}})(?: Proceedings)?$", label):
            continue
        year = re.search(r"\b(20\d{2})\b", label)
        volume = re.search(r'href=[\"\'](?:https://proceedings\.mlr\.press/)?(v\d+)/?[\"\']', entry)
        if volume and year and int(year[1]) <= today.year:
            volumes.append(volume[1])
    if not volumes:
        raise JournalSourceError("no_matching_proceedings")
    merged, seen, failures = dict(db), set(), []
    days = int(settings.get("fallback_days", 30))
    for volume in list(dict.fromkeys(volumes))[:2]:
        try:
            feed = ET.fromstring(_get_text(f"https://proceedings.mlr.press/{volume}/assets/rss/feed.xml"))
            if feed.tag != "rss" or feed.find("channel") is None:
                raise JournalSourceError("invalid_feed")
        except (ET.ParseError, JournalSourceError):
            failures.append(volume)
            continue
        volume_keys = []
        for item in feed.findall("channel/item"):
            url = item.findtext("link", "").strip()
            match = re.fullmatch(rf"https://proceedings\.mlr\.press/({volume}/[A-Za-z0-9_-]+)\.html", url)
            try:
                published = parsedate_to_datetime(item.findtext("pubDate", "")).date().isoformat()
            except (ValueError, TypeError):
                continue
            title = _text(item.findtext("title", ""), 2000)
            if not match or not title or not _recent(published, today, days):
                continue
            key = "pmlr:" + match[1]
            merged[key] = dict(merged.get(key, {}), **_base(venue, "conference"), source="pmlr", paper_id=key,
                title=title, abstract=_text(item.findtext("description", "")),
                authors=_text(item.findtext("author", "")) or merged.get(key, {}).get("authors", ""), publish_date=published, date_precision="day",
                date_basis="proceedings_publication", paper_url=url,
                first_seen_date=merged.get(key, {}).get("first_seen_date", today.isoformat()), last_seen_date=today.isoformat())
            seen.add(key)
            volume_keys.append(key)
        # PMLR RSS omits authors. The public volume index supplies bibliographic
        # metadata needed for exact title/first-author preprint deduplication.
        if volume_keys:
            try:
                listing = _get_text(f"https://proceedings.mlr.press/{volume}/")
                for block in re.findall(r'<div\s+class="paper"[^>]*>(.*?)</div>', listing, re.S):
                    link = re.search(rf'https://proceedings\.mlr\.press/({volume}/[A-Za-z0-9_-]+)\.html', block)
                    authors = re.search(r'<span\s+class="authors"[^>]*>(.*?)</span>', block, re.S)
                    if link and authors and "pmlr:" + link[1] in seen:
                        merged["pmlr:" + link[1]]["authors"] = _text(authors[1])
            except JournalSourceError:
                failures.append(volume + " author metadata")
    if all(volume in failures for volume in list(dict.fromkeys(volumes))[:2]):
        raise JournalSourceError("proceedings_feeds_unavailable")
    note = f"PMLR {abbreviation} 正式论文集取得{len(seen)}条记录，使用条目发表日期，不使用会议举行或RSS构建日期。"
    if failures:
        note += "来源失败：部分论文集无法读取。"
    return merged, len(seen), note


def collect_openreview(db: dict, today: date, venue: dict, settings: dict) -> tuple[dict, int, str]:
    if venue.get("venue_prefix") != "ICLR.cc":
        raise JournalSourceError("invalid_openreview_venue")
    pages, size, timeout = _limits(settings)
    merged, seen, incomplete = dict(db), set(), False
    for year in (today.year, today.year - 1):
        venue_id = f"ICLR.cc/{year}/Conference"
        for page in range(pages):
            data = _get_json("https://api2.openreview.net/notes", {"content.venueid": venue_id,
                "sort": "pdate:desc", "limit": size, "offset": page * size}, timeout, attempts=1)
            rows = data.get("notes")
            if not isinstance(rows, list):
                raise JournalSourceError("invalid_schema")
            older = False
            for row in rows:
                if not isinstance(row, dict) or row.get("ddate"):
                    continue
                content = row.get("content")
                if not isinstance(content, dict):
                    continue
                def value(field):
                    item = content.get(field)
                    return item.get("value") if isinstance(item, dict) else item
                if value("venueid") != venue_id or re.search(r"reject|withdraw|submitted|under review|workshop", str(value("venue")), re.I):
                    continue
                identifier, timestamp = row.get("id"), row.get("pdate")
                if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identifier) or type(timestamp) is not int:
                    continue
                try:
                    published = datetime.fromtimestamp(timestamp / 1000, timezone.utc).date().isoformat()
                except (ValueError, OverflowError, OSError):
                    continue
                if not _recent(published, today, int(settings.get("fallback_days", 30))):
                    older = older or published < today.isoformat()
                    continue
                title = _text(value("title"), 2000)
                if not title:
                    continue
                key = "openreview:" + identifier
                authors = value("authors")
                merged[key] = dict(merged.get(key, {}), **_base(venue, "conference"), source="openreview", paper_id=key,
                    title=title, abstract=_text(value("abstract")),
                    authors=", ".join(a for a in authors if isinstance(a, str)) if isinstance(authors, list) else "",
                    publish_date=published, date_precision="day", date_basis="openreview_acceptance",
                    paper_url="https://openreview.net/forum?id=" + identifier,
                    pdf_url="https://openreview.net/pdf?id=" + identifier,
                    first_seen_date=merged.get(key, {}).get("first_seen_date", today.isoformat()), last_seen_date=today.isoformat())
                seen.add(key)
            if older or len(rows) < size:
                break
            if page + 1 == pages:
                incomplete = True
    note = f"OpenReview ICLR 已录用主会场取得{len(seen)}条记录；日期是来源登记录用日期，未独立核验。"
    if incomplete:
        note += "分页受限，可能不完整。"
    return merged, len(seen), note


def collect_conferences(db: dict, today: date, config: dict) -> tuple[dict, int, str]:
    settings = config.get("conferences", {})
    if not settings.get("enabled", False):
        return dict(db), 0, ""
    settings = dict(settings, fallback_days=config["fallback_days"])
    merged, count, notes = dict(db), 0, []
    pmlr_index = None
    for venue in settings.get("watchlist", []):
        try:
            provider = venue.get("provider")
            if provider == "openalex":
                merged, found, note = collect_openalex(merged, today, venue, settings, venue_type="conference")
            elif provider == "pmlr":
                if pmlr_index is None:
                    pmlr_index = _get_text("https://proceedings.mlr.press/")
                merged, found, note = collect_pmlr(merged, today, venue, settings, index=pmlr_index)
            elif provider == "openreview":
                merged, found, note = collect_openreview(merged, today, venue, settings)
            else:
                raise JournalSourceError("invalid_provider")
            count += found
            notes.append(note)
        except JournalSourceError as error:
            notes.append(f"会议来源失败：{venue.get('abbreviation', 'unknown')}({error})。")
    return merged, count, "".join(notes)
