"""drawing_hangul_check: the 12 venue drawings (aerial-*, archive-*) must be English only."""
import sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "site"))
import build_site as bs

def test_hangul_in_drawing_svg_fails(tmp_path):
    d = tmp_path / "assets/img/aerial"; d.mkdir(parents=True)
    (d / "aerial-x.svg").write_text('<svg><title>\uc628\ud0d5</title></svg>')
    assert any("aerial-x.svg" in b[0] for b in bs.drawing_hangul_check(tmp_path))

def test_english_drawing_svg_passes(tmp_path):
    d = tmp_path / "assets/img/archive"; d.mkdir(parents=True)
    (d / "archive-x.svg").write_text("<svg><title>Sisu House, floor by floor</title></svg>")
    assert not [b for b in bs.drawing_hangul_check(tmp_path) if "archive-x" in b[0]]

def test_repo_drawings_have_no_hangul():
    for p in [*(ROOT / "illustrations" / "svg").glob("aerial-*.svg"), *(ROOT / "illustrations" / "svg").glob("archive-*.svg")]:
        assert not bs.drawing_hangul_scan(p.read_text()), p.name
