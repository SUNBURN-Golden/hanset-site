"""Read the operational vocabulary as literals, never import its package.

This is presentation metadata only. It grants no publication authority to values.
Missing ontology is an explicit disabled state while the base stack is reconciled.
"""
import ast
from pathlib import Path


class CatalogError(ValueError):
    pass


CONSTANTS = ("ENTITY_TYPES", "FACILITY_KINDS", "REQUIRED_SLOTS")
PRIORITY = ("usage_condition", "price_plan", "hours", "facility", "program")
# English descriptors are presentation policy, not values inferred from prose.
DESCRIPTORS = {
    ("usage_condition", "tattoo", "policy"): ("tattoo", ("allowed", "cover_required", "not_allowed", "conditional")),
    ("usage_condition", "foreigner", "policy"): ("foreigner", ("accepted", "id_required", "restricted", "english_guide")),
    ("usage_condition", "access", "policy"): ("access", ("public", "reservation", "guest_only", "member_only", "conditional")),
    ("price_plan", "day_pass", "amount"): ("price", ("known",)),
    ("hours", "regular", "open_hours"): ("sunday", ("yes", "no")),
}


def read_literals(path):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    found = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in CONSTANTS:
                if target.id in found:
                    raise CatalogError("duplicate_catalog_constant")
                try:
                    found[target.id] = ast.literal_eval(node.value)
                except (ValueError, TypeError) as exc:
                    raise CatalogError("nonliteral_catalog_constant") from exc
    if set(found) != set(CONSTANTS):
        raise CatalogError("missing_catalog_constant")
    for key in ("ENTITY_TYPES", "FACILITY_KINDS"):
        values = found[key]
        if not isinstance(values, (list, tuple)) or not values or any(
                not isinstance(v, str) or not v.isidentifier() for v in values):
            raise CatalogError("invalid_catalog_enum")
        if len(values) != len(set(values)):
            raise CatalogError("duplicate_catalog_enum")
    slots = found["REQUIRED_SLOTS"]
    if not isinstance(slots, (list, tuple)) or any(
            not isinstance(s, (list, tuple)) or len(s) != 3 or any(
                not isinstance(v, str) or not v.isidentifier() for v in s)
            or s[0] not in found["ENTITY_TYPES"]
            or (s[0] == "facility" and s[1] not in found["FACILITY_KINDS"])
            for s in slots):
        raise CatalogError("invalid_required_slots")
    if len(slots) != len(set(map(tuple, slots))):
        raise CatalogError("duplicate_required_slot")
    return found


def generate(path, branches=()):
    path = Path(path)
    if not path.exists():
        return {"schema_version": 1, "available": False, "reason": "missing_ontology",
                "sections": [], "facets": []}
    schema = read_literals(path)
    slots = set(map(tuple, schema["REQUIRED_SLOTS"]))
    facets = []
    for slot, (key, options) in DESCRIPTORS.items():
        if slot in slots:
            facets.append({"key": key, "slot": list(slot), "options": list(options),
                           "renderer_supported": True})
    # Keep zero-coverage and new kinds visible. Unknown kinds have no renderer
    # permission; layout is a separate list section, never a presence checkbox.
    supported = {s[1] for s in slots if s[0] == "facility" and s[2] == "exists"}
    supported &= {"dry_sauna", "wet_sauna", "sauna", "kiln_sauna", "cold_plunge",
                  "rapid_cold_plunge", "ice_bath", "warm_bath", "hot_bath"}
    facets.append({"key": "facility", "options": [
        {"value": kind, "label": kind.replace("_", " ") if kind in supported else "Not confirmed",
         "renderer_supported": kind in supported}
        for kind in schema["FACILITY_KINDS"] if kind != "layout"]})
    for key in ("city", "type"):
        facets.append({"key": key, "options": sorted({b[key] for b in branches if b.get(key)})})
    return {"schema_version": 1, "available": True,
            "sections": [t for t in PRIORITY if t in schema["ENTITY_TYPES"] and any(s[0] == t for s in slots)],
            "slots": [list(s) for s in schema["REQUIRED_SLOTS"]], "facets": facets}
