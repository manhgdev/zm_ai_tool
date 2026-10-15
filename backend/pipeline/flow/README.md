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
PYTHONPATH=backend backend/.venv/bin/python -m pytest -q tests/test_flow*.py tests/test_network_wait.py
```
