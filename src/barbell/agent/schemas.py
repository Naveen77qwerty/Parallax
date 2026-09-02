"""
Pydantic models that every LLM output is validated against before it can
reach the risk engine. This is the seam referenced everywhere else as
"schema-validated" — an LLM response that doesn't parse into one of these
is a rejected proposal, not a bug to patch around at the call site.
"""
from pydantic import BaseModel


class HeadlineDigest(BaseModel):
    """Output of screen/headline_triage.py (Featherless-hosted open-source
    model). Informational only — has no veto power and never reaches the
    risk engine directly. It is passed as extra context INTO the catalyst
    gate prompt below; Claude still makes the catalyst_risk call itself.
    A failed or missing digest degrades to an empty one, never blocks the
    pipeline — this stage is a cost/quality optimization, not a dependency."""
    symbol: str
    news_volume: str          # "low" | "normal" | "elevated"
    summary: str
    model_used: str = ""      # e.g. "Qwen/Qwen2.5-7B-Instruct" via Featherless


class CatalystVerdict(BaseModel):
    symbol: str
    catalyst_risk: bool          # True = unscheduled binary risk found, veto candidate
    reasoning: str
    sources_considered: list[str] = []


class ProposedLeg(BaseModel):
    symbol: str            # OCC-format option symbol
    side: str               # "buy" | "sell"
    ratio_qty: int = 1


class ProposedStructure(BaseModel):
    underlying: str
    structure_type: str     # "put_credit_spread" | "call_credit_spread" | "iron_condor" | "put_debit_spread"
    legs: list[ProposedLeg]
    contracts: int
    limit_price: float
    rationale: str
    max_loss_usd: float      # model's own estimate; risk engine recomputes and can override
