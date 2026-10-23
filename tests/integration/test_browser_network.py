"""Mandatory real-browser boundary test. Missing Linux/Chromium support is a failure.

Run separately: python3 -m unittest discover -s tests/integration -p 'test_*.py'
"""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class RealBrowserNetwork(unittest.TestCase):
    def test_isolated_browser_and_real_network_gate(self):
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run([
            'unshare', '--user', '--map-root-user', '--net', '--mount', '--pid', '--fork',
            '--kill-child=KILL', '--mount-proc', sys.executable,
            str(Path(__file__).with_name('browser_network_case.py'))],
            cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
            env={**os.environ, 'PYTHONPATH': str(root)})
        self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
        self.assertIn('forbidden_connections=0 forbidden_packets=0', result.stdout.decode())
