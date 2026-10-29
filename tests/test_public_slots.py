"""Synthetic evidence cases only; no fixtures become public site data."""
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "site"))
import public_slots as S


NOW = dt.datetime(2026, 10, 29, 12, tzinfo=S.KST)


def fact(**changes):
    return dict(public_slug="test-branch", branch_id="branch-1", entity_type="facility",
                kind="dry_sauna", attribute="exists", value=True, unit=None, qualifiers={},
                value_text="Synthetic test evidence", json_pointer="/test/value", status="fact",
                source_type="own_site", source_url="https://venue.example/facilities",
                evidence_locator_id="public-locator", confirmed_on="2026-10-18",
                effective_from="2026-10-18T00:00:00+09:00", effective_to=None,
                conflict_state="clear", quality_clear=True, rights="public_page_factual",
                publication="approved") | changes


def context(row):
    """Proposed contract test doubles, never an operations approval or site data."""
    def descriptor(slot, value, unit=None, qualifiers=None):
        return dict(slot=slot.split("/"), value=value, unit=unit, qualifiers=qualifiers or {})
    contract = dict(schema_version=1, descriptors=[
        descriptor("facility/dry_sauna/exists", {"type": "boolean"}),
        descriptor("facility/sauna/temperature_c", {"type": "number"}, "C"),
        descriptor("usage_condition/foreigner/policy", {"type": "enum", "enum": ["accepted", "id_required", "restricted", "english_guide"]}),
        descriptor("usage_condition/age/min_age", {"type": "integer", "minimum": 0}),
        descriptor("hours/closure/closure_rule", {"type": "string"}, qualifiers={"until": {"type": "date"}}),
        descriptor("price_plan/day_pass/amount", {"type": "number", "minimum": 0}, "KRW", {
            "ticket": {"type": "enum", "enum": ["day_pass"]},
            "scope": {"type": "enum", "enum": ["unspecified", "weekday", "weekend"]},
            "audience": {"type": "enum", "enum": ["adult_non_guest", "hotel_guest"]},
            "duration_hours": {"type": "number", "minimum": 0, "nullable": True}}),
    ], retained=[{key: row[key] for key in S.BINDING_FIELDS}])
    return contract, {"test": {"value": row["value_text"]}}


