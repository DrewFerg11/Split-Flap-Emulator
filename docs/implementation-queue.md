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
| 4 | [#5](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/5) | `mvp/05-scenarios` | `mvp/04-runner` | [PR #12](https://github.com/DrewFerg11/Split-Flap-Emulator/pull/12); rebased onto main `e237b07` for review; three homing seeds pass; strict smoke acceptance blocked by firmware #54 and separate I2C issue #14 |
| 5 | [#6](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/6) | `mvp/06-packaging` | `mvp/05-scenarios` | [Draft PR #13](https://github.com/DrewFerg11/Split-Flap-Emulator/pull/13); local image from `022a2b1` passes resource/lifecycle/rejection checks; strict local smokes time out during homing; CI smoke/publication acceptance blocked by firmware #54 and emulator #14 |
| 6 | [#7](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/7) | `mvp/07-release-gate` | `mvp/06-packaging` | Queued; requires a companion firmware PR and a verified published image |

Hermes' current scenario review identified two useful gaps: the final serial
sweep now also covers scenarios without `assert_no_errors`, and asserted
negative-version integration checks have an independent CI matrix entry.
Fresh checks and exact-head review are required after these fixes. Hermes has
withheld approval while the strict smoke gates fail; the maintainer's policy
remains unchanged and acceptance stays blocked on the documented firmware work.

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

The maintainer chose to retain the strict smoke failures on [SFD #54](https://github.com/DrewFerg11/Split-Flap-Display/issues/54) and owns that firmware fix. Phases 4–6 remain blocked for acceptance. Do not relax assertions, change the baseline, or add firmware workarounds to obtain green gates. Packaging publication needs an actual verified digest after merge, so its implementation PR references issue #6 without prematurely closing it.

The intermittent I2C read Error 263 in [emulator #14](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/14) is a separate unresolved failure, not an established symptom of firmware #54. Keep the strict serial-error assertion and preserve failing evidence.
