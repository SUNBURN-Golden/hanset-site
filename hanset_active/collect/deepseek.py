"""Run-scoped Qwen Token Plan routing and health-gated DeepSeek fallback."""
import json
import math
import os
import re
from contextlib import closing

import requests

from . import CollectError, runtime
from .config import DEFAULT_MODEL, QWEN_MODEL, configured_model
from .credentials import qwen_key
from .ground import INSTRUCTION

ENDPOINT = 'https://api.deepseek.com/chat/completions'
BALANCE_ENDPOINT = 'https://api.deepseek.com/user/balance'
OUTPUT_TOKENS_PER_ITEM = 1200
MAX_BATCH_ITEMS = 6
INPUT_LIMIT = 24_000
BODY_LIMIT = 256_000
INSTRUCTION_ALLOWANCE = 2_000
DEEPSEEK_TIMEOUT = (5, 20)
DEEPSEEK_EXTRAS = {
    'response_format': {'type': 'json_object'},
    'thinking': {'type': 'disabled'},
}

QWEN_ENDPOINT = 'https://token-plan.maas.qwencloudapi.com/compatible-mode/v1/chat/completions'
QWEN_TIMEOUT = (5, 60)
# Qwen thinking can consume the output cap without producing JSON content.
QWEN_EXTRAS = {'enable_thinking': False}

_HTTP_ERRORS = {
    400: 'model_unavailable', 404: 'model_unavailable', 401: 'model_auth', 403: 'model_auth',
    402: 'model_insufficient_balance', 408: 'model_timeout', 429: 'model_rate_limited',
}
_API_ERRORS = {
    'insufficient_balance': 'model_insufficient_balance',
    'invalid_api_key': 'model_auth',
    'model_not_found': 'model_unavailable',
    'rate_limit_exceeded': 'model_rate_limited',
}


def deepseek_key():
    key = os.environ.get('DEEPSEEK_API_KEY', '').strip()
    return key if '\n' not in key and '\x00' not in key and len(key) <= 300 else ''


def credentials_present():
    return bool(qwen_key() or deepseek_key())


def new_qwen_session():
    session = requests.Session()
    session.trust_env = False
    return session


