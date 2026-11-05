import { mkdirSync, mkdtempSync, realpathSync, symlinkSync } from 'node:fs'
import { tmpdir } from 'node:os'

import { expect, test } from 'bun:test'

import { RalphGuard } from './ralph-guard'

const worktree = realpathSync(mkdtempSync(`${tmpdir()}/ralph-oc-`))
mkdirSync(`${worktree}/src`)
symlinkSync('/etc', `${worktree}/escape`)

async function call(tool: string, args: Record<string, unknown>, env: Record<string, string> = { RALPH_GUARD: '1' }) {
  const saved = { ...process.env }
  delete process.env.RALPH_GUARD_ROOTS
  delete process.env.RALPH_GUARD_ALLOW_MANIFESTS
  Object.assign(process.env, env)
  try {
    const hooks = await RalphGuard({ directory: worktree })
    await hooks['tool.execute.before']({ tool }, { args })
    return undefined
  } catch (e) {
    return String((e as Error).message)
  } finally {
    for (const key of Object.keys(process.env)) if (!(key in saved)) delete process.env[key]
    Object.assign(process.env, saved)
  }
}

test('inert without RALPH_GUARD=1', async () => {
  expect(await call('bash', { command: 'git push origin main' }, {})).toBeUndefined()
  expect(await call('write', { filePath: '/etc/x' }, { RALPH_GUARD: '0' })).toBeUndefined()
})

test('refuses pushes and GitHub writes from bash, lets the rest through', async () => {
  expect(await call('bash', { command: 'git push origin feat/x' })).toMatch(/^ralph-guard: only the conductor pushes/)
  expect(await call('bash', { command: 'gh pr merge 4' })).toMatch(/^ralph-guard: GitHub writes/)
  expect(await call('bash', { command: 'python3 -m unittest' })).toBeUndefined()
})

test('writes stay inside the worktree, out of .git and away from manifests', async () => {
  expect(await call('write', { filePath: `${worktree}/src/a.py` })).toBeUndefined()
  expect(await call('edit', { filePath: 'src/a.py' })).toBeUndefined()
  expect(await call('write', { filePath: '/tmp/outside.txt' })).toMatch(/outside the worktree/)
  expect(await call('write', { filePath: 'src/../../evil.py' })).toMatch(/outside the worktree/)
  expect(await call('write', { filePath: `${worktree}/escape/hosts` })).toMatch(/outside the worktree/)
  expect(await call('edit', { filePath: `${worktree}/.git/config` })).toMatch(/\.git/)
  expect(await call('write', { filePath: 'package.json' })).toMatch(/dependency manifests/)
  expect(await call('write', { filePath: 'package.json' }, { RALPH_GUARD: '1', RALPH_GUARD_ALLOW_MANIFESTS: '1' })).toBeUndefined()
})

test('extra roots come from RALPH_GUARD_ROOTS', async () => {
  const notes = realpathSync(mkdtempSync(`${tmpdir()}/ralph-notes-`))
  expect(await call('write', { filePath: `${notes}/n.md` })).toMatch(/outside the worktree/)
  expect(await call('write', { filePath: `${notes}/n.md` }, { RALPH_GUARD: '1', RALPH_GUARD_ROOTS: notes })).toBeUndefined()
})

test('a patch is refused when any file it touches is', async () => {
  const patch = (lines: string[]) => ['*** Begin Patch', ...lines, '*** End Patch'].join('\n')
  expect(await call('apply_patch', { patchText: patch(['*** Add File: src/b.py', '+x']) })).toBeUndefined()
  expect(await call('apply_patch', { patchText: patch(['*** Add File: src/b.py', '+x', '*** Delete File: /etc/hosts']) })).toMatch(/outside the worktree/)
  expect(await call('apply_patch', { patchText: patch(['*** Update File: src/b.py', '*** Move to: ../x.py', '@@']) })).toMatch(/outside the worktree/)
})

test('fails closed on a file tool that names no file', async () => {
  expect(await call('write', {})).toMatch(/no file path to check/)
  expect(await call('apply_patch', { patchText: 'garbage' })).toMatch(/no file path to check/)
})

test('tools that write nothing pass', async () => {
  expect(await call('read', { filePath: '/etc/hosts' })).toBeUndefined()
  expect(await call('grep', { pattern: 'x' })).toBeUndefined()
})
