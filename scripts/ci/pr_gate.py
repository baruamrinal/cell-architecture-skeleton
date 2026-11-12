"""PR policy gate: decides whether github-actions[bot] approves a pull request.

Runs from the default branch (``workflow_run`` / ``pull_request_target`` triggers),
never from the PR's own code, so a PR cannot change the rules that approve it.

Rules (docs/plan.md §9.1):
  R1  CI passed for the PR's latest commit
  R2  targets ``main``, is not a draft, comes from this repository (not a fork)
  R3  TDD: ``src/`` changes come with ``tests/`` changes; an infra module's changes
      come with changes to its ``*.tftest.hcl`` tests
  R4  protected paths (``.github/``, ``scripts/ci/``) always need a human
  R5  PR title follows conventional commits (it becomes the squash-commit message)
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

BOT_LOGIN = "github-actions[bot]"
COMMENT_MARKER = "<!-- pr-gate -->"
CI_WORKFLOW_FILE = "ci.yml"
PROTECTED_PREFIXES = (".github/", "scripts/ci/")
TITLE_PATTERN = re.compile(
    r"^(feat|fix|docs|ci|infra|test|refactor|chore|perf|build)(\([a-z0-9-]+\))?!?: \S.*$"
)
INFRA_MODULE_PATTERN = re.compile(r"^infra/modules/(?P<module>[^/]+)/")
TERRAFORM_TEST_SUFFIX = ".tftest.hcl"


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str  # GitHub file status: added, modified, removed, renamed, ...


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    base_ref: str
    head_sha: str
    head_repo: str
    base_repo: str
    draft: bool
    files: tuple[ChangedFile, ...]


@dataclass(frozen=True)
class CiRun:
    head_sha: str
    status: str
    conclusion: str | None


@dataclass(frozen=True)
class Verdict:
    failures: tuple[str, ...]
    protected_paths: tuple[str, ...]

    @property
    def needs_human(self) -> bool:
        return bool(self.protected_paths)

    @property
    def approve(self) -> bool:
        return not self.failures and not self.needs_human


# --- Rules -------------------------------------------------------------------------------


def check_ci(pr: PullRequest, run: CiRun | None) -> list[str]:
    """R1: CI passed for the PR's latest commit."""
    if run is None:
        return ["CI has not run for the latest commit."]
    if run.head_sha != pr.head_sha:
        return ["CI result is for an older commit, not the latest one."]
    if run.status != "completed":
        return ["CI is still running."]
    if run.conclusion != "success":
        return [f"CI did not pass (conclusion: {run.conclusion})."]
    return []


def check_target(pr: PullRequest) -> list[str]:
    """R2: targets main, is not a draft, and is not from a fork."""
    failures = []
    if pr.base_ref != "main":
        failures.append("PR must target `main`.")
    if pr.draft:
        failures.append("PR is a draft.")
    if pr.head_repo != pr.base_repo:
        failures.append("PR comes from a fork; only same-repository PRs are approved.")
    return failures


def check_tdd(files: Iterable[ChangedFile]) -> list[str]:
    """R3: code changes come with test changes."""
    files = list(files)
    live = [f.path for f in files if f.status != "removed"]
    every = [f.path for f in files]
    failures = []

    if any(p.startswith("src/") for p in live) and not any(p.startswith("tests/") for p in every):
        failures.append("`src/` changed but no files under `tests/` changed (TDD rule).")

    changed_modules = {
        m.group("module")
        for p in live
        if not p.endswith(TERRAFORM_TEST_SUFFIX) and (m := INFRA_MODULE_PATTERN.match(p))
    }
    tested_modules = {
        m.group("module")
        for p in every
        if p.endswith(TERRAFORM_TEST_SUFFIX) and (m := INFRA_MODULE_PATTERN.match(p))
    }
    for module in sorted(changed_modules - tested_modules):
        failures.append(
            f"`infra/modules/{module}` changed but none of its `*{TERRAFORM_TEST_SUFFIX}` "
            "tests changed (TDD rule)."
        )
    return failures


def check_protected_paths(files: Iterable[ChangedFile]) -> list[str]:
    """R4: paths the gate must never approve on its own."""
    return [f.path for f in files if f.path.startswith(PROTECTED_PREFIXES)]


def check_title(title: str) -> list[str]:
    """R5: conventional-commit title."""
    if TITLE_PATTERN.match(title):
        return []
    return ["PR title must follow conventional commits, e.g. `feat: ...` or `fix(router): ...`."]


def evaluate(pr: PullRequest, run: CiRun | None) -> Verdict:
    failures = [
        *check_ci(pr, run),
        *check_target(pr),
        *check_tdd(pr.files),
        *check_title(pr.title),
    ]
    return Verdict(
        failures=tuple(failures),
        protected_paths=tuple(check_protected_paths(pr.files)),
    )


# --- Presentation ------------------------------------------------------------------------


def render_comment(verdict: Verdict, head_sha: str) -> str:
    lines = [COMMENT_MARKER, "### PR policy gate", ""]
    if verdict.approve:
        lines.append(f"✅ **Approved** for commit `{head_sha}`. All rules passed.")
    elif verdict.failures:
        lines.append(f"❌ **Not approved** for commit `{head_sha}`. Fix the following:")
        lines.extend(f"- {failure}" for failure in verdict.failures)
    if verdict.needs_human:
        if verdict.failures:
            lines.append("")
        lines.append(
            "🔒 **Needs human review**: this PR changes protected paths, "
            "which the gate never approves:"
        )
        lines.extend(f"- `{path}`" for path in verdict.protected_paths)
        lines.append("")
        lines.append("Review it yourself and merge with admin bypass.")
    lines.extend(["", "<sub>Rules: docs/plan.md §9.1</sub>"])
    return "\n".join(lines)


