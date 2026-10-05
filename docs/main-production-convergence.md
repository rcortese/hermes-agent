# Main / production convergence

The upgraded production line d1e628971c15f8dc0a639c02f8ee8d15918796af is merged with the prior fork main 24edf5ffef14afd86679e62018074e222dc20d94, without reverting the upstream v0.21.5 modular architecture.

Prior fork deltas and dispositions:

- Shared auth home (b5b316eb55): retained by b732714eb2 and auth modules.
- Codex token generations (27122a5bad): retained by agent/credential_pool_codex_generations.py and hermes_cli/auth_pool_generations.py, with the ported generation regression tests.
- Human memory boundary (af40976597): retained by agent/memory_surface_boundary.py and authenticated Moss memory gate, not environment-only admission.
- Ordered search fallback (fc9d0bd48b): retained by tools/web_search_policy.py and the later d1e628971c explicit-fallback policy, preserving upstream memoization and optional metadata.
- Narrow manual approval floor (b81e879305): missing in the production port; restored in tools/approval_manual_floor.py and the public combined guard. It escalates the existing pipe/interpreter, remote-history and sensitive config-write classes to human approval, not an unconditional ban. Prepared autoapproval, mode-off, permanent allowlists and the smart guardian cannot bypass this floor. Ordinary commands remain unchanged.
- Config-owned categories (24edf5ffef): retained with upgraded task fields (images, group, output_schema), category budgets, model/provider receipts and upfront validation. Hidden task overrides in category mode reach rejection rather than being silently stripped.

The bounded Honcho GET session-context tokens-budget exemption already installed in production is retained in agent/moss_memory_gate.py. It is not an exemption for credential tokens, other methods, duplicate values, other endpoints or arbitrary strings.

The historical X-Hermes-Delegate-Mode: inline request mitigation is retired. Neither API request route contains its opt-out; do not apply the old inline overlay. This does not itself implement automatic completion wake or claim a delegation UX fix. The separately coordinated auto-wake change owns its gateway notification/run paths.

No user state, auth stores, config, memory data or runtime homes are carried by this merge. Image construction and runtime activation are separate from source publication.
