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
| 3 | [#4](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/4) | `mvp/04-runner` | `mvp/03-hardware-model` | [PR #11](https://github.com/DrewFerg11/Split-Flap-Emulator/pull/11) merged as `e237b07`; all three CI jobs and 144 local tests passed; Hermes approved before merge |
| 4 | [#5](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/5) | `mvp/05-scenarios` | `mvp/04-runner` | Implementation merged in [PR #12](https://github.com/DrewFerg11/Split-Flap-Emulator/pull/12) as `2004377`; Hermes approved `6901917`; all seven CI jobs pass under the accepted known-issues baseline; strict firmware follow-up remains open |
| 5 | [#6](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/6) | `mvp/06-packaging` | `mvp/05-scenarios` | [PR #13](https://github.com/DrewFerg11/Split-Flap-Emulator/pull/13) rebased onto main `2004377` for review; container golden runs explicitly mount the accepted baseline; exact candidate verification/publication pending |
| 6 | [#7](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/7) | `mvp/07-release-gate` | `mvp/06-packaging` | Queued; requires a companion firmware PR and a verified published image |

PR #12 includes the accepted checksum-bound known-issues baseline from [#15](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/15).
Hermes verified its final head and approved it before the maintainer merged.
Known serial diagnostics and expected step failures remain visible in manifests,
reports and JUnit; strict behavior remains the default for new firmware releases.
PR #13 uses the same baseline for golden container checks. No old partial image
proof or unpublished digest establishes current packaging acceptance.

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

The maintainer owns the firmware repair in [SFD #54](https://github.com/DrewFerg11/Split-Flap-Display/issues/54).
The accepted golden baseline records those current-image defects explicitly;
when corrected firmware is verified, retire its entries as described in #15.
The intermittent I2C read Error263 remains separately tracked in
[emulator #14](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/14); its
baseline entry does not establish a cause or fix. [#16](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/16)
tracks mechanical realism after the MVP and does not block this queue.
