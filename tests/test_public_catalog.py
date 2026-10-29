"""Catalog extraction must not execute operational Python or invent schema."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "site"))
import public_catalog as C


SCHEMA = '''
raise RuntimeError("operational code must never execute")
ENTITY_TYPES = ('venue', 'branch', 'facility', 'usage_condition', 'hours', 'price_plan', 'program')
FACILITY_KINDS = ('dry_sauna', 'rapid_cold_plunge', 'layout', 'new_kind')
REQUIRED_SLOTS = [('facility', 'dry_sauna', 'exists'),
 ('facility', 'layout', 'bath_list'), ('usage_condition', 'tattoo', 'policy'),
 ('price_plan', 'day_pass', 'amount'), ('hours', 'regular', 'open_hours'),
 ('program', 'program', 'listing')]
'''


class Catalog(unittest.TestCase):
    def generate(self, text=SCHEMA):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ontology.py"
            path.write_text(text)
            return C.generate(path, [{"city": "seoul", "type": "sauna"}])

    def test_literals_without_execution_and_no_package_import(self):
        before = set(sys.modules)
        catalog = self.generate()
        self.assertTrue(catalog["available"])
        self.assertFalse(any(k.startswith("hanset_twin") for k in set(sys.modules) - before))
        self.assertEqual(catalog["sections"], ["usage_condition", "price_plan", "hours", "facility", "program"])
        self.assertEqual(next(f for f in catalog["facets"] if f["key"] == "city")["options"], ["seoul"])

    def test_schema_drives_controls_unknown_kind_has_no_renderer(self):
        facets = self.generate()["facets"]
        self.assertNotIn("foreigner", [f["key"] for f in facets])
        facilities = next(f for f in facets if f["key"] == "facility")["options"]
        self.assertEqual([f["value"] for f in facilities], ["dry_sauna", "rapid_cold_plunge", "new_kind"])
        self.assertTrue(facilities[0]["renderer_supported"])
        self.assertFalse(facilities[-1]["renderer_supported"])
        self.assertEqual(facilities[-1]["label"], "Not confirmed")

    def test_missing_schema_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(C.generate(Path(directory) / "missing.py")["reason"], "missing_ontology")

    def test_invalid_or_drifting_schema_fails_closed(self):
        for text in [SCHEMA.replace("ENTITY_TYPES =", "IGNORED ="),
                     SCHEMA + "ENTITY_TYPES = ('facility',)\n",
                     SCHEMA.replace("('dry_sauna', 'rapid_cold_plunge', 'layout', 'new_kind')", "get_kinds()"),
                     SCHEMA.replace("'new_kind'", "'dry_sauna'"),
                     SCHEMA.replace("('facility', 'dry_sauna', 'exists')", "('missing', 'dry_sauna', 'exists')"),
                     SCHEMA.replace("('facility', 'dry_sauna', 'exists')", "('facility', 'missing', 'exists')")]:
            with self.subTest(text=text), self.assertRaises(C.CatalogError):
                self.generate(text)


if __name__ == "__main__":
    unittest.main()
