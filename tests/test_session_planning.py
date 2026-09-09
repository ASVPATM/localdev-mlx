from __future__ import annotations

from contextlib import contextmanager

import pytest
from typer.testing import CliRunner

from localdev_mlx.cli import app
from localdev_mlx.git import GitRepository
from localdev_mlx.sessions import SessionStore
from localdev_mlx.workflows.session_plan import (
    InitialPlanningQuestions,
    PlanningQuestions,
    SessionProposal,
    full_plan,
)


class Planner:
    def __init__(self, batches):
        self.batches = iter(batches)
        self.calls = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        if issubclass(kwargs["response_model"], PlanningQuestions):
            return kwargs["response_model"](questions=next(self.batches))
        return SessionProposal(proposal="User flows, stored data, and concrete acceptance checks.")


def run_plan(repository, config, provider, ask):
    store = SessionStore(repository)
    with store.operation("plan", "A personal writing assistant", "full") as (session, entry):
        full_plan(
            store, session, entry, config=config, provider=provider, manage_models=False, ask=ask
        )
    return store, session, entry


def test_adaptive_followups_are_not_capped_at_four_questions(sample_repo, global_config):
    provider = Planner([[f"Choice {i}?" for i in range(6)], ["Follow-up?", "One more?"], []])
    answers = iter([f"User preference {i}" for i in range(8)])
    store, session, entry = run_plan(sample_repo, global_config, provider, lambda _: next(answers))
    assert len(entry["clarifications"]) == 8
    assert len(provider.calls) == 5  # Three question rounds, draft, refinement.
    assert "User preference 0" in provider.calls[1]["user_prompt"]
    for call in provider.calls[-2:]:
        assert all(f"User preference {i}" in call["user_prompt"] for i in range(8))
        assert call["profile"].max_tokens <= 4096
        assert "one complete end-to-end implementation" in call["system_prompt"]
        assert "Do not invent user answers" in call["system_prompt"]
        assert "existing project checkout" in call["system_prompt"]
    text = store.handoff(session).read_text()
    assert "User preference 7" in text and "User clarifications" in text
    assert len(list(store.visible.glob("*.md"))) == 1
    assert GitRepository(sample_repo).is_clean()


@pytest.mark.parametrize("answer", [None, ""])
def test_finishing_or_skipping_all_questions_uses_explicit_assumptions(
    sample_repo, global_config, answer
):
    provider = Planner([["Cloud or local?", "Which users?"]])
    store, session, entry = run_plan(sample_repo, global_config, provider, lambda _: answer)
    assert len(provider.calls) == 3
    assert "assumptions" in entry["clarification_note"]
    assert "Unanswered" in store.handoff(session).read_text()
    assert "assumptions" in provider.calls[-1]["user_prompt"]


def test_eof_finishes_clarification_without_losing_request(sample_repo, global_config):
    def eof(_):
        raise EOFError

    store, session, entry = run_plan(sample_repo, global_config, Planner([["Environment?"]]), eof)
    assert entry["state"] == "ready for external implementation"
    assert "personal writing assistant" in store.handoff(session).read_text()


def test_initial_round_requires_a_question_then_can_finish(sample_repo, global_config):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        InitialPlanningQuestions(questions=[])
    provider = Planner([["Confirm: a single-user local writing app?"], []])
    _, _, entry = run_plan(sample_repo, global_config, provider, lambda _: "Yes")
    assert len(provider.calls) == 4
    assert entry["clarifications"][0]["answer"] == "Yes"
    assert (
        provider.calls[0]["response_model"].model_json_schema()["properties"]["questions"][
            "minItems"
        ]
        == 1
    )


def test_repeated_questions_stop_without_reasking(sample_repo, global_config):
    provider = Planner([["Cloud or local?", " cloud OR local? "], ["CLOUD OR LOCAL!"]])
    asked = []

    def answer(question):
        asked.append(question)
        return "Local only"

    _, _, entry = run_plan(sample_repo, global_config, provider, answer)
    assert asked == ["Cloud or local?"]
    assert "Repeated questions stopped" in entry["clarification_note"]


def test_answers_are_durable_before_next_question_and_after_interrupt(sample_repo, global_config):
    store = SessionStore(sample_repo)
    count = 0

    def answer(_):
        nonlocal count
        count += 1
        if count == 2:
            assert store.current()["entries"][0]["clarifications"][0]["answer"] == "Offline only"
            raise KeyboardInterrupt
        return "Offline only"

    with pytest.raises(KeyboardInterrupt):
        run_plan(sample_repo, global_config, Planner([["Environment?", "Feedback?"]]), answer)
    session = store.current()
    assert session["entries"][0]["state"] == "cancelled"
    assert "Offline only" in store.handoff(session).read_text()
    assert "proposal" not in session["entries"][0]


