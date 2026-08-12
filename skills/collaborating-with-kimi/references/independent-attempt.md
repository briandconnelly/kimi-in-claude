# Independent two-member attempt

Use this pattern to obtain one attempt from the host agent and one from Kimi on the same neutral
problem, then synthesize them. Independence must be observable in the transcript, not asserted:
neither member's attempt may be visible to the other before that other member's attempt is
finalized.

## Order of work

1. Declare the pattern and its cap of one paid Kimi call.
2. Establish the neutral task, shared facts, acceptance criteria, and workspace Kimi may inspect.
3. Start Kimi's attempt with the matching `_async` tool — `kimi_consult_async` for an independent
   design or answer, `kimi_delegate_async` for an independent implementation. The start is the one
   paid call; do not poll for the result yet.
4. Produce and finalize the host agent's attempt in full before any call that can return Kimi's answer.
   Keep the draft outside the resolved workspace and every baseline the selected tool received —
   a running job can still read files there.
5. Only after the host agent's attempt is finalized, poll and fetch Kimi's result per the background-jobs
   reference.
6. Compare assumptions, evidence, tradeoffs, and failures; verify the load-bearing differences and
   synthesize one decision.

If only the sync tool is available, finalize the host agent's attempt before making the call. The
reverse order cannot be repaired by intent: once Kimi's answer is in context, everything drafted
afterward is conditioned on it, and "I did not condition on it" is neither enforceable nor
observable.

Consult can read tracked and untracked files in its resolved workspace. Delegate works from the
seeded worktree baseline. Independence is already lost if either route can see the host agent's
draft, or if Kimi's answer entered context before the host agent's attempt was finalized; reclassify the call as
ordinary critique and follow the one-call collaboration rules.

Do not alter git state solely to hide a draft. Stashing, committing, switching branches, or creating
another clean worktree requires explicit user authorization plus checks that all current state is
preserved and recoverable.

Agreement is only weak support because both attempts may inherit the same task framing. Spend the
synthesis effort on disagreements, differing assumptions, and tests that can distinguish the
approaches.
