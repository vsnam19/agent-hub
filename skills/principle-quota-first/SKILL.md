---
name: principle-quota-first
description: Apply when choosing, reordering or overriding a role's chain, or when a link is on cooldown, so non-Claude quota is spent before Claude quota.
disable-model-invocation: true
---

# Quota first

The first goal of routing is to save Claude quota and tokens. Order chains so links that do not use Claude quota come first, and let fallback do the rest.

**Why:** D-057, D-060. The director already spends Claude quota on judgment; workers should not. The one deliberate exception is the normal tier, whose first link is the cheap Claude model.

**Pattern:**
- Read `agent-hub pool` before routing: it shows cooldowns, so you know which links will be skipped.
- Let a cooled-down link fall through to the next; do not pin a Claude link to dodge a cooldown.
- A role's last resort may be a Claude model for hard work and reviews only.
- Change the mapping in `pool.toml`, never in a skill; the CLI enforces it.

**Boundaries:** Pin an agent with `--agent` only when its output is poor or a specific model is required. If every link is exhausted, report it; waiting or `pool --clear` beats burning the director's quota.
