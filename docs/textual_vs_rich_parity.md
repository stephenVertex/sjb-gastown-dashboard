## Textual vs Rich Dashboard Parity

Reference Rich dashboard: `tmux_window_ages.py`

Legend:
- `✓` ported in `textual_dashboard.py`
- `⏳` partially ported or reduced parity
- `✗` missing in Textual, with follow-up bead filed

| Rich feature | Textual status | Notes |
| --- | --- | --- |
| Windows view of tmux sessions | ✓ | Textual renders a top `Windows` panel sourced from `DashboardSnapshot`. |
| Group sessions by rig prefix | ✓ | Rich inserts per-rig separators; Textual keeps rig prefix in each row and grouped ordering. |
| Session age display | ⏳ | Textual shows age text in the windows panel, but not the dedicated `AGE` column presentation from Rich. Follow-up: `sgd-6oa`. |
| Age bar visualization (`BAR`) | ✗ | Rich has the proportional bar column; Textual has no equivalent. Follow-up: `sgd-6oa`. |
| Activity indicator (`ACT`) | ✗ | Rich computes low/med/high activity telemetry; Textual does not expose it. Follow-up: `sgd-6oa`. |
| Bead association in windows list | ✓ | Textual shows the bead label for each session/window row. |
| Command display in windows list | ✓ | Textual includes the current command per row. |
| Window title / path display | ✓ | Textual shows title text in the windows panel. |
| Worked Now panel | ✓ | Ported as `Worked Now`. |
| Queue panel | ✓ | Ported as `Queue`. |
| Recently Closed panel | ✓ | Ported as `Recently Closed`. |
| Rotating bead detail panel | ✓ | Ported as `Bead Detail` with auto-rotation. |
| Pending PRs / orphan branch panel | ✓ | Ported as `Pending PRs`. |
| Bead search and pin detail | ✓ | Textual supports `/s` bead search and pinned bead detail view. |
| Dedicated rigs screen | ✓ | Textual adds `RigsScreen`, which is beyond Rich parity rather than below it. |
| Session drilldown detail pane | ✗ | Rich Textual sessions app supports selecting a session row to inspect pane snapshot, history, bead, and summary details; the main Textual dashboard has no equivalent per-session drilldown. Follow-up: `sgd-3je`. |
| Session search over windows | ✗ | Rich Textual sessions app includes `/` or `s` search over sessions, rig, bead, and title. Main Textual dashboard lacks this workflow. Follow-up: `sgd-381`. |
| Footer key hints | ⏳ | Textual has a Footer with bindings, but not Rich's full sessions-table interaction model. Covered alongside `sgd-3je`/`sgd-381`. |
| LLM one-line summaries in session list | ✗ | Rich surfaces an `LLM SUMMARY` column in the sessions table. Textual dashboard does not show per-session summaries in the windows panel. Follow-up: `sgd-8o4`. |
| LLM status footer | ✗ | Rich shows summarizer progress / last-run status. Textual has no visible status. Follow-up: `sgd-8o4`. |
| Manual LLM re-summarize control | ✗ | Rich supports `r` to force summary refresh in the rich dashboard. Textual uses `r` for rigs and has no re-summarize action. Follow-up: `sgd-8o4`. |

## Follow-up Beads

- `sgd-6oa`: Add AGE/BAR/ACT columns to Textual windows panel
- `sgd-3je`: Add per-session detail drilldown to Textual dashboard
- `sgd-8o4`: Add LLM summary status and refresh control to Textual dashboard
- `sgd-381`: Add Textual windows search over sessions and titles

## Audit Summary

The current Textual dashboard already matches or exceeds the Rich dashboard for the secondary project-health panels: worked-now, queue, recently-closed, bead detail rotation, pending PRs, and rigs visibility. The main remaining parity gap is the top-level session monitoring experience: Rich exposes denser per-session telemetry and drilldown/search affordances, while Textual currently renders a simpler static windows list.
