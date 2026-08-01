"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import json

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
    session_completed_kanban_self_handoff,
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_STOP_NUDGE"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch






def test_env_can_disable(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_nudge_when_no_terminal_tool(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_46be8aa5")
    messages = [
        {"role": "user", "content": "work kanban task"},
        {
            "role": "assistant",
            "content": "Let me write the comprehensive recipe.",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_heartbeat", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_heartbeat", "tool_call_id": "1", "content": "ok"},
    ]
    nudge = build_kanban_stop_nudge(messages=messages, attempts=0)
    assert nudge is not None
    assert "kanban_complete" in nudge
    assert "kanban_block" in nudge
    assert "t_46be8aa5" in nudge
    assert "protocol violation" in nudge.lower() or "protocol" in nudge.lower()


def test_no_nudge_after_kanban_complete(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_complete", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_complete", "tool_call_id": "1", "content": "done"},
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


def _self_handoff_messages(*, task_id: str = "t_self", result: dict | None = None):
    payload = {
        "ok": True,
        "task_id": task_id,
        "self_handoff": True,
    }
    if result is not None:
        payload = result
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "handoff-1",
                    "type": "function",
                    "function": {
                        "name": "kanban_reassign",
                        "arguments": json.dumps({
                            "task_id": task_id,
                            "profile": "worker-code",
                            "reclaim": True,
                        }),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "name": "kanban_reassign",
            "tool_call_id": "handoff-1",
            "content": json.dumps(payload),
        },
    ]


def test_successful_current_task_self_handoff_is_terminal(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_self")
    messages = _self_handoff_messages()

    assert session_completed_kanban_self_handoff(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


@pytest.mark.parametrize(
    "task_id,result",
    [
        ("t_other", None),
        ("t_self", {"ok": False, "task_id": "t_self", "self_handoff": True}),
        ("t_self", {"ok": True, "task_id": "t_self", "self_handoff": False}),
        ("t_self", {"ok": True, "task_id": "t_other", "self_handoff": True}),
    ],
)
def test_failed_or_mismatched_reassign_is_not_terminal(
    clear_kanban_env, task_id, result,
):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_self")
    messages = _self_handoff_messages(task_id=task_id, result=result)

    assert session_completed_kanban_self_handoff(messages) is False
    assert build_kanban_stop_nudge(messages=messages) is not None


# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.




