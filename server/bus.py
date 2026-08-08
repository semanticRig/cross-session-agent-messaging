"""
Session Bus — Maildir-style message spool for CTO↔agent communication.

Layout:
  ~/.agent-memory/sessions/bus/
    <agent_id>/
      tmp/       — write → fsync → rename
      new/       — pending delivery
      cur/       — delivered, awaiting GC
      directives.json  — sticky corrections
    _broadcast/  — fan-out target

Zero daemon. Zero sockets. Atomic renames for crash safety.
Reuses session-registry's _atomic_write pattern.
"""

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BUS_ROOT = Path(os.environ.get(
    "SESSION_BUS_ROOT",
    os.path.expanduser("~/.agent-memory/sessions/bus"),
))

MAX_MSG_BYTES = int(os.environ.get("SESSION_BUS_MAX_MSG", "4096"))
MAX_MSGS_PER_POLL = int(os.environ.get("SESSION_BUS_MAX_POLL", "3"))
MSG_TTL_SECONDS = int(os.environ.get("SESSION_BUS_MSG_TTL", "900"))
MAX_INBOX_COUNT = int(os.environ.get("SESSION_BUS_MAX_INBOX", "50"))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _agent_dir(agent_id: str) -> Path:
    """Safe path for an agent's bus directory."""
    if ".." in agent_id or "/" in agent_id or agent_id.startswith("."):
        raise ValueError(f"Invalid agent_id: {agent_id!r}")
    return BUS_ROOT / agent_id


def _atomic_write(path: Path, data: str) -> None:
    """Write atomically: tempfile → fsync → os.replace."""
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _ensure_dirs(agent_id: str) -> tuple[Path, Path, Path]:
    """Ensure tmp/new/cur exist for agent. Returns (tmp, new, cur)."""
    base = _agent_dir(agent_id)
    for d in ("tmp", "new", "cur"):
        (base / d).mkdir(parents=True, exist_ok=True)
    return base / "tmp", base / "new", base / "cur"


# ---------------------------------------------------------------------------
# Message model
# ---------------------------------------------------------------------------

def create_message(
    from_agent: str,
    to_agent: str,
    body: str,
    *,
    kind: str = "note",
    priority: str = "normal",
    sticky: bool = False,
    ttl_seconds: int = MSG_TTL_SECONDS,
    reply_to: str = "",
) -> dict:
    """Create a message envelope."""
    if len(body.encode("utf-8")) > MAX_MSG_BYTES:
        raise ValueError(f"Message body exceeds {MAX_MSG_BYTES} bytes")
    return {
        "id": uuid.uuid4().hex[:12],
        "from": from_agent,
        "to": to_agent,
        "kind": kind,
        "priority": priority,
        "sticky": sticky,
        "ttl_seconds": ttl_seconds,
        "reply_to": reply_to,
        "body": body,
        "created_at": _utcnow_iso(),
        "expires_at": "",  # set on delivery
        "delivered_at": "",
    }


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

def send(
    to_agent: str,
    body: str,
    *,
    from_agent: str = "cto",
    kind: str = "note",
    priority: str = "normal",
    sticky: bool = False,
    ttl_seconds: int = MSG_TTL_SECONDS,
    reply_to: str = "",
) -> str:
    """Send a message to an agent. Returns message ID."""
    msg = create_message(
        from_agent=from_agent,
        to_agent=to_agent,
        body=body,
        kind=kind,
        priority=priority,
        sticky=sticky,
        ttl_seconds=ttl_seconds,
        reply_to=reply_to,
    )

    # Support broadcast
    target = "_broadcast" if to_agent == "*" else to_agent
    tmp, new, _ = _ensure_dirs(target)

    # Write to tmp, then atomic rename into new/
    msg_json = json.dumps(msg, indent=2, ensure_ascii=False)
    tmpfile = tmp / f"{msg['id']}.json"
    newfile = new / f"{msg['id']}.json"
    _atomic_write(tmpfile, msg_json)
    # Atomic move from tmp into new
    if tmpfile.exists():
        os.replace(tmpfile, newfile)

    # If sticky, update directives
    if sticky:
        _update_directives(to_agent, msg)

    return msg["id"]


def _update_directives(agent_id: str, msg: dict) -> None:
    """Add sticky directive to agent's directives.json."""
    base = _agent_dir(agent_id)
    base.mkdir(parents=True, exist_ok=True)
    path = base / "directives.json"

    directives = {}
    if path.exists():
        try:
            directives = json.loads(path.read_text())
        except Exception:
            directives = {}

    directives[msg["id"]] = {
        "body": msg["body"],
        "from": msg["from"],
        "kind": msg["kind"],
        "added_at": _utcnow_iso(),
    }

    _atomic_write(path, json.dumps(directives, indent=2))


# ---------------------------------------------------------------------------
# Receive / Poll
# ---------------------------------------------------------------------------

