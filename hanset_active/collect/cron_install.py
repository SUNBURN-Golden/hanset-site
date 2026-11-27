"""Install only the explicitly owned collector entries in the user's crontab."""
import argparse
from dataclasses import dataclass
import difflib
import os
from pathlib import Path
import re
import subprocess
import sys

from . import CollectError
from .config import outside
from .cron_command import Command, cron_decode
from .handoff import Outbox
from .proxy_env import PROXY_NAME, SELECTOR
from .schedules import CHECKOUT, DAILY_PUBLISH, DATA, JOBS, MARKER, ProxyAssignments, owned_entries

ASSIGNMENT = re.compile(rb"""^[ \t]*CRON_TZ\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s]+))\s*$""")
ZONE_LINE = re.compile(rb'^[ \t]*CRON_TZ\s*=')
SEOUL = {'Asia/Seoul', 'ROK'}

MACROS = {b'@reboot', b'@yearly', b'@annually', b'@monthly', b'@weekly',
          b'@daily', b'@midnight', b'@hourly'}
MONTHS = b'jan feb mar apr may jun jul aug sep oct nov dec'.split()
DAYS = b'sun mon tue wed thu fri sat'.split()
FIELDS = ((0, 59, []), (0, 23, []), (1, 31, []), (1, 12, MONTHS), (0, 7, DAYS))
ENVIRONMENT = re.compile(rb'^[ \t]*[^ \t=]+[ \t]*=')


def valid_field(field, low, high, names):
    def number(value):
        if value.isdigit() and len(value) <= 3:
            return int(value)
        if value.lower() in names:
            return low + names.index(value.lower())
        return -1

    for entry in field.split(b','):
        bounds, slash, step = entry.partition(b'/')
        if slash and (not step.isdigit() or len(step) > 3 or int(step) == 0):
            return False
        if bounds == b'*':
            continue
        first, dash, last = bounds.partition(b'-')
        a, b = number(first), number(last) if dash else number(first)
        if not (low <= a <= b <= high) or (slash and not dash):
            return False
    return True


def owned_command(line):
    line = line.rstrip(b' \t\r\n')
    if (not line.endswith((b' ' + MARKER, b'\t' + MARKER))
            or line.lstrip().startswith(b'#') or ENVIRONMENT.match(line)):
        return None
    fields = line.split(None, 1)
    if fields[0] in MACROS:
        if len(fields) != 2:
            return None
    else:
        fields = line.split(None, 5)
        if len(fields) != 6 or not all(valid_field(field, *spec)
                                       for field, spec in zip(fields, FIELDS)):
            return None
    raw = fields[-1]
    command, offsets, has_stdin = cron_decode(raw)
    if has_stdin:
        return None

    # Ownership depends on the shell comment, even for unclassifiable jobs.
    marker_at = len(command) - len(MARKER)
    quote, escaped, boundary = None, False, True
    for index, char in enumerate(command):
        if escaped:
            escaped, boundary = False, False
        elif char == ord('\\') and quote != ord("'"):
            escaped = True
        elif quote is not None:
            if char == quote:
                quote = None
        elif char in (ord("'"), ord('"'), ord('`')):
            quote, boundary = char, False
        elif char == ord('#') and boundary:
            return raw[:offsets[index]] if index == marker_at and index > 0 else None
        else:
            boundary = char in b' \t;|&()<>'
    return None


def owned(line):
    return owned_command(line) is not None


SHELLS = (b'bash', b'/bin/bash', b'/usr/bin/bash', b'sh', b'/bin/sh')
SCRIPTS = {name: f'{CHECKOUT}/scripts/{JOBS[name].script}'.encode()
           for name in ('seed_discovery', 'wide_collect', 'daily_collect')}
SCRIPTS['watchdog'] = f'{CHECKOUT}/scripts/collect_watchdog.sh'.encode()


def take_assignments(words, *, env=False):
    count = 0
    while count < len(words) and words[count].assignment_name(env=env) is not None:
        count += 1
    return words[:count], words[count:]


@dataclass(frozen=True)
class Invocation:
    shell: list
    env_command: bytes
    env: list
    words: list
    unsupported: bool

    @property
    def name(self):
        return job_name(self.words)

    @property
    def is_daily(self):
        end = next((i for i, word in enumerate(self.words) if word.operator), len(self.words))
        return job_name(self.words[:end]) == 'daily_collect'


def invocation(raw):
    command = Command.parse(raw)
    shell, words = take_assignments(command.words)
    env, env_command = [], b'env'
    unsupported = not command.complete or command.has_stdin
    if words and words[0].value in (b'env', b'/usr/bin/env') and not words[0].expands:
        env_command = words[0].raw
        words = words[1:]
        # Locate the executable for diagnostics even when env options make
        # copying its assignments unsafe.
        while words and not words[0].expands:
            option = words[0].value
            if option == b'--':
                env_command += b' ' + words[0].raw
                words = words[1:]
                break
            if option in (b'-', b'-i', b'--ignore-environment') or option.startswith((b'--unset=', b'--chdir=')):
                words = words[1:]
            elif option in (b'-u', b'--unset', b'-C', b'--chdir'):
                words = words[2:]
            else:
                break
            unsupported = True
        env, words = take_assignments(words, env=True)
    if words and words[0].value in SHELLS and not words[0].expands:
        words = words[1:]
        if words and words[0].value == b'--' and not words[0].expands:
            words = words[1:]
    return Invocation(shell, env_command, env, words, unsupported)


