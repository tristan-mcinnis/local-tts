#!/bin/sh
# design-lint PostToolUse hook. One line to stderr, exit 2, so the model sees a
# literal the moment it writes one.
LINT="$(cd "$(dirname "$0")/../../.." && pwd)/design-system/bin/design-lint"
[ -x "$LINT" ] || exit 0
exec "$LINT" --hook post-tool "$CLAUDE_PROJECT_DIR"