class PublicSlots(unittest.TestCase):
    def evaluate(self, row, **kwargs):
        contract, retained = context(fact() | row)
        return S.evaluate(row, NOW, contract=contract, retained_content=retained, **kwargs)

    def test_false_is_known_and_unknown_never_exposes_candidates(self):
        self.assertIs(self.evaluate(fact(value=False))["value"], False)
        for status in ("source_claim", "unverified", "ai_inference"):
            self.assertEqual(self.evaluate(fact(status=status, value="private candidate")), {"state": "unknown"})
        self.assertEqual(self.evaluate(fact(publication="hold")), {"state": "unknown"})

    def test_official_status_alone_never_authorizes_publication(self):
        for changes in ({"source_type": "directory"}, {"source_url": ""},
                        {"source_url": "https://user:password@[::1]/"},
                        {"source_url": []}, {"source_url": "https://[invalid/"},
                        {"rights": "internal_review_only"}, {"evidence_locator_id": ""},
                        {"evidence_locator_id": True}, {"evidence_locator_id": " "}, {"json_pointer": True},
                        {"confirmed_on": None}, {"qualifiers": []}, {"value_text": ""},
                        {"value": "true"}):
            with self.subTest(changes=changes), self.assertRaises(S.ExportError):
                self.evaluate(fact(**changes))
        row = fact(); del row["confirmed_on"]
        with self.assertRaises(S.ExportError): self.evaluate(row)

    def test_conflict_quality_null_and_unsupported_status(self):
        self.assertEqual(self.evaluate(fact(conflict_state="unresolved")), {"state": "conflict"})
        for changes in ({"quality_clear": False}, {"quality_clear": 1}, {"value": None}):
            self.assertEqual(self.evaluate(fact(**changes)), {"state": "unknown"})
        with self.assertRaises(S.ExportError): self.evaluate(fact(status="verified_fact"))

    def test_current_interval_date_boundary_and_staleness(self):
        self.assertEqual(self.evaluate(fact(effective_to=NOW.isoformat())), {"state": "unknown"})
        self.assertEqual(self.evaluate(fact(confirmed_on="2026-10-30")), {"state": "unknown"})
        self.assertEqual(self.evaluate(fact(effective_from="2026-10-29T04:00:00+00:00")), {"state": "unknown"})
        for days, expected in ((90, "known"), (91, "stale")):
            result = self.evaluate(fact(confirmed_on=(NOW.date()-dt.timedelta(days=days)).isoformat()))
            self.assertEqual(result["state"], expected)
            self.assertIn("source_url", result)
        with self.assertRaises(S.ExportError):
            self.evaluate(fact(effective_from="2026-10-18T00:00:00"))

    def test_expired_closure_never_means_reopened(self):
        row = fact(entity_type="hours", kind="closure", attribute="closure_rule", value="Test closure",
                   qualifiers={"until": "2026-10-28"})
        self.assertEqual(self.evaluate(row), {"state": "unknown"})
        row["qualifiers"]["until"] = "2026-10-29"
        self.assertEqual(self.evaluate(row)["state"], "known")

    def test_scoped_prices_remain_distinct_and_zero_is_valid(self):
        prices = []
        for audience in ("adult_non_guest", "hotel_guest"):
            row = fact(entity_type="price_plan", kind="day_pass", attribute="amount", value=0, unit="KRW",
                       qualifiers={"ticket": "day_pass", "scope": "unspecified", "audience": audience,
                                   "duration_hours": None})
            result = self.evaluate(row)
            self.assertEqual(result["value"], 0)
            prices.append(result)
        self.assertNotEqual(prices[0]["qualifiers"], prices[1]["qualifiers"])
        for changes in ({"value": True}, {"value": float("nan")}, {"value": -1},
                        {"unit": None}, {"qualifiers": {"ticket": "day_pass"}}):
            with self.subTest(changes=changes), self.assertRaises(S.ExportError):
                self.evaluate(row | changes)

    def read(self, doc, catalog=None):
        if catalog is None:
            catalog = {"available": True, "slots": [["facility", "dry_sauna", "exists"]]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "export.json"
            if doc is not None: path.write_text(json.dumps(doc))
            contract_path = Path(directory) / "contract.json"
            contract, retained = context(fact())
            contract_path.write_text(json.dumps(contract))
            return S.read_export(path, catalog, {"test-branch"}, NOW,
                                 contract_path=contract_path, retained_content=retained)

    def doc(self, **changes):
        return dict(schema_version=1, cutoff=NOW.isoformat(), branches={"test-branch": "branch-1"},
                    slots=[fact()]) | changes

    def test_missing_export_disabled_unsupported_versions_and_identity_rejected(self):
        self.assertFalse(self.read(None)["available"])
        self.assertTrue(self.read(self.doc())["available"])
        for doc in (self.doc(schema_version=2), self.doc(schema_version=True),
                    self.doc(branches={"private-branch": "branch-1"}), self.doc(slots=[fact(branch_id="other")]),
                    self.doc(slots=[fact(kind="unknown_kind")]), self.doc(slots=[fact(kind=[])]),
                    self.doc(cutoff="2026-10-30T00:00:00+09:00"),
                    self.doc(cutoff="2026-10-01T00:00:00+09:00")):
            with self.subTest(doc=doc), self.assertRaises(S.ExportError): self.read(doc)
        with self.assertRaises(S.ExportError): self.read(self.doc(), {"available": False})

    def test_internal_fields_never_exported_and_unknowns_have_no_provenance(self):
        row = fact(quote="internal quotation", origin_ref="private origin", review_notes="private notes")
        result = self.read(self.doc(slots=[row, fact(status="source_claim")]))
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("quote", json.dumps(result))
        self.assertEqual(set(result["slots"][1]), {"public_slug", "slot", "state"})

    def test_duplicate_json_keys_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "export.json"
            path.write_text('{"schema_version":1,"schema_version":2}')
            with self.assertRaises(S.ExportError):
                S.read_export(path, {"available": True}, {"test-branch"}, NOW)

    def test_missing_contract_or_descriptor_never_authorizes_a_value(self):
        row = fact()
        contract, retained = context(row)
        self.assertEqual(S.evaluate(row, NOW), {"state": "unknown"})
        contract["descriptors"] = []
        self.assertEqual(S.evaluate(row, NOW, contract=contract, retained_content=retained), {"state": "unknown"})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "export.json"
            path.write_text(json.dumps(self.doc()))
            result = S.read_export(path, {"available": True, "slots": [["facility", "dry_sauna", "exists"]]},
                                   {"test-branch"}, NOW)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "missing_operations_contract")
        self.assertEqual(result["slots"][0]["state"], "unknown")

    def test_missing_and_malformed_pointers_cannot_be_known(self):
        # Binding approval alone is insufficient: the content must exist too.
        for pointer in ("/does-not-exist", "/test/missing", "/test/value/child", "test/value", "/test/~2"):
            row = fact(json_pointer=pointer)
            contract, retained = context(row)
            with self.subTest(pointer=pointer), self.assertRaises(S.ExportError):
                S.evaluate(row, NOW, contract=contract, retained_content=retained)

    def test_pointer_escape_and_array_resolution_is_exact(self):
        document = {"a/b": {"~key": ["retained", False, 0]}}
        self.assertEqual(S.resolve_pointer(document, "/a~1b/~0key/0"), "retained")
        self.assertIs(S.resolve_pointer(document, "/a~1b/~0key/1"), False)
        self.assertEqual(S.resolve_pointer(document, "/a~1b/~0key/2"), 0)
        for tail in ("-1", "01", "-", "3", "99999999999999999999999"):
            with self.subTest(tail=tail), self.assertRaises(S.ExportError):
                S.resolve_pointer(document, "/a~1b/~0key/" + tail)

    def test_binding_rejects_other_branch_value_text_and_scope(self):
        row = fact()
        contract, retained = context(row)
        for changes in ({"public_slug": "other"}, {"branch_id": "branch-2"},
                        {"json_pointer": "/test/other"}, {"value": False},
                        {"value_text": "changed text"}, {"qualifiers": {"audience": "hotel_guest"}}):
            with self.subTest(changes=changes), self.assertRaises(S.ExportError):
                S.evaluate(row | changes, NOW, contract=contract, retained_content=retained)
        with self.assertRaisesRegex(S.ExportError, "mismatched_retained_value"):
            S.evaluate(row, NOW, contract=contract, retained_content={"test": {"value": "changed content"}})
        with self.assertRaisesRegex(S.ExportError, "missing_retained_content"):
            S.evaluate(row, NOW, contract=contract)

    def test_price_binding_does_not_allow_scope_reassignment(self):
        row = fact(entity_type="price_plan", kind="day_pass", attribute="amount", value=0, unit="KRW",
                   qualifiers={"ticket": "day_pass", "scope": "weekday", "audience": "adult_non_guest",
                               "duration_hours": None})
        contract, retained = context(row)
        for key, value in (("scope", "weekend"), ("audience", "hotel_guest"), ("duration_hours", 2)):
            changed = row | {"qualifiers": row["qualifiers"] | {key: value}}
            with self.subTest(key=key), self.assertRaisesRegex(S.ExportError, "unapproved_retained_binding"):
                S.evaluate(changed, NOW, contract=contract, retained_content=retained)

    def test_typed_policy_age_temperature_and_qualifier_regressions(self):
        cases = [
            fact(entity_type="usage_condition", kind="foreigner", attribute="policy", value=True),
            fact(entity_type="usage_condition", kind="foreigner", attribute="policy", value="unsupported"),
            fact(entity_type="usage_condition", kind="age", attribute="min_age", value=-10),
            fact(entity_type="usage_condition", kind="age", attribute="min_age", value=True),
            fact(entity_type="usage_condition", kind="age", attribute="min_age", value=10.5),
            fact(kind="sauna", attribute="temperature_c", value=80, unit="F"),
            fact(kind="sauna", attribute="temperature_c", value="80", unit="C"),
        ]
        price = fact(entity_type="price_plan", kind="day_pass", attribute="amount", value=0, unit="KRW",
                     qualifiers={"ticket": "day_pass", "scope": "unspecified", "audience": "adult_non_guest",
                                 "duration_hours": None})
        for key, value in (("ticket", True), ("ticket", "guest_rate"), ("scope", []), ("scope", "invented"),
                           ("audience", False), ("audience", "invented"), ("duration_hours", "2"),
                           ("duration_hours", -1), ("duration_hours", True), ("duration_hours", float("inf"))):
            cases.append(price | {"qualifiers": price["qualifiers"] | {key: value}})
        cases.append(price | {"qualifiers": price["qualifiers"] | {"extra": "unsupported"}})
        for row in cases:
            with self.subTest(row=row), self.assertRaises(S.ExportError):
                self.evaluate(row)

    def test_supported_types_preserve_zero_and_policy_distinctions(self):
        for policy in ("accepted", "id_required", "restricted", "english_guide"):
            row = fact(entity_type="usage_condition", kind="foreigner", attribute="policy", value=policy)
            self.assertEqual(self.evaluate(row)["value"], policy)
        for row in (fact(entity_type="usage_condition", kind="age", attribute="min_age", value=0),
                    fact(kind="sauna", attribute="temperature_c", value=0, unit="C")):
            self.assertEqual(self.evaluate(row)["value"], 0)

    def test_unsupported_or_ambiguous_descriptors_fail_closed(self):
        row = fact()
        for value_rule in ({"type": "object"}, {"type": []}, {"type": "boolean", "inferred": True},
                           {"type": "enum", "enum": []}, {"type": "number", "minimum": "0"}):
            contract, retained = context(row)
            contract["descriptors"][0]["value"] = value_rule
            with self.subTest(value_rule=value_rule), self.assertRaises(S.ExportError):
                S.evaluate(row, NOW, contract=contract, retained_content=retained)
        contract, retained = context(row)
        contract["descriptors"].append(contract["descriptors"][0])
        with self.assertRaisesRegex(S.ExportError, "ambiguous_slot_descriptor"):
            S.evaluate(row, NOW, contract=contract, retained_content=retained)
        contract, retained = context(row)
        contract["retained"].append(contract["retained"][0])
        with self.assertRaisesRegex(S.ExportError, "unapproved_retained_binding"):
            S.evaluate(row, NOW, contract=contract, retained_content=retained)

    def test_read_export_resolves_actual_content_and_preserves_output_on_mismatch(self):
        import build_site as B
        row = fact(json_pointer="/venues.json/Synthetic/facts/0")
        contract, _ = context(row)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = root / "content"; content.mkdir()
            (content / "public_slots.json").write_text(json.dumps(self.doc(slots=[row])))
            (content / "public_contract.json").write_text(json.dumps(contract))
            retained_path = content / "venues.json"
            retained_path.write_text(json.dumps({"Synthetic": {"facts": [row["value_text"]]}}))
            catalog = {"available": True, "slots": [["facility", "dry_sauna", "exists"]]}
            result = S.read_export(content / "public_slots.json", catalog, {"test-branch"}, NOW,
                                   contract_path=content / "public_contract.json")
            self.assertEqual(result["slots"][0]["state"], "known")
            retained_path.write_text(json.dumps({"Synthetic": {"facts": ["changed content"]}}))
            dist = root / "dist"; dist.mkdir()
            sentinel = dist / "existing.html"; sentinel.write_text("preserved")
            with patch.object(B, "ROOT", root), patch.object(B, "load", return_value=([{"slug": "test-branch"}], [], [], [])), \
                    patch.object(B.PC, "generate", return_value=catalog), \
                    patch.object(sys, "argv", ["build_site.py", "--release", "--out", str(dist)]):
                with self.assertRaisesRegex(SystemExit, "mismatched_retained_value"):
                    B.main()
            self.assertEqual(sentinel.read_text(), "preserved")

    def test_builder_rejects_bad_export_before_replacing_existing_output(self):
        import build_site as B
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "content").mkdir()
            (root / "content/public_slots.json").write_text('{"schema_version":999}')
            dist = root / "dist"; dist.mkdir()
            sentinel = dist / "existing.html"; sentinel.write_text("keep existing output")
            with patch.object(B, "ROOT", root), patch.object(B, "load", return_value=([], [], [], [])), \
                    patch.object(B.PC, "generate", return_value={"available": True}), \
                    patch.object(sys, "argv", ["build_site.py", "--release", "--out", str(dist)]):
                with self.assertRaisesRegex(SystemExit, "unsupported_export_schema"):
                    B.main()
            self.assertEqual(sentinel.read_text(), "keep existing output")


if __name__ == "__main__":
    unittest.main()
