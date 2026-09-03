#!/bin/sh
# design-lint Stop hook. Blocks the session ending while a repo is over its
# baseline. Fast, and silent when the tool is absent. The lint is found
# relative to this repo (house/<repo>/.claude/hooks -> house/design-system),
# so the tracked file carries no user path.
LINT="$(cd "$(dirname "$0")/../../.." && pwd)/design-system/bin/design-lint"
[ -x "$LINT" ] || { echo "design-lint missing; design check skipped" >&2; exit 0; }
exec "$LINT" --hook stop "$CLAUDE_PROJECT_DIR"
