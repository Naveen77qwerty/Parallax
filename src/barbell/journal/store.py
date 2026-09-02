"""
SQLite-backed (via sqlmodel) append-only log. Tables:

    screen_results     symbol, cycle_id, passed, reason, metrics_json, ts
    catalyst_verdicts   symbol, cycle_id, catalyst_risk, reasoning, ts
    proposed_structures cycle_id, symbol, structure_json, ts
    risk_decisions      cycle_id, symbol, outcome, reason, gate_breakdown_json, ts
    orders              order_id, cycle_id, symbol, legs_json, status, fill_price, ts
    positions_snapshot  cycle_id, nav, positions_json, ts
    kill_switch_events  cycle_id, triggered, nav_at_trigger, ts

Nothing is ever UPDATEd or DELETEd — this table set IS the audit trail for
the write-up's risk-gates section and the demo narration. journal/export.py
reads from here; nothing else does.
"""
