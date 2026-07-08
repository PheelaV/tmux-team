# Agent Knowledge Base

This directory is for agent memory: design history, decisions, and implementation notes. Human operating docs live in [../docs/index.md](../docs/index.md).

Use this rule when reading KB files:

- current behavior is in `README.md`, `docs/`, and tests;
- KB files explain why the repo moved this way;
- if KB text says "should", "future", or "plan", verify against `src/` before treating it as work to do.

Current high-value agent notes:

- Origin: this project started as a replacement for tmux `send-keys` agent messaging. The durable boundary is SQLite inbox state plus app-server wake turns; tmux remains only the visible control surface.
- [04_hardening_checklist.md](04_hardening_checklist.md): implemented baseline and remaining hardening work.
- [05_installable_extension.md](05_installable_extension.md): package/config/lifecycle status.
- [06_hermes_comparison.md](06_hermes_comparison.md): what to borrow and what not to rebuild.
- [07_principles.md](07_principles.md): product bias and non-goals.
- [08_extensibility_and_hooks.md](08_extensibility_and_hooks.md): current extension authoring contract.
- [09_omnigraph_memory_extension.md](09_omnigraph_memory_extension.md): plan for optional structured/RAG memory without moving coordination into Omnigraph.
- [10_scratchpad_memory.md](10_scratchpad_memory.md): contract for top-loaded role memory that survives context compression and supports oversight.
- [11_durable_messaging_state.md](11_durable_messaging_state.md): message-state semantics for reclaimable claims, relation metadata, completion notices, and notice broadcasts.
- [12_operator_supervision.md](12_operator_supervision.md): supervision surfaces for verbose status, obligations, pane hygiene, pane capture, and watchdog checks.
