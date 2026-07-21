# Orchestration Task Result Contract v1

Adapters return a mapping to
`run_task(task, *, run_id, attempt_id, generation)`. The scheduler binds it to
the current fenced event identity; adapters do not write orchestration events.

Required fields:

- `status`: `succeeded`, `failed`, `timed-out`, or `failed-unsafe`.

Failure results also carry a stable `failure_class`. The only scheduler retry
classes are `provider-transient`, `provider-rate-limit`,
`provider-preflight-transient`, and `adapter-transient`; all other failures are
terminal. `provider-preflight-transient` is limited to a bounded wrapper
failure before a provider run exists; it is not quota/usage monitoring and
does not make observed quota exhaustion retryable. Result evidence may carry
IDs, timestamps, receipt/artifact hashes and privacy-safe artifact pointers. It
must not contain prompts, responses, transcripts, credentials, cookies,
account identifiers, full commands or provider configuration bodies.

Runner identity evidence, when observed, is correlated as one chain:
`run_id`, `task_id`, `attempt_id`, `generation`, observed session ID, wrapper
PID/start fingerprint, worktree, branch, and frozen base SHA. Missing or
drifting required identity fails closed; it is never guessed from pane text.

Cleanup is recorded separately for `process`, `worktree`, and `branch`, each
with `succeeded`, `failed`, `preserved`, or `not-applicable`. A success in one
category cannot conceal failure or unknown ownership in another.

Successful dependency results may be projected into a controller-owned
dependency bundle through an explicit allowlist: task/attempt/status,
provider/model/session attribution, candidate commit/diff/path summary, and a
mode-`0600` artifact pointer plus digest. Arbitrary result fields are ignored;
prompt/response/chat/checkpoint/permission material is never propagated.

For tasks declaring `result_contract: analysis-v1`, success additionally
requires a separate bounded semantic artifact with only `summary`, `findings`,
`decisions`, `open_questions`, and `verification`. Its mode and digest are
rechecked at every handoff. Reviews attest the exact set of consumed semantic
artifact hashes; the raw provider answer is not treated as that attestation.

After all nodes succeed, adapters may expose
`finalize_run(plan, state, *, run_id, generation, fencing_token)`. It returns
the same status envelope plus privacy-safe integration evidence such as an
`integration_head` or acceptance artifact pointer. The scheduler records the
integration result before the terminal run event; failure or exception fails
the run.
