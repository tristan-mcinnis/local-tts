#!/bin/sh
# design-lint PostToolUse hook. One line to stderr, exit 2, so the model sees a
# literal the moment it writes one.
LINT="/Users/tristan/Documents/code/house/design-system/bin/design-lint"
[ -x "$LINT" ] || exit 0
exec "$LINT" --hook post-tool "$CLAUDE_PROJECT_DIR"
