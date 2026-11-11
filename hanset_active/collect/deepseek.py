"""Bounded DeepSeek chat call with one Qwen backup attempt.

Transport failures never carry response text. The Qwen key is read at
runtime from a host file and is never logged, persisted, or committed.
"""
import json
import os

import requests

from . import CollectError
from .config import DEFAULT_MODEL
from .credentials import qwen_key
from .ground import INSTRUCTION

ENDPOINT = 'https://api.deepseek.com/chat/completions'
OUTPUT_TOKENS = 1200
INPUT_LIMIT = 24_000
BODY_LIMIT = 64_000
DEEPSEEK_TIMEOUT = (5, 20)
DEEPSEEK_EXTRAS = {
    'response_format': {'type': 'json_object'},
    'thinking': {'type': 'disabled'},
}

QWEN_ENDPOINT = 'https://token-plan.maas.qwencloudapi.com/compatible-mode/v1/chat/completions'
QWEN_MODEL = 'qwen4-max-preview'
QWEN_TIMEOUT = (5, 60)
# No DeepSeek-only thinking param; JSON mode omitted until validated live.
QWEN_EXTRAS = {}

# Owner-approved backup tier (2026-11-11): one Qwen retry after these primary
# failures. Invalid output, exhausted budget, and missing DeepSeek credentials
# never fall back.
FALLBACK_REASONS = frozenset({
    'model_unavailable', 'model_rate_limited', 'model_transport',
    'model_timeout', 'model_auth',
})


def credentials_present():
    key = os.environ.get('DEEPSEEK_API_KEY', '').strip()
    return bool(key) and '\n' not in key and '\x00' not in key and len(key) <= 300


def new_qwen_session():
    # Model calls go direct; the SOCKS proxy is for web fetches only.
    session = requests.Session()
    session.trust_env = False
    return session


class DeepSeek:
    def __init__(self, state, *, model=DEFAULT_MODEL, session=None, qwen_session=None):
        self.state = state
        self.model = model
        self._own = session is None
        self.session = session or requests.Session()
        if self._own:
            self.session.trust_env = False
        self.qwen_session = qwen_session

    def close(self):
        if self._own:
            self.session.close()

    def _payload(self, content, instruction, *, model, extras):
        body = {
            'model': model,
            'messages': [
                {'role': 'system', 'content': instruction},
                {'role': 'user', 'content': content},
            ],
            'temperature': 0,
            'stream': False,
            'max_tokens': OUTPUT_TOKENS,
        }
        body.update(extras)
        return body

    def _attempt(self, session, endpoint, headers, payload, *, timeout):
        response = None
        try:
            response = session.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            )
            code = response.status_code
            if code != 200:
                if code in (400, 404):
                    raise CollectError('model_unavailable')
                if code in (401, 403):
                    raise CollectError('model_auth')
                if code == 429:
                    raise CollectError('model_rate_limited')
                raise CollectError('model_transport')
            body = bytearray()
            for chunk in response.iter_content(8192):
                if chunk:
                    body.extend(chunk)
                if len(body) > BODY_LIMIT:
                    raise CollectError('model_invalid')
            envelope = json.loads(bytes(body))
            message = envelope['choices'][0]['message']
            # Reasoning text is discarded even if the provider includes it.
            parsed = json.loads(message['content'])
            if not isinstance(parsed, dict):
                raise CollectError('model_invalid')
            usage = envelope.get('usage')
            used = usage.get('completion_tokens') if isinstance(usage, dict) else None
            return parsed, used
        except CollectError:
            raise
        except requests.Timeout:
            raise CollectError('model_timeout') from None
        except requests.RequestException:
            raise CollectError('model_transport') from None
        except Exception:
            raise CollectError('model_invalid') from None
        finally:
            if response is not None:
                response.close()

    def ask(self, documents, *, instruction=INSTRUCTION):
        if not credentials_present():
            raise CollectError('missing_credentials')
        content = json.dumps(documents, ensure_ascii=False, separators=(',', ':'))
        if len(content) > INPUT_LIMIT:
            raise CollectError('model_budget_exhausted')
        # A reserved call stays spent even when the attempt fails.
        self.state.reserve_model(len(content), OUTPUT_TOKENS)
        try:
            parsed, used = self._attempt(
                self.session,
                ENDPOINT,
                {'Authorization': 'Bearer ' + os.environ['DEEPSEEK_API_KEY'].strip(),
                 'Content-Type': 'application/json'},
                self._payload(content, instruction, model=self.model, extras=DEEPSEEK_EXTRAS),
                timeout=DEEPSEEK_TIMEOUT,
            )
        except CollectError as original:
            if original.reason not in FALLBACK_REASONS:
                raise
            return self._qwen_fallback(content, instruction, original)
        self.state.reconcile_output(OUTPUT_TOKENS, used)
        self.state.record_provider('deepseek')
        return parsed

    def _qwen_fallback(self, content, instruction, original):
        key = qwen_key()
        if not key:
            # Missing/unreadable key file: skip the backup silently.
            raise original from None
        try:
            self.state.reserve_model(len(content), OUTPUT_TOKENS)
        except CollectError as error:
            if error.reason == 'model_budget_exhausted':
                raise original from None
            raise
        session = self.qwen_session if self.qwen_session is not None else new_qwen_session()
        try:
            parsed, used = self._attempt(
                session,
                QWEN_ENDPOINT,
                {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
                self._payload(content, instruction, model=QWEN_MODEL, extras=QWEN_EXTRAS),
                timeout=QWEN_TIMEOUT,
            )
        except CollectError:
            # No third tier: keep the original primary reason for existing handling.
            raise original from None
        finally:
            if self.qwen_session is None:
                session.close()
        self.state.reconcile_output(OUTPUT_TOKENS, used)
        self.state.record_provider('qwen')
        return parsed
