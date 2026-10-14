# Validation record

Host-side checks ran on macOS, and live execution checks ran on real Render Sandboxes on October 12 and 13, 2026 (America/Los_Angeles). This proves a working preview at the pinned versions, not production readiness or partner approval.

| Component | Tested version |
| --- | --- |
| Hermes Agent | `dce1e9b37581dd62e480a9064dc04a709c2940d3`, unmodified source |
| Host Python | 3.14.3 on macOS |
| Sandbox Python in the three-task run | 3.11.2 on Linux |
| Render Python package | 1.2.0 |
| Render CLI, used only for test login | 2.28.0 |
| Model in end-to-end test | `gpt-6.1-sol`, through Hermes's `openai-codex` connection |
| Paid sandbox plan | starter, explicit 60/300/900-second lifetimes depending on the check, deny-all outbound network |

## Verified behavior

- Real Hermes plugin discovery and provider registration in isolated test profiles.
- Command output, nonzero exit status, standard error, working directory, exported shell variables, standard input, Unicode and large output.
- Hermes file create/read/patch and search tools.
- Binary upload/download with byte-for-byte comparison.
- Separate sandbox filesystems for separate environments.
- Timeout and Hermes interruption stop ordinary children, confirmed by checking that delayed file writes never happened. The environment remains usable.
- Hermes `execute_code` uses a remote Python kernel and retains variables between calls.
- A synthetic skill file uploads through Hermes's normal synchronization and a remote edit synchronizes back before cleanup.
- Render account key is absent from the sandbox environment.
- Lost output streams and SDK transport exceptions produce sanitized failures rather than false success. Cleanup failures remain retryable.
- Real agent task: eight model calls and seven actual tool calls repaired a function. The original function failed three of five tests. The downloaded result passed all five original tests in a second fresh sandbox. Both sandboxes were independently confirmed terminated.

Local behavior suite after the batch-transfer fix: **23 passed, 1 skipped**. The six additional checks cover a 328-file batch, destination validation, and rollback/retry through Hermes's real synchronization manager. The skipped local process-group test needs Linux; the macOS runner rejected `killpg`. Equivalent timeout and child-process behavior passed live on Render Linux. This is an explicit platform limit, not a mocked pass.

Reports: [smoke](validation/public/live-smoke.json), [runtime and interruption](validation/public/live-runtime.json), [Python and synchronization](validation/public/live-features.json), [model task and independent verification](validation/public/live-agent.json). Published reports retain checks and cleanup results; account and sandbox IDs remain in git-ignored local reports. The [original tests](validation/agent-artifacts/test_totals.py) and [downloaded solution](validation/agent-artifacts/verified_totals.py) are synthetic and contain no user project data.

Hermes's official validator passed on both the clean runtime payload and the complete authored source copy, including tests and scripts, with no warnings. Its registration probe succeeded and found no Hermes core overrides. This is not an independent security audit or catalog acceptance.

## GitHub installation acceptance

The normal `hermes plugins install ojusave/hermes-plugin-render` path was tested from a fresh profile and a clean Hermes environment that did not contain the Render package. The successful installation fetched commit `99b6a8fb7701f45dec88e7d421e578e47b3e9e42` from GitHub.

The first attempt, at `ef71230453a24c839e545d24a8b59310f8a6ca1c`, exposed a real installation failure: the plugin's development dependency pointed to a sibling Hermes checkout, which conflicted with Hermes's managed dependency workspace. The earlier prepared-environment tests did not exercise that path. Development dependencies now live in a separate `dev/` project, and the installed plugin declares only its Render dependency. The API-key manifest entry also now requests hidden input.

| Check | Observed result |
| --- | --- |
| Decline dependency preparation | Plugin remained disabled; the clean environment still had no Render package |
| Credential entry | A synthetic test value was masked on screen and absent from the captured terminal log |
| Accept enable and dependency prompts | Hermes prepared the dependencies and reported `Plugin render enabled` |
| Restart and list installed plugins | `render` appeared as enabled, with Git source provenance |
| Run through the installed copy | Loader path matched the GitHub-installed directory; Render loaded from Hermes's selected dependency environment |
| Verify actual output | The CSV task passed in its agent sandbox and in a second fresh sandbox, including original, new and empty inputs |
| Cleanup | Both starter sandboxes terminated; a separate API read confirmed this; the temporary real Render key was removed from the test profile |
| Catalog validation command | `hermes plugins validate <installed-plugin> --install-deps --json` passed all 14 reported checks with no warnings |

The post-install task took 79.64 seconds including independent verification and cleanup. The original Hermes source remains unmodified. The local suite passed 17 tests with the same documented platform skip, and both clean package validations passed without warnings after the packaging fix.

[Installation-flow receipt](validation/public/installation-flow.json), [live task result](validation/public/live-installation.json), and [installed-copy verification script](scripts/verify_installed.py) preserve the evidence. Raw terminal logs remain git-ignored under `validation/private/installer/`. The script requires an already-installed test profile under `.test-home/`, its matching `HERMES_RUNTIME_DIR`, and explicit flags to reuse existing CLI logins. It does not copy local plugin code over the installed GitHub tree.

