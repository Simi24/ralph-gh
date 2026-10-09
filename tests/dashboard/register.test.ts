import { expect, mock, test } from 'claude-code/testing'

const LOG_ONE = `2026-10-08T18:06:57+02:00 ralph-gh session: s1
2026-10-08T18:06:57+02:00 PRD: #1
2026-10-08T18:07:34+02:00 [status] #2 dispatched
`
const LOG_TWO = `${LOG_ONE}2026-10-08T18:08:23+02:00 [status] #2 integrated
`
const DIR = '/cfg/ralph-gh/state/o__r'

/** Hooks beneath the plugin: a log that can grow, and a record of every file read, write and process run. */
function world(on: any, ghWorks = true, hold?: { gate?: Promise<void> }) {
  const seen = { reads: [] as string[], writes: [] as string[], runs: [] as string[][], toasts: [] as string[], status: [] as unknown[] }
  const files = { log: LOG_ONE, current: '' }
  mock.env(on, { CLAUDE_CONFIG_DIR: '/cfg', HOME: '/home' })
  const clock = mock.clock(on, { now: Date.parse('2026-10-08T18:10:00+02:00') })
  on('fs.exists', async (_$: any, e: any) => ({ value: e.path.endsWith('/run.log') }))
  on('fs.read', async (_$: any, e: any) => {
    seen.reads.push(e.path)
    return { value: files.log }
  })
  on('fs.write', async (_$: any, e: any) => {
    seen.writes.push(e.path)
    return { value: undefined }
  })
  on('process.run', async (_$: any, e: any) => {
    seen.runs.push(e.argv)
    await hold?.gate
    const url = e.argv.find((a: string) => a.startsWith('repos/')) ?? ''
    const out = e.argv[1] === 'repo' ? files.current : url.endsWith('/sub_issues?per_page=100') ? '[]' : url.includes('/issues/1') ? 'PRD: demo' : ''
    return { value: { exitCode: ghWorks ? 0 : 1, stdout: ghWorks ? out : '', stderr: '' } }
  })
  on('ui.toast', async (_$: any, e: any) => {
    seen.toasts.push(e.text)
    return { value: undefined }
  })
  on('ui.status', async (_$: any, e: any) => {
    seen.status.push(e.text)
    return { value: undefined }
  })
  on('ui.open', async () => ({ value: { isPlaced: true } }))
  on('ui.close', async () => ({ value: undefined }))
  on('session.start', async (_$: any, e: any) => ({ cwd: e.cwd }))
  on('command.register', async () => ({ value: { command: 'ralph' } }))
  return { seen, files, clock }
}

test('a session start only registers /ralph: no timer, no process, no file read', async ($, on) => {
  const w = world(on)
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true } as never)
  await w.clock.advance(60_000)
  expect(w.seen.reads).toEqual([])
  expect(w.seen.runs).toEqual([])
  expect(w.seen.writes).toEqual([])
})

test('/ralph starts watching and /ralph off cancels the timer and clears the status line', async ($, on) => {
  const w = world(on)
  await $.command.run({ command: 'ralph', args: 'o/r' } as never)
  const readsOpen = w.seen.reads.length
  await w.clock.advance(3000)
  expect(w.seen.reads.length).toBeGreaterThan(readsOpen) // the timer polls the local log
  await $.command.run({ command: 'ralph', args: 'off' } as never)
  const readsOff = w.seen.reads.length
  await w.clock.advance(30_000)
  expect(w.seen.reads.length).toBe(readsOff)
  expect(w.seen.status.at(-1)).toBeUndefined()
})

/** Another plugin closing the pane: its `$.ui.close` raises `ui.close` through every hook, as a person's close does. */
const CLOSER = {
  name: 'closer',
  register(on: any) {
    on('command.run', { command: 'closepane' }, async ($: any) => {
      await $.ui.close({ id: 'ralph-board' })
      return { text: 'closed' }
    })
  },
}

test('closing the pane cancels every timer and clears the status line', { plugins: [CLOSER] }, async ($, on) => {
  const w = world(on)
  await $.command.run({ command: 'ralph', args: 'o/r' } as never)
  await $.command.run({ command: 'closepane', args: '' } as never)
  const reads = w.seen.reads.length
  await w.clock.advance(30_000)
  expect(w.seen.reads.length).toBe(reads)
  expect(w.seen.status.at(-1)).toBeUndefined()
})

