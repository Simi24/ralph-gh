import type { Engine, Register } from 'claude-code'

import { absolute, checkBash, checkWrite } from './rules'

/**
 * ralph-guard: hard limits for the worker sessions ralph-gh starts (`claude --print`).
 *
 * The conductor loads it with `--plugin-dir` and turns it on with RALPH_GUARD=1 in the
 * session's environment; anywhere else it stays out of the way. Extra writable roots come
 * from RALPH_GUARD_ROOTS (':'-separated, e.g. the exploration notes dir), dependency
 * manifests are allowed with RALPH_GUARD_ALLOW_MANIFESTS=1. Every guard fails closed.
 */

const FAILED = { deny: 'ralph-guard: the guard itself failed, so the call is refused (fail closed)' }

async function isActive($: Engine): Promise<boolean> {
  return (await $.env.get('RALPH_GUARD')) === '1'
}

/** The real path of `path`, resolving symlinks through the deepest ancestor that exists. */
async function realPath($: Engine, path: string): Promise<string> {
  let head = path
  let tail = ''
  for (;;) {
    try {
      const stat = await $.fs.stat(head, { resolve: true })
      const real = stat.realPath ?? head
      return tail ? `${real}/${tail}` : real
    } catch {
      if (head === '/') return path
      const cut = head.lastIndexOf('/')
      tail = tail ? `${head.slice(cut + 1)}/${tail}` : head.slice(cut + 1)
      head = cut <= 0 ? '/' : head.slice(0, cut)
    }
  }
}

async function writableRoots($: Engine, cwd: string): Promise<string[]> {
  const extra = ((await $.env.get('RALPH_GUARD_ROOTS')) ?? '').split(':').filter(Boolean)
  return Promise.all([cwd, ...extra].map(root => realPath($, absolute(root, cwd))))
}

export const register: Register = on => {
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    if (!(await isActive($))) return next(e)
    const reason = checkBash(String((e as { command?: unknown }).command ?? ''))
    return reason ? { deny: `ralph-guard: ${reason}` } : next(e)
  }).catch(($, e, next) => (next.called ? next(e) : FAILED))

  on('tool.call', { tool: ['Write', 'Edit', 'NotebookEdit'] }, async ($, e, next) => {
    if (!(await isActive($))) return next(e)
    const input = e as { file_path?: unknown; notebook_path?: unknown }
    const target = String(input.file_path ?? input.notebook_path ?? '')
    if (!target) return { deny: 'ralph-guard: no file path to check' }
    const cwd = await $.session.cwd()
    const path = await realPath($, absolute(target, cwd))
    const allowManifests = (await $.env.get('RALPH_GUARD_ALLOW_MANIFESTS')) === '1'
    const reason = checkWrite(path, await writableRoots($, cwd), allowManifests)
    return reason ? { deny: `ralph-guard: ${reason}` } : next(e)
  }).catch(($, e, next) => (next.called ? next(e) : FAILED))
}
