#!/bin/bash
# Mock bd CLI for integration testing.
# Uses a temp file to track whether the test bead has been claimed.

CLAIMED_FILE="${MOCK_BD_STATE_FILE:-/tmp/mock_bd_claimed_beads}"

BEAD_JSON='{
  "id": "test-bead-001",
  "title": "Create hello.txt",
  "description": "Create a file called hello.txt with content '"'"'hello from bead test-bead-001'"'"'",
  "design": "Write hello.txt to the working directory",
  "notes": "hello.txt",
  "acceptance_criteria": "hello.txt exists with correct content",
  "status": "open",
  "priority": 2,
  "issue_type": "task",
  "created_at": "2024-01-01T00:00:00Z",
  "updated_at": "2024-01-01T00:00:00Z",
  "labels": [],
  "dependencies": [],
  "dependents": [],
  "dependency_count": 0,
  "dependent_count": 0
}'

CLAIMED_BEAD_JSON='{
  "id": "test-bead-001",
  "title": "Create hello.txt",
  "description": "Create a file called hello.txt with content '"'"'hello from bead test-bead-001'"'"'",
  "design": "Write hello.txt to the working directory",
  "notes": "hello.txt",
  "acceptance_criteria": "hello.txt exists with correct content",
  "status": "claimed",
  "priority": 2,
  "issue_type": "task",
  "created_at": "2024-01-01T00:00:00Z",
  "updated_at": "2024-01-01T00:00:00Z",
  "labels": [],
  "dependencies": [],
  "dependents": [],
  "dependency_count": 0,
  "dependent_count": 0
}'

CLOSED_BEAD_JSON='{
  "id": "test-bead-001",
  "title": "Create hello.txt",
  "description": "Create a file called hello.txt with content '"'"'hello from bead test-bead-001'"'"'",
  "design": "Write hello.txt to the working directory",
  "notes": "hello.txt",
  "acceptance_criteria": "hello.txt exists with correct content",
  "status": "closed",
  "priority": 2,
  "issue_type": "task",
  "created_at": "2024-01-01T00:00:00Z",
  "updated_at": "2024-01-01T00:00:00Z",
  "labels": [],
  "dependencies": [],
  "dependents": [],
  "dependency_count": 0,
  "dependent_count": 0
}'

SUBCOMMAND="$1"

case "$SUBCOMMAND" in
  ready)
    # Check if bead has been claimed
    if [ -f "$CLAIMED_FILE" ] && grep -q "test-bead-001" "$CLAIMED_FILE" 2>/dev/null; then
      echo "[]"
    else
      echo "[$BEAD_JSON]"
    fi
    ;;
  update)
    ISSUE_ID="$2"
    # Check for --claim flag
    if echo "$@" | grep -q -- "--claim"; then
      # Mark bead as claimed
      echo "test-bead-001" >> "$CLAIMED_FILE"
      echo "[$CLAIMED_BEAD_JSON]"
    elif echo "$@" | grep -q -- "--status"; then
      # Unclaim: remove from claimed file
      if [ -f "$CLAIMED_FILE" ]; then
        grep -v "^${ISSUE_ID}$" "$CLAIMED_FILE" > "${CLAIMED_FILE}.tmp" 2>/dev/null || true
        mv "${CLAIMED_FILE}.tmp" "$CLAIMED_FILE" 2>/dev/null || true
      fi
      echo "[$BEAD_JSON]"
    else
      echo "[$BEAD_JSON]"
    fi
    ;;
  close)
    ISSUE_ID="$2"
    # Remove from claimed file on close
    if [ -f "$CLAIMED_FILE" ]; then
      grep -v "^${ISSUE_ID}$" "$CLAIMED_FILE" > "${CLAIMED_FILE}.tmp" 2>/dev/null || true
      mv "${CLAIMED_FILE}.tmp" "$CLAIMED_FILE" 2>/dev/null || true
    fi
    echo "[$CLOSED_BEAD_JSON]"
    ;;
  *)
    # Any other subcommand: exit 0 silently
    exit 0
    ;;
esac
