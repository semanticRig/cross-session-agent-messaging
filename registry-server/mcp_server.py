#!/usr/bin/env python3
"""
Session Registry MCP Server.

Exposes session-registry operations as MCP tools so OpenCode sessions
can register, heartbeat, list peers, and unregister cleanly.

Tools:
  - session_register    — Register this session in the shared registry
  - session_heartbeat   — Update last_heartbeat timestamp
  - session_unregister  — Remove this session from the registry
  - session_list        — List all active sessions
  - session_get         — Get details for one session
  - session_cleanup     — Prune stale entries
  - session_update      — Update session fields (description, status, locked_files, tags)

Resources:
  - sessions://active   — JSON list of all active sessions
  - sessions://stale    — JSON list of stale (to be cleaned) entries
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from registry import (
    register,
    heartbeat,
    unregister,
    list_sessions,
    get,
    cleanup,
    is_stale,
    setup_current_session,
    update_session,
    find_conflicts,
    SessionEntry,
)

# Track whether this process registered itself
_current_session_id: str | None = None


def handle_request(request: dict) -> dict:
    """Handle a single MCP JSON-RPC request."""
    method = request.get("method", "")
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "session-registry", "version": "0.1.0"},
                "capabilities": {"tools": {}, "resources": {}},
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": "session_register",
                        "description": "Register this OpenCode session in the shared registry. Call once at session start.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "working_dir": {"type": "string", "description": "Working directory of this session"},
                                "branch": {"type": "string", "description": "Git branch being worked on"},
                                "description": {"type": "string", "description": "Brief description of what this session is doing"},
                                "locked_files": {"type": "array", "items": {"type": "string"}, "description": "Files this session is actively editing"},
                                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags for categorization"},
                            },
                        },
                    },
                    {
                        "name": "session_heartbeat",
                        "description": "Update the heartbeat timestamp. Call periodically to keep session alive.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "session_id": {"type": "string", "description": "Session ID (optional, uses current if omitted)"},
                            },
                        },
                    },
                    {
                        "name": "session_unregister",
                        "description": "Remove this session from the registry. Call on clean shutdown.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "session_id": {"type": "string", "description": "Session ID (optional, uses current if omitted)"},
                            },
                        },
                    },
                    {
                        "name": "session_list",
                        "description": "List all active (non-stale) sessions in the registry. Shows what other sessions are working on.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "include_stale": {"type": "boolean", "description": "Include stale entries (default: false)"},
                            },
                        },
                    },
                    {
                        "name": "session_get",
                        "description": "Get full details for one session by ID.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "session_id": {"type": "string", "description": "Session ID to look up"},
                            },
                            "required": ["session_id"],
                        },
                    },
                    {
                        "name": "session_cleanup",
                        "description": "Prune stale session entries (dead processes, expired heartbeats). Returns count removed.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {},
                        },
                    },
                    {
                        "name": "session_update",
                        "description": "Update fields on the current session entry (description, status, locked_files, tags).",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "session_id": {"type": "string", "description": "Session ID (optional, uses current if omitted)"},
                                "status": {"type": "string", "enum": ["active", "idle", "blocked", "completed"]},
                                "description": {"type": "string"},
                                "locked_files": {"type": "array", "items": {"type": "string"}},
                                "tags": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                    },
                ]
            },
        }

    if method == "resources/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "resources": [
                    {
                        "uri": "sessions://active",
                        "name": "Active Sessions",
                        "description": "All currently active (non-stale) sessions",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": "sessions://stale",
                        "name": "Stale Sessions",
                        "description": "Stale sessions pending cleanup",
                        "mimeType": "application/json",
                    },
                ]
            },
        }

    if method == "resources/read":
        uri = request.get("params", {}).get("uri", "")
        if uri == "sessions://active":
            sessions = [s.to_json() for s in list_sessions(include_stale=False)]
            return {"jsonrpc": "2.0", "id": req_id, "result": {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(sessions, indent=2)}]}}
        if uri == "sessions://stale":
            all_sessions = list_sessions(include_stale=True)
            stale = [s.to_json() for s in all_sessions if is_stale(s)]
            return {"jsonrpc": "2.0", "id": req_id, "result": {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(stale, indent=2)}]}}
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": f"Unknown resource: {uri}"}}

    if method == "tools/call":
        return _handle_tool_call(req_id, request.get("params", {}))

    # Unknown method
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def _handle_tool_call(req_id, params: dict) -> dict:
    """Dispatch tool calls."""
    global _current_session_id
    tool_name = params.get("name", "")
    args = params.get("arguments", {})

    try:
        if tool_name == "session_register":
            sid = setup_current_session(
                working_dir=args.get("working_dir", ""),
                branch=args.get("branch", ""),
                description=args.get("description", ""),
                locked_files=args.get("locked_files", []),
                tags=args.get("tags", []),
            )
            _current_session_id = sid
            entry = get(sid)
            return _tool_result(req_id, {"session_id": sid, "entry": entry.to_json() if entry else None})

        elif tool_name == "session_heartbeat":
            sid = args.get("session_id", _current_session_id)
            if not sid:
                return _tool_error(req_id, "No session_id and no current session")
            entry = heartbeat(sid)
            if entry is None:
                return _tool_error(req_id, f"Session {sid} not found")
            return _tool_result(req_id, {"session_id": sid, "last_heartbeat": entry.last_heartbeat})

        elif tool_name == "session_unregister":
            sid = args.get("session_id", _current_session_id)
            if not sid:
                return _tool_error(req_id, "No session_id and no current session")
            ok = unregister(sid)
            if sid == _current_session_id:
                _current_session_id = None
            return _tool_result(req_id, {"session_id": sid, "removed": ok})

        elif tool_name == "session_list":
            sessions = list_sessions(include_stale=args.get("include_stale", False))
            return _tool_result(req_id, {
                "sessions": [s.to_json() for s in sessions],
                "count": len(sessions),
            })

        elif tool_name == "session_get":
            sid = args.get("session_id", "")
            if not sid:
                return _tool_error(req_id, "session_id is required")
            entry = get(sid)
            if entry is None:
                return _tool_error(req_id, f"Session {sid} not found")
            return _tool_result(req_id, {"entry": entry.to_json(), "is_stale": is_stale(entry)})

        elif tool_name == "session_cleanup":
            removed = cleanup()
            return _tool_result(req_id, {"removed": removed})

        elif tool_name == "session_update":
            sid = args.get("session_id", _current_session_id)
            if not sid:
                return _tool_error(req_id, "No session_id and no current session")
            changes = {}
            if "status" in args:
                changes["status"] = args["status"]
            if "description" in args:
                changes["description"] = args["description"]
            if "locked_files" in args:
                changes["locked_files"] = args["locked_files"]
            if "tags" in args:
                changes["tags"] = args["tags"]
            entry = update_session(sid, **changes)
            if entry is None:
                return _tool_error(req_id, f"Session {sid} not found")
            return _tool_result(req_id, {"session_id": sid, "entry": entry.to_json()})

        else:
            return _tool_error(req_id, f"Unknown tool: {tool_name}")

    except Exception as e:
        return _tool_error(req_id, str(e))


def _tool_result(req_id, data: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"content": [{"type": "text", "text": json.dumps(data, indent=2)}]},
    }


def _tool_error(req_id, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"content": [{"type": "text", "text": json.dumps({"error": message})}], "isError": True},
    }


# ---------------------------------------------------------------------------
# Main loop — stdio JSON-RPC
# ---------------------------------------------------------------------------

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle_request(request)
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
