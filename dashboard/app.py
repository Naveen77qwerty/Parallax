"""
Streamlit dashboard reading directly from data/barbell.db (read-only) —
optional but cheap, and useful for both the demo video and live monitoring
during the window. Not on the order-submission path; deleting this file
changes nothing about how the agent trades.

    streamlit run dashboard/app.py

Shows: NAV vs. starting $100k, Sleeve A vs. Sleeve B P&L, open positions
table, live gate-decision feed (last N risk_decisions rows), kill-switch
status, and countdown to the Sep 4 11:00 ET submission deadline.
"""
