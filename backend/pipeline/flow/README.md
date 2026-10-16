# Flow service modules

`service.py` is the entry point for `FlowService` and its shared `service`
instance. It owns runtime state, logging, and startup cleanup. Task-specific
mixins keep the same service methods available to API routes, Series, and
automation callers.

| Module | Responsibility |
| --- | --- |
| `accounts.py` | Account metadata, account/model suspension, credit sync, capability discovery, account selection |
| `auth.py` | Google login, cookie seeding, cookie verification, saving live cookies |
| `profiles.py` | Cookie normalization, profile cleanup, profile copies for sessions and workers |
| `sessions.py` | Open/connect/reset sessions, periodic refresh, browser ownership and cleanup |
| `recovery.py` | Shared reconnect, cookie recovery, account/model fallback |
| `models.py` | Model catalogs, request settings, entitlement costs, concurrency settings |
| `projects.py` | Project identity, creation, navigation, migration recovery |
| `controls.py` | Flow mode/model/ratio/duration/resolution controls |
| `submission.py` | Prompt/reference input, submit button detection, submission acknowledgement |
| `media.py` | Media identity and ownership, result discovery, progress, recovery after submission |
| `downloads.py` | Video download quality, menu/API downloads, download recovery |
| `outputs.py` | Output folders and filenames, file validation, logo removal |
| `queue.py` | Enqueue, list, cancel, delete, retry, batch queue operations |
| `worker.py` | Dispatch order, concurrency admission, running image/video jobs |
| `errors.py` | Error classification, network interruption, retry backoff |
| `constants.py` | Shared limits, selectors, provider identifiers |

## Editing a flow

- Keep shared mutable state in `FlowService.__init__`; mixins run on that same
  object and use its existing locks.
- Call related service operations through `self`. Import standalone helpers
  from their defining modules. Implementation modules must not import
  `service.py`, which composes them.
- Legacy helper imports from `service.py` remain available for compatibility.
- Mock a replaced function where it is looked up, for example
  `pipeline.flow.worker._network_ready_for_flow_job` or
  `pipeline.flow.sessions.time`.

Run the Flow regression tests from the repository root:

```sh
PYTHONPATH=backend backend/.venv/bin/python -m unittest discover -s tests -p 'test_flow*.py'
```

## Manhgdev browser compatibility check — 2026-10-16

