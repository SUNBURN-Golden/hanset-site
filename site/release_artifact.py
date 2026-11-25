"""Verify and seal the static artifact passed to the operations deploy script."""

from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree

import build_site as builder
import release_manifest as rm

VERIFICATION = 'google1cc2d472584fa095.html'
SITEMAP_NS = '{http://www.sitemaps.org/schemas/sitemap/0.9}'


def release_shape(dist, manifest):
    try:
        files = manifest['artifacts']
        if (not files.get(VERIFICATION, {}).get('bytes') or not files.get('404.html', {}).get('bytes')
                or any(name == 'ads.html' or name.startswith('ads/') for name in files)
                or (dist / 'ads').exists()
                or 'noindex' in (dist / 'index.html').read_text().casefold()
                or 'Allow: /' not in (robots := (dist / 'robots.txt').read_text().splitlines())
                or 'Disallow: /' in robots):
            raise ValueError
        root = ElementTree.fromstring((dist / 'sitemap.xml').read_bytes())
        if root.tag != SITEMAP_NS + 'urlset':
            raise ValueError
        seen = set()
        for entry in root:
            loc = entry.findtext(SITEMAP_NS + 'loc')
            if entry.tag != SITEMAP_NS + 'url' or not loc or not loc.startswith(builder.SITE_URL + '/'):
                raise ValueError
            route = urlsplit(loc)
            name = route.path[1:] + ('index.html' if route.path.endswith('/') else '')
            if (route.query or route.fragment or name in ('404.html', VERIFICATION)
                    or name not in manifest['pages'] or loc in seen):
                raise ValueError
            seen.add(loc)
        if builder.SITE_URL + '/' not in seen:
            raise ValueError
    except (OSError, ValueError, ElementTree.ParseError):
        raise rm.GateError('release_shape', 'invalid_release_output') from None


def seal(dist: Path, data_sha, *, expected=None):
    manifest = rm.artifact_manifest(dist, data_sha)
    rm.require_home(manifest)
    if builder.leak_check(dist):
        raise rm.GateError('leak', 'emitted_output_rejected')
    release_shape(dist, manifest)
    if expected is not None and manifest != expected:
        raise rm.GateError('sanity', 'artifact_drift')
    return manifest
