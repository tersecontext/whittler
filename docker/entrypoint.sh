#!/bin/bash
set -e

# Read bead config from /bead.json
BEAD_ID=$(jq -r '.id' /bead.json)
DESCRIPTION=$(jq -r '.description' /bead.json)
BODY=$(jq -r '.body // ""' /bead.json)
DESIGN=$(jq -r '.design // ""' /bead.json)
NOTES=$(jq -r '.notes // ""' /bead.json)
ACCEPTANCE=$(jq -r '.acceptance_criteria // ""' /bead.json)

MAX_RETRIES=${WHITTLER_MAX_RETRIES:-3}
VALIDATION_CMD=${WHITTLER_VALIDATION_CMD:-"echo 'no validation configured'"}

# Write CLAUDE.md from design field (agent instructions)
if [ -n "$DESIGN" ]; then
    echo "$DESIGN" > /work/CLAUDE.md
fi

# Build prompt
PROMPT="You are executing bead $BEAD_ID.

## Task
$DESCRIPTION

## Details
$BODY

## Instructions
Read CLAUDE.md for full implementation instructions.

## Acceptance Criteria
$ACCEPTANCE

## Expected Files
$NOTES

After completing the work, run the validation command: $VALIDATION_CMD
If validation fails, read the errors, fix your code, and run validation again.
Do not stop until validation passes or you have tried $MAX_RETRIES times.
When done, exit."

# Run solver
claude -p "$PROMPT" \
    --dangerously-skip-permissions \
    --max-turns 200 \
    --output-format json \
    > /work/.whittler-result.json 2>&1

EXIT_CODE=$?

# Clean up CLAUDE.md (whittler will stage everything else, not this)
rm -f /work/CLAUDE.md

exit $EXIT_CODE