test('a bare /ralph after /ralph o/other returns to the current repo', async ($, on) => {
  const w = world(on)
  w.files.current = 'o/cur'
  await $.command.run({ command: 'ralph', args: 'o/other' } as never)
  await $.command.run({ command: 'ralph', args: 'off' } as never)
  w.seen.reads.length = 0
  await $.command.run({ command: 'ralph', args: '' } as never)
  expect(w.seen.reads).toContain('/cfg/ralph-gh/state/o__cur/run.log')
  expect(w.seen.reads).not.toContain('/cfg/ralph-gh/state/o__other/run.log')
})

test('GitHub is read on open and on a new log event, never on an unchanged poll', async ($, on) => {
  const w = world(on)
  await $.command.run({ command: 'ralph', args: 'o/r' } as never)
  const afterOpen = w.seen.runs.length
  expect(afterOpen).toBeGreaterThan(0)
  await w.clock.advance(9000) // three polls, the log is the same
  expect(w.seen.runs.length).toBe(afterOpen)
  w.files.log = LOG_TWO
  await w.clock.advance(3000)
  expect(w.seen.runs.length).toBeGreaterThan(afterOpen)
  const afterEvent = w.seen.runs.length
  await w.clock.advance(9000)
  expect(w.seen.runs.length).toBe(afterEvent)
})

test('every gh call is a read-only GET', async ($, on) => {
  const w = world(on)
  await $.command.run({ command: 'ralph', args: 'o/r' } as never)
  const gh = w.seen.runs.filter(argv => argv[1] === 'api')
  expect(gh.length).toBeGreaterThan(0)
  for (const argv of gh) expect(argv.join(' ')).not.toMatch(/(^| )(-X|--method|-f|-F|--field|--raw-field|--input)( |$)/)
})

test('Drain writes the STOP file of the watched repo and nothing else', async ($, on) => {
  const w = world(on)
  await $.command.run({ command: 'ralph', args: 'o/r' } as never)
  const pane = await $.ui.mount({ plugin: 'ralph-gh', surface: 'terminal', component: 'Pane', props: {} as never, requestId: 'ralph-board' })
  await pane.press({ key: 'drain' })
  expect(w.seen.writes).toEqual([`${DIR}/STOP`])
})

test('without GitHub data the pane still draws from run.log alone', async ($, on) => {
  const w = world(on, false)
  await $.command.run({ command: 'ralph', args: 'o/r' } as never)
  const pane = await $.ui.mount({ plugin: 'ralph-gh', surface: 'terminal', component: 'Pane', props: {} as never, requestId: 'ralph-board' })
  expect(await pane.find({ text: /#2/ })).toBeDefined()
  expect(w.seen.writes).toEqual([])
})

/** A poll whose `gh` call is held while the watch ends, then released: it must not touch the status line or toasts. */
async function lateAfter(end: ($: any) => Promise<unknown>, $: any, on: any) {
  const hold: { gate?: Promise<void> } = {}
  const w = world(on, true, hold)
  await $.command.run({ command: 'ralph', args: 'o/r' } as never)
  w.files.log = LOG_TWO
  let release = () => {}
  hold.gate = new Promise<void>(resolve => (release = resolve))
  await w.clock.advance(3000) // the poll sees the new event and waits on gh
  await end($)
  const toasts = w.seen.toasts.length
  release()
  await w.clock.advance(3000)
  expect(w.seen.status.at(-1)).toBeUndefined()
  expect(w.seen.toasts.length).toBe(toasts)
}

test('a poll still waiting on gh when /ralph off runs sets no status and no toast', async ($, on) => {
  await lateAfter($ => $.command.run({ command: 'ralph', args: 'off' } as never), $, on)
})

test('a poll still waiting on gh when the pane closes sets no status and no toast', { plugins: [CLOSER] }, async ($, on) => {
  await lateAfter($ => $.command.run({ command: 'closepane', args: '' } as never), $, on)
})
