#!/bin/sh
# design-lint Stop hook. Blocks the session ending while a repo is over its
# baseline. Fast, and silent when the tool is absent.
LINT="/Users/tristan/Documents/code/house/design-system/bin/design-lint"
[ -x "$LINT" ] || { echo "design-lint missing; design check skipped" >&2; exit 0; }
exec "$LINT" --hook stop "$CLAUDE_PROJECT_DIR"
