"""
Pipeline Stage 3 — proposes a specific options structure for a name that
passed both the numeric screen and the catalyst gate.

    propose_structure(symbol: str, chain: OptionChain, screen_result: ScreenResult) -> ProposedStructure

This is the ONLY module allowed to construct a candidate trade shape. It
does not touch AlpacaClient and cannot submit an order — its return value
goes straight into risk/engine.py, which is the sole path to execution/.
"""
