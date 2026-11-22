# Shared operation policy (#1456)

`core/operation_policy.py` describes the worker operations and owns profile resource
lifetimes and install/repair preflight. CLI spelling `remake-prefix` is normalized at
this boundary. Dispatch, Qt signals, confirmations, output and the GUI queue stay in
their adapters. `core/launchgate.py` remains the only entry to the two pre-Play guards
(#1451); backend process checks and Proton command construction (#1449) are unchanged.

## Characterized policy table

This table records the starting behavior, retained by this extraction. The contract
is exercised in `tests/test_operation_policy_1456.py`, including real profile locks and
fixture-backed backend calls through both adapters. Existing queue, notice, resume,
launch and capability tests cover the adapter-specific pieces.

- **O**: `OperationLock(profile)`; **P**: `PredownloadLock(profile)`.
  Neither lock is keyed by installation folder. Two profiles naming one installation
  can acquire separate O locks. This is characterized, not changed or declared safe.
- **Backend** in the running column means no central running-game prohibition: the
  backend decides. Qt's foreground worker slot also refuses another foreground job
  while its launch worker is active; Stop queues after cancelling that worker.
  Background probes do not queue behind it. Predownload has its own GUI controller.
- Cancellation describes the GUI offer/cooperative worker token, not a guarantee that
  every backend step can be interrupted. CLI SIGINT retains exit 130. Verify and quality
  cleanup have no GUI Cancel button. Resume for install/update/repair requires the
  backend's `supports_resume`; predownload reuses its backend cache, without GUI Pause.
- **C/G** account notice means CLI/GUI respectively. A notice is needed only for a
  backend requiring acknowledgment. CLI refusal is still exit 2; GUI asks in a dialog.
- Every operation retains its backend-specific prerequisites (installed files, login,
  running-process checks, capability support). The last column lists adapter preflight,
  not a promise that every backend supports the operation.

| Operation | Resources / scope | While game runs | Cancel | Resume | Account C/G | Adapter prerequisites |
|---|---|---|---|---|---|---|
| status | none | allowed | yes | no | no/no | CLI published-version refresh; Flatpak status report |
| install | O / profile; GUI disk promise | Backend | yes | supports_resume | yes/yes | CLI selected-size check; GUI reservation and confirmation |
| update | O / profile | Backend | yes | supports_resume | yes/yes | backend update checks; GUI confirmation |
| repair | O / profile | Backend | yes | supports_resume | yes/yes | best-effort remembered runtime-failure reset; GUI confirmation |
| remake_prefix | O / profile | Backend | yes | no | yes/yes | CLI --yes (otherwise exit 2); GUI rebuild confirmation |
| login | none | Backend | yes | no | no/no | GUI unofficial-launcher notice |
| launch | probe O/P, hold none | Backend | yes | no | yes/yes | launchgate graphics/restart guards; GUI unofficial notice |
| stop | none | allowed | yes | no | no/no | CLI status probe and stop result wording |
| verify | none | Backend | no | no | no/no | backend verify; CLI formats report and not-installed hint |
| set_remember | none | Backend | yes | no | no/no | GUI payload; backend capability; persist_flag=False |
| logout | none | Backend | yes | no | no/no | backend capability |
| news | none | allowed | yes | no | no/no | GUI source; unavailable feed fallback |
| media | none | allowed | yes | no | no/no | GUI source/art fallback |
| check_update | none | allowed | yes | no | no/no | backend check |
| predownload | O + P / profile | allowed | yes | backend cache | no/no | GUI supports_predownload; CLI callable hook |
| remove_qualities | O / profile | Backend | no | no | no/no | GUI confirmed tiers and configuration snapshot |

O and P are acquired in that order and released in reverse order on success, error,
cancel and partial acquisition failure. Launch permits O when P is also held; it
refuses O alone with the existing `OPERATION_IN_PROGRESS` wording. Ordinary operations
refuse a held O with the existing `OPERATION_BUSY` wording. A fresh GUI install promises
`max(0, install_size_hint - tree_bytes(folder))` bytes until it ends. Promises are
accounted by filesystem and profile owner, not an installation-folder mutex. Update
and repair do not reserve disk space.

The remaining lifecycle sets now derive from descriptors:

| GUI lifecycle property | Operations |
|---|---|
| Mutating (controller bookkeeping, not an OS lock) | install, update, repair, remake_prefix, login, launch, set_remember, logout, remove_qualities |
| Capped / pausable | install, update, repair |
| Wait for paused download | verify, repair, update, login, logout, set_remember |
| Background / silent | status, news, media, check_update |
| Reprobe after failure | install, update, launch, verify, remove_qualities |
| State changes after success | install, update, repair, remake_prefix; GUI-only uninstall remains local |
| Announce success | install, update, repair, verify, remake_prefix |
| Retry button | install, update, repair, remake_prefix, verify, login, launch |

## CLI/GUI differences and decisions

No user-visible bug fix is inferred from duplication alone. All observed differences
below are preserved; unresolved differences are explicit compatibility decisions for
this extraction, not assertions that history proves deliberate design.

1. **Account notice on repair/rebuild.** Resolved (#1567, the GUI is the reference): the GUI
   now asks before Repair and Rebuild prefix as well, as the CLI did, because both change an
   unofficial-launcher game's files like Install and Update. Both adapters read the same set.
2. **Install space.** `4daa2bb6` explicitly added the CLI's early space refusal;
   `eb225dc6` explicitly added GUI concurrent reservations (#770), allowing a lone
   install regardless of the estimate. CLI checks the selected quality's size;
   worker reservations use the backend size hint and tolerate an unresolvable folder.
   GUI also asks its download-size/space question before creating a worker. Preserve
   these deliberately different interaction points and existing size/error behavior.
   Both paths now enter the same preflight context; only GUI owns a reservation.
3. **Repair runtime retry.** `db885814` added both best-effort reset paths. GUI emits
   the existing Visual C++ status event when a marker was cleared; CLI stays quiet.
   GUI propagates `BackendError`, CLI swallows it; both swallow the same five ordinary
   exception types. The event is intentional adapter presentation; the exception
   asymmetry has no explained rationale in that change. Preserve and assert it.
4. **Predownload support.** The GUI checks `supports_predownload` (#1457); CLI tests
   whether the method is callable and lets it decide support. History starts with
   `de77b365` (CLI) and `97548488` (GUI). Retain this unresolved declaration mismatch
   rather than change dispatch behavior during lock extraction. GUI unsupported text
   is `this game has no predownload`; CLI's missing-method error includes the profile.
5. **Result presentation and exit codes.** CLI status may refresh a published version;
   worker status only reports. CLI status errors return 1, while Qt emits a Status
   result. CLI verify formats hints and maps a negative report to exit 1; Qt emits
   the report. CLI Stop probes status, prints `stopped`/`not running`, returning 0/1;
   Qt calls Stop and emits completion. Backend error codes and cancellation exit 130
   remain CLI behavior; Qt uses failed/cancelled signals. These are retained adapter
   interfaces, covered by existing tests plus new entrypoint exit-code tests.
6. **GUI interaction and scheduling.** CLI rebuild requires --yes; GUI confirms with a
   dialog. GUI unofficial-launcher notices, download/repair questions, queue cap,
   preemption, paused-entry protection and pause/resume controls remain in Qt. The
   descriptors supply their sets, without imposing a CLI queue or dialogs.
7. **Operation surface.** set_remember, logout, news, media, check_update and
   remove_qualities are worker operations without matching backend CLI commands.
   Remove/uninstall/move are separate flows; their lock consumers are unchanged.

The issue establishes duplication and requests characterization; it explicitly does
not establish a new race. In particular, changing lock identity to installation
folders would need a separate compatibility/race design (#1304/#1455), not an inferred
fix here. The existing two-profile behavior and predownload-vs-Play are tested first.

## Contract and mutation checks

The 65 new cases cover every descriptor group, install/repair success/error/cancel in
both adapters, update/rebuild/predownload lifetimes, real CLI exit codes, prerequisite
refusal, lock reacquisition, partial
O/P acquisition failure, two profiles sharing one folder, predownload-vs-Play, notice
coverage and retained capability/runtime-exception differences. The fixture asserts
preflight occurs before the backend and reservations remain held until it finishes.

After a passing baseline, four temporary source mutations were tested separately
against `tests/test_operation_policy_1456.py`, restoring the source after each run:

| Mutation | Failing tests (exit 1) |
|---|---:|
| Omit install preflight | 8 |
| Omit repair preflight | 9 |
| Omit operation lock release | 28 |
| Omit reservation release | 3 |

Thus a missing preflight or leaked resource cannot silently satisfy the new contract.
