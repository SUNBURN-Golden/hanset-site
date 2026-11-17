"""Memory-only parser boundary fixtures; no database, browser or network required."""
import json
from pathlib import Path
import unittest

from crawler.config import Rejected
from crawler.fetcher import MinimalHTML


FIXTURES = json.loads((Path(__file__).parent / 'fixtures/minimal_regions.json').read_text())


class MinimalHTMLTests(unittest.TestCase):
    def test_shared_regions_whole_and_chunked(self):
        # Single-character input also splits tags, attributes and character references.
        for case in FIXTURES:
            for size in (len(case['html']), 1, 7):
                with self.subTest(case=case['name'], chunk_size=size):
                    parser = MinimalHTML()
                    for offset in range(0, len(case['html']), size):
                        parser.feed(case['html'][offset:offset + size])
                    parser.close()
                    if 'reason' in case:
                        with self.assertRaisesRegex(Rejected, '^' + case['reason'] + '$'):
                            parser.result()
                        if case['reason'] in ('missing_minimal_region', 'private_region'):
                            self.assertIsNone(parser.locator)
                            self.assertEqual(parser.parts, [])
                            self.assertEqual(parser.links, [])
                    else:
                        result = parser.result()
                        self.assertEqual(' '.join(result['excerpt'].split()), case['excerpt'])
                        self.assertEqual(result['locator'], case['locator'])
                        self.assertEqual(result['title'], case['title'])
                        self.assertEqual(parser.links, case['links'])
                        self.assertEqual(' '.join(''.join(parser.parts).split()), case['excerpt'])
                        self.assertEqual(result['facts'], [])
                        self.assertIsNone(result['published_at'])

    def test_separators_never_escape_selected_unblocked_scope(self):
        parser = MinimalHTML()
        parser.feed('<div>Outside</div><main>A<p hidden>Excluded</p>B</unmatched>'
                    '<span contenteditable>Excluded</span>C</main></p><div>Outside</div>')
        parser.close()
        self.assertEqual(parser.parts, ['A', 'B', 'C', '\n'])
        self.assertEqual(parser.result()['excerpt'], 'ABC')
        parser.feed('<main>Later</main><p>Outside</p>')
        self.assertEqual(parser.parts, ['A', 'B', 'C', '\n'])
        self.assertTrue(parser.candidate_seen)
        self.assertTrue(parser.region_eligible)
        self.assertTrue(parser.region_closed)
        self.assertIsNone(parser.region_depth)

    def test_self_closing_void_and_unmatched_end_tags_do_not_reopen_region(self):
        parser = MinimalHTML()
        parser.feed('<main>A<br/><input/><img/> B</unknown></main></main><article>Later</article>')
        parser.close()
        self.assertEqual(parser.result()['excerpt'], 'A B')
        self.assertEqual(parser.locator, 'main')

    def test_inline_style_hiding_variants_and_inheritance(self):
        for style in ('display:\nNONE\t! important', 'VISIBILITY : HIDDEN !IMPORTANT',
                      'visibility:collapse', 'color:red; display /* comment */ : none',
                      'display:none;display:block'):
            for wrapper in (False, True):
                with self.subTest(style=style, wrapper=wrapper):
                    parser = MinimalHTML()
                    parser.feed(('<div style="' + style + '"><main>Body</main></div>') if wrapper
                                else '<main style="' + style + '">Body</main>')
                    with self.assertRaisesRegex(Rejected, '^private_region$'):
                        parser.result()

    def test_privacy_contacts_title_and_limits(self):
        contact = 'reader' + chr(64) + 'example.invalid'
        phone = '-'.join(['0' + '10', '1' * 4, '2' * 4])
        parser = MinimalHTML()
        parser.feed('<title>' + contact + '</title><main>Public ' + contact + ' ' + phone +
                    ' hanset@soulbound.co.kr</main>')
        result = parser.result()
        self.assertEqual(result['title'], '[redacted]')
        self.assertNotIn(contact, result['excerpt'])
        self.assertNotIn(phone, result['excerpt'])
        self.assertIn('hanset@soulbound.co.kr', result['excerpt'])
        self.assertIn(contact, parser.result([contact])['excerpt'])
        parser = MinimalHTML()
        parser.feed('<title>' + 'T' * 300 + '</title><main>' + 'B' * 2500 + '</main>')
        result = parser.result()
        self.assertEqual(len(result['title']), 240)
        self.assertEqual(len(result['excerpt']), 2000)
        self.assertEqual(len(''.join(parser.parts).strip()), 2500)
