"""
APScheduler-driven long-running process — what `barbell run-cycle` becomes
when left running unattended (systemd / tmux / nohup; see docs/architecture.md
"Runtime" section for why this isn't serverless).

Every `cycle_interval_minutes` (config), while broker/clock.py says the
market is open: pulls endgame phase -> runs screen (if phase allows entries)
-> catalyst gate -> structure agent -> risk engine -> execution -> reconcile
-> journal. A single stuck cycle must not take down the process — each stage
is wrapped so one failure logs and skips to reconcile, never crashes the loop.
"""
