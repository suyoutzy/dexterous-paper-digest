"""Durable send intents and an allowlisted, data-only artifact publisher."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
PENDING_STATUSES = {"inflight", "failed", "uncertain", "complete"}


class StateError(RuntimeError):
    pass


def load_json(path: Path, default=None):
    if not path.exists():
        if default is None:
            raise StateError(f"Required data file is missing: {path.name}")
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise StateError(f"Data file cannot be read safely: {path.name}") from None


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def validate_state(state: dict) -> None:
    if not isinstance(state, dict) or state.get("version") != 1:
        raise StateError("Unsupported send-state format.")
    sent = state.get("sent_ids")
    if not isinstance(sent, list) or any(not isinstance(p, str) or len(p) > 600 for p in sent):
        raise StateError("Invalid sent-paper identifiers.")
    if state.get("last_sent_date") and not DATE.fullmatch(state["last_sent_date"]):
        raise StateError("Invalid last delivery date.")


def validate_pending(pending: dict) -> None:
    if not isinstance(pending, dict) or pending.get("version") != 1:
        raise StateError("Unsupported pending-batch format.")
    if pending.get("status") not in PENDING_STATUSES or not DATE.fullmatch(str(pending.get("date", ""))):
        raise StateError("Invalid pending-batch status or date.")
    cards = pending.get("cards")
    if not isinstance(cards, list) or not 1 <= len(cards) <= 6:
        raise StateError("Invalid pending cards.")
    for card in cards:
        if not isinstance(card, dict) or card.get("msg_type") != "interactive" or not isinstance(card.get("card"), dict):
            raise StateError("Invalid card format in pending batch.")
        if len(json.dumps(card, ensure_ascii=False).encode("utf-8")) > 20000:
            raise StateError("Pending card exceeds its size limit.")
    acknowledged = pending.get("acknowledged_cards", [])
    if not isinstance(acknowledged, list) or any(type(i) is not int or not 0 <= i < len(cards) for i in acknowledged):
        raise StateError("Invalid card acknowledgement indexes.")
    if not isinstance(pending.get("paper_ids"), list) or any(not isinstance(p, str) or len(p) > 600 for p in pending["paper_ids"]):
        raise StateError("Invalid pending-paper identifiers.")


def load_state(root: Path) -> dict:
    state = load_json(root / ".daily/state.json", {"version": 1, "sent_ids": [], "last_sent_date": ""})
    validate_state(state)
    return state


def resume_pending(pending: dict, run_id: str, retry_uncertain: bool = False) -> dict:
    validate_pending(pending)
    if pending["status"] == "complete":
        raise StateError("A completed batch cannot be replayed.")
    if pending["status"] in {"inflight", "uncertain"} and not retry_uncertain:
        raise StateError("Previous delivery is unconfirmed. Check the Feishu group, then use the explicit retry option if resending is necessary.")
    result = dict(pending)
    result.update(status="inflight", attempt_run_id=run_id)
    return result


def acknowledge_all(state: dict, pending: dict) -> None:
    if set(pending["acknowledged_cards"]) != set(range(len(pending["cards"]))):
        raise StateError("Cannot mark papers sent before every card is acknowledged.")
    state["sent_ids"] = sorted(set(state["sent_ids"]) | set(pending["paper_ids"]))
    state["last_sent_date"] = pending["date"]
    pending["status"] = "complete"


def publish(source: Path, root: Path, phase: str, expected_run_id: str) -> None:
    """Copy only named JSON/Markdown data, never execute or copy artifact code."""
    if source.is_symlink():
        raise StateError("Artifact directories cannot be symlinks.")
    for name in ("state.json", "pending.json"):
        if (source / name).is_symlink():
            raise StateError("Artifact files cannot be symlinks.")
    state = load_json(source / "state.json")
    pending = load_json(source / "pending.json")
    validate_state(state)
    validate_pending(pending)
    if pending.get("attempt_run_id") != expected_run_id:
        raise StateError("Artifact belongs to another workflow run.")
    if phase == "plan":
        if pending["status"] != "inflight":
            raise StateError("Only a delivery intent can be published before sending.")
        current_state = load_state(root)
        if current_state.get("last_sent_date") == pending["date"]:
            raise StateError("A delivery for this date is already confirmed; stale plans cannot overwrite it.")
        if set(pending["paper_ids"]) & set(current_state["sent_ids"]):
            raise StateError("A stale plan contains papers that have already been sent.")
        db_path = source / "papers_db.json"
        if db_path.is_symlink():
            raise StateError("Database artifact cannot be a symlink.")
        db = load_json(db_path)
        if not isinstance(db, dict) or any(not isinstance(p, dict) for p in db.values()):
            raise StateError("Invalid paper database artifact.")
        write_json(root / "docs/papers_db.json", db)
    else:
        original = load_json(root / ".daily/pending.json")
        if original.get("batch_id") != pending.get("batch_id") or original.get("attempt_run_id") != expected_run_id:
            raise StateError("Delivery result does not match the persisted intent.")
        if pending["status"] == "complete":
            if set(pending["acknowledged_cards"]) != set(range(len(pending["cards"]))):
                raise StateError("Complete batch lacks acknowledgements.")
            report = pending.get("report", "")
            if not isinstance(report, str) or len(report) > 50000:
                raise StateError("Invalid digest archive.")
            archive = root / "docs/digests" / f"{pending['date']}.md"
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_text(report, encoding="utf-8")
    write_json(root / ".daily/state.json", state)
    write_json(root / ".daily/pending.json", pending)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--phase", choices=("plan", "result"), required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        publish(args.source, args.root, args.phase, args.run_id)
    except StateError as error:
        parser.exit(1, str(error) + "\n")
