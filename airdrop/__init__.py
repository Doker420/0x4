"""Airdrop research agent: discovery, eligibility checks and guarded on-chain actions.

Legal-notes
-----------
* The agent only monitors public information and manages wallets that belong to
  the operator. It never touches third-party accounts, never automates account
  creation on exchanges/protocols and never performs sybil farming.
* Every on-chain action is opt-in: contract addresses must be allow-listed by the
  operator, amounts are capped and `dry_run` is ON by default.
"""

__all__ = ["settings", "db", "models", "wallet", "chains", "discovery", "eligibility", "actions", "agent", "notify"]

__version__ = "1.0.0"
