# Validation record

Tested locally on October 12, 2026 (America/Los_Angeles). This proves a working preview at the pinned versions, not production readiness or partner approval.

| Component | Tested version |
| --- | --- |
| Hermes Agent | `dce1e9b37581dd62e480a9064dc04a709c2940d3`, unmodified source |
| Host Python | 3.14.3 on macOS |
| Render Python package | 1.2.0 |
| Render CLI, used only for test login | 2.28.0 |
| Model in end-to-end test | `gpt-6.1-sol`, through Hermes's `openai-codex` connection |
| Paid sandbox plan | starter, explicit 900-second lifetime, deny-all outbound network |

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

Local behavior suite: **17 passed, 1 skipped**. The skipped local process-group test needs Linux; the macOS runner rejected `killpg`. Equivalent timeout and child-process behavior passed live on Render Linux. This is an explicit platform limit, not a mocked pass.

Reports: [smoke](validation/public/live-smoke.json), [runtime and interruption](validation/public/live-runtime.json), [Python and synchronization](validation/public/live-features.json), [model task and independent verification](validation/public/live-agent.json). Published reports retain checks and cleanup results; account and sandbox IDs remain in git-ignored local reports. The [original tests](validation/agent-artifacts/test_totals.py) and [downloaded solution](validation/agent-artifacts/verified_totals.py) are synthetic and contain no user project data.

Hermes's official validator passed on both the clean runtime payload and the complete authored source copy, including tests and scripts, with no warnings. Its registration probe succeeded and found no Hermes core overrides. This is not an independent security audit or catalog acceptance.

## Issues found and resolved during testing

- The minimal provider documentation does not cover every current execution behavior. The implementation uses the pinned source's actual process-handle interface and `returncode` contract.
- Eager relative import in the root plugin entry interfered with pytest collection. Deferring that import to `register()` preserves actual plugin loading and permits normal test discovery.
- The first Python-tool test expected the wrong result field. Actual Hermes returns `output`; the corrected live test passed, including kernel reuse.
- Validating the entire working directory scanned dependency caches and test runtime files, producing irrelevant binary/library findings. Packaging now creates an explicit clean runtime payload and a separate copy of all authored source. Both are passed to the unmodified official validator; no scanner rules are disabled.
- SDK exceptions from control/file/termination operations are now replaced with fixed diagnostics so downstream logs do not expose request details.

## Release work still needed

Review Hermes compatibility and lifecycle behavior with maintainers, including whether the next milestone requires state across turns. Test normal interactive installation and dependency consent, gateway account isolation/concurrency, failure recovery and sustained use. Source is published at [ojusave/hermes-plugin-render](https://github.com/ojusave/hermes-plugin-render) as a development preview. Organization ownership, licensing, a reviewed release, catalog submission and partner documentation remain to be agreed. No catalog submission has been made.

These checks establish technical feasibility at the pinned versions. Broader compatibility and production readiness remain unverified.
