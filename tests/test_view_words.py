"""view_word_check: old floor sketches must never be called 조감도/aerial/isometric; new aerial drawings may."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "site"))
import build_site as B


class ViewWordTest(unittest.TestCase):
    def test_old_sketch_with_jogamdo_fails(self):
        self.assertTrue(B.view_word_scan(Path("dist/assets/img/archive/archive-termeden.svg"), "<title>테르메덴 조감도</title>"))
        self.assertTrue(B.view_word_scan(Path("illustrations/svg/archive-sisu-house.svg"), "isometric hand drawing"))
        html = '<article class="arc" id="termeden-icheon"><img src="/assets/img/archive/archive-termeden-thumb.png" alt="Termeden from above"></article>'
        self.assertTrue(B.view_word_scan(Path("dist/archive/index.html"), html))

    def test_aerial_drawing_passes(self):
        self.assertIsNone(B.view_word_scan(Path("dist/assets/img/aerial/aerial-termeden.svg"), "<title>테르메덴 조감도</title>"))
        self.assertIsNone(B.view_word_scan(Path("illustrations/make_aerial.py"), "조감도 손그림"))
        html = '<!--aerial--><section id="aerial"><h2>Bird’s-eye drawing</h2><img src="/assets/img/aerial/aerial-termeden-thumb.webp" alt="Bird’s-eye drawing"></section><!--/aerial-->'
        self.assertIsNone(B.view_word_scan(Path("dist/finder/termeden-icheon/index.html"), html))

    def test_aerial_block_does_not_whitelist_rest_of_page(self):
        html = '<!--aerial--><p>조감도</p><!--/aerial--><p>Termeden, isometric sketch</p>'
        self.assertTrue(B.view_word_scan(Path("dist/archive/index.html"), html))


if __name__ == "__main__":
    unittest.main()