This covers the CLI installation and a real task at the recorded versions. It does not establish catalog approval, installer support on other operating systems, or interactive command-approval behavior.

## Bounded reliability and real failures

On October 13, the final reliability run tested runtime commit `13950d1` on real Render resources in **304.70 seconds**. The [reliability runner](scripts/live_reliability.py) loads the real plugin in separate host processes. These are provider-operation checks, not ten model conversations or a long-duration load test. [Source fingerprints and results](validation/public/live-reliability.json).

| Check | Observed result |
| --- | --- |
| Repeated use | 10 fresh sessions, each with 10 changing-output commands and an expected nonzero exit; shell state, skill upload/edit/sync-back, file download and repeated cleanup passed. |
| Simultaneous profiles | Three separate processes and Hermes profiles performed 12 changing-output commands each, with overlapping work intervals. Identical paths retained the correct profile-specific contents. All used one Render workspace and credential. |
| Host crash | The host worker was killed with SIGKILL after remote execution began. A 10-second remote command limit prevented an ordinary child's delayed write. With a requested 60-second sandbox lifetime, Render recorded termination 75 seconds after creation. The actual status was `errored` with `terminated_at`, and the resource was absent from the complete active listing. |
| Remote failure | Terminating the sandbox during execution produced a connection failure. The broken environment rejected another command; a newly created environment worked. |
| Cleanup failure | An invalid test credential caused the real API to reject cleanup. The environment remained retryable, and cleanup succeeded after restoring the authorized connection. The user's credential was never changed. |

All 16 reliability resources were confirmed stopped using an independent connection. Ten ordinary sessions and three simultaneous sessions requested 300-second lifetimes; the crash case requested 60 seconds; the two failure cases used 900 seconds.

A separate [connection-drop runner](scripts/live_transport.py) closed a real encrypted network stream after an observer confirmed that the command had started on Render. The plugin reported that completion was unknown, refused another command in that environment even after connectivity returned, and did not automatically replay the command: the independently read dispatch count was one. Its sandbox was independently confirmed terminated. The local test forwarder did not decrypt traffic, disable certificate validation or change system-wide networking. [Connection-drop receipt](validation/public/live-transport.json).

The same bounded suite and connection check also passed before the batch-transfer fix. Their [earlier reliability](validation/public/live-reliability-before-batch.json) and [transport](validation/public/live-transport-before-batch.json) receipts retain the old source hashes. The final onboarding, reliability and connection runs cover 20 resources on the fixed runtime. A [separate cleanup audit](validation/public/live-cleanup-audit.json) includes all 44 resources created during this evidence-gathering work, including failed attempts and both versions.

These checks establish failure reporting, bounded crash containment and small-scale profile separation. They do not establish automatic task recovery, isolation between different customer accounts through Hermes's gateway, hours or days of continuous load, or an independent security review.

## Ordinary CLI onboarding and the full skill library

The [ordinary CLI runner](scripts/live_onboarding.py) installed the published plugin at `13950d1200f444340ec9e2734920e01bd48d9de0` into a fresh profile, using a base Hermes environment with no Render package. It accepted the normal dependency prompts, merged `config.example.yaml`, and ran `hermes chat --oneshot --format stream-json` followed by `--resume`. It did not instantiate the agent directly or add an output-export callback. All installed runtime file hashes match the working source.

The first task computed 66 from `[12, 24, 30]`, wrote and read `/workspace/report.json`, reported Linux, and confirmed `RENDER_API_KEY` was absent. A real terminal command counted **328 skill files** in the sandbox, matching the newly initialized host profile. The resumed conversation confirmed `FILE_ABSENT` without recreating the previous turn's report. This validates the documented disposable-turn behavior, not persistence.

The two chat commands took 66.58 and 27.13 seconds. Hermes itself terminated all three resources before the test's fallback cleanup ran: one operating-system probe used to build its prompt, plus one environment for each turn. A separate API connection confirmed cleanup. Temporary Render and model credentials were removed. [Successful receipt](validation/public/live-onboarding.json).

The full-profile check found a real startup failure in the prior published revision, `19d881a`: individual skill uploads failed synchronization, and the task timed out after creating four sandboxes. The parent test cleaned up all four. The old path performed a directory command and an upload for each file. The plugin now uses Hermes's existing batch-upload callback to transfer the selected files in one archive and extract them once. No Hermes or Render package code was changed. The original sanitized exception did not preserve the underlying HTTP status, so rate limiting is a suspected cause, not a proven diagnosis. [Failed pre-fix receipt](validation/public/live-onboarding-before-batch.json).

Two test assumptions were also corrected. Hermes initializes its bundled skills when chat starts, so counting before chat incorrectly returned zero. Hermes's short-lived operating-system probe also meant the original two-sandbox expectation was wrong. Both turns and normal cleanup had already passed in that attempt; its [failed count assertion](validation/public/live-onboarding-count-assumption.json) is retained. The final run above used the corrected checks.

Hermes blocked an inline Python command under its default unattended-command approval rules. Those rules stayed enabled. The agent then used its normal file-writing tool and ran the resulting script, which Hermes permitted. This observes the existing guard behavior; it is not an interactive approval-dialog test. This onboarding was automated on macOS, not performed by an independent new user.

