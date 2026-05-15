#!/bin/bash
# PreToolUse firewall — fires on every Bash call,
# including when --dangerously-skip-permissions is active.
# Hooks bypass the bypass. This is intentional.

set -euo pipefail

cmd=$(jq -r '.tool_input.command // ""' < /dev/stdin)

# Hard block: any rm command. User policy.
if echo "$cmd" | grep -qE '(^|[^a-zA-Z_])rm($|[^a-zA-Z_])'; then
  echo "Blocked by firewall: rm is disabled in this project." >&2
  echo "If you need to remove a file, ask the user first, or use git rm for tracked files." >&2
  exit 2
fi

# Hard block: destructive git operations.
git_deny=(
  'git[[:space:]]+push'
  'git[[:space:]]+reset[[:space:]]+--hard'
  'git[[:space:]]+clean[[:space:]]+-[a-zA-Z]*f'
  'git[[:space:]]+branch[[:space:]]+-[Dd]'
  'git[[:space:]]+checkout[[:space:]]+(--force|-f)'
  'git[[:space:]]+commit[[:space:]]+--amend'
)
for pat in "${git_deny[@]}"; do
  if echo "$cmd" | grep -qE "$pat"; then
    echo "Blocked by firewall: destructive git op matched '$pat'." >&2
    echo "Get explicit user approval before this." >&2
    exit 2
  fi
done

# Hard block: pipe-to-shell from network.
if echo "$cmd" | grep -qE '(curl|wget)[^|]*\|[[:space:]]*(bash|sh|zsh)'; then
  echo "Blocked by firewall: piping network content to a shell is dangerous." >&2
  exit 2
fi

# Hard block: sudo.
if echo "$cmd" | grep -qE '(^|[^a-zA-Z_])sudo($|[[:space:]])'; then
  echo "Blocked by firewall: sudo is not allowed in this project." >&2
  exit 2
fi

# Hard block: command injection bypass attempts via interpreter eval.
if echo "$cmd" | grep -qE 'python[0-9.]*[[:space:]]+-c[[:space:]]+[^|]*os\.(system|remove|unlink|rmdir)'; then
  echo "Blocked by firewall: python -c with os filesystem calls. Write a script file instead." >&2
  exit 2
fi

exit 0