def poll(agent_id: str, *, max_msgs: int = MAX_MSGS_PER_POLL) -> list[dict]:
    """Poll agent's inbox. Returns up to max_msgs pending messages (oldest first)."""
    _, new, cur = _ensure_dirs(agent_id)

    messages = []
    files = sorted(new.glob("*.json"))
    for f in files[:max_msgs]:
        try:
            data = json.loads(f.read_text())
        except Exception:
            # Corrupt: quarantine, don't delete
            quarantine = _agent_dir(agent_id) / ".quarantine"
            quarantine.mkdir(parents=True, exist_ok=True)
            f.rename(quarantine / f"{f.name}.corrupt-{int(time.time())}")
            continue

        # Check expiry
        created = data.get("created_at", "")
        if created:
            try:
                ct = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - ct).total_seconds()
                ttl = data.get("ttl_seconds", MSG_TTL_SECONDS)
                if age > ttl:
                    f.unlink(missing_ok=True)
                    continue
            except Exception:
                pass

        # Deliver: rename to cur/
        dest = cur / f.name
        os.replace(f, dest)
        data["delivered_at"] = _utcnow_iso()
        messages.append(data)

    return messages


def peek(agent_id: str) -> list[dict]:
    """Peek at pending messages without delivering them."""
    _, new, _ = _ensure_dirs(agent_id)
    messages = []
    for f in sorted(new.glob("*.json")):
        try:
            messages.append(json.loads(f.read_text()))
        except Exception:
            pass
    return messages


def ack(agent_id: str, msg_id: str) -> bool:
    """Acknowledge a delivered message. Removes from cur/."""
    cur = _agent_dir(agent_id) / "cur"
    path = cur / f"{msg_id}.json"
    if path.exists():
        path.unlink()
        return True

    # Also check sticky directives
    directives_path = _agent_dir(agent_id) / "directives.json"
    if directives_path.exists():
        try:
            directives = json.loads(directives_path.read_text())
            if msg_id in directives:
                del directives[msg_id]
                _atomic_write(directives_path, json.dumps(directives, indent=2))
                return True
        except Exception:
            pass

    return False


def get_sticky_directives(agent_id: str) -> list[str]:
    """Get active sticky directives for an agent."""
    path = _agent_dir(agent_id) / "directives.json"
    if not path.exists():
        return []
    try:
        directives = json.loads(path.read_text())
    except Exception:
        return []
    return [d["body"] for d in directives.values()]


# ---------------------------------------------------------------------------
# Peers / Discovery
# ---------------------------------------------------------------------------

def list_peers() -> list[dict]:
    """List agents with pending messages or active inboxes."""
    peers = []
    if not BUS_ROOT.exists():
        return peers

    for d in sorted(BUS_ROOT.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or d.name.startswith("_"):
            continue
        new_count = len(list((d / "new").glob("*.json"))) if (d / "new").exists() else 0
        cur_count = len(list((d / "cur").glob("*.json"))) if (d / "cur").exists() else 0
        peers.append({
            "agent_id": d.name,
            "pending": new_count,
            "delivered_unacked": cur_count,
        })

    # Also check broadcast inbox
    broadcast = BUS_ROOT / "_broadcast" / "new"
    if broadcast.exists():
        bc_count = len(list(broadcast.glob("*.json")))
        if bc_count > 0:
            peers.append({"agent_id": "_broadcast", "pending": bc_count, "delivered_unacked": 0})

    return peers


# ---------------------------------------------------------------------------
# GC / Cleanup
# ---------------------------------------------------------------------------

def gc(agent_id: str = "", *, max_age_seconds: int = 3600) -> int:
    """Garbage collect expired messages. If agent_id is empty, GC all agents."""
    removed = 0
    now = datetime.now(timezone.utc)

    targets = [_agent_dir(a) for a in [agent_id]] if agent_id else [
        d for d in BUS_ROOT.iterdir() if d.is_dir() and not d.name.startswith(".")
    ]

    for base in targets:
        for sub in ("cur", "new", "tmp"):
            subdir = base / sub
            if not subdir.exists():
                continue
            for f in subdir.glob("*.json"):
                try:
                    data = json.loads(f.read_text())
                    ct_str = data.get("created_at", "")
                    if ct_str:
                        ct = datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
                        if (now - ct).total_seconds() > max_age_seconds:
                            f.unlink(missing_ok=True)
                            removed += 1
                except Exception:
                    # Corrupt, quarantine
                    q = base / ".quarantine"
                    q.mkdir(parents=True, exist_ok=True)
                    f.rename(q / f"{f.name}.corrupt-{int(time.time())}")
                    removed += 1

        # Prune empty sticky directives
        dp = base / "directives.json"
        if dp.exists():
            try:
                directives = json.loads(dp.read_text())
                if not directives:
                    dp.unlink(missing_ok=True)
            except Exception:
                dp.unlink(missing_ok=True)

    return removed


# ---------------------------------------------------------------------------
# Injection helper — formats a message for context injection
# ---------------------------------------------------------------------------

def format_for_injection(messages: list[dict]) -> str:
    """Format pending messages for injection into subagent context.
    Returns a fenced block suitable for appending to tool results or system prompts.
    """
    if not messages:
        return ""

    lines = ["\n⚠️ [BUS] Pending messages:"]
    for m in messages:
        lines.append(f"  [{m['from']}→{m['to']}] [{m.get('kind','note')}] [{m.get('priority','normal')}]")
        lines.append(f"  {m['body']}")
        lines.append(f"  ack: bus_ack({m['id']})")
    return "\n".join(lines)
