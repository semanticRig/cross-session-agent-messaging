"""
Session Registry — core library. v1.1

Atomic writes. Safe cleanup (quarantine, not delete). Session ID validation.
Conflict detection. Boot-ID for PID reuse detection. Python 3.10/3.11 compat.

Each OpenCode session writes its state to a JSON file in
~/.agent-memory/sessions/active/<session-id>.json
"""

import fcntl
import json
import os
import re
import socket
import tempfile
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Configuration (resolved at import; set env before importing)
# ---------------------------------------------------------------------------

REGISTRY_ROOT = Path(os.environ.get(
    "SESSION_REGISTRY_ROOT",
    os.path.expanduser("~/.agent-memory/sessions/active"),
))

_HEARTBEAT_TTL_SECONDS = int(os.environ.get("SESSION_REGISTRY_TTL", "120"))
_CLEANUP_GRACE_SECONDS = int(os.environ.get("SESSION_REGISTRY_GRACE", "30"))
_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Session ID validation
# ---------------------------------------------------------------------------

_VALID_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# ---------------------------------------------------------------------------
# Boot ID for PID reuse detection
# ---------------------------------------------------------------------------

def _read_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except Exception:
        return "unknown"


_BOOT_ID = _read_boot_id()


def _pid_start_time(pid: int) -> Optional[float]:
    """Read process start time from /proc/<pid>/stat (field 22, in jiffies)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # Field 22 is after the closing ')' of field 2 (comm)
        after_comm = stat.rfind(")") + 2
        fields = stat[after_comm:].split()
        if len(fields) >= 20:
            jiffies = int(fields[19])
            clock_ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
            return jiffies / clock_ticks
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SessionEntry:
    session_id: str
    pid: int
    schema_version: int = _SCHEMA_VERSION
    started_at: str = ""
    last_heartbeat: str = ""
    working_dir: str = ""
    branch: str = ""
    tty: str = ""
    status: str = "active"         # active | idle | blocked | completed
    description: str = ""
    locked_files: list[str] = field(default_factory=list)
    worktree: str = ""
    tags: list[str] = field(default_factory=list)
    hostname: str = ""
    username: str = ""
    boot_id: str = ""
    pid_start_time: float = 0.0

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "SessionEntry":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow_iso() -> str:
    """ISO 8601 UTC timestamp, Z-suffix for Python 3.10/3.11 compat."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_iso(ts: str) -> Optional[datetime]:
    """Parse ISO timestamp, handling Z suffix and fractional seconds."""
    if not ts:
        return None
    # Replace Z with +00:00 for fromisoformat compat (Python 3.10 needs this)
    normalized = ts.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except (ValueError, TypeError):
        return None


