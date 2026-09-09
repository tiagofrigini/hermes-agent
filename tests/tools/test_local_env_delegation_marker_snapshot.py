"""Regression coverage for delegated lineage and shared terminal snapshots.

The local backend shares one cached ``LocalEnvironment`` between a front-door
turn and its delegated child. The child marker must cross the real subprocess
boundary, but it must never become reusable shell state for the next parent
command, including when an older snapshot is already contaminated.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import tools.environments.base_session_env as session_env

from agent.delegation_context import (
    DELEGATED_CHILD_ENV_MARKER,
    KANBAN_ENV_KEYS,
    delegated_child_context,
)
from tools.environments.base_session_env import _export_dump_excluding_session_vars
from tools.environments.local import LocalEnvironment

_MARKER = DELEGATED_CHILD_ENV_MARKER


def _probe(name: str) -> str:
    return (
        f'printf "presence=%s value=%s\\n" '
        f'"${{{name}+set}}" "${{{name}-unset}}"'
    )


def test_child_marker_crosses_subprocess_but_not_new_shared_snapshot(tmp_path, monkeypatch):
    """A real child subprocess sees lineage; the next parent command does not."""
    monkeypatch.delenv(_MARKER, raising=False)
    env = LocalEnvironment(cwd=str(tmp_path), timeout=15)
    try:
        snapshot = Path(env._snapshot_path)
        assert _MARKER not in snapshot.read_text(encoding="utf-8")

        with delegated_child_context():
            child = env.execute(_probe(_MARKER), timeout=15)
        assert child["returncode"] == 0
        assert "presence=set value=1" in child["output"]
        assert _MARKER not in snapshot.read_text(encoding="utf-8")

        parent = env.execute(_probe(_MARKER), timeout=15)
        assert parent["returncode"] == 0
        assert "presence= value=unset" in parent["output"]
        assert _MARKER not in snapshot.read_text(encoding="utf-8")
    finally:
        env.cleanup()


def test_contaminated_legacy_snapshot_is_scrubbed_before_parent_command(tmp_path, monkeypatch):
    """A stale marker cannot block the first ordinary command after upgrade."""
    monkeypatch.delenv(_MARKER, raising=False)
    env = LocalEnvironment(cwd=str(tmp_path), timeout=15)
    try:
        snapshot = Path(env._snapshot_path)
        snapshot.write_text(
            snapshot.read_text(encoding="utf-8") + f"\nexport {_MARKER}=1\n",
            encoding="utf-8",
        )

        parent = env.execute(_probe(_MARKER), timeout=15)
        assert parent["returncode"] == 0
        assert "presence= value=unset" in parent["output"]
        assert _MARKER not in snapshot.read_text(encoding="utf-8")

        with delegated_child_context():
            child = env.execute(_probe(_MARKER), timeout=15)
        assert child["returncode"] == 0
        assert "presence=set value=1" in child["output"]
    finally:
        env.cleanup()


def test_dispatcher_ownership_names_are_not_persisted_but_user_config_survives(
    tmp_path, monkeypatch
):
    """Only invocation-scoped Kanban identity is excluded, not user settings."""
    owner_key = KANBAN_ENV_KEYS[0]
    monkeypatch.setenv(owner_key, "board-owner-token")
    monkeypatch.setenv("HERMES_KANBAN_HOME", "/home/u/.hermes")
    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "0")
    env = LocalEnvironment(cwd=str(tmp_path), timeout=15)
    try:
        env.execute("true", timeout=15)
        snapshot = Path(env._snapshot_path).read_text(encoding="utf-8")
        assert owner_key not in snapshot
        assert "HERMES_KANBAN_HOME" in snapshot
        assert "HERMES_KANBAN_DISPATCH_IN_GATEWAY" in snapshot
    finally:
        env.cleanup()


def test_export_filter_removes_lineage_and_ownership_but_keeps_user_exports(monkeypatch):
    """The shell dump uses real ``unset`` semantics, not a source-text filter."""
    monkeypatch.delenv(_MARKER, raising=False)
    exports = "; ".join(
        [
            'export PATH="/usr/bin"',
            'export HERMES_HOME="/home/u/.hermes"',
            f'export {_MARKER}="1"',
            f'export {KANBAN_ENV_KEYS[0]}="tok"',
            'export HERMES_SESSION_ID="s1"',
            'export MYVAR="keep"',
        ]
    )
    snippet = _export_dump_excluding_session_vars("/dev/stdout")
    proc = subprocess.run(
        ["bash", "-c", f"{exports}; {snippet}"],
        capture_output=True,
        text=True,
        check=True,
    )
    out = proc.stdout
    assert f"declare -x {_MARKER}=" not in out
    assert f"declare -x {KANBAN_ENV_KEYS[0]}=" not in out
    assert "declare -x HERMES_SESSION_ID=" not in out
    assert 'declare -x PATH="/usr/bin"' in out
    assert 'declare -x HERMES_HOME="/home/u/.hermes"' in out
    assert 'declare -x MYVAR="keep"' in out


def test_legacy_aux_export_cannot_create_parent_lineage_marker(tmp_path, monkeypatch):
    """A legacy auxiliary export cannot turn an ordinary parent into a child."""
    monkeypatch.delenv(_MARKER, raising=False)
    env = LocalEnvironment(cwd=str(tmp_path), timeout=15)
    try:
        snapshot = Path(env._snapshot_path)
        snapshot.write_text(
            snapshot.read_text(encoding="utf-8")
            + "\nexport __hermes_dcc=legacy-internal\n",
            encoding="utf-8",
        )

        parent = env.execute(_probe(_MARKER), timeout=15)
        assert parent["returncode"] == 0
        assert "presence= value=unset" in parent["output"]
    finally:
        env.cleanup()


def test_legacy_aux_export_cannot_remove_child_lineage_marker(tmp_path, monkeypatch):
    """A legacy empty auxiliary export cannot erase a real child marker."""
    monkeypatch.delenv(_MARKER, raising=False)
    env = LocalEnvironment(cwd=str(tmp_path), timeout=15)
    try:
        snapshot = Path(env._snapshot_path)
        snapshot.write_text(
            snapshot.read_text(encoding="utf-8")
            + "\nexport __hermes_dcc=\n",
            encoding="utf-8",
        )

        with delegated_child_context():
            child = env.execute(_probe(_MARKER), timeout=15)
        assert child["returncode"] == 0
        assert "presence=set value=1" in child["output"]
    finally:
        env.cleanup()


def test_marker_restore_uses_literal_python_value_with_shell_quoting():
    """Marker restoration must not use a shell temporary that a snapshot can overwrite."""
    marker_value = "child value 'quoted' $(not-executed)"
    restore = session_env._marker_restore_script(marker_value)
    assert "__hermes_dcc" not in restore
    assert "__hermes_marker" not in restore

    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'export {_MARKER}=legacy; {restore}; printf "%s" "${{{_MARKER}-unset}}"',
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout == marker_value


def test_user_exports_functions_and_cwd_persist_across_execute(tmp_path, monkeypatch):
    """The session contract keeps ordinary env, functions, and CWD between commands."""
    monkeypatch.delenv(_MARKER, raising=False)
    (tmp_path / "sub").mkdir()
    env = LocalEnvironment(cwd=str(tmp_path), timeout=15)
    try:
        assert env.execute("export MYVAR=keep", timeout=15)["returncode"] == 0
        read_export = env.execute("printf 'MYVAR=%s\\n' \"${MYVAR-unset}\"", timeout=15)
        assert read_export["returncode"] == 0
        assert read_export["output"] == "MYVAR=keep\n"

        assert env.execute("myfunc() { printf 'FUNC=keep\\n'; }", timeout=15)["returncode"] == 0
        call_function = env.execute("myfunc", timeout=15)
        assert call_function["returncode"] == 0
        assert call_function["output"] == "FUNC=keep\n"

        assert env.execute("cd sub", timeout=15)["returncode"] == 0
        read_cwd = env.execute("pwd -P", timeout=15)
        assert read_cwd["returncode"] == 0
        assert read_cwd["output"] == f"{tmp_path / 'sub'}\n"
    finally:
        env.cleanup()


@pytest.mark.parametrize(
    "legacy_line",
    [
        f"export {_MARKER}=1",
        f'declare -x {_MARKER}="1"',
        "export __hermes_dcc=legacy-internal",
        'declare -x __hermes_dcc="legacy-internal"',
    ],
)
@pytest.mark.parametrize("child", [False, True])
def test_legacy_marker_and_aux_declarations_cannot_change_parent_child_lineage(
    tmp_path, monkeypatch, legacy_line, child
):
    """Both legacy declaration forms are scrubbed without erasing a real child marker."""
    monkeypatch.delenv(_MARKER, raising=False)
    env = LocalEnvironment(cwd=str(tmp_path), timeout=15)
    try:
        snapshot = Path(env._snapshot_path)
        snapshot.write_text(snapshot.read_text(encoding="utf-8") + f"\n{legacy_line}\n", encoding="utf-8")
        if child:
            with delegated_child_context():
                result = env.execute(_probe(_MARKER), timeout=15)
        else:
            result = env.execute(_probe(_MARKER), timeout=15)
        assert result["returncode"] == 0
        expected = "presence=set value=1" if child else "presence= value=unset"
        assert expected in result["output"]
    finally:
        env.cleanup()