def job_name(words):
    if not words or words[0].expands:
        return None
    script, *args = (word.value for word in words)
    if script == SCRIPTS['daily_collect']:
        if args[:1] == [b'wide']:
            return 'wide_collect'
        if args in ([], [b'web']):
            return 'daily_collect'
        return None
    for name in ('seed_discovery', 'watchdog'):
        if script == SCRIPTS[name]:
            return name
    return None


DAILY_LINE = re.compile(rb'^([ \t]*15[ \t]+0?4[ \t]+\*[ \t]+\*[ \t]+\*[ \t]+)([^\r\n]+)(\r?\n)?$')
DAILY_TAG = b' # hanset-collx-daily'
SELECTOR_NAME = SELECTOR.encode()


def is_proxy(name):
    return name == SELECTOR_NAME or (name is not None and PROXY_NAME.fullmatch(name.decode('ascii')) is not None)


class UnsupportedDailyProxy(ValueError):
    pass


@dataclass(frozen=True)
class DailyEntry:
    prefix: bytes
    raw: bytes
    ending: bytes
    invocation: Invocation
    managed: bool
    decorated: bool

    @property
    def can_decorate(self):
        return not self.invocation.unsupported and self.invocation.name == 'daily_collect'

    def proxy_assignments(self):
        shell = proxy_words(self.invocation.shell)
        env = proxy_words(self.invocation.env, env=True)
        names = [name for name, _ in shell + env]
        if names and (not self.can_decorate or (SELECTOR_NAME in names and len(names) != 1)):
            raise UnsupportedDailyProxy
        return ProxyAssignments(tuple(word for _, word in shell), tuple(word for _, word in env),
                                self.invocation.env_command)

    def render(self, enabled):
        command = self.raw
        if enabled and self.can_decorate:
            command = DAILY_PUBLISH + command + DAILY_TAG
        return self.prefix + command + self.ending


def proxy_words(words, *, env=False):
    assignments = []
    for word in words:
        name = word.assignment_name(env=env)
        if not is_proxy(name):
            continue
        if (word.expands or (env and word.env_pattern)
                or (name == SELECTOR_NAME and not word.value.partition(b'=')[2].startswith(b'/'))):
            raise UnsupportedDailyProxy
        assignments.append((name, word.raw))
    return assignments


def daily_entry(line):
    match = DAILY_LINE.fullmatch(line)
    if not match:
        return None
    prefix, raw, ending = match.groups()
    managed = owned_command(line) is not None
    decorated = not managed and raw.startswith(DAILY_PUBLISH) and raw.endswith(DAILY_TAG)
    if decorated:
        raw = raw[len(DAILY_PUBLISH):-len(DAILY_TAG)]
    parsed = invocation(raw)
    if not parsed.is_daily:
        return None
    return DailyEntry(prefix, raw, ending or b'', parsed, managed, decorated)


def proposed(table, uninstall, *, daily_publish=False):
    enabled = daily_publish and not uninstall
    # Pick the source before any managed jobs are replaced. An operator's
    # daily entry wins even when a managed entry comes earlier in the table.
    original = lf_lines(table)
    daily = {index: entry for index, line in enumerate(original)
             if (entry := daily_entry(line)) is not None}
    operator_daily = next((entry for entry in daily.values() if not entry.managed), None)
    source = operator_daily
    if source is None and enabled:
        source = next(iter(daily.values()), None)
    entries = {}
    if not uninstall:
        assignments = source.proxy_assignments() if source is not None else ProxyAssignments()
        entries = owned_entries(daily_publish=enabled and operator_daily is None, proxy=assignments)

    lines = []
    for index, line in enumerate(original):
        entry = daily.get(index)
        if entry is not None and not entry.managed:
            lines.append(entry.render(enabled))
        elif (command := owned_command(line)) is not None:
            name = invocation(command).name
            if name == 'daily_collect' and source is not None and entry is not source:
                continue
            replacement = entries.pop(name, None)
            if replacement is not None:
                lines.append(replacement)
        else:
            lines.append(line)
    if entries and lines and not lines[-1].endswith(b'\n'):
        lines.append(b'\n')
    lines.extend(entries.values())
    return b''.join(lines)


def seed_only_table(table):
    entry = owned_entries()['seed_discovery']
    lines = []
    for line in lf_lines(table):
        command = owned_command(line)
        if command is not None and invocation(command).name == 'seed_discovery':
            if entry:
                lines.append(entry)
                entry = b''
        else:
            lines.append(line)
    separator = b'\n' if entry and table and not table.endswith(b'\n') else b''
    return b''.join(lines) + separator + entry


