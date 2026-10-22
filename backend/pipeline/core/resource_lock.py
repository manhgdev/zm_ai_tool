"""Cross-worker installation locks for shared Web resources."""
from contextlib import contextmanager
import os


@contextmanager
def shared_resource_lock(name: str):
    if os.environ.get("ZM_AI_TOOL_WEB_WORKER") != "1":
        yield
        return
    # Web workers are POSIX-only. flock releases even if a worker is killed.
    # ponytail: one host/filesystem; use a distributed lock for multi-host installs.
    import fcntl
    from .config import RESOURCE_DATA

    root = RESOURCE_DATA / ".install-locks"
    root.mkdir(parents=True, exist_ok=True)
    with (root / f"{name}.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