This investigation covers the requested account-specific browser fallback.
It is not a complete audit or import of gflow-cli. Compared upstream `develop`
at [38ce8ac8f299e19c9d35e1def30ab7339ad64cdc](https://github.com/ffroliva/gflow-cli/tree/38ce8ac8f299e19c9d35e1def30ab7339ad64cdc),
verified against the remote branch during this run. Relevant upstream sources:
`config.py`, `browser_manager.py`, `api/transports/ui_automation.py`,
`worker/queue.py`, and `services/media_recovery.py`.

### Comparison

| Feature | Existing ZM | gflow-cli at the compared commit | Decision / action |
| --- | --- | --- | --- |
| Installed Chrome | Native Chrome with owned CDP lifecycle | Chrome discovery and persistent Playwright context | Keep ZM lifecycle; no extra Chromium or dependency |
| Generation mode | Requested headless mode remains unchanged after reset | UI transport launches headed Chrome; config explicitly does not promise headless support | Add an opt-in account fallback; headless acceptance remains unproven |
| Window placement | Visible jobs use screen geometry | `browser_window_position` defaults to `-30000,-30000` | Support an explicit position in the existing browser manager |
| macOS off-screen placement | Screen placement clamps windows into the work area | Position is passed as a Chrome argument | Real test found macOS clamps negative coordinates; minimize only the owned job window |
| Queue and retry | Existing worker, global concurrency limit, shared reset | Separate persistent queue and claim/checkpoint system | Reuse ZM scheduler and limits; do not import another engine |
| Media recovery | Account/project/submission identity and download recovery | Profile-scoped media identity and recovery records | Preserve completed media and unresolved accepted submissions |
| Browser spoofing | Native UA and no injected webdriver override | Upstream UI transport includes browser modifications | No spoofing code or experimental engine imported |

### Changes in this focused fix

| File | Change | Reason / verification |
| --- | --- | --- |
| `backend/api/routes/flow.py`, `accounts.py` | Optional `headlessFallbackEnabled`; retain it on account edits | Scope behavior to Manhgdev; account persistence test |
| `recovery.py` | After the existing reset budget, requeue one rejected headless attempt using headed Chrome; persist `headlessFallbackAt` | Same account, one fallback, existing scheduler; cancellation/media/display guards tested |
| `worker.py` | Honor the account fallback for background generation; record effective mode | Real APP trace verified headless → reset → headless → headed → downloaded output |
| `browser.py` | Position argument and CDP placement; skip bring-to-front; minimize the owned macOS job window | Native process arguments and CDP bounds inspected live |
| `queue.py` | Clear the per-job fallback counter when explicitly retrying | Existing single/batch retry lifecycle retained |
| `frontend/src/features/flow/flow.explain.ts` | Bilingual event for automatic headed fallback | Stable `browser_headed_fallback` event; i18n check |
| `tests/test_flow_block_recovery.py`, `tests/test_flow_browser_security.py`, `tests/i18n.catalog.test.mjs` | Focused fallback, configuration, window and localization checks | Results below |

The worktree already contained authentication, bounded result collection,
submission clearing and recovery changes from the preceding work. They were
preserved and included in the local APP build. No source backups were created.

### Deleted files

| File | Previous purpose | Evidence / replacement |
| --- | --- | --- |
| None | — | No module was proved unused within this focused investigation; no speculative deletion |

### Measured behavior

Same account, prompt `con vịt`, model Nano Banana 2.1, 9:16, one image.
The first pair used isolated copies of the saved APP profile, without resetting
or changing credentials between the two modes. These are individual samples,
not benchmark averages.

| Measurement | Headless baseline | Headed baseline / new APP | Interpretation |
| --- | --- | --- | --- |
| Browser ready | 0.85 s | 1.13 s headed | Headed startup was 0.28 s slower in this sample |
| Request submitted | 13.61 s | 14.12 s headed | Headed submission was 0.51 s slower |
| Final result | Unusual activity, failed after 14.58 s | Downloaded image, done after 36.69 s | Compatibility difference confirmed for this account/sample |
| Installed APP automatic recovery | No headed fallback | Headed selected at 59.49 s; output completed at 108.33 s; observer saw done at 109.35 s | No manual retry between recovery stages; account unchanged |
| Subsequent job mode | Requested headless | `con dê` immediately used headed at 0.11 s | Account preference reused without another block/reset |
| macOS native window | Coordinate request is clamped | Requested -30000,-30000; actual -1040,33 and `windowState=minimized` | Window hidden by minimization, not a claim that macOS accepted the requested position |
| CPU/RAM and sustained concurrency | Not measured | Not measured | No performance claim |

### Verification matrix

| Check | Platform | Result | Boundary |
| --- | --- | --- | --- |
| Recovery tests | macOS, Python 3.12 | 19 passed | Mocked failures; includes opt-in, one-attempt limit, cancellation, display absence, media preservation and default modes |
| New browser position tests | macOS test runner | 2 passed | Covers Windows-style placement and macOS owned-window minimization with mocks |
| Browser suite after startup follow-up | macOS | 41 passed | Stale geometry expectations updated to the two-column layout introduced by commit `4c180908`; production geometry unchanged |
| Vietnamese/English catalog | Node | 25 passed | New event mapped in both languages |
| Frontend build and changed Python compilation | macOS | Passed | Vite bundle-size warning remains |
| APP build and signature | macOS arm64 | Passed and installed | Local 8.6.73 build; no release published |
| Bundled code comparison | macOS | 8 source modules matched embedded bytecode | API, accounts, auth, browser, queue, recovery, worker and media |
| Live image fallback | Installed macOS APP | `06fdd50b4326` done on Manhgdev | One session reset and one headed fallback; generated JPEG 768×1376, 185266 bytes |
| Live accepted-result lookup | Installed macOS APP | `868b4c68e832` still has `FLOW_GENERATION_TIMEOUT` | No new submission; original project/time preserved; older local file predates this submission |
| Live video, concurrent accounts, network interruption, Windows/Linux builds | — | Not rerun | No new video credits spent; no cross-platform live claim |

### Current state and limits

`headlessFallbackEnabled=true` is enabled only on Manhgdev. After activation,
later background jobs on that account use the compatible headed mode. Other
accounts retain their selected mode. Explicit visible jobs remain visible.
Setting this account field to `false` clears the remembered fallback.
An environment with no graphical display cannot use this fallback.

The APP queue now shows 7/8 done. The old `con dê` submission remains unresolved;
its 8-second lookup ended without a matching result. Its local file was written
about 250 seconds before the preserved submission timestamp, so it cannot be
reported as that submission's result. The lookup sent zero generation requests.

This establishes a working headed fallback for the tested image. Headless still
reproduces the unusual-activity error. Google may still require human sign-in or
verification, and no unconditional unattended-completion guarantee is made.

### Startup visibility follow-up (8.6.74)

Setting `windowState=minimized` alone still painted a macOS window and its Dock
animation for about 0.6 seconds in the startup probe. The background path now
passes `--no-startup-window`, uses macOS LaunchServices (`open -g -j -n -W`) to
launch a separate hidden Chrome instance, then creates its first page in a
minimized window with CDP. Page preparation never restores that window to normal.
The launcher is waited/reaped with the owned browser; profile-scoped cleanup
still kills Chrome on failed startup. Manual/visible sessions keep their normal
launch path. No browser metadata or security behavior is modified.

Three real Chrome 155 startup/navigation/click/shutdown cycles on macOS recorded
zero on-screen window samples (304 window-server snapshots, requested interval
10 ms). Both profile reopenings retained a test session cookie, and every cycle
left no owned Chrome process. These samples establish the observed behavior;
Windows/Linux initial visibility was not tested live.

The signed macOS 8.6.74 APP was installed and tested with Manhgdev, prompt
`con vịt`, Nano Banana 2.1, 9:16, one image. Job `3fca326f49dc` completed and
downloaded its JPEG in 68.67 seconds. Its native Chrome process used headed
mode, `--no-startup-window` and `--window-position=-30000,-30000`; 4,279
window-server snapshots recorded zero on-screen windows for that process.
The APP showed 1/1 completed, the downloaded image was visually checked, and
the owned Chrome process exited after the job. No video generation was run.

Runnable regression checks:

```sh
PYTHONPATH=backend backend/.venv/bin/python tests/test_flow_browser_security.py -v
PYTHONPATH=backend backend/.venv/bin/python tests/test_flow_block_recovery.py -v
npm run test:i18n
npm run build
```
