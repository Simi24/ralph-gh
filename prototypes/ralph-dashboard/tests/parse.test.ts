import { expect, test } from 'claude-code/testing'

import { elapsed, notable, parseRun, since, summary } from '../hooks/parse'

const OLD = `2026-10-07T10:00:00+02:00 ralph-gh session: ralph-old
2026-10-07T10:00:01+02:00 [status] #9 dispatched`

const RUN = `${OLD}
2026-10-08T18:06:57+02:00 ralph-gh session: ralph-1791475617
2026-10-08T18:06:57+02:00 PRD: #1
2026-10-08T18:07:34+02:00 [status] #2 dispatched
2026-10-08T18:08:01+02:00 [status] #2 PR #4 opened (feat/1-ticket-2 -> feat/1-prd-greeting-module)
2026-10-08T18:08:23+02:00 [status] #2 integrated
2026-10-08T18:08:28+02:00 [status] PRD PR #5 opened (feat/1-prd-greeting-module -> main)
2026-10-08T18:08:32+02:00 [status] PRD integration verify green on b0b1708
2026-10-08T18:08:36+02:00 [status] #3 dispatched
2026-10-08T18:09:12+02:00 [status] #3 ticket-gate session started`

test('reads only the last run, with ticket rows, PRs and the integration state', () => {
  const board = parseRun(RUN)!
  expect(board.session).toBe('ralph-1791475617')
  expect(board.prd).toBe(1)
  expect(board.tickets.map(t => [t.number, t.tone, t.pr])).toEqual([
    [2, 'ok', 4],
    [3, 'run', undefined],
  ])
  expect(board.finalPr).toBe(5)
  expect(board.integrationTone).toBe('ok')
  expect(board.ended).toBeUndefined()
  expect(summary(board)).toContain('1/2 integrated')
})

test('an ended run carries its reason', () => {
  const board = parseRun(`${RUN}
2026-10-08T18:10:54+02:00 [status] PRD run ended: final review passed, merge withheld: autonomy=halt-each-pr
2026-10-08T18:10:55+02:00 ralph-gh exited: final review passed, merge withheld: autonomy=halt-each-pr`)!
  expect(board.ended).toBe('final review passed, merge withheld: autonomy=halt-each-pr')
})

test('a red integration branch and a failed ticket are bad', () => {
  const board = parseRun(`${RUN}
2026-10-08T18:09:40+02:00 [status] PRD integration verify RED on c4ade49
2026-10-08T18:09:41+02:00 [status] #3 failed: the ticket gate failed after 1 fix round(s).`)!
  expect(board.integrationTone).toBe('bad')
  expect(board.tickets.find(t => t.number === 3)?.tone).toBe('bad')
})

test('each ticket walks the pipeline: claim, implement, verify, PR, gate, merge, done', () => {
  const at = (n: number, text: string) => `2026-10-09T10:0${n}:00+02:00 [status] #8 ${text}`
  const steps = [
    'dispatched', 'implementer session started', 'verify started', 'PR #11 opened (a -> b)',
    'ticket-gate session started', 'PR #11 merged (merge)', 'integrated',
  ]
  steps.forEach((text, i) => {
    const log = ['x ralph-gh session: s', ...steps.slice(0, i + 1).map((t, j) => at(j, t))].join('\n')
    expect(parseRun(log)!.tickets[0].stage).toBe(i)
  })
  const done = parseRun(['x ralph-gh session: s', ...steps.map((t, j) => at(j, t))].join('\n'))!
  expect(done.tickets[0].tone).toBe('ok')
  expect(elapsed(done.tickets[0].firstAt, done.tickets[0].at)).toBe('6m00s')
})

test('a fix round never moves a ticket backwards', () => {
  const log = [
    'x ralph-gh session: s',
    '2026-10-09T10:00:00+02:00 [status] #8 ticket-gate session started',
    '2026-10-09T10:01:00+02:00 [status] #8 fix session started',
    '2026-10-09T10:02:00+02:00 [status] #8 verify started',
  ].join('\n')
  expect(parseRun(log)!.tickets[0].stage).toBe(4)
})

test('the PRD walks explore, tickets, final review, done', () => {
  expect(parseRun(RUN)!.prdStage).toBe(1)
  const ended = parseRun(`${RUN}
2026-10-08T18:09:40+02:00 [status] PRD final-review session started
2026-10-08T18:10:54+02:00 [status] PRD run ended: final review passed, merge withheld: autonomy=halt-each-pr`)!
  expect(ended.prdStage).toBe(3)
  expect(ended.endedTone).toBe('ok')
})

test('a running step shows the time since its last event, not a frozen gap', () => {
  const at = '2026-10-09T10:34:05+02:00'
  expect(since(at, Date.parse('2026-10-09T10:41:17+02:00'))).toBe('7m12s')
  expect(since(at, Date.parse('2026-10-09T12:04:05+02:00'))).toBe('1h30m')
  expect(since(at, Date.parse('2026-10-09T10:34:00+02:00'))).toBe('') // a clock behind the log reads as nothing
})

test('the final review reads as rounds, a verdict and what runs now', () => {
  const at = (t: string, text: string) => `2026-10-09T${t}+02:00 [status] PRD ${text}`
  const head = ['x ralph-gh session: s', 'x PRD: #69']
  const after = (...lines: string[]) => parseRun([...head, ...lines].join('\n'))!.final
  expect(after(at('15:06:48', 'final-review session started'))).toEqual({ round: 1, verdict: 'pending', activity: 'review', since: '2026-10-09T15:06:48+02:00' })
  const fixing = after(
    at('15:06:48', 'final-review session started'),
    at('15:19:19', 'final-review session finished (exit 0, 750s)'),
    at('15:19:25', 'final-fix session started'),
  )
  expect([fixing?.round, fixing?.verdict, fixing?.activity]).toEqual([1, 'fail', 'fix'])
  const reviewingAgain = after(
    at('15:06:48', 'final-review session started'),
    at('15:19:19', 'final-review session finished (exit 0, 750s)'),
    at('15:19:25', 'final-fix session started'),
    at('15:25:00', 'final-fix session finished (exit 0, 335s)'),
    at('15:29:00', 'final-review session started'),
  )
  expect([reviewingAgain?.round, reviewingAgain?.verdict, reviewingAgain?.activity]).toEqual([2, 'fail', 'review'])
  const passed = after(
    at('15:29:00', 'final-review session started'),
    at('15:35:00', 'final-review session finished (exit 0, 360s)'),
    at('15:35:05', 'run ended: final review passed, merge withheld: autonomy=halt-each-pr'),
  )
  expect([passed?.verdict, passed?.activity]).toEqual(['pass', null])
  expect(parseRun(head.join('\n'))!.final).toBeUndefined()
})

test('toasts only what is new in the same run', () => {
  const before = parseRun(RUN)!
  const after = parseRun(`${RUN}
2026-10-08T18:09:31+02:00 [status] #3 integrated`)!
  expect(notable(before, after)).toEqual(['#3 integrated'])
  expect(notable(null, after)).toEqual([])
  expect(parseRun('nothing here')).toBeNull()
})
