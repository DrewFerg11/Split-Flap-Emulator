# Ordered MVP implementation queue

The maintainer merges each PR. Automation implements, verifies, updates issues,
and handles Hermes feedback. It never merges or enables auto-merge.

All implementation branches use `mvp/` for consistent job categorization. The
first branch starts at freshly fetched `main` (`b4ada96` at the start of this
queue). Each later branch starts at its predecessor's tip and opens as a draft
against that predecessor. Record the exact parent base SHA in each stacked PR description
when creating or rebasing it. Only the first unmerged item is ready for review.

| Order | Issue | Branch | Initial PR base | State |
|---|---|---|---|---|
| 1 | [#2](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/2) | `mvp/02-foundation` | `main` | [PR #9](https://github.com/DrewFerg11/Split-Flap-Emulator/pull/9) merged as `9663764`; CI passed and Hermes approved before merge |
| 2 | [#3](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/3) | `mvp/03-hardware-model` | `mvp/02-foundation` | [PR #10](https://github.com/DrewFerg11/Split-Flap-Emulator/pull/10) merged as `3eebd00`; CI passed both layouts and Hermes approved before merge |
| 3 | [#4](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/4) | `mvp/04-runner` | `mvp/03-hardware-model` | [PR #11](https://github.com/DrewFerg11/Split-Flap-Emulator/pull/11); based on main `3eebd00`; local 144 tests pass after review fixes for fault reporting and startup-only retries; previous CI passed both real CLI boots; fresh CI and current-head Hermes approval required |
| 4 | [#5](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/5) | `mvp/05-scenarios` | `mvp/04-runner` | Implemented; scenario integration verification pending |
| 5 | [#6](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/6) | `mvp/06-packaging` | `mvp/05-scenarios` | Queued |
| 6 | [#7](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/7) | `mvp/07-release-gate` | `mvp/06-packaging` | Queued; requires a companion firmware PR and a verified published image |

After a squash merge, preserve the predecessor's old tip, fetch current main,
and rebase only the next branch's own commits using
`git rebase --onto origin/main <old-predecessor-tip> <next-branch>`. Do not replay
the predecessor's squashed commits. Replay later descendants in order using
their recorded old base tips. Push rewritten branches with explicit
`--force-with-lease=<branch>:<previous-remote-sha>` after checking for concurrent
changes. Retarget the next PR to main, run its checks, and mark it ready only
after the update is verified. Leave later descendants as drafts.

For each ready PR, fetch both submitted reviews and review/comment threads.
Evaluate feedback against actual code, fix valid findings, explain any declined
finding with evidence, verify the fix, and commit/push. An approval must apply to
the current head; pushing a fix requires another review. Keep issues open with
working progress/evidence; use PR closing references so merging closes them.
Never mark a phase complete from a local run or an open PR alone. Record newly
discovered work or blockers in the affected issue (or a linked new issue when it
has separate scope), and keep the durable six-phase tracker current.
