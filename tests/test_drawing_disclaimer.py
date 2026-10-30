"""disclaimer_check: no conceptual / not-to-scale / source captions near drawings."""
import sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "site"))
import build_site as bs

def test_caption_near_drawing_fails(tmp_path):
    (tmp_path / "x.html").write_text('<article class="arc"><img src="/assets/img/archive/a.png"><p>Conceptual layout</p></article>')
    assert bs.disclaimer_check(tmp_path)

def test_svg_disclaimer_fails(tmp_path):
    (tmp_path / "a.svg").write_text("<svg><desc>Not to scale</desc></svg>")
    assert bs.disclaimer_check(tmp_path)

def test_clean_passes(tmp_path):
    (tmp_path / "x.html").write_text('<figure><img src="/assets/img/guide/a.svg"><figcaption>How to sauna</figcaption></figure>')
    assert not [b for b in bs.disclaimer_check(tmp_path) if "x.html" in b[0]]
