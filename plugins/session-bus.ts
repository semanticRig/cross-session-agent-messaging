/**
 * session-bus.ts — OpenCode Plugin for CTO↔Subagent Live Messaging
 *
 * Injects pending messages into subagent context via:
 *   1. tool.execute.after — mid-turn injection (primary)
 *   2. chat.system.transform — turn-boundary injection (secondary)
 *   3. shell.env — exports BUS_AGENT_ID for reply attribution
 *
 * Zero daemon. Messages stored in maildir spool at ~/.agent-memory/sessions/bus/
 */

import { readFileSync, existsSync, readdirSync, renameSync, unlinkSync, mkdirSync, writeFileSync } from "node:fs"
import { join, dirname } from "node:path"
import { homedir } from "node:os"

const BUS_ROOT = process.env.SESSION_BUS_ROOT || join(homedir(), ".agent-memory/sessions/bus")
const MAX_BODY_BYTES = 2048

interface BusMessage {
  id: string
  from: string
  to: string
  kind: string  // "note" | "correction" | "halt" | "request" | "reply"
  priority: string  // "normal" | "urgent" | "low"
  sticky: boolean
  ttl_seconds: number
  reply_to: string
  body: string
  created_at: string
  delivered_at: string
}

/** Read and deliver pending messages for an agent (maildir: new → cur) */
function deliverMessages(agentId: string, maxMsgs: number = 3): BusMessage[] {
  const newDir = join(BUS_ROOT, agentId, "new")
  const curDir = join(BUS_ROOT, agentId, "cur")

  if (!existsSync(newDir)) return []
  mkdirSync(curDir, { recursive: true })

  const files = readdirSync(newDir)
    .filter(f => f.endsWith(".json"))
    .sort()
    .slice(0, maxMsgs)

  const delivered: BusMessage[] = []
  for (const f of files) {
    const src = join(newDir, f)
    const dst = join(curDir, f)
    try {
      const raw = readFileSync(src, "utf-8")
      const msg: BusMessage = JSON.parse(raw)

      // Check expiry
      if (msg.created_at) {
        const age = (Date.now() - new Date(msg.created_at).getTime()) / 1000
        if (age > (msg.ttl_seconds || 900)) {
          unlinkSync(src)
          continue
        }
      }

      // Truncate long bodies
      if (Buffer.byteLength(msg.body, "utf-8") > MAX_BODY_BYTES) {
        msg.body = msg.body.slice(0, MAX_BODY_BYTES) + "…[truncated]"
      }

      renameSync(src, dst)
      msg.delivered_at = new Date().toISOString()
      delivered.push(msg)
    } catch {
      // Corrupt: quarantine
      const qdir = join(BUS_ROOT, agentId, ".quarantine")
      mkdirSync(qdir, { recursive: true })
      try { renameSync(src, join(qdir, f + ".corrupt")) } catch {}
    }
  }

  return delivered
}

/** Read sticky directives */
function getStickyDirectives(agentId: string): string[] {
  const path = join(BUS_ROOT, agentId, "directives.json")
  if (!existsSync(path)) return []
  try {
    const dirs = JSON.parse(readFileSync(path, "utf-8"))
    return Object.values(dirs).map((d: any) => d.body as string)
  } catch { return [] }
}

/** Format messages for context injection */
function formatInjection(msgs: BusMessage[], directives: string[]): string {
  const parts: string[] = []
  if (msgs.length > 0) {
    parts.push("\n⚠️ [SESSION-BUS] Messages:")
    for (const m of msgs) {
      const prio = m.priority === "urgent" ? "!!!" : m.priority === "low" ? "·" : ""
      parts.push(`  [${m.from}→${m.to}]${prio} [${m.kind}]: ${m.body}`)
    }
  }
  if (directives.length > 0) {
    parts.push("\n📌 [SESSION-BUS] Standing directives:")
    for (const d of directives) {
      parts.push(`  • ${d}`)
    }
  }
  return parts.join("\n")
}

// ---------------------------------------------------------------------------
// Plugin hooks
// ---------------------------------------------------------------------------

export default {
  name: "session-bus",
  version: "1.0.0",
  description: "Live CTO↔agent messaging via maildir bus",

  hooks: {
    /** MID-TURN: Inject messages into tool results */
    "tool.execute.after": async (input: any, output: any) => {
      try {
        const agentId = input.sessionID || input.session_id || ""
        if (!agentId) return output

        const msgs = deliverMessages(agentId)
        const dirs = getStickyDirectives(agentId)
        const injection = formatInjection(msgs, dirs)

        if (injection) {
          const text = typeof output === "string" ? output : output?.text || output?.content || JSON.stringify(output)
          return text + injection
        }
        return output
      } catch {
        return output  // fail open — never break the agent
      }
    },

    /** TURN BOUNDARY: Inject into system prompt */
    "experimental.chat.system.transform": async (system: string[]) => {
      try {
        // We don't know agentId at system.transform time (no sessionID easily available)
        // So we inject a general directive to check the bus
        system.push("[BUS] Check your session bus inbox periodically. " +
          "Use bash to read ~/.agent-memory/sessions/bus/<your-session-id>/new/ for pending CTO messages.")
        return system
      } catch {
        return system
      }
    },

    /** Export BUS_AGENT_ID into shell environment for reply attribution */
    "shell.env": async (env: Record<string, string>, input: any) => {
      try {
        env["BUS_AGENT_ID"] = input.sessionID || ""
        env["BUS_ROOT"] = BUS_ROOT
      } catch {}
      return env
    },
  },
}
