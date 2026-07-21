#!/usr/bin/env bash

# Copyright The Kubernetes Authors.
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

init_message() {
  if [ -z "${GITHUB_REPOSITORY:-}" ]; then
    echo "Error: GITHUB_REPOSITORY environment variable is not set." >&2
    exit 1
  fi
  if [ -z "${COMMAND:-}" ]; then
    echo "Error: COMMAND environment variable is not set." >&2
    exit 1
  fi
  if [ -z "${ACTOR:-}" ]; then
    echo "Error: ACTOR environment variable is not set." >&2
    exit 1
  fi
  if [ -z "${ACTION_LINK:-}" ]; then
    echo "Error: ACTION_LINK environment variable is not set." >&2
    exit 1
  fi
  if [ -z "${ISSUE_NUMBER:-}" ]; then
    echo "Error: ISSUE_NUMBER environment variable is not set." >&2
    exit 1
  fi

  local body="${COMMAND} action triggered by ${ACTOR}
You can track action execution at: ${ACTION_LINK}"

  gh issue comment "$ISSUE_NUMBER" --body "$body"
}

finalize_message() {
  local result_input="$1"

  if [ -z "${GITHUB_REPOSITORY:-}" ]; then
    echo "Error: GITHUB_REPOSITORY environment variable is not set." >&2
    exit 1
  fi
  if [ -z "${COMMAND:-}" ]; then
    echo "Error: COMMAND environment variable is not set." >&2
    exit 1
  fi
  if [ -z "${ACTOR:-}" ]; then
    echo "Error: ACTOR environment variable is not set." >&2
    exit 1
  fi
  if [ -z "${ACTION_LINK:-}" ]; then
    echo "Error: ACTION_LINK environment variable is not set." >&2
    exit 1
  fi
  if [ -z "${COMMENT_LINK:-}" ]; then
    echo "Error: COMMENT_LINK environment variable is not set." >&2
    exit 1
  fi

  local comment_id
  comment_id="${COMMENT_LINK##*issuecomment-}"

  if [ -z "$comment_id" ] || [ "$comment_id" = "$COMMENT_LINK" ]; then
    echo "Error: Invalid comment link format. Expected link containing '#issuecomment-ID'." >&2
    exit 1
  fi

  local result_body=""
  if [ -f "$result_input" ]; then
    result_body=$(cat "$result_input")
  else
    result_body="$result_input"
  fi

  local body="${COMMAND} action (triggered by ${ACTOR}) has finished

${result_body}

You can check execution details at: ${ACTION_LINK}"

  gh api -X PATCH "repos/${GITHUB_REPOSITORY}/issues/comments/${comment_id}" \
    -f body="$body" > /dev/null
}
