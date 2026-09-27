"""Collect metadata, rank abstracts, summarize selected papers, and deliver."""

from __future__ import annotations

import hashlib
import calendar
import json
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

from .delivery import DeliveryError, DeliveryUncertainError, build_cards, send_card
from .llm import LLMClient, LLMError
from .state import StateError, acknowledge_all, load_json, load_state, resume_pending, validate_pending, write_json
from .sources import collect_rss
from .journals import collect_journals, normalize_doi

ARXIV_ID = re.compile(r"\d{4}\.\d{4,5}\Z", re.ASCII)
HAND_TERMS = (
    "dexterous hand", "dexterous manipulation", "in-hand", "in hand manipulation",
    "multi-finger", "multifinger", "multi finger", "robotic hand", "robot hand",
    "anthropomorphic hand", "tendon-driven hand", "tendon driven hand", "allegro hand",
    "shadow hand", "leap hand", "aero hand", "fingertip sensing", "fingertip",
    "soft gripper", "tactile sensor", "robot skin", "robotic skin",
)
TECH_TERMS = ("tactile", "force control", "impedance", "tendon", "underactuated", "compliance", "calibration")


class PipelineError(RuntimeError):
    pass


class SourceUnavailable(PipelineError):
    pass


def relevance(paper: dict) -> int:
    title = str(paper.get("title", "")).lower()
    text = title + " " + str(paper.get("abstract", "")).lower()
    identity = sum(term in text for term in HAND_TERMS)
    direct_title = any(term in title for term in HAND_TERMS)
    technical = sum(term in text for term in TECH_TERMS)
    grasp = any(term in text for term in ("grasp", "gripper", "robotic manipulation"))
    if not identity and not (grasp and technical):
        return 0
    return 20 * min(identity, 3) + 20 * direct_title + 5 * min(technical, 4) + 5 * bool(paper.get("code_url"))


def candidates(db: dict, state: dict, today: date, config: dict) -> list[dict]:
    sent = set(state["sent_ids"])
    earliest = today - timedelta(days=config["fallback_days"])
    recent = today - timedelta(days=config["recent_days"])
    eligible = []
    sent_dois = {normalize_doi(db[key].get("doi", "")) for key in sent if key in db}
    sent_titles = {_identity(db[key]) for key in sent if key in db} - {""}
    for identifier, paper in db.items():
        arxiv_id = paper.get("arxiv_id", "")
        doi = normalize_doi(paper.get("doi", ""))
        journal = paper.get("source") == "crossref" and doi and identifier == f"doi:{doi}"
        arxiv = isinstance(arxiv_id, str) and ARXIV_ID.fullmatch(arxiv_id) and identifier == f"arxiv:{arxiv_id}"
        if identifier in sent or not (journal or arxiv) or (doi and doi in sent_dois) or _identity(paper) in sent_titles:
            continue
        try:
            published, latest = date_span(paper.get("publish_date", ""))
        except (ValueError, TypeError):
            continue
        score = relevance(paper)
        if latest < earliest or published > today or score == 0:
            continue
        item = dict(paper, paper_id=identifier, rule_score=score, is_recent=published >= recent)
        # Canonical links come from validated IDs, never model prose.
        if journal:
            from urllib.parse import quote
            item["paper_url"] = "https://doi.org/" + quote(doi, safe="/:;()._-~")
            item["arxiv_url"] = ""
            item["pdf_url"] = ""
            if not item.get("abstract"):
                companion = next((p for p in db.values() if p.get("arxiv_id") and p.get("abstract")
                    and normalize_doi(p.get("doi", "")) == doi), None)
                if companion:
                    item["abstract"] = companion["abstract"]
                    item["abstract_source"] = "arxiv_same_doi"
        else:
            item["arxiv_url"] = f"https://arxiv.org/abs/{arxiv_id}"
            item["paper_url"] = item["arxiv_url"]
            item["pdf_url"] = f"https://arxiv.org/pdf/{arxiv_id}"
        eligible.append(item)
    # Prefer the journal version of the same work; retain only one version in
    # the paid shortlist. Title+first-author matching is deliberately exact.
    eligible.sort(key=lambda p: (p.get("source") == "crossref", bool(p.get("abstract"))), reverse=True)
    unique, dois, identities = [], set(), set()
    for item in eligible:
        doi, identity = normalize_doi(item.get("doi", "")), _identity(item)
        if (doi and doi in dois) or (identity and identity in identities):
            continue
        unique.append(item)
        if doi:
            dois.add(doi)
        if identity:
            identities.add(identity)
    unique.sort(key=lambda p: (p["is_recent"], p["rule_score"], p.get("source") == "crossref", p["publish_date"]), reverse=True)
    limit = config["shortlist_limit"]
    journal_slots = min(limit, max(0, int(config.get("journals", {}).get("shortlist_slots", 10))))
    journal_pool = [p for p in unique if p.get("source") == "crossref"][:journal_slots]
    reserved = {p["paper_id"] for p in journal_pool}
    shortlist = journal_pool + [p for p in unique if p["paper_id"] not in reserved][:limit - len(journal_pool)]
    shortlist.sort(key=lambda p: (p["is_recent"], p["rule_score"], p.get("source") == "crossref", p["publish_date"]), reverse=True)
    return shortlist


