"""Child process entrypoint: driver AND Chromium live in an empty network namespace.

Only the parent can fetch. stdin/stdout is a bounded, single-job response protocol.
"""
import base64
import json
import os
from pathlib import Path
import socket
import fcntl
import struct
import subprocess
import sys
import tempfile
from .config import Rejected
from .privacy_filter import text

PHASE = 'isolation'

EXTRACT = r'''() => {
  const root = document.querySelector('main, article');
  if (!root) throw new Error('missing_minimal_region');
  const blocked = 'input,textarea,[contenteditable],form,iframe,script,style,template,nav,footer,header,[hidden],[aria-hidden="true"],[class*="private"],[class*="profile"],[class*="comment"],[class*="message"],[class*="chat"]';
  if (root.closest(blocked) || !root.getClientRects().length || getComputedStyle(root).display === 'none' || getComputedStyle(root).visibility === 'hidden') throw new Error('private_region');
  const copy = root.cloneNode(true);
  const originals = [...root.querySelectorAll('*')], clones = [...copy.querySelectorAll('*')];
  for (let i=0; i<originals.length; i++) {
    const e=originals[i], style=getComputedStyle(e);
    if(e.matches(blocked) || style.display === 'none' || style.visibility === 'hidden') clones[i].remove();
  }
  return {title:document.title.slice(0,240),excerpt:copy.textContent.slice(0,24000),locator:root.tagName.toLowerCase(),url:location.href};
}'''


def send(value):
    sys.stdout.write(json.dumps(value) + '\n')
    sys.stdout.flush()


def receive():
    line = sys.stdin.buffer.readline(3000000)
    if not line or len(line) >= 3000000:
        raise Rejected('ipc_failure')
    return json.loads(line)


def isolation_check():
    # Fresh unshare --net has only a DOWN loopback and no routes, including IPv6.
    interfaces = {line.split(':')[0].strip() for line in Path('/proc/net/dev').read_text().splitlines()[2:]}
    if interfaces != {'lo'}:
        raise Rejected('network_isolation_missing')
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        flags = struct.unpack_from('H', fcntl.ioctl(probe, 0x8913, struct.pack('256s', b'lo')), 16)[0]
        if flags & 1:
            raise Rejected('loopback_must_be_down')
    if len(Path('/proc/net/route').read_text().splitlines()) > 1 or Path('/proc/net/if_inet6').read_text().strip():
        raise Rejected('network_routes_present')
    for family, address in ((socket.AF_INET, ('127.0.0.1', 9)), (socket.AF_INET6, ('::1', 9))):
        with socket.socket(family) as s:
            s.settimeout(.2)
            if s.connect_ex(address) == 0:
                raise Rejected('isolation_probe_failed')


async def extract(config):
    global PHASE
    import asyncio
    from playwright.async_api import async_playwright
    ipc_lock = asyncio.Lock()
    PHASE = 'driver'
    async with async_playwright() as p:
        PHASE = 'launch'
        browser = await p.chromium.launch(headless=True, timeout=15000, args=[
            '--no-sandbox', '--disable-quic', '--disable-background-networking',
            '--disable-component-update', '--disable-sync', '--no-first-run',
            '--disable-extensions', '--disable-features=WebRtcHideLocalIpsWithMdns',
            '--force-webrtc-ip-handling-policy=disable_non_proxied_udp', '--disable-breakpad'])
        context = await browser.new_context(service_workers='block', accept_downloads=False,
                                            permissions=[], java_script_enabled=True)
        PHASE = 'context'
        async def close_websocket(route):
            await route.close()
        await context.route_web_socket('**/*', close_websocket)
        page = await context.new_page()
        blocked_navigation = False
        async def route_request(route):
            nonlocal blocked_navigation
            request = route.request
            try:
                if request.frame.page != page or request.frame != page.main_frame:
                    await route.abort()
                    return
                async with ipc_lock:
                    send({'type': 'request', 'url': request.url, 'method': request.method,
                          'resource_type': request.resource_type})
                    response = await asyncio.to_thread(receive)
                if response.get('type') != 'response':
                    if request.is_navigation_request():
                        blocked_navigation = True
                    await route.abort()
                    return
                await route.fulfill(status=response['status'], headers=response['headers'],
                                    body=base64.b64decode(response['body']))
            except Exception:
                try: await route.abort()
                except Exception: pass
        await context.route('**/*', route_request)
        page.on('dialog', lambda dialog: asyncio.create_task(dialog.dismiss()))
        PHASE = 'navigation'
        await page.goto(config['url'], wait_until='domcontentloaded', timeout=45000)
        await page.wait_for_timeout(config.get('settle_ms', 500))
        if blocked_navigation:
            raise Rejected('navigation_denied')
        PHASE = 'extraction'
        raw = await page.evaluate(EXTRACT)
        result = {'title': text(raw['title'])[:240], 'excerpt': text(raw['excerpt'])[:2000],
                  'locator': raw['locator'], 'facts': [], 'published_at': None,
                  'document_key': config['document_id'], 'final_url': raw['url']}
        if not result['excerpt']:
            raise Rejected('empty_region')
        await context.close()
        await browser.close()
    return result


def main():
    try:
        isolation_check()
        # Original bytes, Chromium profiles and temporary driver artifacts stay in RAM.
        subprocess.run(['mount', '-t', 'tmpfs', '-o', 'size=256m,mode=1777', 'tmpfs', '/tmp'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.environ['TMPDIR'] = '/tmp'
        for key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME'):
            Path(os.environ[key]).mkdir(mode=0o700, parents=True, exist_ok=True)
        import asyncio
        result = asyncio.run(extract(receive()))
        send({'type': 'result', 'data': result})
    except BaseException as exc:
        send({'type': 'error', 'reason': 'browser_extraction_failed', 'error_type': type(exc).__name__, 'phase': PHASE})
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
