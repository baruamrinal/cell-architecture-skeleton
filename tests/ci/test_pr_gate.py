"""Tests for the PR policy gate (scripts/ci/pr_gate.py).

The gate decides whether github-actions[bot] approves a pull request.
Rules R1-R5 are documented in docs/plan.md §9.1.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from pr_gate import (
    BOT_LOGIN,
    COMMENT_MARKER,
    ChangedFile,
    CiRun,
    PullRequest,
    check_ci,
    check_protected_paths,
    check_target,
    check_tdd,
    check_title,
    evaluate,
    pr_numbers_from_event,
    render_comment,
    run_gate,
)

HEAD = "abc123"


def make_pr(**overrides: Any) -> PullRequest:
    base = PullRequest(
        number=7,
        title="feat: add routing cache",
        base_ref="main",
        head_sha=HEAD,
        head_repo="owner/repo",
        base_repo="owner/repo",
        draft=False,
        files=(
            ChangedFile("src/cellarch/router/cache.py", "added"),
            ChangedFile("tests/router/test_cache.py", "added"),
        ),
    )
    return replace(base, **overrides)


def green_run(**overrides: Any) -> CiRun:
    return replace(CiRun(head_sha=HEAD, status="completed", conclusion="success"), **overrides)


def files(*paths: str, status: str = "modified") -> tuple[ChangedFile, ...]:
    return tuple(ChangedFile(p, status) for p in paths)


# --- R1: CI passed for the latest commit -------------------------------------------------


class TestCheckCi:
    def test_green_run_on_head_passes(self) -> None:
        assert check_ci(make_pr(), green_run()) == []

    def test_missing_run_fails(self) -> None:
        assert check_ci(make_pr(), None) == ["CI has not run for the latest commit."]

    def test_run_for_older_commit_fails(self) -> None:
        result = check_ci(make_pr(), green_run(head_sha="old999"))
        assert result == ["CI result is for an older commit, not the latest one."]

    def test_run_in_progress_fails(self) -> None:
        result = check_ci(make_pr(), green_run(status="in_progress", conclusion=None))
        assert result == ["CI is still running."]

    @pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "skipped"])
    def test_non_success_conclusion_fails(self, conclusion: str) -> None:
        result = check_ci(make_pr(), green_run(conclusion=conclusion))
        assert result == [f"CI did not pass (conclusion: {conclusion})."]


# --- R2: targets main, not a draft, not from a fork ---------------------------------------


class TestCheckTarget:
    def test_valid_pr_passes(self) -> None:
        assert check_target(make_pr()) == []

    def test_other_base_branch_fails(self) -> None:
        assert check_target(make_pr(base_ref="release")) == ["PR must target `main`."]

    def test_draft_fails(self) -> None:
        assert check_target(make_pr(draft=True)) == ["PR is a draft."]

    def test_fork_fails(self) -> None:
        result = check_target(make_pr(head_repo="someone/fork"))
        assert result == ["PR comes from a fork; only same-repository PRs are approved."]

    def test_reports_every_problem(self) -> None:
        assert len(check_target(make_pr(base_ref="dev", draft=True))) == 2


# --- R3: TDD - code changes come with test changes ----------------------------------------


class TestCheckTdd:
    def test_src_with_tests_passes(self) -> None:
        assert check_tdd(files("src/cellarch/a.py", "tests/test_a.py")) == []

    def test_src_without_tests_fails(self) -> None:
        assert check_tdd(files("src/cellarch/a.py", "docs/plan.md")) == [
            "`src/` changed but no files under `tests/` changed (TDD rule)."
        ]

    def test_only_removed_src_needs_no_tests(self) -> None:
        assert check_tdd(files("src/cellarch/old.py", status="removed")) == []

    def test_docs_only_passes(self) -> None:
        assert check_tdd(files("docs/plan.md", "README.md")) == []

    def test_infra_module_with_its_tests_passes(self) -> None:
        changed = files("infra/modules/cell/main.tf", "infra/modules/cell/tests/cell.tftest.hcl")
        assert check_tdd(changed) == []

    def test_infra_module_without_tests_fails(self) -> None:
        assert check_tdd(files("infra/modules/cell/main.tf")) == [
            "`infra/modules/cell` changed but none of its `*.tftest.hcl` tests changed (TDD rule)."
        ]

    def test_infra_tests_must_belong_to_the_same_module(self) -> None:
        changed = files("infra/modules/cell/main.tf", "infra/modules/router/tests/r.tftest.hcl")
        assert len(check_tdd(changed)) == 1

    def test_infra_test_only_change_passes(self) -> None:
        assert check_tdd(files("infra/modules/cell/tests/cell.tftest.hcl")) == []


# --- R4: protected paths always need a human ----------------------------------------------


class TestCheckProtectedPaths:
    @pytest.mark.parametrize(
        "path",
        [".github/workflows/ci.yml", ".github/CODEOWNERS", "scripts/ci/pr_gate.py"],
    )
    def test_protected_paths_are_reported(self, path: str) -> None:
        assert check_protected_paths(files(path, "src/x.py")) == [path]

    def test_unprotected_paths_pass(self) -> None:
        assert check_protected_paths(files("src/x.py", "docs/plan.md")) == []


# --- R5: conventional commit title -------------------------------------------------------


class TestCheckTitle:
    @pytest.mark.parametrize(
        "title",
        [
            "feat: add router",
            "fix(router): handle 421",
            "docs: update plan",
            "ci: add gate",
            "infra(cell): add module",
            "test: cover cache",
            "refactor: tidy",
            "chore: bump deps",
            "feat!: breaking change",
        ],
    )
    def test_valid_titles_pass(self, title: str) -> None:
        assert check_title(title) == []

    @pytest.mark.parametrize(
        "title", ["update readme.md", "Feat: capitalised", "feat:missing space", "wip: stuff", ""]
    )
    def test_invalid_titles_fail(self, title: str) -> None:
        assert check_title(title) == [
            "PR title must follow conventional commits, e.g. `feat: ...` or `fix(router): ...`."
        ]


# --- evaluate: combining the rules -------------------------------------------------------


class TestEvaluate:
    def test_all_rules_pass_approves(self) -> None:
        verdict = evaluate(make_pr(), green_run())
        assert verdict.approve
        assert verdict.failures == ()
        assert verdict.protected_paths == ()

    def test_any_failure_blocks_approval(self) -> None:
        verdict = evaluate(make_pr(title="bad title"), green_run())
        assert not verdict.approve
        assert len(verdict.failures) == 1

    def test_failures_are_collected_across_rules(self) -> None:
        pr = make_pr(title="bad", draft=True, files=files("src/a.py"))
        verdict = evaluate(pr, None)
        assert len(verdict.failures) == 4

    def test_protected_paths_block_approval_even_when_green(self) -> None:
        pr = make_pr(files=files(".github/workflows/ci.yml"))
        verdict = evaluate(pr, green_run())
        assert not verdict.approve
        assert verdict.failures == ()
        assert verdict.protected_paths == (".github/workflows/ci.yml",)
        assert verdict.needs_human


# --- comment rendering -------------------------------------------------------------------


class TestRenderComment:
    def test_approved_comment(self) -> None:
        body = render_comment(evaluate(make_pr(), green_run()), HEAD)
        assert body.startswith(COMMENT_MARKER)
        assert "Approved" in body
        assert HEAD in body

    def test_failure_comment_lists_failures(self) -> None:
        body = render_comment(evaluate(make_pr(title="bad"), green_run()), HEAD)
        assert body.startswith(COMMENT_MARKER)
        assert "Not approved" in body
        assert "- PR title must follow conventional commits" in body

    def test_comment_shows_failures_and_protected_paths_together(self) -> None:
        pr = make_pr(title="bad", files=files(".github/workflows/ci.yml"))
        body = render_comment(evaluate(pr, green_run()), HEAD)
        assert "Not approved" in body
        assert "Needs human review" in body
        assert body.index("Not approved") < body.index("Needs human review")

    def test_protected_comment_asks_for_human(self) -> None:
        pr = make_pr(files=files(".github/workflows/ci.yml"))
        body = render_comment(evaluate(pr, green_run()), HEAD)
        assert "Needs human review" in body
        assert "`.github/workflows/ci.yml`" in body


# --- event parsing -----------------------------------------------------------------------


class TestPrNumbersFromEvent:
    def test_workflow_run_from_pull_request(self) -> None:
        payload = {"workflow_run": {"event": "pull_request", "pull_requests": [{"number": 3}]}}
        assert pr_numbers_from_event("workflow_run", payload) == [3]

    def test_workflow_run_from_push_is_ignored(self) -> None:
        payload = {"workflow_run": {"event": "push", "pull_requests": [{"number": 3}]}}
        assert pr_numbers_from_event("workflow_run", payload) == []

    def test_workflow_run_without_prs(self) -> None:
        payload = {"workflow_run": {"event": "pull_request", "pull_requests": []}}
        assert pr_numbers_from_event("workflow_run", payload) == []

    def test_pull_request_target(self) -> None:
        payload = {"pull_request": {"number": 9}}
        assert pr_numbers_from_event("pull_request_target", payload) == [9]

    def test_unknown_event(self) -> None:
        assert pr_numbers_from_event("push", {}) == []


# --- run_gate: orchestration against a fake GitHub ---------------------------------------


@dataclass
class FakeGitHub:
    pull: dict[str, Any]
    changed: list[dict[str, str]]
    ci_run: dict[str, Any] | None
    reviews: list[dict[str, Any]] = field(default_factory=list)
    comments: list[dict[str, Any]] = field(default_factory=list)
    approvals: list[tuple[int, str]] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    updated: list[tuple[int, str]] = field(default_factory=list)

    def get_pull(self, number: int) -> dict[str, Any]:
        return self.pull

    def list_files(self, number: int) -> list[dict[str, str]]:
        return self.changed

    def latest_ci_run(self, head_sha: str) -> dict[str, Any] | None:
        return self.ci_run

    def list_reviews(self, number: int) -> list[dict[str, Any]]:
        return self.reviews

    def approve(self, number: int, head_sha: str, body: str) -> None:
        self.approvals.append((number, head_sha))

    def list_comments(self, number: int) -> list[dict[str, Any]]:
        return self.comments

    def create_comment(self, number: int, body: str) -> None:
        self.created.append(body)

    def update_comment(self, comment_id: int, body: str) -> None:
        self.updated.append((comment_id, body))


def fake_github(**overrides: Any) -> FakeGitHub:
    defaults: dict[str, Any] = {
        "pull": {
            "number": 7,
            "title": "feat: add routing cache",
            "draft": False,
            "base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
            "head": {"sha": HEAD, "repo": {"full_name": "owner/repo"}},
        },
        "changed": [
            {"filename": "src/cellarch/a.py", "status": "added"},
            {"filename": "tests/test_a.py", "status": "added"},
        ],
        "ci_run": {"head_sha": HEAD, "status": "completed", "conclusion": "success"},
    }
    defaults.update(overrides)
    return FakeGitHub(**defaults)


class TestRunGate:
    def test_approves_and_comments_when_green(self) -> None:
        gh = fake_github()
        verdict = run_gate(gh, 7)
        assert verdict.approve
        assert gh.approvals == [(7, HEAD)]
        assert len(gh.created) == 1

    def test_does_not_approve_twice_for_same_commit(self) -> None:
        gh = fake_github(
            reviews=[{"user": {"login": BOT_LOGIN}, "state": "APPROVED", "commit_id": HEAD}]
        )
        run_gate(gh, 7)
        assert gh.approvals == []

    def test_approves_again_for_new_commit(self) -> None:
        gh = fake_github(
            reviews=[{"user": {"login": BOT_LOGIN}, "state": "APPROVED", "commit_id": "old999"}]
        )
        run_gate(gh, 7)
        assert gh.approvals == [(7, HEAD)]

    def test_ignores_approvals_from_other_users(self) -> None:
        gh = fake_github(
            reviews=[{"user": {"login": "someone"}, "state": "APPROVED", "commit_id": HEAD}]
        )
        run_gate(gh, 7)
        assert gh.approvals == [(7, HEAD)]

    def test_does_not_approve_when_ci_failed(self) -> None:
        gh = fake_github(ci_run={"head_sha": HEAD, "status": "completed", "conclusion": "failure"})
        verdict = run_gate(gh, 7)
        assert not verdict.approve
        assert gh.approvals == []
        assert "Not approved" in gh.created[0]

    def test_updates_existing_gate_comment(self) -> None:
        gh = fake_github(
            comments=[
                {"id": 1, "body": "unrelated", "user": {"login": "someone"}},
                {"id": 2, "body": f"{COMMENT_MARKER}\nold", "user": {"login": BOT_LOGIN}},
            ]
        )
        run_gate(gh, 7)
        assert gh.created == []
        assert [cid for cid, _ in gh.updated] == [2]

    def test_ignores_marker_in_comments_by_other_users(self) -> None:
        gh = fake_github(
            comments=[{"id": 5, "body": f"{COMMENT_MARKER} spoof", "user": {"login": "someone"}}]
        )
        run_gate(gh, 7)
        assert gh.updated == []
        assert len(gh.created) == 1

    def test_handles_missing_ci_run(self) -> None:
        gh = fake_github(ci_run=None)
        verdict = run_gate(gh, 7)
        assert not verdict.approve
        assert gh.approvals == []
