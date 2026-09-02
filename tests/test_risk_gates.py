"""
Highest-priority test file in the repo. Every gate in risk/gates.py gets:
    - a case that should PASS cleanly
    - a case exactly at the threshold (boundary)
    - a case that should VETO
    - a case that should RESIZE down rather than veto, where applicable

Also: a property-style test asserting risk/engine.py never returns a
`contracts` value greater than what was proposed, across randomized inputs —
this is the test that backs the "can only tighten" claim in the write-up.
"""
