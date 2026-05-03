#!/usr/bin/env bash
# Commit a fixed list of bot-generated paths and push to origin/main with
# a retry+rebase loop that resolves conflicts in our favour (the run with
# the freshest data wins). Intentionally exits 0 on every "safe" failure
# mode so concurrent workflow runs don't litter the CI tab with reds -
# the next scheduled run will pick up the work.
#
# Usage:
#   scripts/commit_bot_outputs.sh "<commit message>" <path>...
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: $0 <commit-message> <path>..." >&2
  exit 64
fi

msg="$1"
shift
paths=("$@")

# Nothing to commit -> nothing to push.
if [ -z "$(git status --porcelain -- "${paths[@]}")" ]; then
  echo "No changes in tracked bot-output paths."
  exit 0
fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add -- "${paths[@]}"

# Re-check after git add: if all changes were e.g. line-ending or
# permission only and add absorbed them, the commit would be empty.
if git diff --cached --quiet; then
  echo "Staged set is empty after git add; nothing to commit."
  exit 0
fi

git commit -m "$msg"

remote_branch="${GITHUB_REF_NAME:-main}"

for attempt in 1 2 3 4 5; do
  if git push origin "HEAD:${remote_branch}"; then
    echo "Pushed bot outputs on attempt ${attempt}."
    exit 0
  fi

  echo "Push rejected on attempt ${attempt}; rebasing on origin/${remote_branch}..."
  if ! git fetch --quiet origin "${remote_branch}"; then
    echo "Fetch failed; will retry."
    sleep $((attempt * 3))
    continue
  fi

  # In `git rebase`, "theirs" refers to the commits being rebased (our
  # local commit). For bot-generated outputs (predictions, features,
  # ratings, ...) the run with the latest data should always win, so
  # let the rebase auto-resolve any path conflict in our favour.
  if ! git -c rebase.autoStash=true pull --rebase -X theirs origin "${remote_branch}"; then
    echo "Rebase still has unresolved conflicts; aborting."
    git rebase --abort 2>/dev/null || true
    # Soft-exit: the next scheduled run regenerates everything from
    # scratch on top of the latest origin, so a single failed push is
    # not worth a CI red.
    exit 0
  fi

  sleep $((attempt * 3))
done

echo "Push still failing after 5 attempts; the next scheduled run will retry."
exit 0
