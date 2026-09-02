"""
NOT a Python client — this module documents how the MCP path is wired, since
there is no MCP server call from inside the autonomous loop.

alpaca-mcp-server (alpacahq/alpaca-mcp-server) runs standalone via
`uvx alpaca-mcp-server` over stdio, and is registered directly with the
Claude client (Claude Code / Claude Desktop) that drives the interactive
demo session — see the repo README for the mcpServers config block.

Why it's here and not deleted: the demo video and any live-narrated trading
decision during judging goes through Claude driving Alpaca's MCP tools
directly (get_option_snapshot, place_option_order, etc.), NOT through this
codebase. The scheduled/unattended cycles (barbell run-cycle) go through
AlpacaClient + alpaca-py instead, because a long-running loop needs a stable
SDK session, not a per-invocation MCP stdio process.

Both paths write to the same paper account and the same journal DB, so
`barbell status` reflects trades made either way.
"""