def _pid_alive(pid: int) -> bool:
    """Check if a process exists by sending signal 0."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pid_matches(entry: SessionEntry) -> bool:
    """Check if PID is alive AND boot_id + start_time match (detect PID reuse)."""
    if not _pid_alive(entry.pid):
        return False
    if not entry.boot_id:
        return True  # legacy entry, can't verify
    if entry.boot_id != _BOOT_ID:
        return False  # different boot
    if entry.pid_start_time:
        actual = _pid_start_time(entry.pid)
        if actual is not None and abs(actual - entry.pid_start_time) > 1.0:
            return False  # PID reused
    return True


def _generate_session_id() -> str:
    """Generate a unique session ID from hostname + pid + timestamp."""
    host = socket.gethostname()
    pid = os.getpid()
    ts = int(time.time())
    return f"{host}-{pid}-{ts}"


def _validate_session_id(session_id: str) -> None:
    """Raise ValueError if session_id is invalid (path traversal, special chars)."""
    if not _VALID_SESSION_ID.match(session_id):
        raise ValueError(
            f"Invalid session_id: {session_id!r}. "
            f"Must match {_VALID_SESSION_ID.pattern}"
        )
    if ".." in session_id or session_id.startswith("/"):
        raise ValueError(f"session_id must not contain '..' or start with '/': {session_id!r}")


# ---------------------------------------------------------------------------
# File I/O with atomic writes and locking
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, data: dict) -> None:
    """Write JSON atomically: tempfile in same dir → fsync → os.replace."""
    REGISTRY_ROOT.mkdir(parents=True, exist_ok=True)
    fd = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(REGISTRY_ROOT), prefix=".tmp-", suffix=".json"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _safe_read(path: Path) -> Optional[dict]:
    """Read JSON with error handling and symlink protection."""
    try:
        if path.is_symlink():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _try_lock(path: Path, shared: bool = False) -> bool:
    """Try to acquire an advisory lock. Non-blocking. Returns True on success."""
    try:
        op = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(path.fileno(), op | fcntl.LOCK_NB)
        return True
    except (OSError, IOError):
        return False


# ---------------------------------------------------------------------------
# Registry operations
# ---------------------------------------------------------------------------

def _entry_path(session_id: str) -> Path:
    _validate_session_id(session_id)
    return REGISTRY_ROOT / f"{session_id}.json"


def register(session_id: str, **kwargs) -> SessionEntry:
    """Create or overwrite a session entry (atomic write)."""
    _validate_session_id(session_id)

    entry = SessionEntry(
        session_id=session_id,
        pid=kwargs.get("pid", os.getpid()),
        started_at=kwargs.get("started_at", _utcnow_iso()),
        last_heartbeat=kwargs.get("last_heartbeat", _utcnow_iso()),
        working_dir=kwargs.get("working_dir", os.getcwd()),
        branch=kwargs.get("branch", ""),
        tty=kwargs.get("tty", ""),
        status=kwargs.get("status", "active"),
        description=kwargs.get("description", ""),
        locked_files=kwargs.get("locked_files", []),
        worktree=kwargs.get("worktree", ""),
        tags=kwargs.get("tags", []),
        hostname=kwargs.get("hostname", socket.gethostname()),
        username=kwargs.get("username", os.environ.get("USER", "")),
        boot_id=_BOOT_ID,
        pid_start_time=_pid_start_time(os.getpid()) or 0.0,
    )

    _atomic_write(_entry_path(session_id), entry.to_json())
    return entry


def heartbeat(session_id: str) -> Optional[SessionEntry]:
    """Update the heartbeat timestamp (atomic)."""
    path = _entry_path(session_id)
    data = _safe_read(path)
    if data is None:
        return None

    data["last_heartbeat"] = _utcnow_iso()
    _atomic_write(path, data)
    return SessionEntry.from_json(data)


def unregister(session_id: str) -> bool:
    """Remove a session entry. Returns True if entry existed and was removed."""
    path = _entry_path(session_id)
    existed = path.exists()
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return existed


def get(session_id: str) -> Optional[SessionEntry]:
    """Read a single session entry without staleness check."""
    path = _entry_path(session_id)
    data = _safe_read(path)
    if data is None:
        return None
    try:
        return SessionEntry.from_json(data)
    except Exception:
        return None


def is_stale(entry: SessionEntry) -> bool:
    """Check if an entry is stale (process dead/mismatched OR heartbeat too old)."""
    try:
        pid = int(entry.pid)
    except (ValueError, TypeError):
        return True

    if not _pid_matches(entry):
        return True

    if not entry.last_heartbeat:
        return False  # no heartbeat yet, treat as fresh

    hb = _parse_iso(entry.last_heartbeat)
    if hb is None:
        return True

    age = (datetime.now(timezone.utc) - hb).total_seconds()
    return age > _HEARTBEAT_TTL_SECONDS


def list_sessions(*, include_stale: bool = False) -> list[SessionEntry]:
    """List all registered sessions. Stale entries filtered by default."""
    active = []
    if not REGISTRY_ROOT.exists():
        return active

    for path in sorted(REGISTRY_ROOT.glob("*.json")):
        if not path.is_file():
            continue
        data = _safe_read(path)
        if data is None:
            continue
        try:
            entry = SessionEntry.from_json(data)
        except Exception:
            continue

        if not include_stale and is_stale(entry):
            continue

        active.append(entry)

    return active


def list_sessions_by_working_dir(
    working_dir: str, *, include_stale: bool = False
) -> list[SessionEntry]:
    """List sessions in a specific directory tree (exact-path or subdirectory)."""
    wd = os.path.abspath(os.path.realpath(working_dir))
    # Use commonpath to avoid prefix collisions (/repo matching /repo-other)
    return [
        s for s in list_sessions(include_stale=include_stale)
        if os.path.commonpath([
            os.path.abspath(os.path.realpath(s.working_dir)), wd
        ]) == wd
    ]


def cleanup() -> int:
    """Remove stale entries. Dead-PID entries removed immediately.
    Heartbeat-stale entries require grace period. Unparseable files are quarantined."""
    removed = 0
    quarantine = REGISTRY_ROOT / ".quarantine"
    if not REGISTRY_ROOT.exists():
        return removed

    now = datetime.now(timezone.utc)

    for path in sorted(REGISTRY_ROOT.glob("*.json")):
        if not path.is_file():
            continue

        data = _safe_read(path)
        if data is None:
            # Corrupt/unreadable: quarantine instead of delete
            try:
                quarantine.mkdir(parents=True, exist_ok=True)
                path.rename(quarantine / f"{path.name}.corrupt-{int(time.time())}")
                removed += 1
            except OSError:
                pass
            continue

        try:
            entry = SessionEntry.from_json(data)
        except Exception:
            continue  # can't parse fields, skip (don't delete)

        if not is_stale(entry):
            continue

        # Determine grace: zero for dead PID, full grace for live-but-stale-heartbeat
        pid_dead = not _pid_alive(entry.pid)
        grace = 0.0 if pid_dead else _CLEANUP_GRACE_SECONDS

        hb = _parse_iso(entry.last_heartbeat)
        if hb is not None:
            age = (now - hb).total_seconds()
        elif pid_dead:
            age = float("inf")  # dead PID, can't parse heartbeat → remove
        else:
            age = 0.0  # alive PID, can't parse heartbeat → don't remove

        if age < grace:
            continue

        try:
            path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            pass

    return removed


def update_session(session_id: str, **changes) -> Optional[SessionEntry]:
    """Non-destructive update: read current, merge changes, atomic write.

    Unlike register() which creates a fresh entry, this preserves all
    existing fields and only overwrites the keys provided.
    """
    path = _entry_path(session_id)
    data = _safe_read(path)
    if data is None:
        return None

    for key in ("session_id", "started_at", "pid", "boot_id"):
        changes.pop(key, None)

    data.update(changes)
    _atomic_write(path, data)
    return SessionEntry.from_json(data)


def find_conflicts(
    session_id: str, locked_files: Optional[list[str]] = None
) -> list[dict]:
    """Find sessions that have overlapping locked_files with this session.

    Returns list of {session_id, description, overlapping_files, branch, status}.
    """
    me = get(session_id)
    if me is None:
        return []

    my_files = set(locked_files if locked_files is not None else me.locked_files)
    if not my_files:
        return []

    conflicts = []
    for peer in list_sessions(include_stale=False):
        if peer.session_id == session_id:
            continue
        peer_files = set(peer.locked_files)
        overlap = sorted(my_files & peer_files)
        if overlap:
            conflicts.append({
                "session_id": peer.session_id,
                "description": peer.description,
                "branch": peer.branch,
                "status": peer.status,
                "working_dir": peer.working_dir,
                "overlapping_files": overlap,
            })

    return conflicts


# ---------------------------------------------------------------------------
# Convenience: setup for the current process
# ---------------------------------------------------------------------------

def setup_current_session(**kwargs) -> str:
    """Register the current process as a session. Returns session_id."""
    sid = _generate_session_id()
    register(sid, **kwargs)
    return sid
