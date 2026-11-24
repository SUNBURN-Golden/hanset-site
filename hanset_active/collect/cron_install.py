"""Install only the explicitly owned collector entries in the user's crontab."""
import argparse
import difflib
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

from . import CollectError
from .config import outside
from .handoff import Outbox
from .schedules import CHECKOUT, DATA, JOBS, MARKER, owned_entries

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
        command = fields[1] if len(fields) == 2 else b''
    else:
        fields = line.split(None, 5)
        if len(fields) != 6 or not all(valid_field(field, *spec)
                                       for field, spec in zip(fields, FIELDS)):
            return None
        command = fields[5]

    # Cron consumes unescaped percent signs before the shell parses its command.
    # The marker must start a shell comment, outside quotes and after a separator.
    quote, escaped, boundary = None, False, True
    marker_at = len(command) - len(MARKER)
    for index, char in enumerate(command):
        if escaped:
            escaped, boundary = False, False
        elif char == ord('%'):
            return None
        elif char == ord('\\') and quote != ord("'"):
            escaped = True
        elif quote is not None:
            if char == quote:
                quote = None
        elif char in (ord("'"), ord('"'), ord('`')):
            quote, boundary = char, False
        elif char == ord('#') and boundary:
            return command[:index].rstrip() if index == marker_at and index > 0 else None
        else:
            boundary = char in b' \t;|&()<>'
    return None


def owned(line):
    return owned_command(line) is not None


def job_name(command):
    try:
        words = shlex.split(os.fsdecode(command))
    except ValueError:
        return None
    if words and words[0] in ('env', '/usr/bin/env'):
        words.pop(0)
    while words and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*=.*', words[0]):
        words.pop(0)
    if words and words[0] in ('bash', '/bin/bash', '/usr/bin/bash', 'sh', '/bin/sh'):
        words.pop(0)
    if not words:
        return None
    for name, script in (
        ('seed_discovery', JOBS['seed_discovery'].script),
        ('wide_collect', JOBS['wide_collect'].script),
        ('watchdog', 'collect_watchdog.sh'),
    ):
        if words[0] == f'{CHECKOUT}/scripts/{script}' and (name != 'wide_collect' or words[1:2] == ['wide']):
            return name
    return None


def proposed(table, uninstall):
    entries = {} if uninstall else owned_entries()
    lines = []
    # Split on LF only so unrelated lines (including CRLF) retain their bytes.
    for line in lf_lines(table):
        command = owned_command(line)
        if command is not None:
            replacement = entries.pop(job_name(command), None)
            if replacement is not None:
                lines.append(replacement)
        else:
            lines.append(line)
    if entries and lines and not lines[-1].endswith(b'\n'):
        lines.append(b'\n')
    lines.extend(entries.values())
    return b''.join(lines)


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


def timezone_conflict(table, default):
    zone = default
    for line in lf_lines(table):
        if ZONE_LINE.match(line):
            match = ASSIGNMENT.fullmatch(line.rstrip(b'\r\n'))
            zone = next((value.decode('ascii', 'replace') for value in match.groups() if value is not None), '') if match else None
        command = owned_command(line)
        if command is not None and (default not in SEOUL or zone not in SEOUL):
            return job_name(command) or 'managed_job'
    return None


def show_diff(old, new):
    # Zero context prevents adjacent unowned environment assignments leaking.
    changes = difflib.diff_bytes(difflib.unified_diff, lf_lines(old), lf_lines(new),
                                 fromfile=b'crontab', tofile=b'crontab (updated)', n=0)
    for line in changes:
        if line.startswith(b'-') and line != b'--- crontab\n':
            line = b'-[redacted]' + (b' ' + MARKER if owned(line[1:]) else b'') + b'\n'
        elif line.startswith(b'+') and line != b'+++ crontab (updated)\n':
            if line[1:] not in owned_entries().values():
                line = b'+[line ending]\n'
        sys.stdout.buffer.write(line)


def main(argv=None, *, data_root=DATA):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--uninstall', action='store_true')
    args = parser.parse_args(argv)
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
    desired = proposed(current.stdout, args.uninstall)
    if args.dry_run:
        show_diff(current.stdout, desired)
    if not args.uninstall:
        conflict = timezone_conflict(desired, scheduler_default())
        try:
            ready = Outbox(outside(Path(data_root))).ready()
        except (OSError, CollectError):
            ready = False
        if conflict:
            diagnostic('scheduler_timezone_unverified_or_conflicting ' + conflict)
        if not ready:
            diagnostic('publication_readiness_required')
        if conflict or not ready:
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
