# sjb-gastown-dashboard

Dashboard and monitoring scripts for Gas Town infrastructure.

## Scripts

### tmux_window_ages.py
Live dashboard showing tmux session/window ages, agent status, and resource metrics.
Requires Python 3.14+ and `rich`.

```bash
uv run tmux_window_ages.py
```

### tmux_window_ages_live.sh
Wrapper to run the dashboard in live/polling mode against a specific tmux socket.

```bash
./tmux_window_ages_live.sh [socket-name]
```

### bead_lookup.py
Interactive TUI for looking up beads across databases.
Requires Python 3.14+ and `textual`.

```bash
uv run bead_lookup.py
```
