"""Exercise the example's embedded Python with synthetic metadata and signals."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'proxy_heal_example.sh'
PORT = 1080
ARGV = (b'ssh', b'-N', b'-D', b'127.0.0.1:1080')
IDENTITY = (b'100', 1, 2, ARGV)


@pytest.fixture
def example():
    source = SCRIPT.read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    namespace = {'__name__': 'proxy_heal_example'}
    exec(compile(source, str(SCRIPT), 'exec'), namespace)
    # Every process and signaling API is mocked before any heal invocation.
    namespace['os'] = SimpleNamespace(
        getuid=Mock(return_value=1000), geteuid=Mock(return_value=1000),
        pidfd_open=Mock(return_value=99), close=Mock(), readlink=namespace['os'].readlink)
    namespace['signal'] = SimpleNamespace(
        SIGTERM=namespace['signal'].SIGTERM, pidfd_send_signal=Mock())
    example = SimpleNamespace(**namespace)
    example.namespace = namespace
    return example


@pytest.mark.parametrize('args,expected', [
    ([], None), (['1080', '1081'], None), (['0'], None), (['65536'], None),
    (['-1'], None), (['+80'], None), ([' 80'], None), (['80\n'], None),
    (['1.0'], None), (['１２３'], None), (['123456'], None),
    (['1'], 1), (['65535'], 65535), (['01080'], 1080), (['0001080'], 1080),
])
def test_port_validation(example, args, expected):
    assert example.parse_port(args) == expected
    if expected is None:
        assert example.main(args) == 2
        example.os.pidfd_open.assert_not_called()


@pytest.mark.parametrize('args,expected', [
    ([b'-N', b'-D', b'127.0.0.1:1080'], True),
    ([b'-fN', b'-D127.0.0.1:1080'], True),
    ([b'-ND127.0.0.1:1080'], True),
    ([b'-o', b'BatchMode=yes', b'-N', b'-D127.0.0.1:1080'], True),
    ([b'-N', b'-D', b'127.0.0.1:10801'], False),
    ([b'-N', b'-D', b'localhost:1080'], False),
    ([b'-N', b'-D', b'0.0.0.0:1080'], False),
    ([b'-N', b'-D', b'1080'], False),
    ([b'-D', b'127.0.0.1:1080'], False),
    ([b'-N', b'-L', b'127.0.0.1:1080'], False),
    ([b'-o', b'-N', b'-D127.0.0.1:1080'], False),
    ([b'-N', b'-o', b'-D127.0.0.1:1080'], False),
    ([b'position', b'-N', b'-D127.0.0.1:1080'], False),
    ([b'--', b'-N', b'-D127.0.0.1:1080'], False),
    ([b'-N', b'-D'], False),
    ([b'-Z', b'-N', b'-D127.0.0.1:1080'], False),
])
def test_only_exact_dynamic_forward_with_no_command(example, args, expected):
    assert example.matches_forward([b'ssh', *args], PORT) is expected


def fake_root(*pids):
    return SimpleNamespace(iterdir=lambda: [Path(str(pid)) for pid in pids])


@pytest.mark.parametrize('identities,code', [
    ({}, 3), ({10: None}, 3),
    ({10: IDENTITY, 11: IDENTITY}, 4),
    ({10: (b'100', 1, 2, (b'bash', b'loop'))}, 3),
])
def test_missing_or_ambiguous_process_never_signaled(example, identities, code):
    example.namespace['read_process'] = Mock(side_effect=lambda pid, *_: identities.get(pid))
    assert example.heal(PORT, fake_root(*identities)) == code
    example.os.pidfd_open.assert_not_called()
    example.signal.pidfd_send_signal.assert_not_called()


def test_unique_process_rechecked_then_only_term(example):
    reader = Mock(return_value=IDENTITY)
    example.namespace['read_process'] = reader
    root = fake_root(10)
    assert example.heal(PORT, root) == 0
    assert reader.call_args_list == [call(10, 1000, root), call(10, 1000, root)]
    example.os.pidfd_open.assert_called_once_with(10, 0)
    example.signal.pidfd_send_signal.assert_called_once_with(99, example.signal.SIGTERM, None, 0)
    example.os.close.assert_called_once_with(99)


@pytest.mark.parametrize('replacement', [
    None, (b'101', 1, 2, ARGV), (b'100', 1, 3, ARGV),
    (b'100', 1, 2, (b'ssh', b'-N', b'-D127.0.0.1:1081')),
])
def test_identity_change_before_signal_fails_closed(example, replacement):
    example.namespace['read_process'] = Mock(side_effect=[IDENTITY, replacement])
    assert example.heal(PORT, fake_root(10)) == 5
    example.signal.pidfd_send_signal.assert_not_called()
    example.os.close.assert_called_once_with(99)


@pytest.mark.parametrize('failure', ['open', 'signal', 'uid', 'platform'])
def test_process_errors_are_quiet(example, monkeypatch, capsys, failure):
    example.namespace['read_process'] = Mock(return_value=IDENTITY)
    if failure == 'open':
        example.os.pidfd_open.side_effect = OSError('SYNTHETIC_PRIVATE_METADATA')
    elif failure == 'signal':
        example.signal.pidfd_send_signal.side_effect = OSError('SYNTHETIC_PRIVATE_METADATA')
    elif failure == 'uid':
        example.os.geteuid.return_value = 0
    else:
        monkeypatch.delattr(example.os, 'pidfd_open')
    assert example.heal(PORT, fake_root(10)) == (6 if failure == 'platform' else 5)
    if failure != 'signal':
        example.signal.pidfd_send_signal.assert_not_called()
    else:
        example.os.close.assert_called_once_with(99)
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('variant,matched', [
    ('ssh', True), ('non_ssh', False), ('other_uid', False),
    ('mixed_uid', False), ('missing', False), ('bad_stat', False),
])
def test_linux_metadata_requires_actual_owned_ssh(example, tmp_path, variant, matched):
    process = tmp_path / '10'
    process.mkdir()
    uid = process.stat().st_uid
    # Synthetic /proc fields, including a comm with spaces and parentheses.
    stat = b'10 (ssh (worker)) ' + b' '.join([b'S'] + [b'0'] * 18 + [b'100'])
    (process / 'stat').write_bytes(b'bad' if variant == 'bad_stat' else stat)
    uids = [uid] * 4
    if variant == 'mixed_uid':
        uids[1] += 1
    (process / 'status').write_text('Uid:\t' + '\t'.join(map(str, uids)) + '\n')
    (process / 'cmdline').write_bytes(b'\0'.join(ARGV) + b'\0')
    executable = tmp_path / ('bash' if variant == 'non_ssh' else 'ssh')
    executable.touch()
    (process / 'exe').symlink_to(executable)
    if variant == 'missing':
        (process / 'cmdline').unlink()
    identity = example.read_process(10, uid + 1 if variant == 'other_uid' else uid, tmp_path)
    assert (identity is not None) is matched
    if matched:
        assert identity[0] == b'100' and identity[-1] == ARGV
