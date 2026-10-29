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


class PublicSlots(unittest.TestCase):
    def test_false_is_known_and_unknown_never_exposes_candidates(self):
        self.assertIs(S.evaluate(fact(value=False), NOW)["value"], False)
        for status in ("source_claim", "unverified", "ai_inference"):
            self.assertEqual(S.evaluate(fact(status=status, value="private candidate"), NOW), {"state": "unknown"})
        self.assertEqual(S.evaluate(fact(publication="hold"), NOW), {"state": "unknown"})

    def test_official_status_alone_never_authorizes_publication(self):
        for changes in ({"source_type": "directory"}, {"source_url": ""},
                        {"source_url": "https://user:password@[::1]/"},
                        {"source_url": []}, {"source_url": "https://[invalid/"},
                        {"rights": "internal_review_only"}, {"evidence_locator_id": ""},
                        {"confirmed_on": None}, {"qualifiers": []}, {"value_text": ""},
                        {"value": "true"}):
            with self.subTest(changes=changes), self.assertRaises(S.ExportError):
                S.evaluate(fact(**changes), NOW)
        row = fact(); del row["confirmed_on"]
        with self.assertRaises(S.ExportError): S.evaluate(row, NOW)

    def test_conflict_quality_null_and_unsupported_status(self):
        self.assertEqual(S.evaluate(fact(conflict_state="unresolved"), NOW), {"state": "conflict"})
        for changes in ({"quality_clear": False}, {"quality_clear": 1}, {"value": None}):
            self.assertEqual(S.evaluate(fact(**changes), NOW), {"state": "unknown"})
        with self.assertRaises(S.ExportError): S.evaluate(fact(status="verified_fact"), NOW)

    def test_current_interval_date_boundary_and_staleness(self):
        self.assertEqual(S.evaluate(fact(effective_to=NOW.isoformat()), NOW), {"state": "unknown"})
        self.assertEqual(S.evaluate(fact(confirmed_on="2026-10-30"), NOW), {"state": "unknown"})
        self.assertEqual(S.evaluate(fact(effective_from="2026-10-29T04:00:00+00:00"), NOW), {"state": "unknown"})
        for days, expected in ((90, "known"), (91, "stale")):
            result = S.evaluate(fact(confirmed_on=(NOW.date()-dt.timedelta(days=days)).isoformat()), NOW)
            self.assertEqual(result["state"], expected)
            self.assertIn("source_url", result)
        with self.assertRaises(S.ExportError):
            S.evaluate(fact(effective_from="2026-10-18T00:00:00"), NOW)

    def test_expired_closure_never_means_reopened(self):
        row = fact(entity_type="hours", kind="closure", attribute="closure_rule", value="Test closure",
                   qualifiers={"until": "2026-10-28"})
        self.assertEqual(S.evaluate(row, NOW), {"state": "unknown"})
        row["qualifiers"]["until"] = "2026-10-29"
        self.assertEqual(S.evaluate(row, NOW)["state"], "known")

    def test_scoped_prices_remain_distinct_and_zero_is_valid(self):
        prices = []
        for audience in ("adult_non_guest", "hotel_guest"):
            row = fact(entity_type="price_plan", kind="day_pass", attribute="amount", value=0, unit="KRW",
                       qualifiers={"ticket": "day_pass", "scope": "unspecified", "audience": audience,
                                   "duration_hours": None})
            result = S.evaluate(row, NOW)
            self.assertEqual(result["value"], 0)
            prices.append(result)
        self.assertNotEqual(prices[0]["qualifiers"], prices[1]["qualifiers"])
        for changes in ({"value": True}, {"value": float("nan")}, {"value": -1},
                        {"unit": None}, {"qualifiers": {"ticket": "day_pass"}}):
            with self.subTest(changes=changes), self.assertRaises(S.ExportError):
                S.evaluate(row | changes, NOW)

    def read(self, doc, catalog=None):
        if catalog is None:
            catalog = {"available": True, "slots": [["facility", "dry_sauna", "exists"]]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "export.json"
            if doc is not None: path.write_text(json.dumps(doc))
            return S.read_export(path, catalog, {"test-branch"}, NOW)

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
