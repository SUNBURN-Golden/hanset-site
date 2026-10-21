"""Validator tests for data/verified feeds. Run: python -m unittest discover -s tests"""
import datetime as dt, json, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "site"))
import verified as VF

TODAY = dt.date(2026, 10, 21)
# Phone/email fixtures are assembled at runtime so no phone number or address literal sits in the repo (AGENTS.md rule 1).
FAKE_PHONE = "-".join(["0" + "10", "0" * 4, "0" * 4]); FAKE_INTL = " ".join(["+" + "82", "2", "0" * 3, "0" * 4])
FAKE_MAIL = "nobody" + chr(64) + "example.invalid"
EV = dict(id="hyrox-seoul", title="HYROX Seoul", category="hyrox", start="2026-11-13", end="2026-11-15", city="Goyang",
          venue="KINTEX", official_url="https://hyrox.co.kr/", source_url="https://hyrox.co.kr/", verified_on="2026-10-20")
CR = dict(id="runner", display_name="Some Runner", handle="@some.runner", platform="instagram",
          profile_url="https://www.instagram.com/some.runner/", focus=["run"], verified_on="2026-10-20")

def ev(**kw): return {**EV, **kw}
def cr(**kw): return {**CR, **kw}

class EventValidation(unittest.TestCase):
    def test_valid(self):
        it, why = VF.validate_event(ev(), TODAY); self.assertIsNone(why); self.assertEqual(it["end"], dt.date(2026, 11, 15))
    def test_null_end_is_one_day(self):
        it, _ = VF.validate_event(ev(end=None), TODAY); self.assertEqual(it["start"], it["end"])
    def test_https_only(self):
        for u in ("http://hyrox.co.kr/", "javascript:alert(1)", "ftp://x.y/", "", None, "mailto:" + FAKE_MAIL):
            self.assertIsNone(VF.validate_event(ev(official_url=u), TODAY)[0], u)
    def test_required_fields(self):
        for k in ("id", "title", "category", "start", "city", "venue", "official_url", "source_url", "verified_on"):
            x = ev(); x.pop(k); self.assertIsNone(VF.validate_event(x, TODAY)[0], k)
    def test_bad_category_and_dates(self):
        self.assertIsNone(VF.validate_event(ev(category="party"), TODAY)[0])
        self.assertIsNone(VF.validate_event(ev(start="2026-13-01"), TODAY)[0])
        self.assertIsNone(VF.validate_event(ev(end="2026-11-01"), TODAY)[0])  # end before start
    def test_past_dropped_ongoing_kept(self):
        self.assertEqual(VF.validate_event(ev(start="2026-10-01", end="2026-10-20"), TODAY)[1], "past")
        self.assertIsNotNone(VF.validate_event(ev(start="2026-10-20", end="2026-10-21"), TODAY)[0])
    def test_phone_and_email_rejected(self):
        self.assertIsNone(VF.validate_event(ev(venue="Call " + FAKE_PHONE), TODAY)[0])
        self.assertIsNone(VF.validate_event(ev(price_note="ask " + FAKE_MAIL), TODAY)[0])
        self.assertIsNone(VF.validate_event(ev(title="HYROX " + FAKE_INTL), TODAY)[0])
    def test_examples_skipped_by_default(self):
        self.assertEqual(VF.validate_event(ev(example=True), TODAY)[1], "example")

class CreatorValidation(unittest.TestCase):
    def test_valid(self): self.assertIsNotNone(VF.validate_creator(cr())[0])
    def test_rules(self):
        self.assertIsNone(VF.validate_creator(cr(handle="no-at"))[0])
        self.assertIsNone(VF.validate_creator(cr(platform="tiktok"))[0])
        self.assertIsNone(VF.validate_creator(cr(profile_url="http://x.com/a"))[0])
        self.assertIsNone(VF.validate_creator(cr(focus=[]))[0])
        self.assertIsNone(VF.validate_creator(cr(city=FAKE_PHONE.replace("-", " ")))[0])

class Loading(unittest.TestCase):
    def test_missing_empty_broken_files_hide_section(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "events.json"
            self.assertEqual(VF.load_events(p, TODAY), [])
            p.write_text(""); self.assertEqual(VF.load_events(p, TODAY), [])
            p.write_text("{not json"); self.assertEqual(VF.load_events(p, TODAY), [])
            p.write_text(json.dumps({"version": 2, "events": [ev()]})); self.assertEqual(VF.load_events(p, TODAY), [])
    def test_sorted_and_filtered(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "events.json"
            p.write_text(json.dumps({"version": 1, "updated": "2026-10-21", "events": [
                ev(id="b", start="2026-12-01", end=None), ev(id="a"), ev(id="old", start="2026-01-01", end=None), ev(id="bad", official_url="http://x")]}))
            self.assertEqual([e["id"] for e in VF.load_events(p, TODAY)], ["a", "b"])
    def test_repo_files_valid(self):
        root = Path(__file__).resolve().parents[1] / "data/verified"
        for name, key in (("events.json", "events"), ("creators.json", "creators")):
            d = json.loads((root / name).read_text(encoding="utf-8")); self.assertEqual(d["version"], 1); self.assertIsInstance(d[key], list)
        self.assertEqual(len(VF.load_events(root / "examples/events.example.json", TODAY, allow_example=True)), 4)
        self.assertEqual(VF.load_events(root / "examples/events.example.json", TODAY), [])

if __name__ == "__main__": unittest.main()