def encode_payload(documents):
    return json.dumps(documents, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def fits(content, instruction):
    return len(content) + max(len(instruction), INSTRUCTION_ALLOWANCE) <= INPUT_LIMIT


def _number(value):
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _usage(envelope):
    usage = envelope.get('usage')
    if not isinstance(usage, dict):
        return {}
    cost, currency = usage.get('cost'), usage.get('currency')
    if (type(cost) not in (int, float) or not 0 <= cost <= 1e12
            or not math.isfinite(cost) or currency not in ('USD', 'CNY', 'KRW')):
        cost, currency = None, None
    return {'prompt': _number(usage.get('prompt_tokens')),
            'completion': _number(usage.get('completion_tokens')), 'cost': cost, 'currency': currency}


def _response(request, endpoint, key, *, timeout, **kwargs):
    response = None
    try:
        response = request(endpoint, headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
                           timeout=timeout, allow_redirects=False, stream=True, **kwargs)
        status = response.status_code
        http_error = _HTTP_ERRORS.get(status, 'model_transport') if status != 200 else None
        if status == 402 or 300 <= status < 400:
            raise CollectError(http_error)
        try:
            body = bytearray()
            for chunk in response.iter_content(8192):
                body.extend(chunk)
                if len(body) > BODY_LIMIT:
                    raise CollectError('model_invalid')
            envelope = json.loads(body, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
        except (CollectError, requests.RequestException, UnicodeError, ValueError, TypeError, RecursionError):
            if http_error:
                raise CollectError(http_error) from None
            raise
        error = envelope.get('error') if isinstance(envelope, dict) else None
        error_code = error.get('code') if isinstance(error, dict) else None
        if isinstance(error_code, str) and error_code in _API_ERRORS:
            raise CollectError(_API_ERRORS[error_code])
        if http_error or error is not None:
            raise CollectError(http_error or 'model_transport')
        if not isinstance(envelope, dict):
            raise CollectError('model_invalid')
        return envelope
    except requests.Timeout:
        raise CollectError('model_timeout') from None
    except requests.RequestException:
        raise CollectError('model_transport') from None
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise CollectError('model_invalid') from None
    finally:
        if response is not None:
            response.close()


def _pairs(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise CollectError('model_invalid')
    return value


def _invalid_constant(_value):
    raise CollectError('model_invalid')


def _completion_json(envelope, provider):
    try:
        choice = envelope['choices'][0]
        if choice.get('finish_reason') not in (None, 'stop'):
            raise CollectError('model_invalid')
        content = choice['message']['content']
        if provider == 'qwen' and isinstance(content, str):
            content = content.strip()
            fence = re.fullmatch(r'```json[ \t]*\r?\n(.*?)\r?\n```', content, re.DOTALL)
            if fence:
                content = fence[1]
        parsed = json.loads(content, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
    except (AttributeError, IndexError, KeyError, ValueError, TypeError, RecursionError):
        raise CollectError('model_invalid') from None
    if not isinstance(parsed, dict):
        raise CollectError('model_invalid')
    return parsed


class DeepSeek:
    """Compatibility name for the shared Qwen-first collector router."""

    def __init__(self, state, *, model=DEFAULT_MODEL, session=None, qwen_session=None):
        self.state = state
        self.model = configured_model(model)
        self.session = session
        self.qwen_session = qwen_session
        self._owned = []
        self._deepseek_health = None
        self._balance_latched = False
        self._probed = False
        self.last_model = None
        self.last_attempt = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        for session in self._owned:
            session.close()
        self._owned.clear()

    def _session(self, provider):
        attr = 'qwen_session' if provider == 'qwen' else 'session'
        session = getattr(self, attr)
        if session is None:
            session = new_qwen_session()
            setattr(self, attr, session)
            self._owned.append(session)
        return session

    def _health(self, db, outcome):
        self._deepseek_health = outcome
        runtime.remember_health(db, 'deepseek', self.state.clock, outcome)
        if outcome == 'insufficient_balance':
            self._balance_latched = True
            self.state.deepseek_skipped = outcome

    def _deepseek_usable(self, db, key):
        if self._balance_latched:
            return False
        self._deepseek_health = runtime.health(db, 'deepseek', self.state.clock)
        if self._deepseek_health == 'insufficient_balance':
            self.state.deepseek_skipped = 'insufficient_balance'
        if self._deepseek_health is not None:
            return self._deepseek_health == 'healthy'
        if self._probed:
            return False

        self._probed = True
        attempt = runtime.dispatched(db, 'deepseek', self.model, 'probe', self.state.clock, 0, 0)
        try:
            envelope = _response(self._session('deepseek').get, BALANCE_ENDPOINT, key, timeout=DEEPSEEK_TIMEOUT)
            available = envelope.get('is_available')
            if type(available) is not bool or not isinstance(envelope.get('balance_infos', []), list):
                raise CollectError('model_invalid')
            outcome = 'healthy' if available else 'insufficient_balance'
        except CollectError as error:
            outcome = error.reason.removeprefix('model_')
        runtime.finished(db, attempt, outcome)
        self._health(db, outcome)
        return outcome == 'healthy'

    def _complete(self, db, provider, key, content, instruction, output):
        qwen = provider == 'qwen'
        model = QWEN_MODEL if qwen else self.model
        payload = {'model': model, 'messages': [{'role': 'system', 'content': instruction},
                                               {'role': 'user', 'content': content}],
                   'temperature': 0, 'stream': False, 'max_tokens': output,
                   **(QWEN_EXTRAS if qwen else DEEPSEEK_EXTRAS)}
        chars = len(content) + len(instruction)
        self.state.reserve_model(chars, output)
        attempt = runtime.dispatched(db, provider, model, 'completion', self.state.clock, chars, output)
        self.state.record_provider(provider)
        self.last_attempt = attempt
        usage, outcome, completion_received = {}, 'ok', False
        try:
            envelope = _response(self._session(provider).post, QWEN_ENDPOINT if qwen else ENDPOINT, key,
                                 timeout=QWEN_TIMEOUT if qwen else DEEPSEEK_TIMEOUT, json=payload)
            usage = _usage(envelope)
            completion_received = isinstance(envelope.get('choices'), list) and bool(envelope['choices'])
            parsed = _completion_json(envelope, provider)
        except CollectError as error:
            outcome = error.reason
            raise
        finally:
            runtime.finished(db, attempt, outcome, **usage)
            self.state.reconcile_output(output, usage.get('completion'))
            if not qwen:
                # A completion with malformed content still establishes
                # transport and balance health for the smaller requests.
                if completion_received:
                    self._health(db, 'healthy')
                elif outcome != 'model_invalid':
                    self._health(db, outcome.removeprefix('model_'))
        self.last_model = model
        return parsed

    def reject_last(self):
        runtime.invalid_output(self.state.active_dir, self.last_attempt)

    def ask(self, documents, *, instruction=INSTRUCTION):
        self.last_attempt = self.last_model = None
        content = encode_payload(documents)
        count = len(documents) if isinstance(documents, list) else 1
        if not 1 <= count <= MAX_BATCH_ITEMS or not fits(content, instruction):
            raise CollectError('model_input_limit')
        output = OUTPUT_TOKENS_PER_ITEM * count
        primary, fallback = qwen_key(), deepseek_key()
        if not primary and not fallback:
            raise CollectError('missing_credentials')
        with closing(runtime.connect(self.state.active_dir)) as db:
            reason = 'model_unavailable'
            if primary:
                try:
                    return self._complete(db, 'qwen', primary, content, instruction, output)
                except CollectError as error:
                    if error.reason == 'model_invalid':
                        raise
                    reason = error.reason
            if fallback and self._deepseek_usable(db, fallback):
                return self._complete(db, 'deepseek', fallback, content, instruction, output)
            if self._deepseek_health and not primary:
                reason = 'model_' + self._deepseek_health
            raise CollectError(reason)
