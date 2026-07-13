## Intent

Goal: Make the local skill audit see directory-symlinked skills such as `apple-design` and classify the Emil Kowalski bundle with its registered provenance.

User-facing outcome: Claude and Codex runtime availability should agree with the governance manifest, so a broken link or upstream drift cannot hide outside the audit.

In scope: Symlink-safe skill discovery, `emilkowalski-skills` source/update policy classification, and focused regression coverage.

Out of scope: Copying shared skills into runtime directories, updating upstream skill content, changing invocation policy, or touching unrelated dirty work in this branch.

Deliberate tradeoffs: Follow directory links with a realpath cycle guard; if several aliases resolve to the same directory inside one runtime, audit the first deterministic path once rather than duplicate identical content.

Constraints: Preserve the current shared-worktree installation, keep discovery deterministic, and do not weaken pin enforcement.

Verification expected: Focused tests pass; the live dry-run manifest contains `apple-design` once for Claude and once for Codex with source group `emilkowalski-skills`, update policy `review-then-ff-only`, and the pinned source commit.

Task shape: ordinary bug fix plus throwaway UI smoke test

Risk zone: normal governance tooling; no production runtime mutation

Model seats: direction=Codex judgment; landing=Codex implementation; final_review=independent Codex pass

Effort budget: high

Scale gates: focused verification, final diff review
