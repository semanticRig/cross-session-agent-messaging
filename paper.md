# Claude Code Got Agent Teams. So Did We.

July 2026. Claude Code ships agent teams. You open two terminals, two independent sessions, and they can message each other. The lead watches teammates in real time. If one veers off track, you send a correction mid-task. No restart, no context drop.

OpenCode had nothing like this. Subagents are fire-and-forget. You dispatch them, they run, they report. You cannot correct one mid-flight. The orchestrator is blocked inside `task()` until completion.

Then we found the hook.

## The hook

OpenCode subagents are not separate processes. They are conversations inside the parent session. Every subagent row in the SQLite `session` table has a `parent_id` and shares the parent's process tree.

Because they share the process, they also share the parent's plugin hooks.

`tool.execute.after` fires after every tool call in every session. Subagents included. It receives the session ID, the tool name, and the output text. And it can mutate that text before the language model sees it.

This was not documented. The hook payload shape is not versioned. But it works. Deterministically. Every tool call, every subagent, every time.

A probe plugin confirmed it. The injected text landed in the subagent's durable transcript. The model read it on the next turn. No API call, no bash tool that the model might skip, no cooperation required. The mechanism was hiding in plain sight.

## The bus

The hook is the injection point. We needed a transport.

Unix domain sockets need a process in an event loop. A subagent is a conversation, not a process. There is nothing to connect to. A broker daemon would need its own lifecycle and crash recovery. Named pipes have blocking semantics and no persistence.

We used a maildir. Three directories: `tmp`, `new`, `cur`. Write to `tmp`, sync, atomic rename into `new`. Reader atomically renames to `cur` when delivering. No locks, no partial reads, crash-safe since the 1990s.

```
~/.agent-memory/sessions/bus/
  qa-agent/
    tmp/       being written
    new/       pending delivery
    cur/       delivered, waiting for ack
    directives.json   sticky corrections
  _broadcast/
    new/       messages sent to all agents
```

A message is a JSON file with sender, kind, priority, and body. Sending writes to `tmp`, syncs, renames. About 200 microseconds. That speed is irrelevant because delivery is gated by the subagent's tool call cadence, not the wire.

Delivery is the plugin. On every `tool.execute.after`, the plugin checks the subagent's `new/` directory. Messages found are atomically moved to `cur/` and appended to the tool output:

```
⚠️ [SESSION-BUS] Messages:
  [cto→qa-agent]!! [correction]: Use bcrypt not sha256 for password hashing
```

For reasoning turns with no tool call, `chat.system.transform` injects standing directives. For urgent cases, a `halt` message rejects the pending tool call and replaces it with an error containing the correction. Sticky messages persist until explicitly acknowledged.

## The registry

Messaging requires knowing which sessions exist. Phase one was a passive registry: each session writes its state to a JSON file. Process ID, working directory, branch, locked files. Other sessions read it on startup.

This found real bugs in its own implementation: shell injection in the CLI scripts, a cleanup routine that deleted live sessions, PID reuse after reboot. All fixed with atomic writes, input validation, and boot ID tracking.

Phase two is the bus. Registry for discovery, bus for communication. Together they form a complete cross-session system with no daemons anywhere.

## What we tested

Ten subagents dispatched in parallel. All returned. OpenCode has no concurrent subagent limit. DeepSeek V4 Flash permits 2,500 concurrent requests. The practical ceiling is context management, not infrastructure.

A CTO directive sent to a test agent: "Run a council. Dispatch three subagents for database review, API analysis, and pipeline audit." The subagent read its inbox, parsed the directive, and asked if it should proceed.

Crash recovery: kill a session mid-write and the temp file is orphaned but harmless. Kill it after write but before delivery and the message sits in `new/` waiting. Kill it after delivery but before ack and the message re-delivers on next poll. At-least-once semantics with message ID dedup handle all three.

Cleanup: expired messages are garbage collected. Corrupt files are quarantined, not deleted. Broadcast messages fan out to all registered agents.

## What this opens up

The immediate use case is correcting a subagent mid-task. But the bus is general. A security auditor subagent can message the implementation subagent. A test runner can notify the deployer. The dashboard shows all active agents, their status, and pending messages.

The injection model works for any hook. You could inject into the system prompt, modify tool arguments before execution, or export environment variables for shell attribution. The transport is decoupled from the injection mechanism.

## The code

This repository. About 300 lines of Python for the core, 100 lines of TypeScript for the plugin, 100 lines for the MCP server. Built from architecture to integration test in one afternoon.

We opened a spec PR on OpenCode proposing that the `tool.execute.after` subagent behavior be documented and versioned so plugins can rely on it: [#41305](https://github.com/anomalyco/opencode/pull/41305). Two other contributors are already building session messaging transports. The hook contract is the missing piece.

## References

Maildir: Daniel J. Bernstein, qmail (1997). Atomic rename: POSIX `rename(2)`. OpenCode plugin API: v1.18.15 (2026). Claude Code agent teams: v2.1.224 (July 2026).
