# Orchestration Event Contract v1

The event journal is the sole V1 runtime state authority. It is append-only
JSONL, created mode `0600`, and guarded by an independent cross-process flock.
It is never replaced as a whole and never backed by a second task database.

Every event has exactly:

- `version: 1`, stable `event_id`, UTC `timestamp`, and `event_type`;
- explicit `run_id` and `attempt_id`;
- positive controller `generation` and non-empty `fencing_token`;
- `controller_pid` and `controller_start_fingerprint`;
- optional `task_id`; and
- privacy-checked `payload` mapping.

`controller_acquired` advances generation by exactly one. Every other mutation
must match the journal's current generation and fencing token. Duplicate event
IDs with identical content deduplicate; conflicting content fails closed.

Dispatch is ordered `dispatch_intent` then `dispatch_claimed` then one terminal
task event. Writer resource creation is ordered `resource_intent` then
`resource_created` or `resource_failed`. A crash after intent preserves a
disputable resource and never grants cleanup authority. Completed tasks are
not replayed on resume; an unreconciled claimed wrapper becomes
`task_failed_unsafe`.

Before launch, the adapter creates one deterministic private attempt directory
with a mode-`0600` write-ahead manifest and separate mode-`0600` stdout/stderr
captures. The manifest binds run/task/attempt/generation, deadline, checkpoint
event, and the compiler-projected seat; it never contains the prompt, response,
environment, or full command. After `Popen(start_new_session=True)`,
`dispatch_claimed` records the real wrapper PID, start fingerprint, process
group, deadline, manifest pointer, checkpoint event, and compiled seat. A crash
from manifest creation through journal claim is reconciled from those private
artifacts and never launches the stable attempt again. An unverifiable launch
window is preserved and terminalized `failed-unsafe`.

Resume never recreates a confirmed resource. A new controller must reconcile
its full durable identity through the adapter and append `resource_reconciled`;
missing or drifting evidence fails the task closed.

Controller-owned resource, candidate and integration manifests are private
mode-`0600`, atomically replaced, and self-hash their canonical payload. A new
generation may adopt a completed writer candidate only when run/task/path/
branch/base/ledger slug, candidate parent/HEAD/diff, frozen acceptance hashes,
and live Git facts all match. Adoption writes the new fencing token; any drift
is `failed-unsafe` and preserves the resource. A running writer whose provider
attempt cannot be uniquely reconciled is never replayed or guessed.

For a non-review dependent task, `dependency_context_prepared` follows its
matching `dispatch_intent` and records only the private bundle pointer, digest,
count, and coarse status. Bundle contents are not copied into JSONL. Review
tasks continue to use the stronger post-integration
`review_context_prepared` contract.

After all task nodes succeed, an optional adapter `finalize_run` hook performs
the deterministic join without creating a scheduler-to-join import. Its
privacy-checked result is recorded as `integration_succeeded` or
`integration_failed` before the terminal run event. A failed or invalid hook
can never produce `run_completed`.

Cancellation is `cancel_requested` plus `run_canceling`: admission stops,
pending nodes become canceled, live wrappers drain to receipt/deadline, and ETA
is the conservative maximum remaining child deadline. The orchestrator does
not externally kill a live wrapper.

The bridge, not the scheduler, owns the mandatory hard deadline. At expiry it
signals the isolated process group with TERM, waits the fixed grace period,
uses KILL if necessary, reaps an owned wrapper, and verifies no group member
remains. Unexpected descendants remain `failed-unsafe` even when cleanup later
succeeds. Process, worktree, and branch cleanup outcomes are independent.
After deterministic join and every governed review succeed, terminal cleanup
may remove only a clean generated worktree whose private manifest, live Git
identity, allowed root and current fencing token all match. Branches are
preserved by default and never force-deleted. Failed, dirty, unknown or
unreconciled resources remain in place; cleanup failure is recorded separately
and does not rewrite the primary run result.

An independent CLI requests cancellation only by atomically replacing a mode
`0600` request file. The request binds `run_id`, controller `generation`, and
`fencing_token`; the live controller validates and converts it to the journal
event. It cannot write terminal state. Malformed, foreign, or stale-generation
requests are ignored, so an old request never cancels a resumed generation.

Allowed payload evidence includes IDs, hashes, timestamps, failure classes,
coarse status, ownership and artifact pointers. Prompt/response/transcript
bodies, tokens, cookies, credentials, account/email identifiers, full commands,
environment, base URLs and raw provider configuration are rejected.

Small replaceable controller manifests use same-directory temporary file,
flush/fsync, rename, and directory fsync through
`journal.write_replaceable_manifest`; this operation is never used for JSONL.
