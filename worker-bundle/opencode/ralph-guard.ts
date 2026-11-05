import { realpathSync, writeFileSync } from 'node:fs'

import { absolute, checkBash, checkWrite, opencodeWrites } from '../hooks/rules'

/**
 * ralph-guard for OpenCode: the same hard limits as the Claude hooks (`../hooks/register.ts`),
 * as an OpenCode plugin. The conductor loads it through OPENCODE_CONFIG_CONTENT in every
 * `opencode run` worker session and turns it on with RALPH_GUARD=1; anywhere else it stays out
 * of the way. A refusal throws, so OpenCode reports the reason to the model instead of running
 * the tool. Extra writable roots come from RALPH_GUARD_ROOTS, manifests are allowed with
 * RALPH_GUARD_ALLOW_MANIFESTS=1. Every guard fails closed.
 *
 * OpenCode skips a plugin that fails to load and runs the session anyway, so on load the plugin
 * writes the conductor's nonce (RALPH_GUARD_OPENCODE_NONCE) to RALPH_GUARD_OPENCODE_READY: a
 * session without it is discarded by the conductor.
 */

const FAILED = 'the guard itself failed, so the call is refused (fail closed)'

/** The real path of `path`, resolving symlinks through the deepest ancestor that exists. */
function realPath(path: string): string {
  let head = path
  let tail = ''
  for (;;) {
    try {
      const real = realpathSync(head)
      return tail ? `${real}/${tail}` : real
    } catch {
      if (head === '/') return path
      const cut = head.lastIndexOf('/')
      tail = tail ? `${head.slice(cut + 1)}/${tail}` : head.slice(cut + 1)
      head = cut <= 0 ? '/' : head.slice(0, cut)
    }
  }
}

function check(tool: string, args: Record<string, unknown>, cwd: string): string | undefined {
  if (tool === 'bash') return checkBash(String(args.command ?? ''))
  const targets = opencodeWrites(tool, args)
  if (targets === undefined) return undefined
  if (targets.length === 0) return 'no file path to check'
  const extra = (process.env.RALPH_GUARD_ROOTS ?? '').split(':').filter(Boolean)
  const roots = [cwd, ...extra].map(root => realPath(absolute(root, cwd)))
  const allowManifests = process.env.RALPH_GUARD_ALLOW_MANIFESTS === '1'
  for (const target of targets) {
    const reason = checkWrite(realPath(absolute(target, cwd)), roots, allowManifests)
    if (reason) return reason
  }
  return undefined
}

type Call = { tool: string }
type Args = { args?: Record<string, unknown> }

export const RalphGuard = async ({ directory }: { directory: string }) => {
  const ready = process.env.RALPH_GUARD_OPENCODE_READY
  if (process.env.RALPH_GUARD === '1' && ready) writeFileSync(ready, process.env.RALPH_GUARD_OPENCODE_NONCE ?? '')
  return { 'tool.execute.before': before(directory) }
}

function before(directory: string) {
  return async (input: Call, output: Args): Promise<void> => {
    if (process.env.RALPH_GUARD !== '1') return
    let reason: string | undefined
    try {
      reason = check(input.tool, output.args ?? {}, directory)
    } catch {
      reason = FAILED
    }
    if (reason) throw new Error(`ralph-guard: ${reason}`)
  }
}