def test_model_lease_released_while_answering(sample_repo, global_config):
    class Manager:
        leased = False
        loaded = False

        @contextmanager
        def lease(self):
            assert not self.leased
            self.leased = True
            try:
                yield
            finally:
                self.leased = False

        def ensure(self, _):
            assert self.leased
            self.loaded = True

        def stop(self):
            self.loaded = False

    manager = Manager()

    def answer(_):
        import math

        from localdev_mlx.execution.budget import _guards

        assert not manager.leased and not manager.loaded
        assert len(_guards) == 1 and math.isinf(_guards[0][0])
        return "Offline"

    store = SessionStore(sample_repo)
    with store.operation("plan", "An app", "full") as (session, entry):
        full_plan(
            store,
            session,
            entry,
            config=global_config,
            provider=Planner([["Environment?"], []]),
            manager=manager,
            ask=answer,
        )
    assert not manager.leased and not manager.loaded


def test_cancellation_while_answering_preserves_answers_and_stops_before_proposal(
    sample_repo, global_config
):
    store = SessionStore(sample_repo)

    def answer(_):
        store.cancel()
        return "Saved preference"

    provider = Planner([["Environment?", "Users?"]])
    with pytest.raises(KeyboardInterrupt, match="Cancellation requested"):
        run_plan(sample_repo, global_config, provider, answer)
    entry = store.current()["entries"][0]
    assert entry["state"] == "cancelled"
    assert entry["clarifications"][0]["answer"] == "Saved preference"
    assert len(provider.calls) == 1


def test_clarification_context_budget_stops_explicitly_without_losing_answers(
    sample_repo, global_config, monkeypatch
):
    from dataclasses import replace

    from localdev_mlx.config import load_project_config

    monkeypatch.setattr(
        "localdev_mlx.sessions.load_project_config",
        lambda root: replace(load_project_config(root), planner_context_chars=1000),
    )
    answer = "Detailed user preference. " * 100
    provider = Planner([["Environment?"]])
    store, session, entry = run_plan(sample_repo, global_config, provider, lambda _: answer)
    assert "budget reached" in entry["clarification_note"]
    assert answer.strip() in provider.calls[-1]["user_prompt"]
    assert answer.strip() in store.handoff(session).read_text()


@pytest.mark.parametrize("interactive", [False, True])
def test_full_plan_cli_collects_answers_and_done(
    sample_repo, global_config, monkeypatch, interactive
):
    import localdev_mlx.cli as module

    provider = Planner([["Cloud or local?", "Feedback?"]])
    actual = full_plan
    monkeypatch.setattr(module, "load_global_config", lambda: global_config)
    monkeypatch.setattr(module, "MLXOpenAIProvider", lambda: provider)
    monkeypatch.setattr(module, "full_plan", lambda *a, **kw: actual(*a, **kw, manage_models=False))
    args = ["--repo", str(sample_repo)]
    command = ["plan", "A writing assistant", "--mode", "full"]
    if interactive:
        answers = 'plan "A writing assistant" --mode full\nOffline only\n/done\nsession --end\n'
    else:
        args += command
        answers = "Offline only\n/done\n"
    result = CliRunner().invoke(app, args, input=answers)
    assert result.exit_code == 0, result.stdout
    assert "Cloud or local?" in result.stdout
    entry = SessionStore(sample_repo).current()["entries"][0]
    assert entry["clarifications"][0]["answer"] == "Offline only"
    assert entry["clarifications"][1]["answer"] is None
    assert entry["state"] == "ready for external implementation"


def test_no_questions_cli_is_noninteractive_and_light_still_uses_no_model(
    sample_repo, global_config, monkeypatch
):
    import localdev_mlx.cli as module

    provider = Planner([])
    monkeypatch.setattr(module, "load_global_config", lambda: global_config)
    monkeypatch.setattr(module, "MLXOpenAIProvider", lambda: provider)
    monkeypatch.setattr(
        module, "full_plan", lambda *a, **kw: full_plan(*a, **kw, manage_models=False)
    )
    result = CliRunner().invoke(
        app, ["plan", "An app", "--mode", "full", "--no-questions", "--repo", str(sample_repo)]
    )
    assert result.exit_code == 0, result.stdout
    assert len(provider.calls) == 2
    result = CliRunner().invoke(
        app, ["plan", "An app", "--no-questions", "--repo", str(sample_repo)]
    )
    assert result.exit_code != 0 and "requires --mode full" in result.stdout


def test_proposal_accepts_details_beyond_old_limit():
    assert SessionProposal(proposal="Detailed requirements. " * 300)
