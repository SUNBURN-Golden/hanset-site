"""Synthetic emitted-output fixtures; no collector or browser dependencies."""
import contextlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'site'))
import build_site as B

# Generated test-only contacts, never venue or owner details.
PHONE = '-'.join(('010', '2468', '1357'))
EMAIL = '@'.join(('fixture', 'example.invalid'))
REMOTE = 'https://assets.example.invalid/file'
ALLOWED = 'hanset@soulbound.co.kr'


def wrap(suffix, text):
    if suffix == '.html': return '<p title="fixture">' + text + '</p>'
    if suffix == '.svg': return '<svg xmlns="http://www.w3.org/2000/svg"><desc>' + text + '</desc></svg>'
    if suffix == '.css': return '/* ' + text + ' */'
    if suffix == '.js': return 'const note = ' + json.dumps(text) + ';'
    if suffix == '.json': return json.dumps({'note': text})
    if suffix == '.xml': return '<root><note>' + text + '</note></root>'
    return 'BEGIN:VCALENDAR\r\nDESCRIPTION:' + text + '\r\nEND:VCALENDAR\r\n'


class LeakCheckTests(unittest.TestCase):
    def scan(self, text, suffix='.html'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ('fixture' + suffix)).write_text(text, encoding='utf-8')
            return {rule for _, rule in B.leak_check(root)}

    def test_every_format_enforces_contacts_and_wording(self):
        for suffix in sorted(B.LEAK_SUFFIXES):
            for text, rule in ((PHONE, 'contact.phone'), (EMAIL, 'contact.email'),
                               ('광 · 고 문의', 'wording.advertising'), ('면역력 증가', 'wording.health')):
                with self.subTest(suffix=suffix, rule=rule):
                    self.assertIn(rule, self.scan(wrap(suffix, text), suffix))

    def test_contact_shapes(self):
        numbers = [PHONE, PHONE + '.', PHONE + ',', PHONE.replace('-', ''), ' '.join(('02', '246', '1357')),
                   ''.join(('031', '2468', '1357')), '-'.join(('070', '2468', '1357')),
                   '-'.join(('1588', '1357')), ''.join(('1800', '1357')),
                   '+82 (0)10 ' + '2468 1357', ' '.join(('0082', '10', '2468', '1357')), '+1 (' + '202) 555 ' + '0147']
        for number in numbers:
            with self.subTest(shape=numbers.index(number)):
                self.assertIn('contact.phone', self.scan('<p>' + number + '</p>'))
        for scheme in ('tel:', 'sms:', 'TEL:'):
            self.assertIn('contact.phone', self.scan('<a href="' + scheme + '123">contact</a>'))
        for address in (EMAIL, ALLOWED + '.invalid', 'x' + ALLOWED, ALLOWED.replace('@', '+extra@')):
            with self.subTest(shape=address == EMAIL):
                self.assertIn('contact.email', self.scan('<a href="mailto:' + address + '">contact</a>'))
        self.assertIn('contact.email', self.scan('<a href="mailto:' + ALLOWED + ',' + EMAIL + '">contact</a>'))
        self.assertIn('contact.email', self.scan('<a href="mailto:' + ALLOWED + '?cc=' + EMAIL + '">contact</a>'))

    def test_encoded_contacts_and_wording(self):
        fixtures = [
            ('.html', '<!--' + ''.join('&#%d;' % ord(c) for c in PHONE) + '-->', 'contact.phone'),
            ('.html', '<img alt="' + EMAIL.replace('@', '&#64;') + '" src="/local.svg">', 'contact.email'),
            ('.html', '<a href="mailto:' + EMAIL.replace('@', '%40') + '">contact</a>', 'contact.email'),
            ('.svg', '<svg><metadata>' + EMAIL.replace('@', '&#x40;') + '</metadata></svg>', 'contact.email'),
            ('.xml', '<root><![CDATA[' + PHONE + ']]></root>', 'contact.phone'),
            ('.xml', '<!DOCTYPE root [<!ENTITY a "' + PHONE + '">]><root>&a;</root>', 'contact.phone'),
            ('.json', '{"' + EMAIL.replace('@', r'\u0040') + '":"safe"}', 'contact.email'),
            ('.json', json.dumps(PHONE).replace('0', r'\u0030'), 'contact.phone'),
            ('.js', "const note='" + EMAIL.replace('@', r'\x40') + "';", 'contact.email'),
            ('.js', "const note='" + PHONE.replace('-', r'\u002d') + "';", 'contact.phone'),
            ('.css', 'p::after{content:"' + EMAIL.replace('@', '\\40 ') + '"}', 'contact.email'),
            ('.css', '/* ' + PHONE.replace('-', '\\2d ') + ' */', 'contact.phone'),
            ('.ics', 'DESCRIPTION:' + PHONE[:5] + '\r\n ' + PHONE[5:], 'contact.phone'),
            ('.ics', 'DESCRIPTION:' + EMAIL[:4] + '\n\t' + EMAIL[4:], 'contact.email'),
            ('.html', '<script type="application/ld+json">' + json.dumps(EMAIL).replace('@', r'\u0040') + '</script>', 'contact.email'),
            ('.html', '<script>' + "const note='" + EMAIL.replace('@', r'\x40') + "';</script>", 'contact.email'),
            ('.html', '<style>p{content:"' + PHONE.replace('-', '\\2d ') + '"}</style>', 'contact.phone'),
            ('.html', '<p style="--note: &quot;' + EMAIL + '&quot;">safe</p>', 'contact.email'),
        ]
        for suffix, text, rule in fixtures:
            with self.subTest(suffix=suffix, index=fixtures.index((suffix, text, rule))):
                self.assertIn(rule, self.scan(text, suffix))

    def test_vocabulary_and_legacy_rules(self):
        ads = ['광고', '협 찬', '제-휴', '후원', '스폰서', '파트너십', '미디어 · 킷',
               'Sponsorship', 'Advertising', 'partnership', 'media_kit', 'collaboration']
        health = ['디톡스', '해독', '면역력 증가', '면역 강화', '살 빠진다', '체중 감량', '지방 연소',
                  '치유', '완치', '힐링', 'detoxification', 'immunity enhancement', 'boost immunity',
                  'weight loss', 'lose weight', 'fat-burning', 'healing', 'cures', 'curing']
        for term in ads + health:
            with self.subTest(index=(ads + health).index(term)):
                self.assertIn('wording.advertising' if term in ads else 'wording.health', self.scan('<p>' + term + '</p>'))
        for suffix in B.LEAK_SUFFIXES:
            self.assertIn('wording.forbidden', self.scan(wrap(suffix, 'PRIVATE-DM'), suffix))
            self.assertIn('wording.forbidden', self.scan(wrap(suffix, '442'), suffix))
            self.assertIn('resource.tracker', self.scan(wrap(suffix, 'https://google-analytics.example.invalid/a'), suffix))

    def test_positive_content_in_all_formats(self):
        positive = '2026-11-17 2026.11.17 20261117 20261117T091500Z 09:00–22:00 ₩49,000 15000 79000 ' + ALLOWED + ' ' + B.SAFETY
        for suffix in B.LEAK_SUFFIXES:
            with self.subTest(suffix=suffix):
                self.assertEqual(set(), self.scan(wrap(suffix, positive), suffix))
        self.assertEqual(set(), self.scan('<a href="mailto:' + ALLOWED.upper() + '">contact</a>'))
        self.assertEqual(set(), self.scan('<p>몸이 불편하면 이용을 중단하고 의사에게 문의하세요.</p>'))
        self.assertEqual(set(), self.scan('<svg viewBox="0 0 442 800"><path d="M442 ' + PHONE.replace('-', '') + ' L0 0"/><text>₩49,000</text></svg>', '.svg'))
        self.assertEqual(set(), self.scan('const n=1024681357; const x=442; const hash=0x1024681357;', '.js'))
        self.assertEqual(set(), self.scan('{"number":1024681357,"code":442}', '.json'))
        self.assertEqual(set(), self.scan('.x{width:442px;--size:1024681357;color:#024681}', '.css'))
        self.assertEqual(set(), self.scan('DTSTART:20261117T091500Z\r\nDTEND:20261117T101500Z', '.ics'))

    def test_inert_links_and_local_loads(self):
        markup = ('<a href="' + REMOTE + '">Official source</a><link rel="canonical" href="' + REMOTE + '">'
                  '<img src="/assets/local.svg?v=a53b22"><script src="/assets/local.js?v=a53b22"></script>'
                  '<img srcset="/a.svg 1x, /b.svg 2x"><link rel="stylesheet" href="/site.css?v=abc">'
                  '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"><use xlink:href="#local"/></svg>')
        self.assertEqual(set(), self.scan(markup))
        for suffix, text in [('.json', json.dumps({'source': REMOTE})),
                             ('.xml', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>' + REMOTE + '</loc></url></urlset>'),
                             ('.ics', 'URL:' + REMOTE),
                             ('.css', '@font-face{src:url(/local.woff2?v=abc)} .a{background:url(#local);content:"url(' + REMOTE + ')"}'),
                             ('.js', 'const source="' + REMOTE + '"; // fetch("' + REMOTE + '")\nconst r=/fetch\\("[^\\"]+"\\)/;'),
                             ('.js', 'fetch("/api/local"); import("./local.js"); img.src="/local.svg";'),
                             ('.js', 'const base="/assets/"; fetch(base+`local.json`);'),
                             ('.js', 'fetch(("/local/" + "json"), {method:"GET"});')]:
            with self.subTest(suffix=suffix): self.assertEqual(set(), self.scan(text, suffix))
        for url in ('#local', './local.svg?v=abcdef', '../local.svg', B.SITE_URL + ':443/a', B.SITE_URL.upper() + '/a', 'data:image/svg+xml;base64,AAAA'):
            with self.subTest(url_kind=url[:4]): self.assertEqual(set(), self.scan('<img src="' + url + '">'))

    def test_html_and_svg_resource_families(self):
        fixtures = [
            '<script src="{u}"></script>', '<link rel="stylesheet" href="{u}">', '<img src="{u}">',
            '<img srcset="/local.svg 1x, {u} 2x">', '<source srcset="{u} 2x">', '<audio src="{u}"></audio>',
            '<video poster="{u}"></video>', '<video><source src="{u}"></video>', '<track src="{u}">',
            '<iframe src="{u}"></iframe>', '<frame src="{u}">', '<embed src="{u}">', '<object data="{u}"></object>',
            '<link rel="preload" href="{u}">', '<link rel="modulepreload" href="{u}">', '<link rel="prefetch" href="{u}">',
            '<link rel="preconnect" href="{u}">', '<link rel="dns-prefetch" href="{u}">', '<link rel="icon" href="{u}">',
            '<base href="{u}">', '<meta http-equiv="refresh" content="0; URL={u}">', '<a href="/source" ping="{u}">source</a>',
            '<svg><image href="{u}"/></svg>', '<svg:image xmlns:svg="http://www.w3.org/2000/svg" href="{u}"/>', '<svg><use xlink:href="{u}"/></svg>', '<svg><feImage href="{u}"/></svg>',
            '<svg><script href="{u}"/></svg>', '<svg><set attributeName="href" to="{u}"/></svg>',
            '<style>.a{{background:url("{u}")}}</style>', '<p style="background:url({u})">text</p>',
            '<script>fetch("{u}")</script>', '<button onclick="fetch(\'{u}\')">test</button>',
            '<iframe srcdoc="&lt;img src=\'{u}\'&gt;"></iframe>',
        ]
        for fixture in fixtures:
            for suffix in ('.html', '.svg', '.xml'):
                with self.subTest(index=fixtures.index(fixture), suffix=suffix):
                    self.assertIn('resource.external', self.scan(fixture.format(u=REMOTE), suffix))

    def test_url_origin_and_escaping(self):
        urls = ['//assets.example.invalid/a', '///assets.example.invalid/a', 'https:///assets.example.invalid/a', 'HTTPS://ASSETS.EXAMPLE.INVALID/a',
                B.SITE_URL + '.example.invalid/a', B.SITE_URL + ':444/a', B.SITE_URL + ':0/a', B.SITE_URL.replace('https:', 'http:') + '/a',
                'https://' + chr(64).join(('hanset.soulbound.co.kr', 'assets.example.invalid/a')),
                'https://' + chr(64).join(('fixture', 'hanset.soulbound.co.kr/a')),
                r'\\assets.example.invalid\a', 'https://assets%2eexample.invalid/a', 'https:&#47;&#47;assets.example.invalid/a']
        for url in urls:
            with self.subTest(index=urls.index(url)):
                self.assertIn('resource.external', self.scan('<img src="' + url + '">'))
        self.assertIn('resource.unresolved', self.scan('<img src="https://[invalid/a">'))
        for css in ['.x{background:url(' + REMOTE + ')}', '@import "' + REMOTE + '";', "@import '" + REMOTE + "';",
                    '@import url("' + REMOTE + '");', r'.x{background:u\72l(https\3a //assets.example.invalid/a)}',
                    '@font-face{src:url(' + REMOTE + ')}']:
            self.assertIn('resource.external', self.scan(css, '.css'))
        self.assertIn('resource.external', self.scan('fetch("' + REMOTE.replace('https:', r'https\x3a') + '")', '.js'))
        self.assertIn('resource.external', self.scan(r'f\u0065tch("' + REMOTE + '")', '.js'))

    def test_javascript_load_operations(self):
        operations = ['fetch({u})', 'fetch?.({u})', 'window.fetch({u})', 'xhr.open("GET",{u})', 'navigator.sendBeacon({u},"x")',
                      'new WebSocket({u})', 'new EventSource({u})', 'new Worker({u})', 'new SharedWorker({u})', 'new Request({u})',
                      'import({u})', 'importScripts("/local.js",{u})', 'navigator.serviceWorker.register({u})',
                      'img.src={u};', 'img["src"]={u};', r'img["s\x72c"]={u};', 'img.srcset={u};', 'video.poster={u};',
                      'object.data={u};', 'link.href={u};', 'location={u};', 'window.location.href={u};',
                      'location.assign({u})', 'window.location.replace({u})', 'window.open({u},"_blank")', 'const a=1;open({u});',
                      'image.setAttribute("src",{u})', 'image.setAttributeNS(null,"href",{u})']
        for template in operations:
            for expression, rule in ((json.dumps(REMOTE), 'resource.external'), ('destination', 'resource.unresolved'),
                                     ('`' + REMOTE + '`', 'resource.external'), ('`https://${host}/a`', 'resource.unresolved')):
                with self.subTest(index=operations.index(template), expression_kind=rule):
                    self.assertIn(rule, self.scan(template.replace('{u}', expression), '.js'))
        for text in ['import "' + REMOTE + '";', 'import x from "' + REMOTE + '";', 'export {x} from "' + REMOTE + '";',
                     'fetch("https://"+"assets.example.invalid/a")', 'const base="https://assets.example.invalid/";fetch(base+"a")',
                     'img.style.cssText="background:url(' + REMOTE + ')";', 'sheet.insertRule(".x{background:url(' + REMOTE + ')}")',
                     'document.write(\'<img src="' + REMOTE + '">\')', 'el.innerHTML=\'<img src="' + REMOTE + '">\';',
                     'el.insertAdjacentHTML("beforeend",\'<img src="' + REMOTE + '">\')']:
            self.assertIn('resource.external', self.scan(text, '.js'))
        for text in ['fetch("/local/" + dynamic)', 'fetch(getDestination())', 'fetch()', 'xhr.open("GET")', 'img.src += "/local";',
                     'const url="/local"; function f(){const url=other;fetch(url)}',
                     'const url="/local"; function f(url){fetch(url)}',
                     'const url="/local"; const f=(url)=>fetch(url)',
                     'const url="/local"; const f=url=>fetch(url)']:
            self.assertIn('resource.unresolved', self.scan(text, '.js'))

    def test_xml_and_calendar_resources(self):
        for text in ['<!DOCTYPE root SYSTEM "' + REMOTE + '"><root/>',
                     '<!DOCTYPE root PUBLIC "fixture" "' + REMOTE + '"><root/>',
                     '<!DOCTYPE root [<!ENTITY ext SYSTEM "' + REMOTE + '">]><root/>',
                     '<?xml-stylesheet type="text/xsl" href="' + REMOTE + '"?><root/>']:
            self.assertIn('resource.external', self.scan(text, '.xml'))
        for text in ['ATTACH:' + REMOTE, 'ATTACH;VALUE=URI:' + REMOTE,
                     'ATTACH;FMTTYPE=image/svg+xml:' + REMOTE[:10] + '\r\n ' + REMOTE[10:]]:
            self.assertIn('resource.external', self.scan(text, '.ics'))
        self.assertEqual(set(), self.scan('ATTACH:/local.svg\r\nURL:' + REMOTE, '.ics'))

    def test_deterministic_redacted_findings_and_read_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('z.HTML', 'nested/a.JSON'):
                p = root / name
                p.parent.mkdir(exist_ok=True)
                p.write_text(wrap(p.suffix.lower(), PHONE + ' ' + EMAIL + ' 광고 detox'), encoding='utf-8')
            (root / 'bad.CSS').write_bytes(b'\xff')
            expected = [('bad.CSS', 'validation.read')]
            for name in ('nested/a.JSON', 'z.HTML'):
                expected.extend((name, rule) for rule in ('contact.email', 'contact.phone', 'wording.advertising', 'wording.forbidden', 'wording.health'))
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured): findings = B.leak_check(root)
            self.assertEqual(sorted(set(expected)), findings)
            self.assertEqual('', captured.getvalue())
            for secret in (PHONE, EMAIL, 'detox'): self.assertNotIn(secret, repr(findings))
            original = Path.read_text
            def fail_css(path, *args, **kwargs):
                if path.suffix == '.CSS': raise OSError('synthetic sensitive error')
                return original(path, *args, **kwargs)
            with patch.object(Path, 'read_text', fail_css):
                self.assertIn(('bad.CSS', 'validation.read'), B.leak_check(root))


class ReleaseLeakTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name) / 'repo'
        cls.root.mkdir()
        shutil.copytree(REPO / 'site', cls.root / 'site', ignore=shutil.ignore_patterns('dist', '.venv', '__pycache__'))
        shutil.copytree(REPO / 'data/verified', cls.root / 'data/verified')
        shutil.copytree(REPO / 'illustrations', cls.root / 'illustrations', ignore=shutil.ignore_patterns('__pycache__'))
        for path in REPO.glob('*.csv'): shutil.copy(path, cls.root / path.name)
        cls.inputs = cls.root / 'site/static/root'
        cls.out = Path(cls.temp.name) / 'out'

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def build(self):
        # -S selects the builder's supported stdlib/font fallback, keeping CLI
        # fixtures fast. Validation itself is never mocked or bypassed.
        return subprocess.run([sys.executable, '-S', str(self.root / 'site/build_site.py'), '--release',
                               '--out', str(self.out)], cwd=self.root, capture_output=True, text=True)

    def test_release_fails_for_each_prohibited_category_before_other_validators(self):
        fixtures = [(wrap('.html', PHONE), '.html', 'contact.phone'),
                    (wrap('.svg', EMAIL), '.svg', 'contact.email'),
                    (wrap('.css', '광고'), '.css', 'wording.advertising'),
                    (wrap('.json', '면역력 증가'), '.json', 'wording.health'),
                    ('fetch("' + REMOTE + '")', '.js', 'resource.external'),
                    ('fetch(dynamicDestination)', '.js', 'resource.unresolved'),
                    (wrap('.xml', 'PRIVATE-DM'), '.xml', 'wording.forbidden'),
                    ('URL:https://google-analytics.example.invalid/a', '.ics', 'resource.tracker')]
        for text, suffix, rule in fixtures:
            with self.subTest(rule=rule):
                path = self.inputs / ('fixture' + suffix)
                path.write_text(text, encoding='utf-8')
                try:
                    result = self.build()
                    output = result.stdout + result.stderr
                    self.assertEqual(2, result.returncode, 'release must fail on prohibited output')
                    self.assertIn('LEAK-CHECK FAIL: fixture' + suffix + ' [' + rule + ']', output)
                    self.assertNotIn('leak check OK', output)
                    self.assertNotIn('built ', output)
                    for value in (PHONE, EMAIL, REMOTE, 'PRIVATE-DM', 'dynamicDestination'):
                        self.assertNotIn(value, output)
                finally: path.unlink()
        # A long-text failure would print its snippet unless leak failure wins.
        path = self.inputs / 'index.html'
        path.write_text('<p>' + PHONE + ' ' + 'x' * 200 + '</p>', encoding='utf-8')
        try:
            result = self.build()
            self.assertEqual(2, result.returncode)
            self.assertNotIn('LONG-TEXT', result.stdout)
            self.assertNotIn(PHONE, result.stdout + result.stderr)
        finally: path.unlink()

    def test_clean_release_preserves_publication_locks(self):
        result = self.build()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('leak check OK', result.stdout)
        self.assertRegex(result.stdout, r'built \d+ pages, \d+ venues')
        out = self.out
        home = (out / 'index.html').read_text()
        self.assertIn('<link rel="canonical" href="' + B.SITE_URL + '/">', home)
        self.assertIn('Allow: /', (out / 'robots.txt').read_text())
        self.assertNotIn('noindex', home)
        self.assertTrue((out / '404.html').is_file())
        self.assertTrue((out / 'google1cc2d472584fa095.html').is_file())
        self.assertFalse((out / 'ads').exists())
        sitemap = (out / 'sitemap.xml').read_text()
        self.assertNotIn('/ads/', sitemap)
        self.assertNotIn('/404.html', sitemap)
        for url in re.findall(r'<loc>(.*?)</loc>', sitemap):
            relative = url.removeprefix(B.SITE_URL).strip('/')
            self.assertTrue((out / relative / 'index.html').is_file())
        self.assertRegex(home, r'/assets/css/site.css\?v=[0-9a-f]+')
        finder = (out / 'finder/index.html').read_text()
        self.assertRegex(finder, r'/assets/js/finder.js\?v=[0-9a-f]+')
        self.assertIn("connect-src 'none'", (out / '_headers').read_text())
        self.assertEqual([], B.leak_check(out))


if __name__ == '__main__': unittest.main()
