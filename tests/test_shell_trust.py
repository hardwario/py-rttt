import pytest

from rttt import shell_trust
from rttt.shell_trust import (
    compute_hash,
    ensure_shell_trust,
    extract_shell_entries,
    load_trusted,
    save_trusted,
)


@pytest.fixture
def trust_file(tmp_path, monkeypatch):
    path = tmp_path / 'rttt_allowed_shells'
    monkeypatch.setattr(shell_trust, 'TRUST_FILE', str(path))
    return path


def test_extract_only_shell_entries():
    subs = {
        'STATIC': 'hello',
        'NESTED': {'shell': 'git rev-parse HEAD'},
        'OTHER_DICT': {'not_shell': 'x'},
    }
    assert extract_shell_entries(subs) == {'NESTED': 'git rev-parse HEAD'}


def test_hash_is_stable_across_order():
    a = {'A': 'x', 'B': 'y'}
    b = {'B': 'y', 'A': 'x'}
    assert compute_hash(a) == compute_hash(b)


def test_hash_changes_when_command_changes():
    assert compute_hash({'A': 'x'}) != compute_hash({'A': 'y'})


def test_save_and_load_roundtrip(trust_file):
    save_trusted({'/path/a': 'abc', '/path/b': 'def'})
    assert load_trusted() == {'/path/a': 'abc', '/path/b': 'def'}


def test_ensure_shell_trust_noop_when_no_shell_entries(trust_file):
    ensure_shell_trust('/some/path', {'STATIC': 'value'}, trust_shells=False)
    assert not trust_file.exists()


def test_ensure_shell_trust_accepts_with_flag(trust_file):
    subs = {'GIT': {'shell': 'echo 1'}}
    ensure_shell_trust('/cfg.yaml', subs, trust_shells=True)
    assert load_trusted() == {'/cfg.yaml': compute_hash({'GIT': 'echo 1'})}


def test_ensure_shell_trust_silent_on_known_hash(trust_file):
    subs = {'GIT': {'shell': 'echo 1'}}
    save_trusted({'/cfg.yaml': compute_hash({'GIT': 'echo 1'})})
    # Should not raise / not prompt — returns normally.
    ensure_shell_trust('/cfg.yaml', subs, trust_shells=False)


def test_ensure_shell_trust_exits_when_hash_differs_and_noninteractive(trust_file, monkeypatch):
    subs = {'GIT': {'shell': 'echo 2'}}  # different command → different hash
    save_trusted({'/cfg.yaml': compute_hash({'GIT': 'echo 1'})})
    monkeypatch.setattr('sys.stdin.isatty', lambda: False)

    with pytest.raises(SystemExit) as exc:
        ensure_shell_trust('/cfg.yaml', subs, trust_shells=False)
    assert exc.value.code == 1
