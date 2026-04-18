#!/bin/sh
# Print a table of rigs with their bead prefix and active status.
# Active = status == "operational" (witness + refinery running).
set -eu

gt rig list --json \
  | jq -r '["prefix","rig","active?","status"],
           (.[] | [.beads_prefix, .name,
                   (if .status == "operational" then "yes" else "no" end),
                   .status]) | @tsv' \
  | column -t -s "$(printf '\t')"
