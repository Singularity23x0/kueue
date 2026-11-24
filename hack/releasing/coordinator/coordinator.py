#!/usr/bin/env python3

# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Interactive coordinator script for Kueue releases.

Reduces handoff delays during a release by:
- Locating the release issue and verifying pre-flight readiness (steps 1-4).
- Posting ChatOps commands and polling GitHub Actions / Release Log for completion.
- Pausing for local PR-creation scripts (running them on Enter) and polling until
  the resulting PRs are merged (allowing replacement PR selection on retry).
- Running `/create-draft-release` in parallel with image promotion (`promote_pull.sh`
  and `kubernetes/k8s.io` PR merge) before joining on `/publish-release`.
- Treating checklist checkboxes as soft enablers: if checked `[x]`, the step is
  treated as already done; if unchecked `[ ]`, it never hard-blocks execution.

Usage:
  ./hack/releasing/coordinator/coordinator.py <version> [options]

Positional arguments:
  version                    Release version to process (format: vX.Y.Z, e.g. v0.20.8).

Options:
  --poll-interval <SECONDS>  Seconds between GitHub polling checks (default: 30).
  --repo <OWNER/REPO>        GitHub repository in OWNER/REPO format
                             (default: kubernetes-sigs/kueue).
  --skip-readiness-check     Skip verifying that checklist steps 1-4 are completed
                             before starting.
  --api-url <URL>            Base URL for the GitHub REST API
                             (default: https://api.github.com; override for mock servers).

Testing with a mock GitHub REST API server:
  Pass `--api-url <URL>` (e.g. `http://127.0.0.1:8787`) to point `GitHubClient`
  at a local mock server instead of `https://api.github.com`. A mock server must
  implement the following GitHub REST API v3 endpoints:
    - GET /repos/{repo}/issues?state=open&labels=area/release&per_page=100
        Returns list of issue objects (`number`, `title`, `html_url`).
    - GET /repos/{repo}/issues/{issue_number}
        Returns issue object (`body`).
    - POST /repos/{repo}/issues/{issue_number}/comments
        Accepts `{"body": "..."}` to post a ChatOps command comment.
    - PATCH /repos/{repo}/issues/{issue_number}
        Accepts `{"state": "closed"}` to close the release issue.
    - GET /repos/{pr_repo}/pulls/{pr_number}
        Returns PR object (`state`, `merged`, `html_url`).
    - GET /repos/{repo}/actions/runs/{run_id}
        Returns workflow run object (`status`, `conclusion`).
    - GET /repos/{repo}/releases?per_page=50
        Returns list of release objects (`tag_name`, `draft`, `prerelease`).
    - GET /user
        Returns user object (`login`), used when `GITHUB_USER` is unset.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from typing import Any, Callable, Optional
import urllib.error
import urllib.parse
import urllib.request

_COORDINATOR_DIR = Path(__file__).resolve().parent
_RELEASING_DIR = _COORDINATOR_DIR.parent
for _p in (str(_COORDINATOR_DIR), str(_RELEASING_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from mark_step_done import LOG_MARKER_START
from release_graph import (
    CHECKLIST_LINE_RE,
    REPO_ROOT,
    STEP_ID_RE,
    STEP_MARKER_RE,
    TEMPLATE_PATH,
    ReleaseGraph,
    Step,
    Version,
    build_release_graph,
    checklist_section,
    parse_version,
    step_key,
)

DEFAULT_REPO = "kubernetes-sigs/kueue"
DEFAULT_API_URL = "https://api.github.com"
DEFAULT_POLL_INTERVAL = 30

ENTRY_RE = re.compile(
    r'<!--\s*entry-start\s+title="(?P<title>[^"]+)"(?:\s+keep[-_]in[-_]history="[^"]*")?\s*-->'
    r"(?P<body>.*?)"
    r"<!--\s*entry-end\s*-->",
    re.DOTALL,
)
ACTION_RUN_RE = re.compile(r"Action link:\s*\S+/actions/runs/(\d+)")
TIMESTAMP_RE = re.compile(r"Timestamp:\s*(.+)")
PR_URL_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)")
PR_REF_RE = re.compile(r"(?:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#(\d+)")


class AbortRelease(Exception):
    """Raised when the operator chooses to quit the coordinator."""


@dataclass
class ChecklistItem:
    checked: bool
    text: str
    step_id: Optional[str] = None
    step_marker: Optional[str] = None


@dataclass
class ReleaseLogEntry:
    title: str
    timestamp: Optional[str]
    run_id: Optional[str]
    action_link: Optional[str]
    body: str


@dataclass(frozen=True)
class PullRequestRef:
    repo: str
    number: int

    @property
    def display(self) -> str:
        return f"{self.repo}#{self.number}"


@dataclass(frozen=True)
class ParsedStepState:
    """Parsed per-step state extracted once from the release issue body."""

    checked: bool = False
    pr_ref: Optional[PullRequestRef] = None


@dataclass(frozen=True)
class ReleaseIssue:
    """Release issue metadata and parsed checklist / Release Log snapshot."""

    number: int = 0
    title: str = ""
    url: str = ""
    steps: dict[str, ParsedStepState] = field(default_factory=dict)
    log_entries: dict[str, ReleaseLogEntry] = field(default_factory=dict)

    def is_checked(self, step_id: str) -> bool:
        state = self.steps.get(step_id)
        return bool(state and state.checked)

    def pr_ref(self, step_id: str) -> Optional[PullRequestRef]:
        state = self.steps.get(step_id)
        return state.pr_ref if state else None

    def log_entry(self, title: str) -> Optional[ReleaseLogEntry]:
        return self.log_entries.get(title)


@dataclass
class ChatOpsHandle:
    step_id: str
    step: Step
    command: str
    before_entry: Optional[ReleaseLogEntry]
    before_auth_entry: Optional[ReleaseLogEntry]
    attached: bool = False


def parse_checklist(issue_body: str) -> list[ChecklistItem]:
    items: list[ChecklistItem] = []
    for line in checklist_section(issue_body).splitlines():
        match = CHECKLIST_LINE_RE.match(line)
        if not match:
            continue
        checked = match.group(1).lower() == "x"
        text = match.group(2)
        id_match = STEP_ID_RE.match(text)
        step_id = id_match.group(1) if id_match else None
        marker_match = STEP_MARKER_RE.search(text)
        step_marker = marker_match.group(1) if marker_match else None
        items.append(ChecklistItem(checked=checked, text=text, step_id=step_id, step_marker=step_marker))
    return items


def find_checklist_item(items: list[ChecklistItem], step: Step) -> Optional[ChecklistItem]:
    """Find a checklist item by explicit numeric ID, falling back to Step metadata."""
    for item in items:
        if item.step_id == step.id:
            return item

    if step.marker:
        # Note: wait-for-prod-images appears on both 8.2 and 8.2.3; prefer the last match.
        matches = [item for item in items if item.step_marker == step.marker]
        if matches:
            return matches[-1]

    if step.pr_example_hint:
        for item in items:
            if step.pr_example_hint in item.text:
                return item

    if step.text_hints:
        for item in items:
            lowered = item.text.lower()
            if all(hint.lower() in lowered for hint in step.text_hints):
                return item

    return None


def check_readiness(release_issue: ReleaseIssue, graph: ReleaseGraph) -> list[str]:
    """Verify required prereq steps in `graph` are checked. Returns unmet requirement descriptions."""
    missing: list[str] = []

    for step_id, step in graph.items():
        if step.kind != "prereq" or step.optional:
            continue
        if not release_issue.is_checked(step_id):
            missing.append(f"[{step_id}] {step.description}")

    return missing


def verify_issue(
    release_issue: ReleaseIssue,
    graph: ReleaseGraph,
    pr_is_merged: Optional[Callable[[PullRequestRef], bool]] = None,
) -> list[str]:
    """Verify that step requirements are fulfilled on the release issue:

    1. Required pre-requisite steps in `graph` are checked.
    2. No checked `[x]` step has an unfulfilled required step
       (a step cannot be marked ok while its required parent step is not ok).
    """
    errors: list[str] = []

    for missing in check_readiness(release_issue, graph):
        errors.append(f"Missing pre-requisite: {missing}")

    pr_cache: dict[PullRequestRef, bool] = {}

    def _step_is_ok(sid: str) -> bool:
        if release_issue.is_checked(sid):
            return True
        st = graph.get(sid)
        if st is not None and st.kind == "wait_pr" and pr_is_merged is not None:
            pr = release_issue.pr_ref(sid)
            if pr is not None:
                if pr not in pr_cache:
                    pr_cache[pr] = pr_is_merged(pr)
                return pr_cache[pr]
        return False

    for step_id, step in graph.items():
        if not release_issue.is_checked(step_id):
            continue
        for req_id in step.required_steps:
            if not _step_is_ok(req_id):
                errors.append(
                    f"Invalid checkmark: step [{step_id}] is checked off, "
                    f"but required step [{req_id}] is not completed."
                )

    return errors


def parse_release_log_entries(issue_body: str) -> dict[str, ReleaseLogEntry]:
    """Parse all active entries from the issue body's Release Log into a title -> entry map."""
    log_idx = issue_body.find(LOG_MARKER_START)
    if log_idx == -1:
        return {}
    log_section = issue_body[log_idx:]
    # Only search the active details section (before history).
    hist_idx = log_section.find("<!-- history-section-start -->")
    if hist_idx != -1:
        log_section = log_section[:hist_idx]

    entries: dict[str, ReleaseLogEntry] = {}
    for match in ENTRY_RE.finditer(log_section):
        title = match.group("title")
        if title in entries:
            continue
        body = match.group("body").strip()
        ts_match = TIMESTAMP_RE.search(body)
        run_match = ACTION_RUN_RE.search(body)
        action_link = None
        link_match = re.search(r"Action link:\s*(\S+)", body)
        if link_match:
            action_link = link_match.group(1)
        entries[title] = ReleaseLogEntry(
            title=title,
            timestamp=ts_match.group(1).strip() if ts_match else None,
            run_id=run_match.group(1) if run_match else None,
            action_link=action_link,
            body=body,
        )
    return entries


def parse_release_issue(
    issue_body: str,
    graph: ReleaseGraph,
    kueue_repo: str = DEFAULT_REPO,
    *,
    number: int = 0,
    title: str = "",
    url: str = "",
) -> ReleaseIssue:
    """Parse checklist checkboxes, PR references, and Release Log entries in a single pass."""
    items = parse_checklist(issue_body)
    steps: dict[str, ParsedStepState] = {}
    for step_id, step in graph.items():
        item = find_checklist_item(items, step)
        checked = bool(item and item.checked)
        pr_ref = None
        if item and step.pr_default_repo:
            # Strip trailing example comment so example PR numbers (#211, etc.) are ignored.
            text = re.split(r"<!--\s*example\b", item.text, maxsplit=1)[0]
            default_repo = step.pr_default_repo.format(kueue_repo=kueue_repo)
            pr_ref = parse_pr_ref_text(text, default_repo)
        steps[step_id] = ParsedStepState(checked=checked, pr_ref=pr_ref)
    return ReleaseIssue(
        number=number,
        title=title,
        url=url,
        steps=steps,
        log_entries=parse_release_log_entries(issue_body),
    )


def command_log_title(command: str) -> str:
    return command.replace("-", " ").capitalize()


def extract_pr_ref(
    issue_body: str, step_id: str, kueue_repo: str, graph: ReleaseGraph
) -> Optional[PullRequestRef]:
    """Extract a PR reference from a 'Wait for PR to merge' checklist line."""
    return parse_release_issue(issue_body, graph, kueue_repo).pr_ref(step_id)


def parse_pr_ref_text(text: str, default_repo: str) -> Optional[PullRequestRef]:
    url_match = PR_URL_RE.search(text)
    if url_match:
        return PullRequestRef(repo=url_match.group(1), number=int(url_match.group(2)))
    ref_match = PR_REF_RE.search(text)
    if ref_match:
        repo = ref_match.group(1) or default_repo
        return PullRequestRef(repo=repo, number=int(ref_match.group(2)))
    bare_num = re.fullmatch(r"\s*(\d+)\s*", text)
    if bare_num:
        return PullRequestRef(repo=default_repo, number=int(bare_num.group(1)))
    return None


def is_latest_semver(version: Version, published_tags: list[str]) -> bool:
    highest = version.tuple
    for tag in published_tags:
        try:
            parsed = parse_version(tag)
        except ValueError:
            continue
        if parsed.tuple > highest:
            return False
    return True


class GitHubClient:
    """GitHub REST API v3 client using stdlib urllib.request."""

    def __init__(
        self,
        repo: str,
        api_url: str = DEFAULT_API_URL,
        token: Optional[str] = None,
        timeout: int = 30,
    ) -> None:
        self.repo = repo
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.token = token if token is not None else self._resolve_token()

    def _resolve_token(self) -> Optional[str]:
        for env_var in ("GITHUB_TOKEN", "GH_TOKEN"):
            val = os.environ.get(env_var, "").strip()
            if val:
                return val
        if self.api_url != DEFAULT_API_URL:
            return None
        try:
            proc = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()
        except FileNotFoundError:
            pass
        return None

    def _url(self, path: str, query: Optional[dict[str, str]] = None) -> str:
        url = f"{self.api_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        return url

    def _request(
        self,
        method: str,
        url: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "kueue-release-coordinator",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        host = urllib.parse.urlparse(url).hostname
        opener = (
            urllib.request.build_opener(urllib.request.ProxyHandler({}))
            if host in ("127.0.0.1", "localhost", "::1")
            else urllib.request.build_opener()
        )
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"GitHub API {method} {url} failed (HTTP {err.code}): {detail}") from err
        except urllib.error.URLError as err:
            raise RuntimeError(f"GitHub API {method} {url} connection error: {err.reason}") from err

    def find_release_issue(self, version: Version) -> tuple[int, str, str]:
        url = self._url(
            f"/repos/{self.repo}/issues",
            query={"state": "open", "labels": "area/release", "per_page": "100"},
        )
        issues = self._request("GET", url) or []
        exact = re.compile(rf"^(?:📦\s+)?Release\s+{re.escape(version.raw)}$")
        matches = [
            issue
            for issue in issues
            if "pull_request" not in issue and exact.match(issue.get("title", "").strip())
        ]
        if not matches:
            raise RuntimeError(
                f"No open release issue found in {self.repo} with title 'Release {version.raw}'."
            )
        if len(matches) > 1:
            nums = ", ".join(f"#{m['number']}" for m in matches)
            raise RuntimeError(f"Multiple open release issues match {version.raw}: {nums}")
        match = matches[0]
        return int(match["number"]), match["title"], match.get("html_url") or match.get("url", "")

    def get_issue_body(self, issue_number: int) -> str:
        data = self._request("GET", self._url(f"/repos/{self.repo}/issues/{issue_number}"))
        return data.get("body") or ""

    def post_comment(self, issue_number: int, body: str) -> None:
        self._request(
            "POST",
            self._url(f"/repos/{self.repo}/issues/{issue_number}/comments"),
            payload={"body": body},
        )

    def close_issue(self, issue_number: int) -> None:
        self._request(
            "PATCH",
            self._url(f"/repos/{self.repo}/issues/{issue_number}"),
            payload={"state": "closed"},
        )

    def get_pr_state(self, pr: PullRequestRef) -> tuple[str, str]:
        data = self._request("GET", self._url(f"/repos/{pr.repo}/pulls/{pr.number}"))
        state = "MERGED" if data.get("merged") else str(data.get("state", "")).upper()
        return state, data.get("html_url") or data.get("url") or pr.display

    def get_run_status(self, run_id: str) -> tuple[str, str]:
        data = self._request("GET", self._url(f"/repos/{self.repo}/actions/runs/{run_id}"))
        return data.get("status", ""), data.get("conclusion") or ""

    def list_published_release_tags(self) -> list[str]:
        releases = self._request(
            "GET",
            self._url(f"/repos/{self.repo}/releases", query={"per_page": "50"}),
        ) or []
        return [
            r["tag_name"]
            for r in releases
            if not r.get("draft") and not r.get("prerelease") and r.get("tag_name")
        ]

    def current_user(self) -> str:
        env_user = os.environ.get("GITHUB_USER", "").strip()
        if env_user:
            return env_user
        try:
            data = self._request("GET", self._url("/user"))
            return str(data.get("login", "")).strip() or "<your-user>"
        except RuntimeError:
            return "<your-user>"


class ReleaseCoordinator:
    def __init__(
        self,
        version: Version,
        gh: GitHubClient,
        repo_root: Path,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        skip_readiness_check: bool = False,
        graph: Optional[ReleaseGraph] = None,
        input_fn: Callable[[str], str] = input,
        sleep_fn: Callable[[float], None] = time.sleep,
        run_local_fn: Optional[Callable[[list[str], dict[str, str]], int]] = None,
    ) -> None:
        self.version = version
        self.gh = gh
        self.repo_root = repo_root
        self.poll_interval = poll_interval
        self.skip_readiness_check = skip_readiness_check
        self.graph = (
            graph
            if graph is not None
            else build_release_graph(TEMPLATE_PATH.read_text(encoding="utf-8"), version=version)
        )
        self.input_fn = input_fn
        self.sleep_fn = sleep_fn
        self.run_local_fn = run_local_fn or self._default_run_local
        self.github_user: str = ""
        self.release_issue: ReleaseIssue = ReleaseIssue()
        self._pending_steps: dict[str, ChatOpsHandle] = {}

    def _print_pending_wait(
        self, still_waiting: list[ChatOpsHandle], before_step_id: Optional[str] = None
    ) -> None:
        if len(still_waiting) > 1:
            context = f" before step [{before_step_id}]" if before_step_id else ""
            print(f"⏳ Waiting on {len(still_waiting)} operations{context}:")
            for h in still_waiting:
                print(f"   - Step [{h.step_id}]: `/{h.command}` ({h.step.description})")
        elif len(still_waiting) == 1:
            h = still_waiting[0]
            print(f"⏳ Step [{h.step_id}]: Waiting for `/{h.command}` to complete...")

    def _check_chatops_handle(self, handle: ChatOpsHandle, state: ReleaseIssue) -> tuple[str, Optional[str]]:
        """Evaluate a single ChatOpsHandle against the latest ReleaseIssue state.

        Returns `(status, detail)` where status is one of:
        - `"done"`: step succeeded (checkbox `[x]` or `✅` / `### Images Ready`).
        - `"failed"`: step failed (`❌`, auth error, or failed workflow run).
        - `"pending"`: still running.
        """
        if state.is_checked(handle.step_id):
            return "done", None

        auth_entry = state.log_entry("Authorization")
        if auth_entry and self._entry_is_newer(auth_entry, handle.before_auth_entry):
            if "❌" in auth_entry.body:
                return "failed", f"{auth_entry.body}\nRun: {auth_entry.action_link or 'n/a'}"

        title = command_log_title(handle.command)
        entry = state.log_entry(title)
        if not entry or (not handle.attached and not self._entry_is_newer(entry, handle.before_entry)):
            return "pending", None

        if any(m in entry.body for m in handle.step.in_progress_markers):
            if entry.run_id:
                status, conclusion = self.gh.get_run_status(entry.run_id)
                if status == "completed" and conclusion != "success":
                    return (
                        "failed",
                        f"Workflow run {entry.action_link or entry.run_id} ended with "
                        f"conclusion={conclusion!r}.",
                    )
            return "pending", None

        if any(m in entry.body for m in handle.step.failure_markers):
            return (
                "failed",
                f"/{handle.command} reported failure.\nRun: {entry.action_link or 'n/a'}\n{entry.body}",
            )

        if any(m in entry.body for m in handle.step.success_markers):
            return "done", None

        return "pending", None

    def _wait_for_pending_steps(
        self,
        step_ids: Optional[tuple[str, ...]] = None,
        before_step_id: Optional[str] = None,
    ) -> None:
        if not self._pending_steps:
            return
        if step_ids is None:
            pending = list(self._pending_steps.values())
            self._pending_steps.clear()
        else:
            target = set(step_ids)
            pending = [h for sid, h in self._pending_steps.items() if sid in target]
            for h in pending:
                self._pending_steps.pop(h.step_id, None)
        if not pending:
            return

        still_waiting: list[ChatOpsHandle] = []
        for h in pending:
            if self.release_issue.is_checked(h.step_id):
                print(f"✅ Step [{h.step_id}] (`/{h.command}`) completed.")
            else:
                still_waiting.append(h)

        if not still_waiting:
            return

        self._print_pending_wait(still_waiting, before_step_id)

        while still_waiting:
            self.sleep_fn(self.poll_interval)
            state = self._sync_release_issue()

            remaining: list[ChatOpsHandle] = []
            changed = False
            for handle in still_waiting:
                status, detail = self._check_chatops_handle(handle, state)
                if status == "done":
                    print(f"✅ Step [{handle.step_id}] (`/{handle.command}`) completed.")
                    changed = True
                elif status == "failed":
                    action = self._prompt_on_failure(
                        handle.step_id,
                        detail or "Command failed.",
                        allow_skip=handle.step.optional,
                    )
                    if action == "skip":
                        changed = True
                        continue
                    new_handle = self._retry_chatops(handle)
                    if new_handle is None:
                        changed = True
                        continue
                    remaining.append(new_handle)
                else:
                    remaining.append(handle)

            still_waiting = remaining
            if changed and still_waiting:
                self._print_pending_wait(still_waiting, before_step_id)

    def _sync_release_issue(self) -> ReleaseIssue:
        body = self.gh.get_issue_body(self.release_issue.number)
        self.release_issue = parse_release_issue(
            body,
            self.graph,
            self.gh.repo,
            number=self.release_issue.number,
            title=self.release_issue.title,
            url=self.release_issue.url,
        )
        return self.release_issue

    def _format(self, template: str) -> str:
        return template.format(
            version=self.version.raw,
            kueue_repo=self.gh.repo,
            issue_number=self.release_issue.number,
            issue_url=self.release_issue.url,
        )

    def _default_run_local(self, cmd: list[str], env: dict[str, str]) -> int:
        merged_env = os.environ.copy()
        merged_env.update(env)
        proc = subprocess.run(cmd, cwd=self.repo_root, env=merged_env, check=False)
        return proc.returncode

    def _notify(self, message: str) -> None:
        print(f"\a{message}", flush=True)

    def _prompt_on_failure(self, step_id: str, reason: str, allow_skip: bool = False) -> str:
        """Inform the user of a failure and await a signal: 'retry', 'skip' (if optional), or 'quit'."""
        self._notify(f"\n❌ Step [{step_id}] failed: {reason}")
        prompt = (
            "Action? [r]etry (default) / [s]kip / [q]uit: "
            if allow_skip
            else "Action? [r]etry (default) / [q]uit: "
        )
        while True:
            ans = self.input_fn(prompt).strip().lower()
            if ans in ("", "r", "retry"):
                return "retry"
            if allow_skip and ans in ("s", "skip"):
                return "skip"
            if ans in ("q", "quit"):
                raise AbortRelease(f"Aborted at step [{step_id}].")
            allowed = "'r' (retry), 's' (skip), or 'q' (quit)" if allow_skip else "'r' (retry) or 'q' (quit)"
            print(f"Please enter {allowed}.")

    def _should_skip_step(self, step_id: str, step: Step) -> bool:
        if step.kind == "prereq":
            return True
        if self.release_issue.is_checked(step_id):
            print(f"✅ Step [{step_id}] is already checked off on the issue — skipping.")
            return True
        if step.only_if_latest:
            published_tags = self.gh.list_published_release_tags()
            if not is_latest_semver(self.version, published_tags):
                print(
                    f"↪ Step [{step_id}]: {self.version.raw} is not the latest published release — "
                    f"skipping {step.description}."
                )
                return True
        return False

    def _is_entry_in_progress(self, step: Step, entry: Optional[ReleaseLogEntry]) -> bool:
        if not entry or not any(m in entry.body for m in step.in_progress_markers):
            return False
        if entry.run_id:
            status, _conclusion = self.gh.get_run_status(entry.run_id)
            if status == "completed":
                return False
        return True

    def _start_chatops(self, step_id: str, step: Step) -> ChatOpsHandle:
        assert isinstance(step.command, str)
        command = step.command
        title = command_log_title(command)
        before_entry = self.release_issue.log_entry(title)
        before_auth = self.release_issue.log_entry("Authorization")
        if self._is_entry_in_progress(step, before_entry):
            run_info = f" (run #{before_entry.run_id})" if before_entry and before_entry.run_id else ""
            print(f"\nℹ️ Step [{step_id}]: `/{command}` is already in progress{run_info}.")
            return ChatOpsHandle(
                step_id=step_id,
                step=step,
                command=command,
                before_entry=before_entry,
                before_auth_entry=before_auth,
                attached=True,
            )
        print(f"\n▶ Step [{step_id}]: Posting `/{command}` on #{self.release_issue.number}...")
        self.gh.post_comment(self.release_issue.number, f"/{command}")
        return ChatOpsHandle(
            step_id=step_id,
            step=step,
            command=command,
            before_entry=before_entry,
            before_auth_entry=before_auth,
        )

    def _retry_chatops(self, handle: ChatOpsHandle) -> Optional[ChatOpsHandle]:
        if self._sync_release_issue().is_checked(handle.step_id):
            print(f"✅ Step [{handle.step_id}] is already checked off on the issue — skipping.")
            return None
        return self._start_chatops(handle.step_id, handle.step)

    @staticmethod
    def _entry_is_newer(
        current: ReleaseLogEntry, previous: Optional[ReleaseLogEntry]
    ) -> bool:
        if previous is None:
            return True
        if current.run_id and previous.run_id and current.run_id != previous.run_id:
            return True
        if current.timestamp and previous.timestamp and current.timestamp != previous.timestamp:
            return True
        return current.body != previous.body

    def _run_local_script_step(self, step_id: str, step: Step) -> None:
        assert isinstance(step.command, tuple)
        cmd = [self._format(part) for part in step.command]
        env = {"GITHUB_USER": self.github_user}
        env_prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
        display_cmd = f"{env_prefix} {' '.join(shlex.quote(c) for c in cmd)}"
        message = self._format(step.confirm_message or step.description)

        while True:
            if step.wait_for_user_confirm:
                self._notify(f"\n⏸ Step [{step_id}]: {message}\n  Command: {display_cmd}")
                prompt = (
                    "[Enter] Run command | [m] Already ran manually | [s] Skip | [q] Quit: "
                    if step.optional
                    else "[Enter] Run command | [m] Already ran manually | [q] Quit: "
                )
                ans = self.input_fn(prompt).strip().lower()
                if ans in ("q", "quit"):
                    raise AbortRelease(f"Aborted at step [{step_id}].")
                if step.optional and ans in ("s", "skip"):
                    print(f"↪ Skipping step [{step_id}].")
                    return
                if ans in ("m", "manual"):
                    print(f"↪ Continuing past step [{step_id}].")
                    self._sync_release_issue()
                    return
                if ans != "":
                    allowed = "'m', 's', or 'q'" if step.optional else "'m' or 'q'"
                    print(f"Please press Enter to run, or enter {allowed}.")
                    continue

            rc = self.run_local_fn(cmd, env)
            if rc == 0:
                print(f"✅ Step [{step_id}] command exited 0.")
                # Local scripts inject the newly opened PR reference into the issue body.
                self._sync_release_issue()
                return
            action = self._prompt_on_failure(
                step_id,
                f"Local command exited with status {rc}.",
                allow_skip=step.optional,
            )
            if action == "skip":
                return

    def _prompt_for_pr(
        self,
        step_id: str,
        description: str,
        default_repo: str,
        prompt_header: str,
        allow_skip: bool = False,
    ) -> Optional[PullRequestRef]:
        """Prompt the operator for a PR reference (or reload the release issue / quit)."""
        prompt = (
            f"Enter PR number/URL (default repo {default_repo}), [r]eload issue, [s]kip, or [q]uit: "
            if allow_skip
            else f"Enter PR number/URL (default repo {default_repo}), [r]eload issue, or [q]uit: "
        )
        while True:
            self._notify(prompt_header)
            raw = self.input_fn(prompt).strip()
            low = raw.lower()
            if low in ("q", "quit"):
                raise AbortRelease(f"Aborted at step [{step_id}].")
            if allow_skip and low in ("s", "skip"):
                return None
            if low in ("", "r", "reload"):
                state = self._sync_release_issue()
                if state.is_checked(step_id):
                    print(f"✅ Step [{step_id}] is now checked off — skipping.")
                    return None
                pr = state.pr_ref(step_id)
                if pr is not None:
                    return pr
                print(f"Still no PR reference found in issue #{self.release_issue.number} for {description}.")
                continue
            pr = parse_pr_ref_text(raw, default_repo)
            if pr is not None:
                return pr
            print(f"Could not parse PR reference from {raw!r}.")

    def _wait_pr_merge_step(self, step_id: str, step: Step) -> None:
        assert step.pr_default_repo is not None
        default_repo = self._format(step.pr_default_repo)
        description = self._format(step.description)
        pr = self.release_issue.pr_ref(step_id)

        if pr is None:
            pr = self._prompt_for_pr(
                step_id,
                description,
                default_repo,
                f"\n⏸ Step [{step_id}]: Could not find PR reference in issue #{self.release_issue.number} "
                f"for {description}.",
                allow_skip=step.optional,
            )
            if pr is None:
                return

        announced_wait = False
        while True:
            pr_state, url = self.gh.get_pr_state(pr)
            if pr_state == "MERGED":
                print(f"✅ Step [{step_id}]: PR {url} is merged.")
                return
            if pr_state == "CLOSED":
                action = self._prompt_on_failure(
                    step_id,
                    f"PR {url} was closed without being merged.",
                    allow_skip=step.optional,
                )
                if action == "skip":
                    return
                # Refresh issue first; if a different PR is already linked, switch to it automatically.
                refreshed_pr = self._sync_release_issue().pr_ref(step_id)
                if refreshed_pr is not None and refreshed_pr != pr:
                    pr = refreshed_pr
                else:
                    new_pr = self._prompt_for_pr(
                        step_id,
                        description,
                        default_repo,
                        f"\n⏸ Step [{step_id}]: Provide a replacement PR for {description} (previous: {pr.display}).",
                        allow_skip=step.optional,
                    )
                    if new_pr is None:
                        return
                    pr = new_pr
                announced_wait = False
                continue

            if not announced_wait:
                print(f"⏳ Step [{step_id}]: Waiting for PR {pr.display} to merge...")
                announced_wait = True
            self.sleep_fn(self.poll_interval)

    def _pause_manual_step(self, step_id: str, step: Step) -> None:
        instructions = self._format(step.confirm_message or step.description)
        self._notify(f"\n⏸ Step [{step_id}]: Manual action required:\n  {instructions}")
        prompt = (
            "[Enter] Done, continue | [s] Skip | [q] Quit: "
            if step.optional
            else "[Enter] Done, continue | [q] Quit: "
        )
        while True:
            ans = self.input_fn(prompt).strip().lower()
            if ans == "" or (step.optional and ans in ("s", "skip")):
                return
            if ans in ("q", "quit"):
                raise AbortRelease(f"Aborted at step [{step_id}].")
            allowed = "Enter, 's', or 'q'" if step.optional else "Enter or 'q'"
            print(f"Please press {allowed}.")

    def _finalize_issue(self, step_id: str, step: Step) -> None:
      msg = self._format(step.confirm_message or "Close release issue #{issue_number}?")
      self._notify(f"\n⏸ Step [{step_id}]: {msg}")
      ans = self.input_fn(
          "[Enter] Close issue | [s] Leave open & finish | [q] Quit: "
      ).strip().lower()
      if ans in ("q", "quit"):
          raise AbortRelease(f"Aborted at step [{step_id}].")
      if ans == "":
          self.gh.close_issue(self.release_issue.number)
          print(f"✅ Closed issue #{self.release_issue.number}.")

    def _confirm_optional_step(self, step_id: str, step: Step) -> bool:
        desc = self._format(step.description)
        self._notify(f"\n⏸ Step [{step_id}] is optional: {desc}")
        while True:
            ans = self.input_fn("Run this optional step? [y]es / [n]o (default) / [q]uit: ").strip().lower()
            if ans in ("y", "yes"):
                return True
            if ans in ("", "n", "no"):
                print(f"↪ Skipping optional step [{step_id}].")
                return False
            if ans in ("q", "quit"):
                raise AbortRelease(f"Aborted at step [{step_id}].")
            print("Please enter 'y' (yes), 'n' (no), or 'q' (quit).")

    def _verify_release_issue(self) -> list[str]:
        def _pr_is_merged(pr: PullRequestRef) -> bool:
            state, _url = self.gh.get_pr_state(pr)
            return state == "MERGED"

        return verify_issue(self.release_issue, self.graph, pr_is_merged=_pr_is_merged)

    def _execute_step(self, step_id: str, step: Step) -> None:
        if self._should_skip_step(step_id, step):
            return

        if step.optional and not self._confirm_optional_step(step_id, step):
            return

        if step.required_steps:
            self._wait_for_pending_steps(step_ids=step.required_steps, before_step_id=step_id)

        if step.kind == "chatops":
            handle = self._start_chatops(step_id, step)
            self._pending_steps[step_id] = handle
        elif step.kind == "local_script":
            self._run_local_script_step(step_id, step)
        elif step.kind == "wait_pr":
            self._wait_pr_merge_step(step_id, step)
        elif step.kind == "manual":
            self._pause_manual_step(step_id, step)

        if step.close_issue_after:
            self._wait_for_pending_steps()
            self._finalize_issue(step_id, step)

    def run(self) -> None:
        print(f"📋 Release flow ({self.version.raw}):")
        print(self.graph.visualize())
        print()

        number, title, url = self.gh.find_release_issue(self.version)
        self.release_issue = ReleaseIssue(number=number, title=title, url=url)
        self.github_user = self.gh.current_user()
        print(f"📦 Found release issue #{self.release_issue.number}: {self.release_issue.title}")
        print(f"   URL: {self.release_issue.url}")
        print(f"   Release type: {'patch' if self.version.is_patch else 'major/minor'}")

        self._sync_release_issue()
        if not self.skip_readiness_check:
            errors = self._verify_release_issue()
            if errors:
                print("\n❌ Release issue verification failed:")
                for item in errors:
                    print(f"   - {item}")
                print("\nFix the above issues (or pass --skip-readiness-check) and re-run.")
                sys.exit(1)
            print("✅ Release issue verification passed.")

        for step_id, step in self.graph.items():
            self._execute_step(step_id, step)
        self._wait_for_pending_steps()
        print(f"\n🎉 Release {self.version.raw} complete!")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "version",
        help="Release version to process (format: vX.Y.Z, e.g. v0.20.8).",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=DEFAULT_POLL_INTERVAL,
        help=f"Seconds between GitHub polling checks (default: {DEFAULT_POLL_INTERVAL}).",
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help=f"GitHub repository in OWNER/REPO format (default: {DEFAULT_REPO}).",
    )
    parser.add_argument(
        "--skip-readiness-check",
        action="store_true",
        help="Skip verifying that checklist steps 1-4 are completed before starting.",
    )
    parser.add_argument(
        "--api-url",
        default=DEFAULT_API_URL,
        help=f"Base URL for the GitHub REST API (default: {DEFAULT_API_URL}).",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        version = parse_version(args.version)
    except ValueError as err:
        parser.error(str(err))

    graph = build_release_graph(TEMPLATE_PATH.read_text(encoding="utf-8"), version=version)
    gh = GitHubClient(repo=args.repo, api_url=args.api_url)
    coordinator = ReleaseCoordinator(
        version=version,
        gh=gh,
        repo_root=REPO_ROOT,
        poll_interval=args.poll_interval,
        skip_readiness_check=args.skip_readiness_check,
        graph=graph,
    )
    try:
        coordinator.run()
    except AbortRelease as err:
        print(f"\n🛑 {err}")
        return 130
    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user.")
        return 130
    except RuntimeError as err:
        print(f"\n❌ {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
