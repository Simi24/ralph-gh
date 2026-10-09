import { expect, mock, test } from 'claude-code/testing'

// The guard's hooks as a session runs them: the engine's `$` raises tool.call through the guard,
// and the hooks beneath it stand for the engine (env, cwd, fs, and the tool itself).

const CWD = '/work/tree'
const OK = { result: 'ran' }
const ACTIVE = { RALPH_GUARD: '1' }

function engine(on: any, env: Record<string, string>, failEnv = false, links: Record<string, string> = {}) {
  if (failEnv) on('env.get', () => { throw new Error('boom') })
  else mock.env(on, env)
  on('session.cwd', () => ({ value: CWD }))
  on('fs.stat', (_$: unknown, e: { path: string }) => ({ value: { realPath: links[e.path] ?? e.path } }))
  on('tool.call', () => OK)
}

const caller = ($: any) => (input: object): Promise<any> => $.tool.call(input)

test('inert without RALPH_GUARD=1', async ($, on) => {
  engine(on, {})
  const result = await caller($)({ tool: 'Bash', command: 'git push origin feat/x' })
  expect(result.deny).toBeUndefined()
})

test('active: refuses a push and says what to do instead', async ($, on) => {
  engine(on, ACTIVE)
  const result = await caller($)({ tool: 'Bash', command: 'git push origin feat/x' })
  expect(result.deny).toBeDefined()
  expect(result.deny).toContain('RALPH:DONE')
})

test('active: lets read-only gh and in-worktree writes through, refuses outside writes', async ($, on) => {
  engine(on, ACTIVE)
  const call = caller($)
  expect((await call({ tool: 'Bash', command: 'gh issue view 3' })).deny).toBeUndefined()
  expect((await call({ tool: 'Write', file_path: 'src/a.py', content: 'x' })).deny).toBeUndefined()
  const outside = await call({ tool: 'Write', file_path: '../other/a.py', content: 'x' })
  expect(outside.deny).toBeDefined()
  expect(outside.deny).toContain('RALPH:BLOCKED')
  expect((await call({ tool: 'Write', file_path: '.git/config', content: 'x' })).deny).toBeDefined()
  expect((await call({ tool: 'Write', file_path: 'package.json', content: 'x' })).deny).toBeDefined()
})

test('active: extra roots and manifest edits are opt-in', async ($, on) => {
  engine(on, { ...ACTIVE, RALPH_GUARD_ROOTS: '/notes', RALPH_GUARD_ALLOW_MANIFESTS: '1' })
  const call = caller($)
  expect((await call({ tool: 'Write', file_path: '/notes/x.md', content: 'x' })).deny).toBeUndefined()
  expect((await call({ tool: 'Write', file_path: 'package.json', content: 'x' })).deny).toBeUndefined()
})

test('a guard that fails refuses the call (fail closed)', async ($, on) => {
  engine(on, ACTIVE, true)
  const result = await caller($)({ tool: 'Bash', command: 'ls' })
  expect(result.deny).toBeDefined()
  expect(result.deny).toContain('fail closed')
})

test('active: a symlink that leads outside the worktree is refused', async ($, on) => {
  engine(on, ACTIVE, false, { '/work/tree/link/a.py': '/home/me/a.py' })
  const result = await caller($)({ tool: 'Write', file_path: 'link/a.py', content: 'x' })
  expect(result.deny).toContain('outside the worktree')
})
