# Living Without Daemons: Cross-Session Messaging for AI Coding Agents

A maildir spool and plugin hooks. No sockets, no servers, no model cooperation required.

---

## The Problem

Claude Code added something useful in mid-2026: agent teams. You can open two terminal windows, start two independent Claude Code sessions, and they can message each other. The lead agent can see what its teammates are doing in real time. If one goes off track, you send a correction mid-task. No restarting, no context loss.

OpenCode, the agent framework we run, has no such thing. You can dispatch subagents inside a session, sure. The orchestrator spawns workers, waits for results, synthesizes. But those subagents are fire and forget. They run, they finish, they report back. You cannot correct one mid-flight. You cannot send a message from one terminal session to another. The only communication channel is the orchestrator, and the orchestrator is blocked inside `task()` until the subagent is done.

We wanted this feature. Here is how we built it.

## What We Found

The subagent model in OpenCode is not what most people assume. Subagents are not separate processes. They are conversations inside the parent session's process tree. When you call `task(subagent_type="qa", prompt="review this")`, OpenCode does not fork. It creates a new row in its SQLite `session` table, stamps a `parent_id`, and runs the conversation in the same node process.

This turned out to be the key. Because subagents share the parent's process, they also share the parent's plugin hooks.

OpenCode has a plugin system. Plugins register callbacks that fire at specific moments: when a tool executes, when the system prompt is built, when a shell command runs. The one that matters is `tool.execute.after`. It fires after every tool call in every session, subagents included. It receives the session ID, the tool name, and the output text. And it can mutate that output before the language model sees it.

This means you can splice a message into a running subagent's context. Deterministically. No API call, no bash tool that the model might skip, no cooperation from the LLM. The hook fires, the text is appended, the model reads it on its next turn.

The assumption that blocked earlier attempts, recorded in our architecture decision log as "mid-turn injection is not available in OpenCode," was wrong. The mechanism was there the whole time.

## The Architecture

We needed a transport. Something to carry messages from a CTO session to a subagent's inbox, where the plugin hook would find them and inject them.

The obvious choices were wrong. Unix domain sockets require a process in an event loop. A subagent is a conversation, not a process. There is nothing to connect to. A broker daemon would need its own lifecycle, crash recovery, authentication. Named pipes have blocking semantics and no persistence.

We used a maildir.

A maildir is a directory layout invented for email servers in the 1990s. It has three subdirectories: `tmp`, `new`, and `cur`. You write a message to a temp file in `tmp`, sync it, and atomically rename it into `new`. The reader atomically renames it from `new` to `cur` when delivering. No locks, no partial reads, crash-safe by construction. Forty years of Qmail and Postfix have proven this pattern.

Our bus layout:

```
~/.agent-memory/sessions/bus/
  qa-agent/
    tmp/       messages being written
    new/       pending delivery
    cur/       delivered, waiting for acknowledgement
    directives.json   sticky corrections
  _broadcast/
    new/       messages sent to all agents
```

A message is a JSON file: who sent it, what kind (note, correction, halt), how urgent, whether it sticks until acknowledged, and the body text.

Sending is a single function call. The Python library writes to `tmp`, syncs, renames into `new`. About 200 microseconds on an NVMe drive. That speed is irrelevant because delivery is gated by the subagent's tool call cadence, not by the wire. A subagent that runs a 30 second build will not see the message for 30 seconds regardless of transport latency.

Delivery is the plugin. On every `tool.execute.after`, the plugin checks the subagent's `new/` directory. If there are messages, it atomically moves them to `cur/` and appends a formatted block to the tool output. The model sees it on its next reasoning step:

```
⚠️ [SESSION-BUS] Messages:
  [cto→qa-agent]!! [correction]: Use bcrypt not sha256 for password hashing
```

For pure reasoning turns where no tool fires, the `chat.system.transform` hook injects a standing directive into the system prompt. And for urgent cases where the agent is about to do something destructive, a `halt` message can reject the pending tool call entirely, replacing it with an error containing the correction.

Sticky messages persist in `directives.json` until the agent explicitly acknowledges them. This prevents corrections from being forgotten across turns.

## The Two-Phase Discovery

Cross-session messaging requires knowing which sessions exist. We built this in two phases.

Phase one was the session registry. A passive, file-based directory where each session writes its state: process ID, working directory, Git branch, description, locked files. Other sessions read it on startup. No live messaging, just discovery and conflict detection. Sessions poll, they do not push.

This already caught real problems. Our QA agent found that the initial implementation had a shell injection vulnerability in the CLI scripts, a cleanup routine that deleted live sessions, and a PID reuse bug that would misidentify stale entries after a reboot. All fixed with atomic writes, input validation, and boot ID tracking.

Phase two is the bus described above. The registry handles discovery. The bus handles communication. Together they form a complete cross-session awareness system with zero daemons.

## What We Tested

We dispatched 10 subagents in parallel. All returned. OpenCode has no limit on concurrent subagents beyond your API provider's rate limits. DeepSeek V4 Flash allows 2,500 concurrent requests. The practical ceiling is context management, not infrastructure.

We sent a CTO directive to a test agent: "Run a council. Dispatch 3 subagents for database schema review, API rate limiting analysis, and deployment pipeline audit." The subagent read its inbox, parsed the directive, and asked if it should proceed.

We tested crash recovery. Kill a session mid-write. The temp file in `tmp/` is orphaned but harmless. Kill it after write but before delivery. The message sits in `new/`, waiting for the next reader. Kill it after delivery but before acknowledgement. The message is in `cur/` and will be redelivered on the next poll. The at-least-once semantics with message ID deduplication handle all three cases.

We tested cleanup. Messages older than their TTL are garbage collected. Corrupt JSON files are quarantined, not deleted, preserving evidence for debugging. Broadcast messages fan out to all registered agents.

## What This Enables

The immediate use case is the human operator correcting a subagent mid-task. But the bus is general. Agents can message each other. A security auditor subagent can send a finding to the implementation subagent. A test runner can notify the deployer that checks passed. The CTO dashboard can show all active agents, their status, and pending messages.

The plugin injection model works for any hook, not just `tool.execute.after`. You could inject context into the system prompt, modify tool arguments before execution, or export environment variables for shell attribution. The bus transport is decoupled from the injection mechanism, so you can add new delivery channels without changing the spool.

## The Code

The code is in this repository. About 300 lines of Python for the core library, 100 lines of TypeScript for the plugin, and 100 lines of Python for the MCP server. Total build time from architecture to integration test: one afternoon.

## References

The maildir specification was first described in Daniel J. Bernstein's qmail documentation (1997). The atomic rename guarantee comes from POSIX `rename(2)`. The plugin hook architecture is documented in OpenCode's plugin API (v1.18.15, 2026). Claude Code's agent teams feature was released in version 2.1.224 (July 2026) as part of their cross-session messaging system.
