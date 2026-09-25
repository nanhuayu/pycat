"""Durable two-file memory plus periodic self-evolution review.

- :mod:`pycat.core.memory.store`: Hermes-style two-file store (project notes + user
  profile) with sanitized entries, budgets, and atomic writes.
- :mod:`pycat.core.memory.service`: conversation-facing facade (snapshot, tool
  actions, inspector listings).
- :mod:`pycat.core.memory.review`: isolated periodic review that curates memory and
  skills without touching the main conversation transcript.
"""