def _identity(paper: dict) -> str:
    title = re.sub(r"[^a-z0-9]", "", str(paper.get("title", "")).lower())
    author = re.sub(r"[^a-z0-9]", "", str(paper.get("authors", "")).split(",", 1)[0].lower())
    return f"{title}:{author}" if len(title) >= 30 and author else ""


def date_span(value: str) -> tuple[date, date]:
    """Keep month/year precision visible, without inventing a publication day."""
    if not isinstance(value, str):
        raise ValueError("Invalid publication date.")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        parsed = date.fromisoformat(value)
        return parsed, parsed
    if re.fullmatch(r"\d{4}-\d{2}", value):
        year, month = map(int, value.split("-"))
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    # A year-only date cannot establish a recently published paper.
    raise ValueError("Insufficient publication date precision.")


def collect(db: dict, today: date, config: dict) -> tuple[dict, int]:
    terms = " OR ".join(f'all:"{term}"' for term in config["keywords"])
    earliest = (today - timedelta(days=config["fallback_days"])).strftime("%Y%m%d")
    query = f'cat:cs.RO AND ({terms}) AND submittedDate:[{earliest}0000 TO {today:%Y%m%d}2359]'
    response = None
    for attempt in range(2):
        try:
            response = requests.get("https://export.arxiv.org/api/query", params={
                "search_query": query, "start": 0, "max_results": config["arxiv_max_results"],
                "sortBy": "submittedDate", "sortOrder": "descending",
            }, timeout=(10, 25), allow_redirects=False)
        except requests.RequestException:
            response = None
        if response is not None and response.status_code == 200:
            break
        if attempt < 1:
            status = f"HTTP {response.status_code}" if response is not None else "network timeout/error"
            print(f"arXiv search unavailable ({status}); retrying the public search.", flush=True)
            time.sleep(6)
    if response is None or response.status_code != 200:
        raise SourceUnavailable("arXiv API search could not be refreshed.")
    try:
        feed = ET.fromstring(response.content)
    except ET.ParseError:
        raise PipelineError("arXiv returned an unreadable feed.") from None
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    if feed.tag != "{http://www.w3.org/2005/Atom}feed":
        raise PipelineError("arXiv response is not an Atom feed.")
    merged = dict(db)
    count = 0
    for entry in feed.findall("atom:entry", ns):
        entry_id = entry.findtext("atom:id", default="", namespaces=ns)
        if "arxiv.org/api/errors" in entry_id or entry.findtext("atom:title", default="", namespaces=ns).strip().lower() == "error":
            raise PipelineError("arXiv returned an API error entry; no digest will be sent.")
        raw_id = entry_id.rsplit("/", 1)[-1]
        identifier = re.sub(r"v\d+$", "", raw_id)
        if not ARXIV_ID.fullmatch(identifier):
            continue
        key = f"arxiv:{identifier}"
        old = merged.get(key, {})
        title = " ".join(entry.findtext("atom:title", default="", namespaces=ns).split())
        abstract = " ".join(entry.findtext("atom:summary", default="", namespaces=ns).split())
        if not title or not abstract:
            continue
        paper = dict(old, paper_id=key, arxiv_id=identifier, title=title, abstract=abstract, date_basis="initial_publication",
            authors=", ".join(e.findtext("atom:name", default="", namespaces=ns) for e in entry.findall("atom:author", ns)),
            affiliations=list(dict.fromkeys(a.text.strip() for e in entry.findall("atom:author", ns)
                for a in e.findall("arxiv:affiliation", ns) if a.text and a.text.strip())),
            journal_ref=entry.findtext("arxiv:journal_ref", default=old.get("journal_ref", ""), namespaces=ns),
            doi=entry.findtext("arxiv:doi", default=old.get("doi", ""), namespaces=ns),
            publish_date=entry.findtext("atom:published", default="", namespaces=ns)[:10],
            arxiv_url=f"https://arxiv.org/abs/{identifier}", source="arxiv")
        comment = entry.findtext("arxiv:comment", default="", namespaces=ns)
        for url in re.findall(r"https://(?:github\.com|gitlab\.com)/[^\s,;]+", comment):
            paper["code_url"] = url.rstrip(".)]")
            break
        paper.setdefault("matched_categories", ["Dexterous-Hand"])
        paper.setdefault("matched_keywords", [])
        merged[key] = paper
        count += 1
    return merged, count


