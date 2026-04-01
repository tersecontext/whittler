#!/bin/bash
set -e
BEAD_ID=$(jq -r '.id' /bead.json)
NOTES=$(jq -r '.notes' /bead.json)
DESCRIPTION=$(jq -r '.description' /bead.json)

# Create the file mentioned in notes
echo "hello from bead $BEAD_ID" > "/work/$NOTES"
echo "Done. Created /work/$NOTES"
exit 0
