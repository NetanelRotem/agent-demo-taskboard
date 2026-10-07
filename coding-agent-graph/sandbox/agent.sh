#!/usr/bin/env bash
# Runs inside a Docker Cloud Sandbox. The orchestrator uploads this file and
# calls one subcommand per step. GitHub and OpenRouter credentials never enter
# the sandbox: GH_TOKEN and OPENROUTER_API_KEY hold placeholders that the
# sandbox proxy swaps for the real secrets on outbound requests.
set -euo pipefail

AGENT_DIR=/home/agent/.coding-agent
REPO_DIR=/home/agent/workspace/repo

wait_for_github() {
  for _ in $(seq 1 60); do
    if curl -s -o /dev/null --max-time 5 https://api.github.com/zen; then
      return 0
    fi
    sleep 2
  done
  echo "GitHub is not reachable from the sandbox (network policy?)" >&2
  return 1
}

changed_files() {
  local main=$1
  cd "$REPO_DIR"
  {
    git diff --name-only "origin/$main...HEAD" 2>/dev/null || true
    git diff --name-only HEAD
    git ls-files --others --exclude-standard
  } | sed '/^$/d' | sort -u
}

cmd=${1:?subcommand required}
shift

case "$cmd" in
  setup)
    repo=${1:?repo}; branch=${2:?branch}; main=${3:?main branch}
    wait_for_github
    gh auth setup-git
    git config --global user.name "coding-agent"
    git config --global user.email "coding-agent@users.noreply.github.com"
    if [ ! -d "$REPO_DIR/.git" ]; then
      mkdir -p "$(dirname "$REPO_DIR")"
      gh repo clone "$repo" "$REPO_DIR"
    fi
    cd "$REPO_DIR"
    git fetch origin "$main"
    if git show-ref --verify --quiet "refs/heads/$branch"; then
      git checkout "$branch"
    elif git ls-remote --exit-code --heads origin "$branch" >/dev/null; then
      git fetch origin "$branch"
      git checkout -b "$branch" "origin/$branch"
    else
      git checkout -b "$branch" "origin/$main"
    fi
    ;;

  changed-files)
    changed_files "${1:?main branch}"
    ;;

  revision)
    main=${1:?main branch}
    files=$(changed_files "$main")
    cd "$REPO_DIR"
    {
      git rev-parse HEAD
      while IFS= read -r name; do
        [ -n "$name" ] || continue
        printf '%s' "$name"
        if [ -f "$name" ]; then cat "$name"; fi
      done <<< "$files"
    } | sha256sum | cut -d' ' -f1
    ;;

  check)
    subdir=${1:?subdir}; shift
    cd "$REPO_DIR/$subdir"
    if [ ! -d node_modules ] && [ -f package-lock.json ]; then
      npm ci --no-audit --no-fund
    fi
    "$@"
    ;;

  commit-push)
    pathspec=${1:?pathspec}; branch=${2:?branch}; message=${3:?message}
    cd "$REPO_DIR"
    git add -- "$pathspec"
    if ! git diff --cached --quiet; then
      git commit -q -m "$message"
    fi
    git push -q -u origin "$branch" >&2
    git rev-parse HEAD
    ;;

  pi)
    statuses=${1:?statuses}; shift
    # Only one Pi per sandbox: clear leftovers from an interrupted step.
    pkill -x pi || true
    cd "$REPO_DIR"
    export CODING_AGENT_RESULT_STATUSES="$statuses"
    exec pi "$@"
    ;;

  stop-pi)
    # Pi sets its process title to "pi", so match the name, not the arguments.
    pkill -x pi || true
    ;;

  *)
    echo "unknown subcommand: $cmd" >&2
    exit 2
    ;;
esac
