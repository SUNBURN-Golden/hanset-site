"""Draft read-only operations-export consumer; no legacy prose classification.

Contract version 1 must be approved/issued by operations before UI activation.
No DB access, package import, candidate quotes or contact information is exported.
"""
import datetime as dt
import json
import math
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


class ExportError(ValueError):
    pass


KST = ZoneInfo("Asia/Seoul")
REQUIRED = {"public_slug", "branch_id", "entity_type", "kind", "attribute", "value",
            "unit", "qualifiers", "value_text", "json_pointer", "status", "source_type",
            "source_url", "evidence_locator_id", "confirmed_on", "effective_from", "effective_to",
            "conflict_state", "quality_clear", "rights", "publication"}


def instant(value):
    try:
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(KST)
    except (TypeError, ValueError) as exc:
        raise ExportError("invalid_export_instant") from exc


def _date(value):
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ExportError("invalid_confirmation_date") from exc


def evaluate(row, cutoff):
    """Return sanitized state. Only approved official values survive this gate."""
    if row.get("status") not in {"fact", "source_claim", "unverified", "ai_inference"}:
        raise ExportError("unsupported_fact_status")
    if row["status"] != "fact" or row.get("publication") != "approved":
        return {"state": "unknown"}
    if not REQUIRED <= row.keys():
        raise ExportError("incomplete_claimed_fact")
    try:
        if not isinstance(row["source_url"], str):
            raise ValueError()
        url = urlsplit(row["source_url"])
        valid_url = (url.scheme == "https" and url.hostname and not url.username and not url.password)
        if url.port is not None and url.port != 443:
            valid_url = False
    except ValueError as exc:
        raise ExportError("invalid_source_url") from exc
    if (row["source_type"] != "own_site" or not valid_url or row["rights"] != "public_page_factual"
            or not row["evidence_locator_id"] or not row["json_pointer"]
            or not isinstance(row["value_text"], str) or not row["value_text"].strip()
            or not isinstance(row["qualifiers"], dict)):
        raise ExportError("invalid_claimed_provenance")
    confirmed = _date(row["confirmed_on"])
    start = instant(row["effective_from"])
    end = instant(row["effective_to"]) if row["effective_to"] is not None else None
    if end is not None and end <= start:
        raise ExportError("invalid_effective_interval")
    if confirmed > cutoff.date() or start > cutoff or (end is not None and cutoff >= end):
        return {"state": "unknown"}
    if row["conflict_state"] not in {"clear", "unresolved"}:
        raise ExportError("invalid_conflict_state")
    if row["conflict_state"] == "unresolved":
        return {"state": "conflict"}
    if row["quality_clear"] is not True or row["value"] is None:
        return {"state": "unknown"}
    if row["entity_type"] == "hours" and row["kind"] == "closure":
        until = row["qualifiers"].get("until")
        if until is not None and _date(until) < cutoff.date():
            return {"state": "unknown"}
    value = row["value"]
    if row["attribute"] == "exists" and type(value) is not bool:
        raise ExportError("invalid_presence_value")
    if row["attribute"] == "amount":
        if (type(value) not in (int, float) or not math.isfinite(value) or value < 0
                or row["unit"] != "KRW" or not all(k in row["qualifiers"] for k in
                    ("ticket", "scope", "audience", "duration_hours"))):
            raise ExportError("invalid_scoped_amount")
    state = "stale" if (cutoff.date() - confirmed).days > 90 else "known"
    return {"state": state, "value": value, "unit": row["unit"],
            "qualifiers": row["qualifiers"], "text": row["value_text"],
            "source_url": row["source_url"], "confirmed_on": row["confirmed_on"]}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ExportError("duplicate_export_key")
        result[key] = value
    return result


def read_export(path, catalog, public_slugs, cutoff):
    """Missing export disables capability; malformed exports fail the build.

    Branch mapping is operations-issued, not guessed from domains or titles.
    Preserve scopes and distinct evidence rows; never resolve conflicts here.
    """
    if not Path(path).exists():
        return {"available": False, "reason": "missing_operations_export", "slots": []}
    if not catalog["available"]:
        raise ExportError("export_requires_ontology")
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ExportError("invalid_export_json") from exc
    if (not isinstance(doc, dict) or type(doc.get("schema_version")) is not int
            or doc["schema_version"] != 1 or not isinstance(doc.get("branches"), dict)
            or not isinstance(doc.get("slots"), list)):
        raise ExportError("unsupported_export_schema")
    snapshot = instant(doc.get("cutoff"))
    # A future snapshot cannot authorize today's claims.
    if snapshot > cutoff:
        raise ExportError("future_export_cutoff")
    bridge = doc["branches"]
    if any(slug not in public_slugs or not isinstance(bid, str) or not bid for slug, bid in bridge.items()):
        raise ExportError("invalid_branch_bridge")
    if len(set(bridge.values())) != len(bridge):
        raise ExportError("ambiguous_branch_bridge")
    allowed_slots = set(map(tuple, catalog["slots"]))
    output = []
    for row in doc["slots"]:
        if not isinstance(row, dict):
            raise ExportError("invalid_export_row")
        slug = row.get("public_slug")
        slot = tuple(row.get(k) for k in ("entity_type", "kind", "attribute"))
        if (not isinstance(slug, str) or slug not in bridge or row.get("branch_id") != bridge[slug]
                or any(not isinstance(k, str) for k in slot) or slot not in allowed_slots):
            raise ExportError("unmapped_export_slot")
        result = evaluate(row, cutoff)
        # Also check the export's own assertion date: later confirmation cannot
        # be smuggled into an older, allegedly consistent snapshot.
        if result["state"] in {"known", "stale"} and (
                _date(row["confirmed_on"]) > snapshot.date() or instant(row["effective_from"]) > snapshot):
            raise ExportError("fact_after_snapshot")
        output.append({"public_slug": slug, "slot": list(slot), **result})
    return {"available": True, "slots": output}
