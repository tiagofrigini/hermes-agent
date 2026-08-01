"""Tests for the decomposer module + `hermes kanban decompose` CLI surface.

The auxiliary LLM client is mocked — no network calls. Tests exercise the
prompt plumbing, response parsing, DB writes (via the real DB helper),
and the assignee-fallback logic.
"""

from __future__ import annotations

import json as jsonlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_decompose as decomp


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_aux_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _mock_client_returning(content: str):
    client = MagicMock()
    client.chat.completions.create = MagicMock(return_value=_fake_aux_response(content))
    return client


def _patch_aux_client(content: str, *, model: str = "test-model"):
    # decompose_task now routes through call_llm (see #35566) — mock it at
    # the source module so task config, extra_body, and retries stay out of
    # unit-test scope.
    return patch(
        "agent.auxiliary_client.call_llm",
        return_value=_fake_aux_response(content),
    )


def _patch_extra_body():
    # No-op shim retained for call-site compatibility: extra_body plumbing
    # now lives inside call_llm, which _patch_aux_client already mocks.
    return patch("agent.auxiliary_client.get_auxiliary_extra_body", return_value={})


def _patch_list_profiles(names: list[str]):
    """Pretend the named profiles exist. The decomposer uses
    profiles_mod.list_profiles() to build the roster + valid-set, and
    profiles_mod.profile_exists() to resolve orchestrator/default."""
    from types import SimpleNamespace
    fake_profiles = [
        SimpleNamespace(
            name=n, is_default=(i == 0), description=f"desc for {n}",
            description_auto=False, model="m", provider="p", skill_count=1,
        )
        for i, n in enumerate(names)
    ]
    return [
        patch("hermes_cli.profiles.list_profiles", return_value=fake_profiles),
        patch("hermes_cli.profiles.profile_exists", side_effect=lambda x: x in names),
        patch("hermes_cli.profiles.get_active_profile_name", return_value=names[0] if names else "default"),
    ]


def _run_decompose(tid: str, payload: dict, *, installed_skills: set[str]):
    patches = _patch_list_profiles(["orchestrator", "worker"])
    patches.extend(
        [
            _patch_aux_client(jsonlib.dumps(payload)),
            patch(
                "hermes_cli.kanban_decompose._is_installed_skill",
                side_effect=lambda name: name in installed_skills,
            ),
        ]
    )
    for item in patches:
        item.start()
    try:
        return decomp.decompose_task(tid, author="orchestrator")
    finally:
        for item in reversed(patches):
            item.stop()


_OMIT = object()


def _child_spec(*, skills: object = _OMIT):
    child: dict[str, object] = {
        "key": "impl",
        "title": "Implement",
        "body": "Do it",
        "assignee": "worker",
        "workspace_kind": "scratch",
    }
    if skills is not _OMIT:
        child["skills"] = skills
    return child


def test_decompose_with_fanout_creates_children(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ship a feature", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "research", "body": "look it up", "assignee": "researcher", "parents": []},
            {"title": "build", "body": "code it", "assignee": "engineer", "parents": [0]},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "researcher", "engineer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    assert outcome.child_ids and len(outcome.child_ids) == 2

    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert root.status == "todo"
    assert c0.status == "ready"
    assert c1.status == "todo"
    assert c0.assignee == "researcher"
    assert c1.assignee == "engineer"


def test_decompose_fanout_false_invalid_llm_assignee_uses_default(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="route me safely",
            triage=True,
            skills=["root-skill"],
        )

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Route to fallback.",
        "assignee": "made_up",
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "fallback"
    assert task.skills == ["root-skill"]


def test_decompose_returns_false_when_task_not_triage(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x")  # ready, not triage

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()
    assert outcome.ok is False
    assert "not in triage" in outcome.reason


def test_decompose_child_inherits_root_skills_when_omitted(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="root",
            triage=True,
            skills=["root-a", "root-b"],
        )

    outcome = _run_decompose(
        tid,
        {"fanout": True, "rationale": "split", "tasks": [_child_spec()]},
        installed_skills={"root-a", "root-b"},
    )

    assert outcome.ok, outcome.reason
    assert outcome.child_ids
    with kb.connect_closing() as conn:
        child = kb.get_task(conn, outcome.child_ids[0])
    assert child is not None
    assert child.skills == ["root-a", "root-b"]


def test_decompose_child_explicit_skills_are_root_first_and_deduped(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="root", triage=True, skills=["root"])

    outcome = _run_decompose(
        tid,
        {
            "fanout": True,
            "rationale": "split",
            "tasks": [_child_spec(skills=["extra", "root", "extra"])],
        },
        installed_skills={"root", "extra"},
    )

    assert outcome.ok, outcome.reason
    assert outcome.child_ids
    with kb.connect_closing() as conn:
        child = kb.get_task(conn, outcome.child_ids[0])
    assert child is not None
    assert child.skills == ["root", "extra"]


@pytest.mark.parametrize(
    ("child_skills", "installed_skills", "error_text"),
    [
        (["extra"], {"root", "extra"}, "must include root skill"),
        (["root", "missing"], {"root"}, "unknown skill"),
        ("root", {"root"}, "must be a list"),
    ],
)
def test_decompose_rejects_invalid_child_skills_atomically(
    kanban_home,
    child_skills,
    installed_skills,
    error_text,
):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="root", triage=True, skills=["root"])

    outcome = _run_decompose(
        tid,
        {
            "fanout": True,
            "rationale": "split",
            "tasks": [_child_spec(skills=child_skills)],
        },
        installed_skills=installed_skills,
    )

    assert outcome.ok is False
    assert error_text in outcome.reason
    with kb.connect_closing() as conn:
        root = kb.get_task(conn, tid)
        child_count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE id != ?", (tid,)
        ).fetchone()[0]
    assert root is not None
    assert root.status == "triage"
    assert child_count == 0


def test_decompose_without_root_or_child_skills_preserves_none(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="root", triage=True)

    outcome = _run_decompose(
        tid,
        {"fanout": True, "rationale": "split", "tasks": [_child_spec()]},
        installed_skills=set(),
    )

    assert outcome.ok, outcome.reason
    assert outcome.child_ids
    with kb.connect_closing() as conn:
        child = kb.get_task(conn, outcome.child_ids[0])
    assert child is not None
    assert child.skills is None


def test_decompose_prompt_exposes_structured_child_skills():
    prompt = decomp._SYSTEM_PROMPT
    assert '\"skills\": [\"required-skill\", \"optional-extra-skill\"]' in prompt
    assert "When omitted, inherit every skill required by the" in prompt
    assert "root task" in prompt
