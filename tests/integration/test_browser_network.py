"""Mandatory real-browser boundary test. Missing Linux/Chromium support is a failure.

Run separately: python3 -m unittest discover -s tests/integration -p 'test_*.py'
"""
import os
from pathlib import Path
import pwd
import subprocess
import sys
import unittest


class RealBrowserNetwork(unittest.TestCase):
    def test_isolated_browser_and_real_network_gate(self):
        # One retry: the case uses tight real-time budgets (page.goto 1.5s, short leases),
        # and on the shared box concurrent Chromium runs by other agents can starve it.
        # A real gate failure fails both attempts; the first attempt's log is kept.
        first = self._run()
        if first.returncode == 0 and b'forbidden_connections=0 forbidden_packets=0' in first.stdout:
            return
        sys.stderr.write('browser_network_case attempt 1 failed, retrying once:\n' + first.stdout.decode()[-2000:] + first.stderr.decode()[-2000:])
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
        self.assertIn('forbidden_connections=0 forbidden_packets=0', result.stdout.decode())

    def _run(self):
        root = Path(__file__).resolve().parents[2]
        env = {**os.environ, 'PYTHONPATH': str(root)}
        if 'PLAYWRIGHT_BROWSERS_PATH' not in env:
            cache = Path.home() / '.cache' / 'ms-playwright'
            if not cache.is_dir():
                # The gate uses a temporary HOME. Look up the account before UID mapping.
                cache = Path(pwd.getpwuid(os.getuid()).pw_dir) / '.cache' / 'ms-playwright'
            env['PLAYWRIGHT_BROWSERS_PATH'] = str(cache)
        return subprocess.run([
            'unshare', '--user', '--map-root-user', '--net', '--mount', '--pid', '--fork',
            '--kill-child=KILL', '--mount-proc', sys.executable,
            str(Path(__file__).with_name('browser_network_case.py'))],
            cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=360,
            env=env)
