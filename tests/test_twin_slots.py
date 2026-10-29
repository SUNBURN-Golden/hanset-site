import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "site"))
import twin_slots as TW

def f(status, value, url="https://example.org/a"):
    return {"entity_type": "hours", "kind": "regular", "attribute": "open_hours", "qualifiers": {},
            "value": value, "unit": None, "status": status, "source_url": url, "confirmed_at": "2026-10-28T10:00:00+09:00"}

class T(unittest.TestCase):
    def test_rules(self):
        self.assertIn("06:00-22:00", TW.render([f("fact", "06:00-22:00")]))
        h = TW.render([f("source_claim", "06:00-22:00")]); self.assertIn(TW.CLAIM, h); self.assertIn("06:00-22:00", h)
        for st in ("unverified", "ai_inference"):
            h = TW.render([f(st, "06:00-22:00")]); self.assertNotIn("06:00-22:00", h); self.assertIn("Unverified", h)
        self.assertNotIn("06:00", TW.render([f("fact", "06:00-22:00", url=None)]))

if __name__ == "__main__": unittest.main()
