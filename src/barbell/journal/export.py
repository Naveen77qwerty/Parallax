"""
    export_writeup(db_path: str) -> str   # renders docs/writeup_generated.md
    export_trade_log_csv(db_path: str) -> str

Turns the append-only journal into the tables and narrative the one-page
write-up needs (AI logic examples, risk-gate trigger counts, final P&L,
account ID) without hand-transcription — run this the morning of Sep 4
rather than writing the write-up from memory.
"""