def _score(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
        raise PipelineError("Model returned an invalid recommendation score.")
    return float(value)


def ranked_papers(result: dict, papers: list[dict], minimum: int) -> list[dict]:
    lookup = {p["paper_id"]: p for p in papers}
    rows = result.get("rankings")
    if not isinstance(rows, list):
        raise PipelineError("Ranking response lacks its rankings list.")
    output, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or row.get("paper_id") not in lookup or row["paper_id"] in seen:
            raise PipelineError("Ranking response contains an unknown or duplicate paper ID.")
        seen.add(row["paper_id"])
        score = _score(row.get("score"))
        if score >= minimum:
            output.append(dict(lookup[row["paper_id"]], ranking_score=score))
    if seen != set(lookup):
        raise PipelineError("Ranking response did not cover all candidate papers.")
    return sorted(output, key=lambda p: p["ranking_score"], reverse=True)


def final_papers(result: dict, evidence: list[dict], config: dict) -> list[dict]:
    lookup = {p["paper_id"]: p for p in evidence}
    rows = result.get("papers")
    if not isinstance(rows, list) or len(rows) != len(evidence) or len(rows) > config["target_count"]:
        raise PipelineError("Summary response returned an invalid paper count.")
    output, seen = [], set()
    limits = {"title_zh": 160, "summary": 320, "why_for_you": 200, "learning_action": 180, "evidence": 160}
    for row in rows:
        if not isinstance(row, dict) or row.get("paper_id") not in lookup or row["paper_id"] in seen:
            raise PipelineError("Summary response contains an unknown or duplicate paper ID.")
        seen.add(row["paper_id"])
        source = lookup[row["paper_id"]]
        # Selection is already settled by the abstract ranking. Summaries cannot
        # silently re-rank, replace or omit its selected papers.
        priority = _score(source.get("ranking_score"))
        if priority < config["minimum_score"]:
            continue
        selected = {key: source.get(key, "") for key in ("paper_id", "arxiv_id", "title", "arxiv_url", "paper_url", "pdf_url", "code_url", "publish_date", "date_basis", "date_precision", "authors", "affiliations", "journal_ref", "journal_name", "publication_status", "source", "doi")}
        for field, limit in limits.items():
            value = row.get(field)
            if not isinstance(value, str) or not value.strip():
                raise PipelineError(f"Summary response lacks required text: {field}.")
            selected[field] = concise_text(value, limit)
        selected["priority_score"] = priority
        selected["reading_depth"] = source["enrichment_note"]
        selected["reading_depth_code"] = source["reading_depth"]
        selected["is_recent"] = source["is_recent"]
        output.append(selected)
    if seen != set(lookup):
        raise PipelineError("Summary response did not cover all selected papers.")
    return sorted(output, key=lambda p: p["priority_score"], reverse=True)


def concise_text(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    prefix = value[:limit]
    boundary = max(prefix.rfind(mark) for mark in ("。", "！", "？", "\n"))
    return prefix[:boundary + 1] if boundary >= limit // 2 else value[:limit - 1] + "…"


RANK_PROMPT = """你是面向灵巧末端设计与控制初学研究者的论文筛选助手。
按给定个人背景选最有学习和研究帮助的论文，评价摘要能支持的贡献，不把热门度当质量。
评分0到100：课题关联35、对机构/传感/控制基础的帮助25、摘要的方法与验证描述20、复现线索15、近期性5。
只根据标题、摘要、日期和提供的作者/发表元数据判断阅读价值，不把摘要声称的实验视为已核实。
作者背景只能作辅助信号；作者署名或机构名称不能证明权威性，不凭记忆编造引用数、头衔或同领域成果。
没有可核实背景资料时标记未核实，不因缺少作者信息扣分；Crossref白名单期刊元数据可作为正式发表登记，不能由此推断实验真实或论文必然优秀。
arXiv的机构/期刊/DOI字段是来源登记，未独立核实。摘要缺失时只评标题的关联与阅读线索，不编造方法或结果，并降低信息充分性分项。
日期只精确到月份时不能声称今天刚发表；first_seen/indexed日期不能当发表日期。
abstract_source=arxiv_same_doi时提供的是同一DOI对应预印本的摘要，可能与期刊最终版本不同，不能声称读过期刊正文。
通用VLA/机械臂、没有直接灵巧手贡献的论文降低分数；综述或硬件设计若适合建立基础可以高分。
至少65分才值得推送。仅使用提供的paper_id，覆盖候选列表每一项，包括低分项，不生成链接。
必须返回required_count个评分，不能只列推荐项。每次请求是一小批候选，按固定评分尺度评分。
返回JSON：{\"rankings\":[{\"paper_id\":\"arxiv:...\",\"score\":80,\"reason\":\"简短理由\"}]}。"""

SUMMARY_PROMPT = """你是灵巧手设计与控制研究导师。输入是已经按摘要评分选好的论文，仅为这些论文写中文阅读推荐。
只依据标题、摘要、日期和来源提供的作者/发表元数据；未下载或读取正文，不描述为全文精读或实验核验。
必须为required_count篇逐一提供总结，不能重新筛选、遗漏、替换或改变评分。只使用给定paper_id，不生成链接。
作者权威性仅能依据提供的可核实背景资料辅助说明，不能凭姓名/机构或记忆编造资历、引用数和录用状态。
source=crossref且journal_name来自白名单时可说明期刊元数据登记；其他来源journal_ref或DOI未独立核实。
缺摘要时summary和evidence明确“暂无摘要，仅依据标题”，不得编造技术路径、性能或结论；learning_action优先核对摘要/方法与平台依赖。
用简明中文写：title_zh译名；summary核心技术路径(≤300字)；why_for_you对背景的帮助(≤180字)；
learning_action一个可以实施的阅读/仿真/标定/复现实验步骤(≤160字)，必须注明额外传感器或平台依赖，
不要假设Aero Hand具备力矩传感器、触觉或力控功能，也不能凭缺少资料断言它一定不具备某项能力，应说明需要核对硬件；evidence用一句简明中文转述摘要中的具体描述(≤110字)，不输出长英文引文，不得编造正文页码。
不以模型分数宣称论文优秀或已通过同行评审。
返回JSON：{\"papers\":[{\"paper_id\":\"arxiv:...\",\"title_zh\":\"...\",\"summary\":\"...\",
\"why_for_you\":\"...\",\"learning_action\":\"...\",\"evidence\":\"...\"}]}。"""


def input_groups(items: list[dict], context: dict, max_chars: int, max_items: int) -> list[list[dict]]:
    """Bound each request without dropping or truncating a paper's evidence."""
    if max_chars <= 0 or max_items <= 0:
        raise PipelineError("Invalid request batch limits.")
    groups, current = [], []
    for item in items:
        candidate = current + [item]
        serialized = json.dumps(dict(context, candidates=candidate), ensure_ascii=False)
        if len(serialized) > max_chars or len(candidate) > max_items:
            if current:
                groups.append(current)
            current = [item]
            if len(json.dumps(dict(context, candidates=current), ensure_ascii=False)) > max_chars:
                raise PipelineError("One paper's metadata exceeds the request input budget.")
        else:
            current = candidate
    if current:
        groups.append(current)
    return groups


def report(digest: dict) -> str:
    lines = [f"# 灵巧手论文日报 · {digest['date']}", "", digest.get("note", ""), "",
        f"模型：{digest.get('model', '未登记')}；分数是针对个人学习价值的推荐依据，非学术质量认证。", ""]
    for index, p in enumerate(digest["papers"], 1):
        lines.extend([f"## {index}. {p['title_zh']}", "", p["title"], "",
            f"**作者**：{p.get('authors') or '来源未提供'}（背景权威性未独立核实）", "",
            f"**来源**：{p.get('journal_name') or 'arXiv 预印本'} · {'公告日期' if p.get('date_basis') == 'rss_announcement' else '发表日期'}：{p['publish_date']} · 阅读范围：{p['reading_depth']}", "",
            f"**核心方法**：{p['summary']}", "", f"**为什么适合你**：{p['why_for_you']}", "",
            f"**可积累的实验经验**：{p['learning_action']}", "", f"**依据**：{p['evidence']}", "",
            f"[论文]({p.get('paper_url') or p['arxiv_url']})", ""])
        if p.get("pdf_url"):
            lines.extend([f"[PDF（按需自行打开）]({p['pdf_url']})", ""])
        if p.get("code_url"):
            # The card renderer checks links; archive only official source links.
            from .delivery import _safe_link
            url = _safe_link(p["code_url"])
            if url:
                lines.extend([f"[公开代码]({url})", ""])
    if not digest["papers"]:
        lines.extend(["今天没有达到推荐门槛且尚未发送的论文。", ""])
    extra = digest.get("other_journal_papers", [])
    if extra:
        lines.extend(["## 其他相关期刊论文", "", "以下仅列元数据，未进入本次五篇精选；供回查，未进行正文阅读。", ""])
        for p in extra:
            lines.append(f"- [{p['title']}]({p['paper_url']}) · {p['journal_name']} · {p['publish_date']}")
        lines.append("")
    return "\n".join(lines)


def _outputs(**values) -> None:
    destination = os.environ.get("GITHUB_OUTPUT")
    if destination:
        with open(destination, "a", encoding="utf-8") as stream:
            for key, value in values.items():
                stream.write(f"{key}={str(value).lower()}\n")


def attempt_id() -> str:
    return os.environ.get("GITHUB_RUN_ID", "local-preview") + "-" + os.environ.get("GITHUB_RUN_ATTEMPT", "1")


def archive_link(today: date) -> str:
    """Use the deploying repository, never the original fork's address."""
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not repository:
        return ""  # Local previews have no published archive to link to.
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repository) or repository.rsplit("/", 1)[1] in {".", ".."}:
        raise PipelineError("Invalid deployment repository for the archive link.")
    return f"https://github.com/{repository}/blob/main/docs/digests/{today.isoformat()}.md"


def prepare(root: Path, config_path: Path, output: Path, preview: bool, retry_uncertain: bool) -> None:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not 1 <= config["target_count"] <= 5:
        raise PipelineError("Invalid digest configuration.")
    llm_config = config["llm"]
    if not llm_config["base_url"].startswith("https://"):
        raise PipelineError("LLM endpoint must use HTTPS.")
    if not all(isinstance(llm_config.get(key), str) and llm_config[key].strip() for key in ("ranking_model", "summary_model")):
        raise PipelineError("Both model stages need explicit model names.")
    run_id = attempt_id()
    today = datetime.now(ZoneInfo(config["timezone"])).date()
    state = load_state(root)
    _outputs(should_send=False)
    if state["last_sent_date"] == today.isoformat():
        print("A digest was already confirmed today; skipping without model calls.")
        return
    pending = load_json(root / ".daily/pending.json", {})
    db = load_json(root / "docs/papers_db.json", {})
    if not isinstance(db, dict):
        raise StateError("Invalid source paper database.")
    output.mkdir(parents=True, exist_ok=True)
    if pending and pending.get("status") != "complete":
        pending = resume_pending(pending, run_id, retry_uncertain)
        print("Resuming the saved batch; already acknowledged cards will be skipped.")
    else:
        source_note = "来源：arXiv API 与近期数据库。"
        sources_unavailable = False
        try:
            db, fetched = collect(db, today, config)
        except SourceUnavailable:
            try:
                db, fetched = collect_rss(db, today)
                source_note = f"arXiv API 暂不可用；官方 RSS 本次含{fetched}条可合并公告，结合近期缓存筛选。"
            except StateError:
                fetched = 0
                sources_unavailable = True
                source_note = "arXiv API/RSS 本次刷新失败；arXiv 部分使用已保存的最近30天缓存，未确认今日 arXiv 更新。"
        before_journals = set(db)
        db, journal_count, journal_note = collect_journals(db, today, config)
        # Keep the inherited database and all newly discovered relevant papers;
        # unrelated journal metadata does not accumulate in the public digest.
        db = {key: p for key, p in db.items() if key in before_journals or relevance(p) > 0}
        source_note += journal_note
        shortlist = candidates(db, state, today, config)
        if sources_unavailable and not shortlist and not journal_count:
            raise PipelineError("Paper sources unavailable and no recent unsent cache exists; no empty digest will be sent.")
        print(f"Source refresh: arXiv {fetched}, journal metadata {journal_count}; {len(shortlist)} relevant, unsent candidates.", flush=True)
        selected, usage = [], {}
        if shortlist:
            api_key = os.environ.get("LLM_API_KEY", "")
            if not api_key:
                raise PipelineError("LLM_API_KEY is not configured in repository Secrets.")
            client = LLMClient(llm_config["base_url"], llm_config["ranking_model"], api_key,
                reasoning_effort=llm_config["reasoning_effort"], timeout=llm_config["timeout_seconds"], api_style=llm_config["api_style"],
                off_peak_only=os.environ.get("GITHUB_EVENT_NAME") == "schedule")
            metadata_fields = ("paper_id", "title", "abstract", "abstract_source", "authors", "affiliations", "journal_ref", "journal_name", "source", "publication_status", "doi", "publish_date", "date_basis", "date_precision", "code_url", "is_recent")
            briefs = [{k: p.get(k, "") for k in metadata_fields} for p in shortlist]
            context = {"profile": config["profile"]}
            rank_context = dict(context, required_count=llm_config["ranking_batch_size"])
            groups = input_groups(briefs, rank_context, llm_config["max_input_chars"], llm_config["ranking_batch_size"])
            print(f"Ranking metadata with {llm_config['ranking_model']} in {len(groups)} bounded batches.", flush=True)
            all_rankings = []
            for index, group in enumerate(groups, 1):
                print(f"Ranking batch {index}/{len(groups)}: {len(group)} papers.", flush=True)
                first = client.generate_json(RANK_PROMPT, json.dumps(dict(rank_context, required_count=len(group), candidates=group), ensure_ascii=False), max_output_tokens=6144)
                # Validate every batch before accepting any of its recommendations.
                ranked_papers(first, group, config["minimum_score"])
                all_rankings.extend(first["rankings"])
            ranked = ranked_papers({"rankings": all_rankings}, shortlist, config["minimum_score"])[:config["target_count"]]
            if ranked:
                selected_sources = [dict(p, reading_depth="abstract_only" if p.get("abstract") else "title_only",
                    enrichment_note=("采用同 DOI 的 arXiv 预印本摘要；期刊正文未读取。" if p.get("abstract_source") == "arxiv_same_doi" else "仅依据标题、摘要、日期及来源元数据；未读取正文。") if p.get("abstract") else "暂无摘要，仅依据标题与来源元数据；未读取正文。") for p in ranked]
                inputs = [{k: p.get(k, "") for k in (*metadata_fields, "ranking_score")} for p in selected_sources]
                summary_context = dict(context, required_count=llm_config["summary_batch_size"])
                groups = input_groups(inputs, summary_context, llm_config["max_input_chars"], llm_config["summary_batch_size"])
                print(f"Summarizing {len(ranked)} selected papers with {llm_config['summary_model']} in {len(groups)} bounded batches; no PDF requests.", flush=True)
                source_by_id = {p["paper_id"]: p for p in selected_sources}
                for index, group in enumerate(groups, 1):
                    print(f"Summary batch {index}/{len(groups)}: {len(group)} papers.", flush=True)
                    if llm_config["summary_model"] != client.model:
                        client.model = llm_config["summary_model"]
                    final = client.generate_json(SUMMARY_PROMPT, json.dumps(dict(summary_context, required_count=len(group), candidates=group), ensure_ascii=False), max_output_tokens=8192)
                    selected.extend(final_papers(final, [source_by_id[p["paper_id"]] for p in group], config))
                selected.sort(key=lambda p: p["priority_score"], reverse=True)
            usage = client.usage
        older = sum(not p["is_recent"] for p in selected)
        note = source_note + "优先最近7天；必要时从最近30天未发送论文补充。"
        note += "仅依据标题/摘要与元数据，未读取正文；作者背景未独立核实。"
        if older:
            note += f"本次有{older}篇近期补充，请结合发表日期阅读。"
        if len(selected) < config["target_count"]:
            note += f"本次达到推荐门槛的论文有{len(selected)}篇。"
        selected_ids = {p["paper_id"] for p in selected}
        extras = [p for p in shortlist if p.get("source") == "crossref" and p["paper_id"] not in selected_ids]
        if extras:
            note += f"另有{len(extras)}篇相关期刊候选，标题与链接见当天归档。"
        card_note = "来源：arXiv 与五本期刊的公开元数据。仅依据标题/摘要筛选，未读取正文；作者背景未独立核实。"
        if older:
            card_note += f"有{older}篇来自近30天的补充论文。"
        if sources_unavailable or "来源失败" in journal_note or "分页受限" in journal_note:
            card_note += "部分检索覆盖受限，详细情况见归档。"
        if extras:
            card_note += f"归档另列{len(extras)}篇相关期刊候选。"
        digest = {"date": today.isoformat(), "papers": selected, "note": note,
            "model": llm_config["summary_model"], "other_journal_papers": extras, "card_note": card_note,
            "archive_url": archive_link(today)}
        cards = build_cards(digest)
        batch_id = hashlib.sha256(json.dumps(digest, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
        pending = {"version": 1, "batch_id": batch_id, "date": today.isoformat(), "status": "inflight",
            "attempt_run_id": run_id, "paper_ids": [p["paper_id"] for p in selected], "acknowledged_cards": [],
            "cards": cards, "report": report(digest), "usage": usage}
    validate_pending(pending)
    write_json(output / "state.json", state)
    write_json(output / "pending.json", pending)
    write_json(output / "papers_db.json", db)
    (output / "preview.md").write_text(pending["report"], encoding="utf-8")
    _outputs(should_send=not preview, prepared=True)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as stream:
            stream.write(pending["report"] + "\n\nToken usage: `" + json.dumps(pending.get("usage", {})) + "`\n")
    print(f"Prepared {len(pending['paper_ids'])} papers in {len(pending['cards'])} card(s). Preview={preview}.", flush=True)
    print("Validated paper IDs: " + ", ".join(pending["paper_ids"]), flush=True)


def deliver(source: Path, output: Path) -> None:
    state = load_json(source / "state.json")
    from .state import validate_state
    validate_state(state)
    pending = load_json(source / "pending.json")
    validate_pending(pending)
    run_id = attempt_id()
    if pending.get("attempt_run_id") != run_id or pending["status"] != "inflight":
        raise StateError("This delivery lacks a matching persisted intent.")
    output.mkdir(parents=True, exist_ok=True)
    # A durable intent was committed before this job. If this runner disappears,
    # the next run requires reconciliation instead of blindly replaying it.
    write_json(output / "state.json", state)
    write_json(output / "pending.json", pending)
    try:
        for index, card in enumerate(pending["cards"]):
            if index in pending["acknowledged_cards"]:
                continue
            send_card(os.environ.get("FEISHU_WEBHOOK", ""), os.environ.get("FEISHU_SIGN_SECRET", ""), card)
            pending["acknowledged_cards"].append(index)
            write_json(output / "pending.json", pending)
        acknowledge_all(state, pending)
        print(f"Feishu confirmed {len(pending['cards'])} card(s), {len(pending['paper_ids'])} papers.", flush=True)
    except DeliveryUncertainError:
        pending["status"] = "uncertain"
        raise
    except DeliveryError:
        pending["status"] = "failed"
        raise
    finally:
        write_json(output / "state.json", state)
        write_json(output / "pending.json", pending)