def lf_lines(table):
    *lines, last = table.split(b'\n')
    return [line + b'\n' for line in lines] + ([last] if last else [])


def scheduler_default():
    # The operator can attest the live daemon's default when its environment is
    # intentionally unreadable. The install command's own TZ is irrelevant.
    defaults = set()
    for path in Path('/proc').glob('[0-9]*/comm'):
        try:
            if path.read_text().strip() not in ('cron', 'crond'):
                continue
            environment = (path.parent / 'environ').read_bytes().split(b'\0')
            for entry in environment:
                if entry.startswith(b'TZ='):
                    defaults.add(entry[3:].decode('ascii').removeprefix(':'))
                    break
            else:
                target = (path.parent / 'root/etc/localtime').resolve()
                defaults.add(str(target).split('/zoneinfo/', 1)[-1])
        except (OSError, UnicodeError):
            continue
    if defaults:
        return defaults.pop() if len(defaults) == 1 else None
    return os.environ.get('HANSET_CRON_TIMEZONE')


def timezone_conflict(table, default, *, seed_only=False):
    zone = default
    for line in lf_lines(table):
        if ZONE_LINE.match(line):
            match = ASSIGNMENT.fullmatch(line.rstrip(b'\r\n'))
            zone = next((value.decode('ascii', 'replace') for value in match.groups() if value is not None), '') if match else None
        command = owned_command(line)
        if default not in SEOUL or zone not in SEOUL:
            if seed_only:
                if command is not None and invocation(command).name == 'seed_discovery':
                    return 'seed_discovery'
                continue
            if command is not None:
                return invocation(command).name or 'managed_job'
            if (entry := daily_entry(line)) is not None and entry.decorated:
                return 'daily_collect'
    return None


def show_diff(old, new):
    # Zero context prevents adjacent unowned environment assignments leaking.
    changes = difflib.diff_bytes(difflib.unified_diff, lf_lines(old), lf_lines(new),
                                 fromfile=b'crontab', tofile=b'crontab (updated)', n=0)
    stock = owned_entries(daily_publish=True).values()
    for line in changes:
        if line.startswith(b'-') and line != b'--- crontab\n':
            line = b'-[redacted]' + (b' ' + MARKER if owned(line[1:]) else b'') + b'\n'
        elif line.startswith(b'+') and line != b'+++ crontab (updated)\n':
            addition = line[1:]
            if addition not in stock:
                entry = daily_entry(addition)
                command = owned_command(addition)
                if entry is not None and entry.decorated:
                    line = b'+[daily_collect with HANSET_AUTO_PUBLISH=1]\n'
                elif entry is not None:
                    line = b'+[daily_collect]\n'
                elif command is not None:
                    name = invocation(command).name or 'managed_job'
                    line = f'+[{name}]\n'.encode()
                else:
                    line = b'+[line ending]\n'
        sys.stdout.buffer.write(line)


def main(argv=None, *, data_root=DATA):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--uninstall', action='store_true')
    parser.add_argument('--daily-publish', action='store_true')
    parser.add_argument('--seed-only', action='store_true', help='install only the managed seed discovery entry')
    args = parser.parse_args(argv)
    if args.seed_only and (args.uninstall or args.daily_publish):
        parser.error('--seed-only cannot be combined with --uninstall or --daily-publish')
    env = {**os.environ, 'LC_ALL': 'C'}
    try:
        current = subprocess.run(['crontab', '-l'], capture_output=True, env=env, check=False)
    except OSError:
        return diagnostic('crontab_read_failed')
    if current.returncode:
        # Vixie cron and Cronie both report the absent table in this form.
        if (current.returncode != 1 or current.stdout or
                not re.fullmatch(rb'(?:crontab: )?no crontab for [^\r\n]+\n?', current.stderr)):
            return diagnostic('crontab_read_failed')
    try:
        desired = (seed_only_table(current.stdout) if args.seed_only else
                   proposed(current.stdout, args.uninstall, daily_publish=args.daily_publish))
    except UnsupportedDailyProxy:
        return diagnostic('daily_proxy_settings_unsupported')
    if args.dry_run:
        show_diff(current.stdout, desired)
    if not args.uninstall:
        conflict = timezone_conflict(desired, scheduler_default(), seed_only=args.seed_only)
        if conflict:
            diagnostic('scheduler_timezone_unverified_or_conflicting ' + conflict)
        if not args.seed_only:
            try:
                ready = Outbox(outside(Path(data_root))).ready()
            except (OSError, CollectError):
                ready = False
            if not ready:
                return diagnostic('publication_readiness_required')
        if conflict:
            return 1
    if not args.dry_run and desired != current.stdout:
        try:
            result = subprocess.run(['crontab', '-'], input=desired, capture_output=True, env=env, check=False)
        except OSError:
            return diagnostic('crontab_write_failed')
        if result.returncode:
            return diagnostic('crontab_write_failed')
    return 0


def diagnostic(code):
    print(code, file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
