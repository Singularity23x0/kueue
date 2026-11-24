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

"""Release checklist graph parsing, validation, and effective-step resolution."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import re
import sys
from typing import Optional, Union
import unicodedata

_RELEASING_DIR = Path(__file__).resolve().parents[1]
if str(_RELEASING_DIR) not in sys.path:
    sys.path.insert(0, str(_RELEASING_DIR))

from mark_step_done import LOG_MARKER_START

REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_PATH = REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "NEW_RELEASE.md"

VERSION_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
CHECKLIST_LINE_RE = re.compile(r"^[ \t]*[-*][ \t]+\[([ xX])\][ \t]+(.*)$")
STEP_ID_RE = re.compile(r"^\*\*\[(\d+(?:\.\d+)*)\]\*\*[ \t]*")
STEP_MARKER_RE = re.compile(r"<!--\s*step:([a-z0-9-]+)\s*-->")
STEP_META_RE = re.compile(r"<!--\s*step-meta:(\{.*?\})\s*-->")
EXAMPLE_HINT_RE = re.compile(r"<!--\s*(example\s+[^>]+?)\s*-->")

VALID_STEP_KINDS = frozenset({"prereq", "chatops", "local_script", "wait_pr", "manual"})
VALID_APPLIES_TO = frozenset({"all", "patch", "major_minor"})


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int

    @property
    def raw(self) -> str:
        return f"v{self.major}.{self.minor}.{self.patch}"

    @property
    def is_patch(self) -> bool:
        return self.patch > 0

    @property
    def is_major_or_minor(self) -> bool:
        return self.patch == 0

    @property
    def tuple(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)


def parse_version(raw: str) -> Version:
    match = VERSION_RE.match(raw.strip())
    if not match:
        raise ValueError(f"Invalid release version {raw!r}. Expected format: vX.Y.Z (e.g. v0.20.8).")
    return Version(int(match.group(1)), int(match.group(2)), int(match.group(3)))


@dataclass(frozen=True)
class Step:
    """Declarative definition of a single release checklist step."""

    kind: str  # "prereq" | "chatops" | "local_script" | "wait_pr" | "manual"
    description: str
    id: str = ""
    marker: Optional[str] = None
    command: Optional[Union[str, tuple[str, ...]]] = None
    pr_example_hint: Optional[str] = None
    pr_default_repo: Optional[str] = None
    text_hints: tuple[str, ...] = ()
    wait_for_user_confirm: bool = False
    confirm_message: Optional[str] = None
    applies_to: str = "all"  # "all" | "patch" | "major_minor"
    optional: bool = False
    only_if_latest: bool = False
    required_steps: tuple[str, ...] = ()
    close_issue_after: bool = False
    success_markers: tuple[str, ...] = ("✅",)
    failure_markers: tuple[str, ...] = ("❌",)
    in_progress_markers: tuple[str, ...] = ("🔄",)

    def is_applicable(self, version: Version) -> bool:
        if self.applies_to == "patch":
            return version.is_patch
        if self.applies_to == "major_minor":
            return version.is_major_or_minor
        return True


def checklist_section(issue_body: str) -> str:
    """Return only the checklist portion of the issue body (above Changelog / Release log)."""
    cut = len(issue_body)
    for marker in (LOG_MARKER_START, "## Changelog", "## Release log"):
        idx = issue_body.find(marker)
        if idx != -1 and idx < cut:
            cut = idx
    return issue_body[:cut]


def step_key(step_id: str) -> tuple[int, ...]:
    return tuple(int(part) for part in step_id.split("."))


def build_release_graph(
    template_text: str,
    version: Optional[Version] = None,
) -> ReleaseGraph:
    """Parse NEW_RELEASE.md, validate the full template, and return the effective ReleaseGraph.

    When `version` is provided, only steps applicable to that release version are
    kept, preserving their original checklist IDs (e.g. '8.2.1') and resolving
    their `required_steps` transitively across any skipped steps. When `version`
    is None, all leaf steps are kept with transitive resolution across optional
    steps.
    """
    raw_steps: dict[str, Step] = {}
    template_step_ids: set[str] = set()
    group_prefixes: set[tuple[int, ...]] = set()

    for line in checklist_section(template_text).splitlines():
        line_match = CHECKLIST_LINE_RE.match(line)
        if not line_match:
            continue
        text = line_match.group(2)
        id_match = STEP_ID_RE.match(text)
        if id_match:
            template_step_ids.add(id_match.group(1))
        meta_match = STEP_META_RE.search(text)
        if not meta_match:
            continue
        if not id_match:
            raise ValueError(f"Step with step-meta is missing numeric **[N]** prefix: {line!r}")

        step_id = id_match.group(1)
        path = step_key(step_id)

        if step_id in raw_steps:
            raise ValueError(f"Duplicate step ID [{step_id}] in release template.")
        if path in group_prefixes:
            raise ValueError(
                f"Step [{step_id}] collides with existing group [{step_id}] (cannot be both leaf and group)."
            )
        for length in range(1, len(path)):
            parent_id = ".".join(str(p) for p in path[:length])
            if parent_id in raw_steps:
                raise ValueError(
                    f"Step [{step_id}] collides with leaf step [{parent_id}] (cannot be both leaf and group)."
                )
            group_prefixes.add(path[:length])

        meta = json.loads(meta_match.group(1))
        kind = meta["kind"]
        raw_cmd = meta.get("command")
        command: Optional[Union[str, tuple[str, ...]]] = (
            tuple(raw_cmd) if isinstance(raw_cmd, list) else raw_cmd
        )

        marker_match = STEP_MARKER_RE.search(text)
        example_match = EXAMPLE_HINT_RE.search(text)
        cleaned_text = re.sub(r"<!--.*?-->", "", STEP_ID_RE.sub("", text)).strip().rstrip(":")

        raw_steps[step_id] = Step(
            id=step_id,
            kind=kind,
            description=meta.get("description") or cleaned_text,
            marker=meta.get("marker") or (marker_match.group(1) if marker_match else None),
            command=command,
            pr_example_hint=meta.get("pr_example_hint") or (example_match.group(1) if example_match else None),
            pr_default_repo=meta.get("pr_default_repo"),
            text_hints=tuple(meta.get("text_hints", ())),
            wait_for_user_confirm=meta.get("wait_for_user_confirm", kind in ("local_script", "manual")),
            confirm_message=meta.get("confirm_message"),
            applies_to=meta.get("applies_to", "all"),
            optional=bool(meta.get("optional", False)),
            only_if_latest=bool(meta.get("only_if_latest", False)),
            required_steps=tuple(meta.get("required_steps", ())),
            close_issue_after=bool(meta.get("close_issue_after", False)),
            success_markers=tuple(meta.get("success_markers", ("✅",))),
            failure_markers=tuple(meta.get("failure_markers", ("❌",))),
            in_progress_markers=tuple(meta.get("in_progress_markers", ("🔄",))),
        )

    release_graph = ReleaseGraph(
        (sid, raw_steps[sid]) for sid in sorted(raw_steps, key=step_key)
    )
    errors = release_graph._validate() + release_graph._validate_template(template_step_ids)
    if errors:
        joined = "\n  - ".join(errors)
        raise ValueError(f"Invalid release graph:\n  - {joined}")

    if version is None:
        return release_graph

    resolved_reqs = release_graph._resolve_transitive_required_steps(version)

    effective = ReleaseGraph(version=version)
    for sid, step in release_graph.items():
        if not step.is_applicable(version):
            continue
        effective[sid] = replace(step, required_steps=resolved_reqs[sid])
    return effective


class ReleaseGraph(dict[str, Step]):
    """Ordered DAG of effective release steps keyed by their issue checklist ID (e.g. '8.2.1')."""

    def __init__(self, *args, version: Optional[Version] = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.version: Optional[Version] = version

    def _validate(self) -> list[str]:
        """Validate field and DAG invariants on this ReleaseGraph."""
        errors: list[str] = []
        if not self:
            return ["Release graph is empty."]

        for step_id, step in self.items():
            if step.kind not in VALID_STEP_KINDS:
                errors.append(f"Step [{step_id}] has invalid kind {step.kind!r}.")
            if step.applies_to not in VALID_APPLIES_TO:
                errors.append(f"Step [{step_id}] has invalid applies_to {step.applies_to!r}.")
            if not step.description.strip():
                errors.append(f"Step [{step_id}] has an empty description.")

            if step.kind == "chatops":
                if not isinstance(step.command, str) or not step.command.strip():
                    errors.append(f"ChatOps step [{step_id}] must define a non-empty string command.")
                if not step.marker:
                    errors.append(f"ChatOps step [{step_id}] must have a <!-- step:... --> marker.")
                if not step.success_markers or not step.failure_markers or not step.in_progress_markers:
                    errors.append(f"ChatOps step [{step_id}] must define non-empty status markers.")
            elif step.kind == "local_script":
                if not isinstance(step.command, tuple) or not step.command:
                    errors.append(f"Local script step [{step_id}] must define a non-empty command list.")
            elif step.kind == "wait_pr":
                if not step.pr_default_repo or not step.pr_default_repo.strip():
                    errors.append(f"PR wait step [{step_id}] must define pr_default_repo.")

            for req_id in step.required_steps:
                req_step = self.get(req_id)
                if req_step is None:
                    errors.append(f"Step [{step_id}] requires unknown or non-leaf step [{req_id}].")
                    continue
                if step_key(req_id) >= step_key(step_id):
                    errors.append(
                        f"Step [{step_id}] requires [{req_id}], which does not precede it in execution order."
                    )
                if req_step.optional:
                    errors.append(f"Step [{step_id}] cannot require optional step [{req_id}].")
                if self.version is None and req_step.applies_to not in ("all", step.applies_to):
                    errors.append(
                        f"Step [{step_id}] (applies_to={step.applies_to!r}) cannot require "
                        f"[{req_id}] (applies_to={req_step.applies_to!r})."
                    )

        return errors

    def _validate_template(self, template_step_ids: Optional[set[str]] = None) -> list[str]:
        """Validate template-level numbering contiguity and checklist annotation completeness."""
        if not self:
            return []

        errors: list[str] = []
        group_children: dict[tuple[int, ...], set[int]] = {}
        group_ids: set[str] = set()
        for sid in self:
            parts = step_key(sid)
            for length in range(len(parts)):
                prefix = parts[:length]
                group_children.setdefault(prefix, set()).add(parts[length])
                if length > 0:
                    group_ids.add(".".join(str(p) for p in prefix))

        for prefix, children in sorted(group_children.items()):
            keys = sorted(children)
            expected = list(range(1, len(keys) + 1))
            if keys != expected:
                label = ".".join(str(p) for p in prefix) if prefix else "root"
                errors.append(
                    f"Step group [{label}] has non-contiguous numbering: got {keys}, expected {expected}."
                )

        if template_step_ids is not None:
            known_ids = set(self) | group_ids
            missing = sorted(template_step_ids - known_ids, key=step_key)
            for sid in missing:
                errors.append(
                    f"Checklist item [{sid}] has no <!-- step-meta:{{...}} --> and no child steps."
                )

        return errors

    def _resolve_transitive_required_steps(
        self,
        version: Version,
    ) -> dict[str, tuple[str, ...]]:
        """Resolve required_steps transitively across optional or non-applicable steps."""
        resolved: dict[str, tuple[str, ...]] = {}
        prev_id: Optional[str] = None
        for sid, step in self.items():
            direct_parents = step.required_steps or ((prev_id,) if prev_id is not None else ())
            reqs: list[str] = []
            for pid in direct_parents:
                parent = self[pid]
                if parent.optional or not parent.is_applicable(version):
                    reqs.extend(resolved[pid])
                else:
                    reqs.append(pid)
            resolved[sid] = tuple(dict.fromkeys(reqs))
            prev_id = sid
        return resolved

    def visualize(self) -> str:
        """Render the effective ReleaseGraph as a boxed Mermaid-style Unicode flowchart."""
        if not self:
            return "(empty release graph)"

        children: dict[str, list[str]] = {sid: [] for sid in self}
        for sid, step in self.items():
            for req_id in step.required_steps:
                if req_id in children:
                    children[req_id].append(sid)

        def _node_label(sid: str) -> str:
            step = self[sid]
            desc = step.description.replace("{version}", self.version.raw) if self.version else step.description
            suffix = " (optional)" if step.optional else ""
            return f"[{sid}] {desc}{suffix}"

        # Decompose the DAG into sequential Single(sid) and Parallel(branches) blocks.
        blocks: list[tuple[str, object]] = []
        placed: set[str] = set()

        def _trace_branch(start_id: str) -> list[str]:
            chain: list[str] = []
            cur: Optional[str] = start_id
            while cur is not None and cur not in placed:
                if chain and len(self[cur].required_steps) > 1:
                    break
                chain.append(cur)
                placed.add(cur)
                nexts = [c for c in children[cur] if c not in placed]
                if len(nexts) == 1 and len(self[nexts[0]].required_steps) <= 1:
                    cur = nexts[0]
                else:
                    break
            return chain

        for sid in self:
            if sid in placed:
                continue
            # Check if this node is the start of a fork from an already-placed parent.
            reqs = self[sid].required_steps
            if len(reqs) == 1 and reqs[0] in placed:
                siblings = [c for c in children[reqs[0]] if c not in placed and len(self[c].required_steps) == 1]
                if len(siblings) > 1:
                    branches = [_trace_branch(sib) for sib in siblings]
                    branches = [b for b in branches if b]
                    if branches:
                        blocks.append(("parallel", branches))
                        continue
            placed.add(sid)
            blocks.append(("single", sid))

        # Compute canvas width and box widths.
        max_cols = max((len(data) for kind, data in blocks if kind == "parallel"), default=1)
        col_gap = 2
        if max_cols > 1:
            col_box_width = 40
            canvas_width = max_cols * col_box_width + (max_cols - 1) * col_gap
            single_box_width = min(canvas_width, 52)
        else:
            single_box_width = min(max(_display_width(_node_label(sid)) + 4 for sid in self), 60)
            col_box_width = single_box_width
            canvas_width = single_box_width

        lines: list[str] = []
        # Map each rendered step_id -> its box center X coordinate.
        node_center_x: dict[str, int] = {}

        for kind, data in blocks:
            if kind == "single":
                sid = str(data)
                left_pad = (canvas_width - single_box_width) // 2
                center_x = left_pad + single_box_width // 2

                from_xs = [node_center_x[r] for r in self[sid].required_steps if r in node_center_x]
                if from_xs:
                    lines.extend(_render_connector(from_xs, [center_x]))

                box = _make_box(_node_label(sid), single_box_width)
                pad_str = " " * left_pad
                for row in box:
                    lines.append(f"{pad_str}{row}")
                node_center_x[sid] = center_x
            else:
                branches = data  # type: ignore[assignment]
                n_cols = len(branches)
                total_w = n_cols * col_box_width + (n_cols - 1) * col_gap
                base_pad = (canvas_width - total_w) // 2
                col_lefts = [base_pad + i * (col_box_width + col_gap) for i in range(n_cols)]
                col_centers = [left + col_box_width // 2 for left in col_lefts]

                # Draw fork connector from parent(s) of the branch heads to all branch heads.
                head_parents = list(
                    dict.fromkeys(
                        node_center_x[r]
                        for b in branches
                        for r in self[b[0]].required_steps
                        if r in node_center_x
                    )
                )
                if head_parents:
                    lines.extend(_render_connector(head_parents, col_centers))

                # Determine which branch tails continue into a downstream node (so we draw vertical pass-through lines).
                branch_has_downstream = [
                    any(c not in set(b) for c in children[b[-1]]) for b in branches
                ]
                max_len = max(len(b) for b in branches)

                for depth in range(max_len):
                    # Draw in-branch vertical connectors between depth-1 and depth.
                    if depth > 0:
                        conn_row1 = [" "] * (canvas_width + 1)
                        conn_row2 = [" "] * (canvas_width + 1)
                        for col_idx, b in enumerate(branches):
                            cx = col_centers[col_idx]
                            if depth < len(b):
                                conn_row1[cx] = "│"
                                conn_row2[cx] = "▼"
                            elif branch_has_downstream[col_idx]:
                                conn_row1[cx] = "│"
                                conn_row2[cx] = "│"
                        lines.append("".join(conn_row1).rstrip())
                        lines.append("".join(conn_row2).rstrip())

                    # Draw the 3 box rows for all columns at this depth.
                    col_rows: list[list[str]] = []
                    for col_idx, b in enumerate(branches):
                        if depth < len(b):
                            sid = b[depth]
                            col_rows.append(_make_box(_node_label(sid), col_box_width))
                            node_center_x[sid] = col_centers[col_idx]
                        elif branch_has_downstream[col_idx]:
                            mid = col_box_width // 2
                            pass_row = " " * mid + "│" + " " * (col_box_width - mid - 1)
                            col_rows.append([pass_row, pass_row, pass_row])
                        else:
                            empty_row = " " * col_box_width
                            col_rows.append([empty_row, empty_row, empty_row])

                    for r_idx in range(3):
                        row_str = (" " * base_pad) + (" " * col_gap).join(
                            col_rows[c][r_idx] for c in range(n_cols)
                        )
                        lines.append(row_str.rstrip())

        return "\n".join(lines)


_CONNECTOR_GLYPHS: dict[tuple[bool, bool, bool, bool], str] = {
    (True, True, False, False): "│",
    (False, False, True, True): "─",
    (False, True, False, True): "┌",
    (False, True, True, False): "┐",
    (True, False, False, True): "└",
    (True, False, True, False): "┘",
    (False, True, True, True): "┬",
    (True, False, True, True): "┴",
    (True, True, False, True): "├",
    (True, True, True, False): "┤",
    (True, True, True, True): "┼",
}


def _display_width(text: str) -> int:
    """Return terminal display width of a string (accounting for wide Unicode chars/emoji)."""
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _truncate_display(text: str, max_width: int) -> str:
    """Truncate text to fit within max_width terminal display columns."""
    if _display_width(text) <= max_width:
        return text
    out: list[str] = []
    width = 0
    for ch in text:
        ch_w = 0 if unicodedata.combining(ch) else (2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1)
        if width + ch_w > max_width - 1:
            break
        out.append(ch)
        width += ch_w
    return "".join(out).rstrip() + "…"


def _pad_display(text: str, target_width: int) -> str:
    """Right-pad text with spaces to reach target_width terminal display columns."""
    pad = max(0, target_width - _display_width(text))
    return text + (" " * pad)


def _render_connector(from_xs: list[int], to_xs: list[int]) -> list[str]:
    """Render box-drawing connector lines from `from_xs` columns down to `to_xs` columns."""
    if not from_xs or not to_xs:
        return []
    from_set = set(from_xs)
    to_set = set(to_xs)
    min_x = min(from_set | to_set)
    max_x = max(from_set | to_set)

    row1 = [" "] * (max_x + 1)
    for x in range(min_x, max_x + 1):
        up = x in from_set
        down = x in to_set
        left = x > min_x
        right = x < max_x
        row1[x] = _CONNECTOR_GLYPHS.get((up, down, left, right), "─")

    row2 = [" "] * (max_x + 1)
    for x in to_set:
        row2[x] = "▼"

    return ["".join(row1).rstrip(), "".join(row2).rstrip()]


def _make_box(label: str, box_width: int) -> list[str]:
    """Render a 3-line Unicode box of exact display width `box_width`."""
    inner = box_width - 4  # "│ " + text + " │"
    text = _pad_display(_truncate_display(label, inner), inner)
    bar = "─" * (box_width - 2)
    return [
        f"┌{bar}┐",
        f"│ {text} │",
        f"└{bar}┘",
    ]
