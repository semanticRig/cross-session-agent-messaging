#!/usr/bin/env python3
"""
Session Bus MCP Server — Live CTO↔Agent Messaging.

Tools:
  bus_send    — Send message to agent (or * for broadcast)
  bus_inbox   — Read + deliver pending messages for agent
  bus_peers   — List agents with active inboxes
  bus_ack     — Acknowledge delivered message
  bus_directives — Read sticky directives for agent

Resources:
  bus://peers    — JSON list of agents with pending messages
"""

import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "session-bus" / "server"))
from bus import send, poll, peek, ack, list_peers, get_sticky_directives


def handle_request(request: dict) -> dict:
    method = request.get("method", "")
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "session-bus", "version": "1.0.0"},
                "capabilities": {"tools": {}, "resources": {}},
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {"tools": [
                {
                    "name": "bus_send",
                    "description": "Send a message to an agent via the session bus. Use * to broadcast to all.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "to": {"type": "string", "description": "Agent ID, or * for broadcast"},
                            "body": {"type": "string", "description": "Message body"},
                            "kind": {"type": "string", "enum": ["note","correction","halt","request","reply"], "default": "note"},
                            "priority": {"type": "string", "enum": ["normal","urgent","low"], "default": "normal"},
                            "sticky": {"type": "boolean", "default": False},
                            "from_agent": {"type": "string", "default": "cto", "description": "Sender identity"},
                        },
                        "required": ["to", "body"],
                    },
                },
                {
                    "name": "bus_inbox",
                    "description": "Read and deliver pending messages for an agent.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "agent_id": {"type": "string", "description": "Agent ID to read inbox for"},
                        },
                        "required": ["agent_id"],
                    },
                },
                {
                    "name": "bus_peers",
                    "description": "List all agents with pending messages or active inboxes.",
                    "inputSchema": {"type": "object", "properties": {}},
                },
                {
                    "name": "bus_ack",
                    "description": "Acknowledge a delivered message — removes it from the inbox.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "agent_id": {"type": "string"},
                            "message_id": {"type": "string"},
                        },
                        "required": ["agent_id", "message_id"],
                    },
                },
                {
                    "name": "bus_directives",
                    "description": "Read sticky directives for an agent (persistent corrections).",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "agent_id": {"type": "string"},
                        },
                        "required": ["agent_id"],
                    },
                },
            ]},
        }

    if method == "resources/list":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {"resources": [
                {"uri": "bus://peers", "name": "Bus Peers", "mimeType": "application/json"},
            ]},
        }

    if method == "resources/read":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {"contents": [{"uri": "bus://peers", "mimeType": "application/json", "text": json.dumps(list_peers(), indent=2)}]},
        }

    if method == "tools/call":
        return _handle_tool(req_id, request.get("params", {}))

    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Not found: {method}"}}


def _handle_tool(req_id, params):
    tool = params.get("name", "")
    args = params.get("arguments", {})

    try:
        if tool == "bus_send":
            mid = send(
                args.get("to", ""),
                args.get("body", ""),
                from_agent=args.get("from_agent", "cto"),
                kind=args.get("kind", "note"),
                priority=args.get("priority", "normal"),
                sticky=args.get("sticky", False),
            )
            return _ok(req_id, {"message_id": mid, "to": args.get("to")})

        elif tool == "bus_inbox":
            msgs = poll(args["agent_id"])
            return _ok(req_id, {"messages": msgs, "count": len(msgs)})

        elif tool == "bus_peers":
            return _ok(req_id, {"peers": list_peers()})

        elif tool == "bus_ack":
            ok = ack(args["agent_id"], args["message_id"])
            return _ok(req_id, {"acknowledged": ok})

        elif tool == "bus_directives":
            dirs = get_sticky_directives(args["agent_id"])
            return _ok(req_id, {"directives": dirs, "count": len(dirs)})

        else:
            return _err(req_id, f"Unknown tool: {tool}")

    except Exception as e:
        return _err(req_id, str(e))


def _ok(req_id, data):
    return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(data, indent=2)}]}}


def _err(req_id, msg):
    return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps({"error": msg})}], "isError": True}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle_request(req)
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
