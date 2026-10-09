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

// `claude plugin test` loads the hooks module of the folder it runs in. From the repo root that is
// the dashboard, not the guard, so a guard test stands down there; `claude plugin test worker-bundle` runs it.
// An active guard refuses a push, and so does one that fails closed.
async function guardLoaded($: any): Promise<boolean> {
  const result = await caller($)({ tool: 'Bash', command: 'git push origin feat/x' })
  return result.deny !== undefined
}

/** A test that runs only where the ralph-guard hooks are the loaded module. */
function guardTest(name: string, setup: (on: any) => void, body: ($: any) => Promise<void>) {
  test(name, async ($, on) => {
    setup(on)
    if (await guardLoaded($)) await body($)
  })
}

test('inert without RALPH_GUARD=1', async ($, on) => {
  engine(on, {})
  const result = await caller($)({ tool: 'Bash', command: 'git push origin feat/x' })
  expect(result.deny).toBeUndefined()
})

guardTest('active: refuses a push and says what to do instead', on => engine(on, ACTIVE), async $ => {
  const result = await caller($)({ tool: 'Bash', command: 'git push origin feat/x' })
  expect(result.deny).toBeDefined()
  expect(result.deny).toContain('RALPH:DONE')
})

guardTest('active: lets read-only gh and in-worktree writes through, refuses outside writes', on => engine(on, ACTIVE), async $ => {
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

guardTest('a guard that fails refuses the call (fail closed)', on => engine(on, ACTIVE, true), async $ => {
  const result = await caller($)({ tool: 'Bash', command: 'ls' })
  expect(result.deny).toBeDefined()
  expect(result.deny).toContain('fail closed')
})

guardTest('active: a symlink that leads outside the worktree is refused', on => engine(on, ACTIVE, false, { '/work/tree/link/a.py': '/home/me/a.py' }), async $ => {
  const result = await caller($)({ tool: 'Write', file_path: 'link/a.py', content: 'x' })
  expect(result.deny).toContain('outside the worktree')
})
