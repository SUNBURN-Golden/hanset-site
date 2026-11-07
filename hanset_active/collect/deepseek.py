"""Bounded DeepSeek chat call. Transport failures never carry response text."""
import json
import os

import requests

from . import CollectError
from .config import DEFAULT_MODEL
from .ground import INSTRUCTION

ENDPOINT = 'https://api.deepseek.com/chat/completions'
OUTPUT_TOKENS = 1200
INPUT_LIMIT = 24_000
BODY_LIMIT = 64_000


def credentials_present():
    key = os.environ.get('DEEPSEEK_API_KEY', '').strip()
    return bool(key) and '\n' not in key and '\x00' not in key and len(key) <= 300


class DeepSeek:
    def __init__(self, state, *, model=DEFAULT_MODEL, session=None):
        self.state = state
        self.model = model
        self._own = session is None
        self.session = session or requests.Session()
        if self._own:
            self.session.trust_env = False

    def close(self):
        if self._own:
            self.session.close()

    def ask(self, documents, *, instruction=INSTRUCTION):
        if not credentials_present():
            raise CollectError('missing_credentials')
        content = json.dumps(documents, ensure_ascii=False, separators=(',', ':'))
        if len(content) > INPUT_LIMIT:
            raise CollectError('model_budget_exhausted')
        self.state.reserve_model(len(content), OUTPUT_TOKENS)
        response = None
        try:
            response = self.session.post(
                ENDPOINT,
                headers={'Authorization': 'Bearer ' + os.environ['DEEPSEEK_API_KEY'].strip(), 'Content-Type': 'application/json'},
                json={
                    'model': self.model,
                    'messages': [
                        {'role': 'system', 'content': instruction},
                        {'role': 'user', 'content': content},
                    ],
                    'response_format': {'type': 'json_object'},
                    'temperature': 0,
                    'stream': False,
                    'max_tokens': OUTPUT_TOKENS,
                    'thinking': {'type': 'disabled'},
                },
                timeout=(5, 20),
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
            self.state.reconcile_output(OUTPUT_TOKENS, envelope.get('usage', {}).get('completion_tokens'))
            return parsed
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