# --- Orchestration -----------------------------------------------------------------------


def pr_numbers_from_event(event_name: str, payload: dict[str, Any]) -> list[int]:
    if event_name == "workflow_run":
        run = payload.get("workflow_run", {})
        if run.get("event") != "pull_request":
            return []
        return [int(pr["number"]) for pr in run.get("pull_requests", [])]
    if event_name == "pull_request_target":
        return [int(payload["pull_request"]["number"])]
    return []


class GitHub(Protocol):
    def get_pull(self, number: int) -> dict[str, Any]: ...
    def list_files(self, number: int) -> list[dict[str, str]]: ...
    def latest_ci_run(self, head_sha: str) -> dict[str, Any] | None: ...
    def list_reviews(self, number: int) -> list[dict[str, Any]]: ...
    def approve(self, number: int, head_sha: str, body: str) -> None: ...
    def list_comments(self, number: int) -> list[dict[str, Any]]: ...
    def create_comment(self, number: int, body: str) -> None: ...
    def update_comment(self, comment_id: int, body: str) -> None: ...


def pull_request_from_api(raw: dict[str, Any], files: Sequence[dict[str, str]]) -> PullRequest:
    return PullRequest(
        number=int(raw["number"]),
        title=str(raw["title"]),
        base_ref=str(raw["base"]["ref"]),
        head_sha=str(raw["head"]["sha"]),
        head_repo=str((raw["head"].get("repo") or {}).get("full_name", "")),
        base_repo=str(raw["base"]["repo"]["full_name"]),
        draft=bool(raw.get("draft", False)),
        files=tuple(ChangedFile(f["filename"], f["status"]) for f in files),
    )


def run_gate(gh: GitHub, number: int) -> Verdict:
    pr = pull_request_from_api(gh.get_pull(number), gh.list_files(number))
    raw_run = gh.latest_ci_run(pr.head_sha)
    run = (
        CiRun(
            head_sha=str(raw_run["head_sha"]),
            status=str(raw_run["status"]),
            conclusion=raw_run.get("conclusion"),
        )
        if raw_run
        else None
    )
    verdict = evaluate(pr, run)

    body = render_comment(verdict, pr.head_sha)
    existing = next(
        (
            c
            for c in gh.list_comments(number)
            if c["user"]["login"] == BOT_LOGIN and str(c["body"]).startswith(COMMENT_MARKER)
        ),
        None,
    )
    if existing is None:
        gh.create_comment(number, body)
    else:
        gh.update_comment(int(existing["id"]), body)

    already_approved = any(
        r["user"]["login"] == BOT_LOGIN
        and r["state"] == "APPROVED"
        and r.get("commit_id") == pr.head_sha
        for r in gh.list_reviews(number)
    )
    if verdict.approve and not already_approved:
        gh.approve(number, pr.head_sha, "Approved by the PR policy gate: all rules passed.")
    return verdict


# --- GitHub REST boundary (thin; exercised in CI, not unit-tested) ------------------------


class RestGitHub:  # pragma: no cover
    def __init__(self, token: str, repo: str, api: str = "https://api.github.com") -> None:
        self._token = token
        self._repo = repo
        self._api = api

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        request = urllib.request.Request(  # noqa: S310 - fixed https API base URL
            f"{self._api}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "pr-gate",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            data = response.read()
        return json.loads(data) if data else None

    def _paginate(self, path: str) -> list[Any]:
        items: list[Any] = []
        separator = "&" if "?" in path else "?"
        page = 1
        while True:
            batch = self._request("GET", f"{path}{separator}per_page=100&page={page}")
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1

    def get_pull(self, number: int) -> dict[str, Any]:
        result: dict[str, Any] = self._request("GET", f"/repos/{self._repo}/pulls/{number}")
        return result

    def list_files(self, number: int) -> list[dict[str, str]]:
        return self._paginate(f"/repos/{self._repo}/pulls/{number}/files")

    def latest_ci_run(self, head_sha: str) -> dict[str, Any] | None:
        result = self._request(
            "GET",
            f"/repos/{self._repo}/actions/workflows/{CI_WORKFLOW_FILE}/runs"
            f"?head_sha={head_sha}&event=pull_request&per_page=1",
        )
        runs: list[dict[str, Any]] = result.get("workflow_runs", [])
        return runs[0] if runs else None

    def list_reviews(self, number: int) -> list[dict[str, Any]]:
        return self._paginate(f"/repos/{self._repo}/pulls/{number}/reviews")

    def approve(self, number: int, head_sha: str, body: str) -> None:
        self._request(
            "POST",
            f"/repos/{self._repo}/pulls/{number}/reviews",
            {"commit_id": head_sha, "event": "APPROVE", "body": body},
        )

    def list_comments(self, number: int) -> list[dict[str, Any]]:
        return self._paginate(f"/repos/{self._repo}/issues/{number}/comments")

    def create_comment(self, number: int, body: str) -> None:
        self._request("POST", f"/repos/{self._repo}/issues/{number}/comments", {"body": body})

    def update_comment(self, comment_id: int, body: str) -> None:
        self._request("PATCH", f"/repos/{self._repo}/issues/comments/{comment_id}", {"body": body})


def main() -> int:  # pragma: no cover
    event_name = os.environ["GITHUB_EVENT_NAME"]
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as handle:
        payload = json.load(handle)
    numbers = pr_numbers_from_event(event_name, payload)
    if not numbers:
        print("No pull request associated with this event; nothing to do.")
        return 0
    gh = RestGitHub(os.environ["GITHUB_TOKEN"], os.environ["GITHUB_REPOSITORY"])
    for number in numbers:
        verdict = run_gate(gh, number)
        print(f"PR #{number}: {'approved' if verdict.approve else 'not approved'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
