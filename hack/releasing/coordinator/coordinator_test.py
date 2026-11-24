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

"""Unit tests for coordinator.py and release_graph.py."""

from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import sys
import threading
import unittest
from unittest import mock

_COORDINATOR_DIR = Path(__file__).resolve().parent
_RELEASING_DIR = _COORDINATOR_DIR.parent
for _p in (str(_COORDINATOR_DIR), str(_RELEASING_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import coordinator as rc
from mark_step_done import LOG_MARKER_END, LOG_MARKER_START, mark_step_done

REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_PATH = REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "NEW_RELEASE.md"


def make_ready_body(patch: bool = True) -> str:
    body = TEMPLATE_PATH.read_text()
    body = mark_step_done(body, "sync-release-notes")
    body = body.replace(
        "- [ ] **[3]** [OWNERS]",
        "- [x] **[3]** [OWNERS]",
        1,
    )
    if not patch:
        body = mark_step_done(body, "create-release-branch")
    return body


def append_log_entry(body: str, title: str, message: str, run_id: str = "1001", ts: str = "2026-11-23 10:00:00 UTC") -> str:
    entry = (
        f'<!-- entry-start title="{title}" keep-in-history="true" -->\n'
        f"### {title}\n"
        f"Triggered by: @tester\n"
        f"Timestamp: {ts}\n"
        f"Action link: https://github.com/kubernetes-sigs/kueue/actions/runs/{run_id}\n\n"
        f"{message}\n"
        f"<!-- entry-end -->"
    )
    if LOG_MARKER_START not in body:
        return f"{body}\n\n{LOG_MARKER_START}\n## Release log\n\n{entry}\n{LOG_MARKER_END}"
    idx = body.find(LOG_MARKER_START) + len(LOG_MARKER_START)
    return f"{body[:idx]}\n## Release log\n\n{entry}\n{body[idx:]}"


class FakeGitHubClient:
    """In-memory fake of GitHubClient for deterministic coordinator tests."""

    def __init__(
        self,
        body: str,
        issue_number: int = 19142,
        repo: str = "kubernetes-sigs/kueue",
        published_tags: list[str] | None = None,
        fail_once_commands: set[str] | None = None,
    ) -> None:
        self.repo = repo
        self.issue_number = issue_number
        self.body = body
        self.published_tags = published_tags or []
        self.fail_once_commands = set(fail_once_commands or ())
        self.comments: list[str] = []
        self.closed = False
        self.pr_states: dict[tuple[str, int], list[str]] = {}
        self.run_counter = 1000
        self.events: list[str] = []
        self.get_body_calls = 0

    def find_release_issue(self, version: rc.Version) -> tuple[int, str, str]:
        return (
            self.issue_number,
            f"📦 Release {version.raw}",
            f"https://github.com/{self.repo}/issues/{self.issue_number}",
        )

    def get_issue_body(self, issue_number: int) -> str:
        assert issue_number == self.issue_number
        self.get_body_calls += 1
        return self.body

    def post_comment(self, issue_number: int, body: str) -> None:
        assert issue_number == self.issue_number
        self.comments.append(body)
        self.events.append(f"comment:{body}")
        cmd = body.lstrip("/")
        title = rc.command_log_title(cmd)
        self.run_counter += 1
        if cmd in self.fail_once_commands:
            self.fail_once_commands.remove(cmd)
            self.body = append_log_entry(
                self.body,
                title,
                f"❌ Failed `{body}`.",
                run_id=str(self.run_counter),
                ts=f"2026-11-23 10:00:{self.run_counter % 60:02d} UTC",
            )
            return
        self.body = mark_step_done(self.body, cmd)
        self.body = append_log_entry(
            self.body,
            title,
            f"✅ `{body}` succeeded.",
            run_id=str(self.run_counter),
            ts=f"2026-11-23 10:00:{self.run_counter % 60:02d} UTC",
        )

    def close_issue(self, issue_number: int) -> None:
        assert issue_number == self.issue_number
        self.closed = True
        self.events.append("close_issue")

    def get_pr_state(self, pr: rc.PullRequestRef) -> tuple[str, str]:
        states = self.pr_states.get((pr.repo, pr.number))
        if states:
            state = states.pop(0) if len(states) > 1 else states[0]
        else:
            state = "MERGED"
        return state, f"https://github.com/{pr.repo}/pull/{pr.number}"

    def get_run_status(self, run_id: str) -> tuple[str, str]:
        return "completed", "success"

    def list_published_release_tags(self) -> list[str]:
        return list(self.published_tags)

    def current_user(self) -> str:
        return "tester"


class ParsingTests(unittest.TestCase):
    def test_active_release_template_builds_valid_release_graph(self) -> None:
        """Guardrail: ensure the active NEW_RELEASE.md template always builds into a valid ReleaseGraph."""
        template = TEMPLATE_PATH.read_text(encoding="utf-8")

        raw_graph = rc.build_release_graph(template)
        self.assertTrue(raw_graph, "Active release template produced an empty graph")

        for raw_ver in ("v0.20.8", "v0.21.0", "v1.0.0"):
            ver = rc.parse_version(raw_ver)
            graph = rc.build_release_graph(template, ver)
            self.assertTrue(graph, f"Active release template produced an empty graph for {raw_ver}")

    def test_parse_version(self) -> None:
        v = rc.parse_version("v0.20.8")
        self.assertEqual((v.major, v.minor, v.patch), (0, 20, 8))
        self.assertTrue(v.is_patch)
        self.assertFalse(v.is_major_or_minor)

        v0 = rc.parse_version("v0.21.0")
        self.assertTrue(v0.is_major_or_minor)
        self.assertFalse(v0.is_patch)

        with self.assertRaises(ValueError):
            rc.parse_version("0.20.8")

    def test_template_contains_all_numeric_step_ids(self) -> None:
        template = TEMPLATE_PATH.read_text()
        full_graph = rc.build_release_graph(template)
        items = rc.parse_checklist(template)
        found_ids = {item.step_id for item in items if item.step_id is not None}
        # Every leaf ID in the graph plus its parent group prefixes must match the template.
        expected_ids: set[str] = set()
        for sid in full_graph:
            parts = rc.step_key(sid)
            for length in range(1, len(parts) + 1):
                expected_ids.add(".".join(str(p) for p in parts[:length]))
        self.assertEqual(found_ids, expected_ids)

    def test_release_graph_effective_steps_and_ids(self) -> None:
        template = TEMPLATE_PATH.read_text()
        patch_graph = rc.build_release_graph(template, rc.parse_version("v0.20.8"))
        minor_graph = rc.build_release_graph(template, rc.parse_version("v0.21.0"))

        # Each kept step retains its exact release-issue checklist ID.
        for sid, st in patch_graph.items():
            self.assertEqual(st.id, sid)
        for sid, st in minor_graph.items():
            self.assertEqual(st.id, sid)

        # Patch graph omits major_minor steps; minor graph omits patch-only step 11.3.
        self.assertNotIn("2", patch_graph)
        self.assertNotIn("4", patch_graph)
        self.assertNotIn("12", patch_graph)
        self.assertNotIn("14.1", patch_graph)
        self.assertIn("11.3", patch_graph)

        self.assertIn("2", minor_graph)
        self.assertIn("4", minor_graph)
        self.assertIn("12", minor_graph)
        self.assertIn("14.1", minor_graph)
        self.assertNotIn("11.3", minor_graph)

        # Group nodes are not present in the effective step map.
        self.assertNotIn("8", patch_graph)
        self.assertNotIn("8.2", patch_graph)

        # Key metadata & branching/joining edges are preserved.
        step_8_1 = patch_graph["8.1"]
        self.assertEqual(step_8_1.marker, "create-draft-release")
        self.assertEqual(step_8_1.required_steps, ("7",))

        step_8_2_1 = patch_graph["8.2.1"]
        self.assertEqual(step_8_2_1.marker, "promote-pull")
        self.assertTrue(step_8_2_1.wait_for_user_confirm)
        self.assertEqual(step_8_2_1.required_steps, ("7",))

        self.assertEqual(patch_graph["9"].required_steps, ("8.1", "8.2.3"))

    def test_effective_required_steps_dag(self) -> None:
        template = TEMPLATE_PATH.read_text()
        patch_graph = rc.build_release_graph(template, rc.parse_version("v0.20.8"))
        minor_graph = rc.build_release_graph(template, rc.parse_version("v0.21.0"))
        self.assertEqual(patch_graph["1"].required_steps, ())
        self.assertEqual(minor_graph["3"].required_steps, ("1",))  # skips optional step 2
        self.assertEqual(patch_graph["5.1"].required_steps, ("3",))  # skips minor-only step 4
        self.assertEqual(minor_graph["5.1"].required_steps, ("4",))
        self.assertEqual(patch_graph["8.1"].required_steps, ("7",))
        self.assertEqual(patch_graph["8.2.1"].required_steps, ("7",))
        self.assertEqual(patch_graph["8.2.2"].required_steps, ("8.2.1",))
        self.assertEqual(patch_graph["9"].required_steps, ("8.1", "8.2.3"))
        self.assertEqual(patch_graph["15"].required_steps, ("13",))
        self.assertEqual(minor_graph["15"].required_steps, ("14.3.2",))

    def test_effective_required_steps_transitive_across_skipped_branches(self) -> None:
        patch = rc.parse_version("v0.20.8")
        minor = rc.parse_version("v0.21.0")

        # Step 3 is a major_minor join of steps 1 and 2; step 4 follows step 3 linearly.
        # On minor, step 4 requires ("3",). On patch, step 3 is skipped so step 4
        # transitively inherits step 3's required_steps ("1", "2").
        tmpl = (
            '- [ ] **[1]** A <!-- step-meta:{"kind":"prereq"} -->\n'
            '- [ ] **[2]** B <!-- step-meta:{"kind":"prereq","required_steps":["1"]} -->\n'
            '- [ ] **[3]** Minor join <!-- step-meta:{"kind":"prereq","applies_to":"major_minor","required_steps":["1","2"]} -->\n'
            '- [ ] **[4]** After join <!-- step-meta:{"kind":"prereq"} -->\n'
        )
        self.assertEqual(rc.build_release_graph(tmpl, minor)["4"].required_steps, ("3",))
        self.assertEqual(rc.build_release_graph(tmpl, patch)["4"].required_steps, ("1", "2"))

        # Multi-hop with deduplication: two skipped steps both pointing to step 1.
        tmpl_dedup = (
            '- [ ] **[1]** Root <!-- step-meta:{"kind":"prereq"} -->\n'
            '- [ ] **[2]** Skip A <!-- step-meta:{"kind":"prereq","applies_to":"major_minor","required_steps":["1"]} -->\n'
            '- [ ] **[3]** Skip B <!-- step-meta:{"kind":"prereq","applies_to":"major_minor","required_steps":["1"]} -->\n'
            '- [ ] **[4]** Skip Join <!-- step-meta:{"kind":"prereq","applies_to":"major_minor","required_steps":["2","3"]} -->\n'
            '- [ ] **[5]** Final <!-- step-meta:{"kind":"prereq"} -->\n'
        )
        self.assertEqual(rc.build_release_graph(tmpl_dedup, minor)["5"].required_steps, ("4",))
        self.assertEqual(rc.build_release_graph(tmpl_dedup, patch)["5"].required_steps, ("1",))

    def test_release_graph_visualize_mermaid_boxes(self) -> None:
        self.assertEqual(rc.ReleaseGraph().visualize(), "(empty release graph)")

        # Linear single-column graph
        linear = rc.build_release_graph(
            '- [ ] **[1]** First <!-- step-meta:{"kind":"prereq"} -->\n'
            '- [ ] **[2]** Second <!-- step-meta:{"kind":"prereq"} -->\n',
            rc.parse_version("v0.20.8"),
        )
        linear_out = linear.visualize()
        self.assertIn("[1] First", linear_out)
        self.assertIn("[2] Second", linear_out)
        self.assertIn("▼", linear_out)

        # Patch effective graph shows parallel branches (8.1 / 8.2.*) and join into 9
        tmpl = TEMPLATE_PATH.read_text()
        patch_out = rc.build_release_graph(tmpl, rc.parse_version("v0.20.8")).visualize()
        self.assertIn("[1] Sync release notes", patch_out)
        self.assertIn("[8.1] Create draft release", patch_out)
        self.assertIn("[8.2.3] Wait for production images", patch_out)
        self.assertIn("[9] Publish release", patch_out)
        self.assertIn("[15] Unpin and close release issue", patch_out)
        self.assertIn("┴", patch_out)  # fork
        self.assertIn("┬", patch_out)  # join
        self.assertNotIn("[4] ", patch_out)
        self.assertNotIn("[14.1] ", patch_out)

        # Minor effective graph includes optional [2], [4], [12], [14.*] and omits [11.3]
        minor_out = rc.build_release_graph(tmpl, rc.parse_version("v0.21.0")).visualize()
        self.assertIn("[2] ", minor_out)
        self.assertIn("[4] ", minor_out)
        self.assertIn("[12] Merge main into website", minor_out)
        self.assertIn("[14.1] Create devel tag", minor_out)
        self.assertNotIn("[11.3] ", minor_out)

    def test_verify_issue_checkmark_correctness(self) -> None:
        graph = rc.build_release_graph(TEMPLATE_PATH.read_text(), rc.parse_version("v0.20.8"))
        # Step 6 checked without 5.1/5.2 -> invalid checkmark.
        bad_body = mark_step_done(make_ready_body(patch=True), "tag-release")
        errors = rc.verify_issue(rc.parse_release_issue(bad_body, graph), graph)
        self.assertTrue(any("step [6] is checked off" in e and "[5.2]" in e for e in errors), errors)

        # Step 6 checked when 5.1 is [x] and 5.2 PR is MERGED (even though 5.2 box is [ ]) -> valid!
        good_body = mark_step_done(bad_body, "prepare-pull-release").replace(
            "<!-- PREPARE_PULL_RELEASE -->", "#19201"
        )
        errors_ok = rc.verify_issue(
            rc.parse_release_issue(good_body, graph),
            graph,
            pr_is_merged=lambda pr: pr.number == 19201,
        )
        self.assertEqual(errors_ok, [])

        # Step 9 checked when 8.1 is unchecked -> invalid checkmark.
        bad_join = mark_step_done(make_ready_body(patch=True), "publish-release")
        errors_join = rc.verify_issue(rc.parse_release_issue(bad_join, graph), graph)
        self.assertTrue(any("step [9] is checked off" in e and "[8.1]" in e for e in errors_join), errors_join)

    def test_build_release_graph_validation_catches_errors(self) -> None:
        template = TEMPLATE_PATH.read_text()
        self.assertEqual(rc.build_release_graph(template)._validate(), [])
        self.assertEqual(rc.build_release_graph(template, rc.parse_version("v0.20.8"))._validate(), [])
        self.assertEqual(rc.build_release_graph(template, rc.parse_version("v0.21.0"))._validate(), [])

        # Gap in numbering (1, 3 without 2)
        with self.assertRaisesRegex(ValueError, "non-contiguous numbering"):
            rc.build_release_graph(
                '- [ ] **[1]** A <!-- step-meta:{"kind":"prereq"} -->\n'
                '- [ ] **[3]** B <!-- step-meta:{"kind":"prereq"} -->\n'
            )

        # Duplicate / leaf-vs-group collision
        with self.assertRaisesRegex(ValueError, "collides"):
            rc.build_release_graph(
                '- [ ] **[1]** A <!-- step-meta:{"kind":"prereq"} -->\n'
                '  - [ ] **[1.1]** B <!-- step-meta:{"kind":"prereq"} -->\n'
            )

        # Forward reference / cycle in required_steps
        with self.assertRaisesRegex(ValueError, "does not precede"):
            rc.build_release_graph(
                '- [ ] **[1]** A <!-- step-meta:{"kind":"prereq","required_steps":["2"]} -->\n'
                '- [ ] **[2]** B <!-- step-meta:{"kind":"prereq"} -->\n'
            )

        # Dependency on optional step
        with self.assertRaisesRegex(ValueError, "optional step"):
            rc.build_release_graph(
                '- [ ] **[1]** A <!-- step-meta:{"kind":"prereq","optional":true} -->\n'
                '- [ ] **[2]** B <!-- step-meta:{"kind":"prereq","required_steps":["1"]} -->\n'
            )

        # Scope mismatch (all depending on major_minor)
        with self.assertRaisesRegex(ValueError, "applies_to"):
            rc.build_release_graph(
                '- [ ] **[1]** A <!-- step-meta:{"kind":"prereq","applies_to":"major_minor"} -->\n'
                '- [ ] **[2]** B <!-- step-meta:{"kind":"prereq","required_steps":["1"]} -->\n'
            )

        # Unannotated leaf checklist item
        with self.assertRaisesRegex(ValueError, "no <!-- step-meta"):
            rc.build_release_graph(
                '- [ ] **[1]** A <!-- step-meta:{"kind":"prereq"} -->\n'
                '- [ ] **[2]** Missing meta\n'
            )

        # ChatOps step missing marker/command
        with self.assertRaisesRegex(ValueError, "ChatOps step"):
            rc.build_release_graph(
                '- [ ] **[1]** Bad chatops <!-- step-meta:{"kind":"chatops"} -->\n'
            )

        # Duplicate step ID
        with self.assertRaisesRegex(ValueError, "Duplicate step ID"):
            rc.build_release_graph(
                '- [ ] **[1]** A <!-- step-meta:{"kind":"prereq"} -->\n'
                '- [ ] **[1]** B <!-- step-meta:{"kind":"prereq"} -->\n'
            )

        # Reverse group-vs-leaf collision (1.1 before 1)
        with self.assertRaisesRegex(ValueError, "collides with existing group"):
            rc.build_release_graph(
                '  - [ ] **[1.1]** B <!-- step-meta:{"kind":"prereq"} -->\n'
                '- [ ] **[1]** A <!-- step-meta:{"kind":"prereq"} -->\n'
            )

        # step-meta missing numeric **[N]** prefix
        with self.assertRaisesRegex(ValueError, "missing numeric"):
            rc.build_release_graph(
                '- [ ] Missing number <!-- step-meta:{"kind":"prereq"} -->\n'
            )

        # Empty graph
        self.assertEqual(rc.ReleaseGraph()._validate(), ["Release graph is empty."])

        # Invalid kind, applies_to, empty description, status markers, local_script, wait_pr, unknown req
        bad_graph = rc.ReleaseGraph(
            {
                "1": rc.Step(id="1", kind="bogus", description="   ", applies_to="invalid"),
                "2": rc.Step(
                    id="2",
                    kind="chatops",
                    description="ChatOps",
                    marker="cmd",
                    command="cmd",
                    success_markers=(),
                ),
                "3": rc.Step(id="3", kind="local_script", description="Script", command=()),
                "4": rc.Step(id="4", kind="wait_pr", description="PR wait", pr_default_repo=""),
                "5": rc.Step(id="5", kind="prereq", description="Bad req", required_steps=("99",)),
            }
        )
        errs = bad_graph._validate()
        self.assertTrue(any("invalid kind" in e for e in errs), errs)
        self.assertTrue(any("invalid applies_to" in e for e in errs), errs)
        self.assertTrue(any("empty description" in e for e in errs), errs)
        self.assertTrue(any("status markers" in e for e in errs), errs)
        self.assertTrue(any("command list" in e for e in errs), errs)
        self.assertTrue(any("pr_default_repo" in e for e in errs), errs)
        self.assertTrue(any("unknown or non-leaf" in e for e in errs), errs)

    def test_check_readiness_patch_vs_minor(self) -> None:
        template = TEMPLATE_PATH.read_text()
        patch_graph = rc.build_release_graph(template, rc.parse_version("v0.20.8"))
        minor_graph = rc.build_release_graph(template, rc.parse_version("v0.21.0"))

        patch_ready = rc.parse_release_issue(make_ready_body(patch=True), patch_graph)
        self.assertEqual(rc.check_readiness(patch_ready, patch_graph), [])

        # Minor release requires step 4 as well.
        patch_as_minor = rc.parse_release_issue(make_ready_body(patch=True), minor_graph)
        missing_minor = rc.check_readiness(patch_as_minor, minor_graph)
        self.assertEqual(len(missing_minor), 1)
        self.assertIn("[4]", missing_minor[0])

        minor_ready = rc.parse_release_issue(make_ready_body(patch=False), minor_graph)
        self.assertEqual(rc.check_readiness(minor_ready, minor_graph), [])

    def test_check_readiness_legacy_template_without_numbers(self) -> None:
        patch_graph = rc.build_release_graph(TEMPLATE_PATH.read_text(), rc.parse_version("v0.20.8"))
        legacy = re.sub(r"\*\*\[\d+(?:\.\d+)*\]\*\* ", "", make_ready_body(patch=True))
        self.assertEqual(rc.check_readiness(rc.parse_release_issue(legacy, patch_graph), patch_graph), [])

    def test_extract_pr_ref_ignores_example_comment(self) -> None:
        body = TEMPLATE_PATH.read_text()
        graph = rc.build_release_graph(body, rc.parse_version("v0.20.8"))
        self.assertIsNone(rc.extract_pr_ref(body, "5.2", "kubernetes-sigs/kueue", graph))

        updated = body.replace("<!-- PREPARE_PULL_RELEASE -->", "#19201")
        self.assertEqual(
            rc.extract_pr_ref(updated, "5.2", "kubernetes-sigs/kueue", graph),
            rc.PullRequestRef("kubernetes-sigs/kueue", 19201),
        )

        updated_k8s = body.replace("<!-- K8S_IO_PULL -->", "kubernetes/k8s.io#8001")
        self.assertEqual(
            rc.extract_pr_ref(updated_k8s, "8.2.2", "kubernetes-sigs/kueue", graph),
            rc.PullRequestRef("kubernetes/k8s.io", 8001),
        )

    def test_is_latest_semver(self) -> None:
        self.assertTrue(rc.is_latest_semver(rc.parse_version("v0.20.8"), ["v0.20.7", "v0.19.9"]))
        self.assertFalse(rc.is_latest_semver(rc.parse_version("v0.19.9"), ["v0.20.8", "v0.19.8"]))


class CoordinatorFlowTests(unittest.TestCase):
    def test_full_patch_release_parallel_step_8_and_krew_skip(self) -> None:
        gh = FakeGitHubClient(
            body=make_ready_body(patch=True),
            published_tags=["v0.21.0", "v0.20.7"],  # v0.20.8 is NOT latest -> skip step 10
        )

        def fake_local(cmd: list[str], env: dict[str, str]) -> int:
            gh.events.append(f"local:{' '.join(cmd)}")
            if "--target" in cmd and "release" in cmd:
                gh.body = gh.body.replace("<!-- PREPARE_PULL_RELEASE -->", "#19201")
                gh.body = mark_step_done(gh.body, "prepare-pull-release")
            elif "promote_pull.sh" in cmd[0]:
                gh.body = gh.body.replace("<!-- K8S_IO_PULL -->", "kubernetes/k8s.io#9001")
                gh.body = mark_step_done(gh.body, "promote-pull")
            elif "--target" in cmd and "main" in cmd:
                gh.body = gh.body.replace("<!-- PREPARE_PULL_MAIN -->", "#19202")
                gh.body = mark_step_done(gh.body, "prepare-pull-main")
            return 0

        coord = rc.ReleaseCoordinator(
            version=rc.parse_version("v0.20.8"),
            gh=gh,
            repo_root=REPO_ROOT,
            poll_interval=0,
            input_fn=lambda _prompt: "",  # press Enter for all prompts
            sleep_fn=lambda _s: None,
            run_local_fn=fake_local,
        )
        coord.run()

        # Verify /create-draft-release is triggered BEFORE promote_pull.sh runs (parallelism).
        draft_idx = gh.events.index("comment:/create-draft-release")
        promote_idx = gh.events.index("local:./hack/releasing/promote_pull.sh v0.20.8")
        wait_prod_idx = gh.events.index("comment:/wait-for-prod-images")
        publish_idx = gh.events.index("comment:/publish-release")
        self.assertLess(draft_idx, promote_idx)
        self.assertLess(promote_idx, wait_prod_idx)
        self.assertLess(wait_prod_idx, publish_idx)

        # Verify non-latest patch skipped krew and major/minor steps.
        self.assertNotIn("/update-krew-index", gh.comments)
        self.assertNotIn("/merge-main-into-website", gh.comments)
        self.assertNotIn("/create-devel-tag", gh.comments)
        self.assertNotIn("/create-milestone", gh.comments)
        self.assertIn("/unpin-release-issue", gh.comments)
        self.assertTrue(gh.closed)

    def test_minor_release_runs_post_publish_steps(self) -> None:
        gh = FakeGitHubClient(
            body=make_ready_body(patch=False),
            published_tags=["v0.20.8"],  # v0.21.0 IS latest -> run step 10
        )

        def fake_local(cmd: list[str], env: dict[str, str]) -> int:
            if "--target" in cmd and "release" in cmd:
                gh.body = gh.body.replace("<!-- PREPARE_PULL_RELEASE -->", "#20001")
            elif "promote_pull.sh" in cmd[0]:
                gh.body = gh.body.replace("<!-- K8S_IO_PULL -->", "kubernetes/k8s.io#9100")
            elif "--target" in cmd and "main" in cmd:
                gh.body = gh.body.replace("<!-- PREPARE_PULL_MAIN -->", "#20002")
            elif "milestone_pull.sh" in cmd[0]:
                gh.body = gh.body.replace("<!-- MILESTONE_PULL -->", "kubernetes/test-infra#35001")
            elif "ci_pull.sh" in cmd[0]:
                gh.body = gh.body.replace("<!-- CI_PULL -->", "kubernetes/test-infra#35002")
            return 0

        coord = rc.ReleaseCoordinator(
            version=rc.parse_version("v0.21.0"),
            gh=gh,
            repo_root=REPO_ROOT,
            poll_interval=0,
            input_fn=lambda _prompt: "",
            sleep_fn=lambda _s: None,
            run_local_fn=fake_local,
        )
        coord.run()

        for expected in (
            "/tag-release",
            "/wait-for-images",
            "/create-draft-release",
            "/wait-for-prod-images",
            "/publish-release",
            "/update-krew-index",
            "/merge-main-into-website",
            "/create-devel-tag",
            "/create-milestone",
            "/unpin-release-issue",
        ):
            self.assertIn(expected, gh.comments)

    def test_failure_awaits_retry_signal_and_recovers(self) -> None:
        gh = FakeGitHubClient(
            body=make_ready_body(patch=True),
            published_tags=["v0.20.7"],
            fail_once_commands={"tag-release"},
        )
        local_attempts = {"release": 0}
        inputs = iter([
            "",   # 5.1 run prepare_pull.sh (will fail)
            "r",  # retry 5.1
            "",   # 5.1 run prepare_pull.sh (succeeds)
            "r",  # retry 6 (/tag-release failed once)
            "",   # 8.2.1 promote_pull.sh
            "",   # 11.1 prepare_pull.sh --target main
            "",   # 11.3 manual cherry-pick
            "",   # 13 manual email
            "s",  # 15 leave issue open
        ])

        def fake_local(cmd: list[str], env: dict[str, str]) -> int:
            if "--target" in cmd and "release" in cmd:
                local_attempts["release"] += 1
                if local_attempts["release"] == 1:
                    return 1
                gh.body = gh.body.replace("<!-- PREPARE_PULL_RELEASE -->", "#19201")
            elif "promote_pull.sh" in cmd[0]:
                gh.body = gh.body.replace("<!-- K8S_IO_PULL -->", "kubernetes/k8s.io#9001")
            elif "--target" in cmd and "main" in cmd:
                gh.body = gh.body.replace("<!-- PREPARE_PULL_MAIN -->", "#19202")
            return 0

        coord = rc.ReleaseCoordinator(
            version=rc.parse_version("v0.20.8"),
            gh=gh,
            repo_root=REPO_ROOT,
            poll_interval=0,
            input_fn=lambda _prompt: next(inputs),
            sleep_fn=lambda _s: None,
            run_local_fn=fake_local,
        )
        coord.run()

        self.assertEqual(local_attempts["release"], 2)
        self.assertEqual(gh.comments.count("/tag-release"), 2)
        self.assertFalse(gh.closed)

    def test_resume_automatically_skips_completed_steps(self) -> None:
        body = make_ready_body(patch=True)
        for marker in ("prepare-pull-release", "tag-release", "wait-for-images", "create-draft-release", "promote-pull"):
            body = mark_step_done(body, marker)
        body = body.replace("<!-- PREPARE_PULL_RELEASE -->", "#19201")
        body = body.replace("<!-- K8S_IO_PULL -->", "kubernetes/k8s.io#9001")
        gh = FakeGitHubClient(body=body, published_tags=["v0.21.0"])
        gh.pr_states[("kubernetes-sigs/kueue", 19201)] = ["MERGED"]
        gh.pr_states[("kubernetes/k8s.io", 9001)] = ["OPEN", "MERGED"]

        def fake_local(cmd: list[str], env: dict[str, str]) -> int:
            if "--target" in cmd and "main" in cmd:
                gh.body = gh.body.replace("<!-- PREPARE_PULL_MAIN -->", "#19202")
            return 0

        coord = rc.ReleaseCoordinator(
            version=rc.parse_version("v0.20.8"),
            gh=gh,
            repo_root=REPO_ROOT,
            poll_interval=0,
            input_fn=lambda _prompt: "",
            sleep_fn=lambda _s: None,
            run_local_fn=fake_local,
        )
        coord.run()

        self.assertNotIn("/tag-release", gh.comments)
        self.assertNotIn("/wait-for-images", gh.comments)
        self.assertNotIn("/create-draft-release", gh.comments)
        self.assertIn("/wait-for-prod-images", gh.comments)
        self.assertIn("/publish-release", gh.comments)

    def test_should_skip_step_checks_applicability_and_latest(self) -> None:
        body = make_ready_body(patch=True)
        gh = FakeGitHubClient(body=body, published_tags=["v0.21.0"])

        patch_v = rc.parse_version("v0.20.8")
        patch_coord = rc.ReleaseCoordinator(
            version=patch_v,
            gh=gh,
            repo_root=REPO_ROOT,
            graph=rc.build_release_graph(body, patch_v),
        )
        patch_coord.release_issue = rc.parse_release_issue(body, patch_coord.graph, number=gh.issue_number)

        # Patch graph omits step 12, includes step 11.3, and skips non-latest step 10.
        self.assertNotIn("12", patch_coord.graph)
        self.assertFalse(patch_coord._should_skip_step("11.3", patch_coord.graph["11.3"]))
        self.assertTrue(patch_coord._should_skip_step("10", patch_coord.graph["10"]))

        minor_v = rc.parse_version("v0.21.0")
        minor_coord = rc.ReleaseCoordinator(
            version=minor_v,
            gh=gh,
            repo_root=REPO_ROOT,
            graph=rc.build_release_graph(body, minor_v),
        )
        minor_coord.release_issue = rc.parse_release_issue(body, minor_coord.graph, number=gh.issue_number)

        # Minor graph includes step 12 and latest step 10, and omits patch-only step 11.3.
        self.assertFalse(minor_coord._should_skip_step("12", minor_coord.graph["12"]))
        self.assertNotIn("11.3", minor_coord.graph)
        self.assertFalse(minor_coord._should_skip_step("10", minor_coord.graph["10"]))

    def test_issue_body_loaded_once_when_all_steps_checked(self) -> None:
        all_checked = TEMPLATE_PATH.read_text().replace("- [ ] ", "- [x] ")
        gh = FakeGitHubClient(body=all_checked, published_tags=["v0.20.8"])
        coord = rc.ReleaseCoordinator(
            version=rc.parse_version("v0.20.8"),
            gh=gh,
            repo_root=REPO_ROOT,
            poll_interval=0,
            input_fn=lambda _prompt: "",
            sleep_fn=lambda _s: None,
        )
        coord.run()
        self.assertEqual(gh.get_body_calls, 1)

    def test_closed_pr_retry_allows_entering_replacement_pr(self) -> None:
        body = make_ready_body(patch=True).replace("<!-- K8S_IO_PULL -->", "kubernetes/k8s.io#8001")
        gh = FakeGitHubClient(body=body, published_tags=["v0.21.0"])
        gh.pr_states[("kubernetes/k8s.io", 8001)] = ["CLOSED"]
        gh.pr_states[("kubernetes/k8s.io", 8002)] = ["OPEN", "MERGED"]

        inputs = iter([
            "r",     # retry after 8001 closed
            "8002",  # enter replacement PR number
        ])
        v = rc.parse_version("v0.20.8")
        coord = rc.ReleaseCoordinator(
            version=v,
            gh=gh,
            repo_root=REPO_ROOT,
            poll_interval=0,
            graph=rc.build_release_graph(body, v),
            input_fn=lambda _prompt: next(inputs),
            sleep_fn=lambda _s: None,
        )
        coord.release_issue = rc.parse_release_issue(body, coord.graph, number=gh.issue_number)
        step_8_2_2 = coord.graph["8.2.2"]
        assert step_8_2_2 is not None
        coord._wait_pr_merge_step("8.2.2", step_8_2_2)
        self.assertEqual(gh.pr_states[("kubernetes/k8s.io", 8002)], ["MERGED"])

    def test_github_client_rest_api(self) -> None:
        recorded: list[tuple[str, str, dict | None]] = []

        class MockGitHubHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def _send_json(self, status: int, data: object) -> None:
                payload = json.dumps(data).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _read_json(self) -> dict | None:
                length = int(self.headers.get("Content-Length", "0"))
                return json.loads(self.rfile.read(length).decode("utf-8")) if length else None

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                recorded.append(("GET", path, None))
                if path == "/repos/kubernetes-sigs/kueue/issues":
                    self._send_json(200, [
                        {"number": 42, "title": "📦 Release v0.20.8", "html_url": "https://example.test/42"},
                        {"number": 99, "title": "Release v0.20.8", "pull_request": {}},
                    ])
                elif path == "/repos/kubernetes-sigs/kueue/issues/42":
                    self._send_json(200, {"body": "mock issue body"})
                elif path == "/repos/kubernetes/k8s.io/pulls/8001":
                    self._send_json(200, {"state": "closed", "merged": True, "html_url": "https://example.test/pr/8001"})
                elif path == "/repos/kubernetes-sigs/kueue/actions/runs/555":
                    self._send_json(200, {"status": "completed", "conclusion": "success"})
                elif path == "/repos/kubernetes-sigs/kueue/releases":
                    self._send_json(200, [
                        {"tag_name": "v0.20.8", "draft": True, "prerelease": False},
                        {"tag_name": "v0.20.7", "draft": False, "prerelease": False},
                    ])
                elif path == "/user":
                    self._send_json(200, {"login": "mock-user"})
                else:
                    self._send_json(404, {"message": "not found"})

            def do_POST(self) -> None:
                body = self._read_json()
                recorded.append(("POST", self.path, body))
                self._send_json(201, {"id": 1})

            def do_PATCH(self) -> None:
                body = self._read_json()
                recorded.append(("PATCH", self.path, body))
                self._send_json(200, {"state": "closed"})

        server = ThreadingHTTPServer(("127.0.0.1", 0), MockGitHubHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            client = rc.GitHubClient(
                repo="kubernetes-sigs/kueue",
                api_url=base_url,
                token="test-token",
            )
            self.assertEqual(
                client.find_release_issue(rc.parse_version("v0.20.8")),
                (42, "📦 Release v0.20.8", "https://example.test/42"),
            )
            self.assertEqual(client.get_issue_body(42), "mock issue body")
            client.post_comment(42, "/tag-release")
            client.close_issue(42)
            self.assertEqual(
                client.get_pr_state(rc.PullRequestRef("kubernetes/k8s.io", 8001)),
                ("MERGED", "https://example.test/pr/8001"),
            )
            self.assertEqual(client.get_run_status("555"), ("completed", "success"))
            self.assertEqual(client.list_published_release_tags(), ["v0.20.7"])
            self.assertEqual(client._request("GET", client._url("/user")), {"login": "mock-user"})
            self.assertIn(("POST", "/repos/kubernetes-sigs/kueue/issues/42/comments", {"body": "/tag-release"}), recorded)
            self.assertIn(("PATCH", "/repos/kubernetes-sigs/kueue/issues/42", {"state": "closed"}), recorded)
        finally:
            server.shutdown()
            server.server_close()

    def test_cli_api_url_flag_parsing(self) -> None:
        args = rc.build_arg_parser().parse_args([
            "v0.20.8",
            "--api-url",
            "http://127.0.0.1:8787",
        ])
        self.assertEqual(args.api_url, "http://127.0.0.1:8787")

    def test_waiting_message_lists_all_pending_operations(self) -> None:
        body = make_ready_body(patch=True)
        gh = FakeGitHubClient(body=body)
        v = rc.parse_version("v0.20.8")
        coord = rc.ReleaseCoordinator(
            version=v,
            gh=gh,
            repo_root=REPO_ROOT,
            graph=rc.build_release_graph(body, v),
        )
        coord.release_issue = rc.parse_release_issue(body, coord.graph, number=gh.issue_number)
        step_8_1 = coord.graph["8.1"]
        step_8_2_3 = coord.graph["8.2.3"]
        assert step_8_1 is not None and step_8_2_3 is not None
        handles = [
            rc.ChatOpsHandle("8.1", step_8_1, "create-draft-release", None, None),
            rc.ChatOpsHandle("8.2.3", step_8_2_3, "wait-for-prod-images", None, None),
        ]

        buf = io.StringIO()
        with redirect_stdout(buf):
            coord._print_pending_wait(handles, before_step_id="9")
        out = buf.getvalue()
        self.assertIn("Waiting on 2 operations before step [9]:", out)
        self.assertIn("Step [8.1]: `/create-draft-release`", out)
        self.assertIn("Step [8.2.3]: `/wait-for-prod-images`", out)
        self.assertLess(
            out.index("Step [8.1]"),
            out.index("Step [8.2.3]"),
        )

    def test_wait_message_updates_when_one_of_multiple_pending_steps_finishes(self) -> None:
        body = make_ready_body(patch=True)
        gh = FakeGitHubClient(body=body)
        poll = {"n": 0}

        def advance(_sec: float) -> None:
            poll["n"] += 1
            if poll["n"] == 1:
                gh.body = mark_step_done(gh.body, "wait-for-prod-images")
            elif poll["n"] == 2:
                gh.body = mark_step_done(gh.body, "create-draft-release")

        v = rc.parse_version("v0.20.8")
        coord = rc.ReleaseCoordinator(
            version=v,
            gh=gh,
            repo_root=REPO_ROOT,
            poll_interval=0,
            graph=rc.build_release_graph(body, v),
            sleep_fn=advance,
        )
        coord.release_issue = rc.parse_release_issue(body, coord.graph, number=gh.issue_number)
        step_8_1 = coord.graph["8.1"]
        step_8_2_3 = coord.graph["8.2.3"]
        assert step_8_1 is not None and step_8_2_3 is not None
        coord._pending_steps["8.1"] = rc.ChatOpsHandle("8.1", step_8_1, "create-draft-release", None, None)
        coord._pending_steps["8.2.3"] = rc.ChatOpsHandle("8.2.3", step_8_2_3, "wait-for-prod-images", None, None)

        buf = io.StringIO()
        with redirect_stdout(buf):
            coord._wait_for_pending_steps(step_ids=("8.1", "8.2.3"), before_step_id="9")
        out = buf.getvalue()
        self.assertIn("Waiting on 2 operations before step [9]:", out)
        self.assertIn("✅ Step [8.2.3] (`/wait-for-prod-images`) completed.", out)
        self.assertIn("⏳ Step [8.1]: Waiting for `/create-draft-release` to complete...", out)
        self.assertLess(
            out.index("✅ Step [8.2.3]"),
            out.index("⏳ Step [8.1]: Waiting for `/create-draft-release` to complete..."),
        )
        self.assertLess(
            out.index("⏳ Step [8.1]: Waiting for `/create-draft-release` to complete..."),
            out.index("✅ Step [8.1]"),
        )

    def test_in_progress_command_is_awaited_without_reposting(self) -> None:
        body = append_log_entry(
            make_ready_body(patch=True),
            "Tag release",
            "🔄 Authorization successful. Running command...",
            run_id="50001",
        )
        gh = FakeGitHubClient(body=body)
        gh.get_run_status = lambda _run_id: ("in_progress", "")  # type: ignore[method-assign]

        poll_count = {"n": 0}

        def finish_on_second_poll(_sec: float) -> None:
            poll_count["n"] += 1
            if poll_count["n"] >= 2:
                gh.body = mark_step_done(gh.body, "tag-release")
                gh.body = append_log_entry(
                    gh.body,
                    "Tag release",
                    "✅ `/tag-release` succeeded.",
                    run_id="50001",
                    ts="2026-11-23 10:01:00 UTC",
                )

        v = rc.parse_version("v0.20.8")
        coord = rc.ReleaseCoordinator(
            version=v,
            gh=gh,
            repo_root=REPO_ROOT,
            poll_interval=0,
            graph=rc.build_release_graph(body, v),
            sleep_fn=finish_on_second_poll,
        )
        coord.release_issue = rc.parse_release_issue(body, coord.graph, number=gh.issue_number)
        step_6 = coord.graph["6"]
        assert step_6 is not None

        coord._execute_step("6", step_6)
        coord._wait_for_pending_steps(before_step_id="7")
        self.assertEqual(gh.comments, [])
        self.assertEqual(poll_count["n"], 2)

    def test_optional_step_prompts_user_before_running(self) -> None:
        body = make_ready_body(patch=True)
        gh = FakeGitHubClient(body=body)
        opt_step = rc.Step(
            kind="chatops",
            description="Optional command",
            command="tag-release",
            optional=True,
        )

        # Declining skips execution.
        coord_no = rc.ReleaseCoordinator(
            version=rc.parse_version("v0.20.8"),
            gh=gh,
            repo_root=REPO_ROOT,
            input_fn=lambda _prompt: "n",
        )
        coord_no.release_issue = rc.parse_release_issue(body, coord_no.graph, number=gh.issue_number)
        coord_no._execute_step("6", opt_step)
        self.assertEqual(gh.comments, [])

        # Confirming runs the step.
        coord_yes = rc.ReleaseCoordinator(
            version=rc.parse_version("v0.20.8"),
            gh=gh,
            repo_root=REPO_ROOT,
            input_fn=lambda _prompt: "y",
        )
        coord_yes.release_issue = rc.parse_release_issue(body, coord_yes.graph, number=gh.issue_number)
        coord_yes._execute_step("6", opt_step)
        self.assertEqual(gh.comments, ["/tag-release"])

    def test_non_optional_step_disallows_skip(self) -> None:
        prompts: list[str] = []
        answers = iter(["s", "r"])
        coord = rc.ReleaseCoordinator(
            version=rc.parse_version("v0.20.8"),
            gh=FakeGitHubClient(body=make_ready_body(patch=True)),
            repo_root=REPO_ROOT,
            input_fn=lambda p: (prompts.append(p), next(answers))[1],
        )
        action = coord._prompt_on_failure("6", "boom", allow_skip=False)
        self.assertEqual(action, "retry")
        self.assertNotIn("[s]kip", prompts[0])
        self.assertEqual(len(prompts), 2)  # 's' was rejected, then 'r' accepted

    def test_check_chatops_handle_auth_run_failure_and_custom_markers(self) -> None:
        v = rc.parse_version("v0.20.8")
        body = make_ready_body(patch=True)
        gh = FakeGitHubClient(body=body)
        coord = rc.ReleaseCoordinator(
            version=v, gh=gh, repo_root=REPO_ROOT, graph=rc.build_release_graph(body, v)
        )

        step_7 = coord.graph["7"]
        handle = rc.ChatOpsHandle(
            step_id="7",
            step=step_7,
            command="wait-for-images",
            before_entry=None,
            before_auth_entry=None,
        )

        # Authorization failure in Release Log
        auth_fail_body = append_log_entry(body, "Authorization", "❌ Not authorized.", run_id="10")
        status, detail = coord._check_chatops_handle(handle, rc.parse_release_issue(auth_fail_body, coord.graph))
        self.assertEqual(status, "failed")
        self.assertIn("Not authorized", detail or "")

        # In-progress log entry whose linked workflow run finished with failure
        in_prog_body = append_log_entry(body, "Wait for images", "🔄 Running...", run_id="20")
        gh.get_run_status = lambda _rid: ("completed", "failure")  # type: ignore[method-assign]
        status, detail = coord._check_chatops_handle(handle, rc.parse_release_issue(in_prog_body, coord.graph))
        self.assertEqual(status, "failed")
        self.assertIn("conclusion='failure'", detail or "")

        # _is_entry_in_progress returns False when linked run already completed
        in_prog_entry = rc.parse_release_issue(in_prog_body, coord.graph).log_entry("Wait for images")
        self.assertFalse(coord._is_entry_in_progress(step_7, in_prog_entry))

        # Custom success marker ("### Images Ready") without checkbox [x]
        ready_body = append_log_entry(body, "Wait for images", "### Images Ready\nAll set.", run_id="21")
        status, detail = coord._check_chatops_handle(handle, rc.parse_release_issue(ready_body, coord.graph))
        self.assertEqual((status, detail), ("done", None))

        # _entry_is_newer checks run_id, timestamp, and body
        e1 = rc.ReleaseLogEntry(title="T", timestamp="t1", run_id="1", action_link=None, body="b1")
        self.assertTrue(coord._entry_is_newer(e1, None))
        self.assertTrue(
            coord._entry_is_newer(
                rc.ReleaseLogEntry(title="T", timestamp="t1", run_id="2", action_link=None, body="b1"), e1
            )
        )
        self.assertTrue(
            coord._entry_is_newer(
                rc.ReleaseLogEntry(title="T", timestamp="t2", run_id="1", action_link=None, body="b1"), e1
            )
        )
        self.assertTrue(
            coord._entry_is_newer(
                rc.ReleaseLogEntry(title="T", timestamp="t1", run_id="1", action_link=None, body="b2"), e1
            )
        )
        self.assertFalse(coord._entry_is_newer(e1, e1))

    def test_local_script_manual_skip_and_abort_paths(self) -> None:
        v = rc.parse_version("v0.20.8")
        body = make_ready_body(patch=True)
        gh = FakeGitHubClient(body=body)
        # Invalid input ('s' on non-optional), then 'm' (manual) continues without executing script
        ran: list[list[str]] = []
        answers = iter(["s", "x", "m"])
        graph = rc.build_release_graph(body, v)
        coord = rc.ReleaseCoordinator(
            version=v,
            gh=gh,
            repo_root=REPO_ROOT,
            graph=graph,
            input_fn=lambda _p: next(answers),
            run_local_fn=lambda cmd, _env: (ran.append(cmd), 0)[1],
        )
        step_5_1 = coord.graph["5.1"]
        coord.release_issue = rc.parse_release_issue(body, coord.graph, number=gh.issue_number)
        coord._run_local_script_step("5.1", step_5_1)
        self.assertEqual(ran, [])

        # 'q' raises AbortRelease
        coord_abort = rc.ReleaseCoordinator(
            version=v,
            gh=gh,
            repo_root=REPO_ROOT,
            graph=graph,
            input_fn=lambda _p: "q",
        )
        coord_abort.release_issue = rc.parse_release_issue(body, coord_abort.graph, number=gh.issue_number)
        with self.assertRaises(rc.AbortRelease):
            coord_abort._run_local_script_step("5.1", step_5_1)

        # Optional local script accepts 's'
        opt_script = rc.Step(
            kind="local_script",
            description="Opt script",
            command=("./hack/releasing/prepare_pull.sh",),
            wait_for_user_confirm=True,
            optional=True,
        )
        coord_skip = rc.ReleaseCoordinator(
            version=v,
            gh=gh,
            repo_root=REPO_ROOT,
            graph=graph,
            input_fn=lambda _p: "s",
            run_local_fn=lambda cmd, _env: (ran.append(cmd), 0)[1],
        )
        coord_skip.release_issue = rc.parse_release_issue(body, coord_skip.graph, number=gh.issue_number)
        coord_skip._run_local_script_step("5.1", opt_script)
        self.assertEqual(ran, [])

    def test_wait_pr_prompt_resync_and_abort_paths(self) -> None:
        v = rc.parse_version("v0.20.8")
        body = make_ready_body(patch=True)
        gh = FakeGitHubClient(body=body)
        graph = rc.build_release_graph(body, v)

        # No PR ref on issue initially -> invalid input -> Enter re-syncs and picks up newly added PR ref
        prompts_seen = {"n": 0}

        def input_resync(_prompt: str) -> str:
            prompts_seen["n"] += 1
            if prompts_seen["n"] == 1:
                return "not-a-pr"
            gh.body = gh.body.replace("<!-- PREPARE_PULL_RELEASE -->", "#19300")
            gh.pr_states[("kubernetes-sigs/kueue", 19300)] = ["MERGED"]
            return ""

        coord = rc.ReleaseCoordinator(
            version=v,
            gh=gh,
            repo_root=REPO_ROOT,
            poll_interval=0,
            graph=graph,
            input_fn=input_resync,
            sleep_fn=lambda _: None,
        )
        step_5_2 = coord.graph["5.2"]
        coord.release_issue = rc.parse_release_issue(body, coord.graph, number=gh.issue_number)
        coord._wait_pr_merge_step("5.2", step_5_2)
        self.assertEqual(prompts_seen["n"], 2)

        # Abort on PR prompt
        gh2 = FakeGitHubClient(body=body)
        coord_abort = rc.ReleaseCoordinator(
            version=v,
            gh=gh2,
            repo_root=REPO_ROOT,
            graph=graph,
            input_fn=lambda _p: "q",
        )
        coord_abort.release_issue = rc.parse_release_issue(body, coord_abort.graph, number=gh2.issue_number)
        with self.assertRaises(rc.AbortRelease):
            coord_abort._wait_pr_merge_step("5.2", step_5_2)

        # Closed PR automatically switches to a replacement PR already linked in the issue body on retry
        body_closed = body.replace("<!-- PREPARE_PULL_RELEASE -->", "#19301")
        gh3 = FakeGitHubClient(body=body_closed)
        gh3.pr_states[("kubernetes-sigs/kueue", 19301)] = ["CLOSED"]
        gh3.pr_states[("kubernetes-sigs/kueue", 19302)] = ["MERGED"]

        def retry_with_updated_issue(_prompt: str) -> str:
            gh3.body = gh3.body.replace("#19301", "#19302")
            return "r"

        coord_swap = rc.ReleaseCoordinator(
            version=v,
            gh=gh3,
            repo_root=REPO_ROOT,
            poll_interval=0,
            graph=graph,
            input_fn=retry_with_updated_issue,
            sleep_fn=lambda _: None,
        )
        coord_swap.release_issue = rc.parse_release_issue(body_closed, coord_swap.graph, number=gh3.issue_number)
        coord_swap._wait_pr_merge_step("5.2", step_5_2)

    def test_manual_optional_and_finalize_abort_paths(self) -> None:
        v = rc.parse_version("v0.20.8")
        body = make_ready_body(patch=True)
        gh = FakeGitHubClient(body=body)
        graph = rc.build_release_graph(body, v)

        # Manual step invalid input then quit
        answers = iter(["invalid", "q"])
        coord = rc.ReleaseCoordinator(
            version=v, gh=gh, repo_root=REPO_ROOT, graph=graph, input_fn=lambda _p: next(answers)
        )
        step_13 = coord.graph["13"]
        with self.assertRaises(rc.AbortRelease):
            coord._pause_manual_step("13", step_13)

        # Optional step confirmation invalid input then quit
        answers2 = iter(["maybe", "q"])
        coord2 = rc.ReleaseCoordinator(
            version=v, gh=gh, repo_root=REPO_ROOT, graph=graph, input_fn=lambda _p: next(answers2)
        )
        with self.assertRaises(rc.AbortRelease):
            coord2._confirm_optional_step("2", rc.Step(kind="prereq", description="Opt", optional=True))

        # Finalize issue quit
        coord3 = rc.ReleaseCoordinator(
            version=v, gh=gh, repo_root=REPO_ROOT, graph=graph, input_fn=lambda _p: "q"
        )
        coord3.release_issue = rc.parse_release_issue(body, coord3.graph, number=gh.issue_number)
        step_15 = coord3.graph["15"]
        with self.assertRaises(rc.AbortRelease):
            coord3._finalize_issue("15", step_15)

    def test_run_exits_when_verification_fails(self) -> None:
        gh = FakeGitHubClient(body=TEMPLATE_PATH.read_text())  # steps 1 and 3 unchecked
        coord = rc.ReleaseCoordinator(
            version=rc.parse_version("v0.20.8"),
            gh=gh,
            repo_root=REPO_ROOT,
            skip_readiness_check=False,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit) as ctx:
                coord.run()
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("Release issue verification failed", buf.getvalue())

    def test_github_client_current_user_fallback(self) -> None:
        client = rc.GitHubClient(repo="kubernetes-sigs/kueue", token="tok", api_url="http://127.0.0.1:1")
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(client, "_request", return_value={"login": "octocat"}):
                self.assertEqual(client.current_user(), "octocat")
            with mock.patch.object(client, "_request", side_effect=RuntimeError("boom")):
                self.assertEqual(client.current_user(), "<your-user>")

    def test_main_exit_codes(self) -> None:
        with mock.patch.object(rc.ReleaseCoordinator, "run", return_value=None):
            self.assertEqual(rc.main(["v0.20.8"]), 0)
        with mock.patch.object(rc.ReleaseCoordinator, "run", side_effect=rc.AbortRelease("stop")):
            self.assertEqual(rc.main(["v0.20.8"]), 130)
        with mock.patch.object(rc.ReleaseCoordinator, "run", side_effect=KeyboardInterrupt()):
            self.assertEqual(rc.main(["v0.20.8"]), 130)
        with mock.patch.object(rc.ReleaseCoordinator, "run", side_effect=RuntimeError("api down")):
            self.assertEqual(rc.main(["v0.20.8"]), 1)
        with mock.patch.object(rc.ReleaseCoordinator, "run", side_effect=ValueError("bad graph")):
            self.assertEqual(rc.main(["v0.20.8"]), 1)

    def test_run_fails_when_issue_body_graph_is_invalid(self) -> None:
        broken_body = (
            '- [x] **[1]** A <!-- step-meta:{"kind":"prereq"} -->\n'
            '- [ ] **[3]** Gap without step 2 <!-- step-meta:{"kind":"prereq"} -->\n'
        )
        gh = FakeGitHubClient(body=broken_body)
        coord = rc.ReleaseCoordinator(
            version=rc.parse_version("v0.20.8"),
            gh=gh,
            repo_root=REPO_ROOT,
        )
        with self.assertRaisesRegex(ValueError, "non-contiguous numbering"):
            coord.run()


if __name__ == "__main__":
    unittest.main()