## Three additional model-driven tasks

The [multi-task runner](scripts/live_scenarios.py) and [scenario inputs and protected checks](scripts/scenario_fixtures.py) exercised three different workflows through real Hermes agents. All passed on their first recorded attempt. The provider runtime was unchanged from commit `cc7d32e8f7483423a20a8d237181b21d73a446be`.

| Task | Independently verified result | Tool calls | Time, including verification and cleanup |
| --- | --- | --- | --- |
| CSV sales analysis | Five paid orders, 5,755 cents total; duplicates, quoted fields, Unicode, exact cents, new input and empty input checked | 3 | 84.39 seconds |
| Multi-file inventory repair | Five original tests plus three additional checks passed; both source files repaired; original tests and other inputs unchanged | 12 | 122.93 seconds |
| SQLite import and reporting | Five unique events, 1,450 cents net; downloaded database and report correct; repeated import added no duplicates; a new event raised the total to 1,475 cents | 5 | 92.99 seconds |

Each task used one agent sandbox and a separate fresh sandbox for verification. Checkers were uploaded only to the verification sandbox, after the agent finished, alongside the original inputs and downloaded output files. The CSV and database programs were rerun on additional inputs to check that they generalized beyond the supplied example. All three agent environments reported Linux and lacked `RENDER_API_KEY`.

The run made 16 model calls and 20 tool calls across the three tasks. All six resources used `starter`, Oregon, a 900-second lifetime and `deny-all` networking. Cleanup succeeded for every resource, and a separate read-only API check confirmed all six were terminated. Source hashes still matched the files used by the run.

The [sanitized report](validation/public/live-scenarios.json) records prompts, tool names, output hashes, independent checks and resource cleanup. Raw reports and downloaded programs, reports and the SQLite file remain under git-ignored `validation/private/scenarios/`. These are synthetic, bounded tasks at the pinned versions. Network package installation, production workloads and multiple simultaneous users were not covered by this run.

## Issues found and resolved during testing

- The minimal provider documentation does not cover every current execution behavior. The implementation uses the pinned source's actual process-handle interface and `returncode` contract.
- Eager relative import in the root plugin entry interfered with pytest collection. Deferring that import to `register()` preserves actual plugin loading and permits normal test discovery.
- The first Python-tool test expected the wrong result field. Actual Hermes returns `output`; the corrected live test passed, including kernel reuse.
- Validating the entire working directory scanned dependency caches and test runtime files, producing irrelevant binary/library findings. Packaging now creates an explicit clean runtime payload and a separate copy of all authored source. Both are passed to the unmodified official validator; no scanner rules are disabled.
- SDK exceptions from control/file/termination operations are now replaced with fixed diagnostics so downstream logs do not expose request details.

## October 14 guide and catalog checks

The revised README CSV example passed through the ordinary Hermes CLI and the GitHub-installed plugin at `bb57949a73aced8d7703554eac48f5a27eec2b2f`. The plugin runtime has not changed since `13950d1`. The agent wrote its CSV input and JSON output, then used `read_file` to return `{"paid_orders": 3, "total_cents": 2325}`. Actual terminal output reported Linux. The run took 45.92 seconds. Every sandbox recorded for this run was already stopped by Hermes when independently checked; temporary test credentials were removed. [Sanitized receipt](validation/public/readme-csv.json).

The README now states the required Python version, uses a complete pasteable example, explains temporary file loss, and includes execution, cleanup and recovery checks. It links to [Render's official skills repository](https://github.com/render-oss/skills). Those skills are maintained separately and are not bundled with this sandbox plugin. The README was inspected with GitHub-style rendering at desktop and 390-pixel widths.

The local behavior suite still passes 23 tests with the documented macOS skip. Ruff passes for runtime, tests and scripts; the preserved agent-generated test fixture is outside that lint scope. Current Hermes `5575a0859d62` validates the authored plugin source and resolves dependencies with `--install-deps`. The security scan is SAFE and the core-override check passes. Unlike the earlier validator, the current validator warns about imports it does not list as public, including `EnvironmentConnectionError` and `FileSyncManager`, plus imports used by development test scripts. These need maintainer review. This admission check does not establish a complete live task on current Hermes; the live result above uses the recorded `dce1e9b` runtime.

## Release work still needed

Review Hermes compatibility and lifecycle behavior with maintainers, including whether the next milestone requires state across turns. Test gateway account isolation, sustained production workloads and installation on additional operating systems. Observe an independent developer using the guide and review interactive approval dialogs. Recovery that resumes an interrupted task remains unsupported; the bounded crash and failure checks above establish containment and truthful errors. Source is published at [ojusave/hermes-plugin-render](https://github.com/ojusave/hermes-plugin-render) as a development preview. The repository now has an MIT license. Organization ownership, a reviewed release and partner documentation remain to be agreed. A separate catalog contribution is being prepared for maintainer review; the Render skills submission does not depend on it.

These checks establish technical feasibility at the pinned versions. Broader compatibility and production readiness remain unverified.
