import { expect, test } from 'claude-code/testing'

import type { Board, Issues, TicketRow } from '../../types'
import { bar, names, plan } from '../../hooks/plan'

const row = (number: number, tone: TicketRow['tone'], stage = 2): TicketRow => ({
  number, tone, stage, text: tone === 'bad' ? 'failed: gate' : 'verify started', at: 't', firstAt: 't',
})

/** A PRD of fifty tickets: 1-10 done earlier, 11-13 running, 14 failed now, 15 blocked earlier,
 *  16-20 queued with no open blocker, 21-49 queued behind #11, 50 never opted in. */
function fifty(): { board: Board; issues: Issues } {
  const tickets = Array.from({ length: 50 }, (_, i) => {
    const n = i + 1
    const labels =
      n <= 10 ? ['ralph:integrated'] :
      n === 15 ? ['ralph:blocked'] :
      n === 50 ? [] :
      n <= 14 ? ['ralph:in-progress'] : ['ralph:queued']
    return { number: n, title: `ticket ${n}`, state: 'open', labels }
  })
  const blockers: Issues['blockers'] = {}
  for (let n = 16; n <= 49; n += 1) {
    blockers[String(n)] = n <= 20 ? [{ number: 3, inPrd: true, closed: false }] : [{ number: 11, inPrd: true, closed: false }]
  }
  const board: Board = {
    session: 's', prd: 99, prdStage: 1, recent: [], lines: 1, statusLines: 0,
    tickets: [row(11, 'run'), row(12, 'run', 4), row(13, 'run', 1), row(14, 'bad')],
  }
  return { board, issues: { prd: 99, title: 'big', tickets, blockers } }
}

test('fifty tickets fall into a handful of groups', () => {
  const { board, issues } = fifty()
  const p = plan(board, issues)
  expect(p.total).toBe(50)
  expect(p.done.length).toBe(10)
  expect(p.inFlight.map(t => t.number)).toEqual([11, 12, 13])
  expect(p.attention.map(t => t.number)).toEqual([14, 15])
  expect(p.ready.map(t => t.number)).toEqual([16, 17, 18, 19, 20]) // their blocker #3 is integrated
  expect(p.waiting.length).toBe(29)
  expect(p.waiting[0].waitsFor).toEqual([11])
  expect(p.other.map(t => t.number)).toEqual([50])
})

test('an external blocker waits until it is closed', () => {
  const { board, issues } = fifty()
  issues.blockers!['16'] = [{ number: 500, inPrd: false, closed: false }]
  expect(plan(board, issues).waiting.some(t => t.number === 16 && t.waitsFor[0] === 500)).toBe(true)
  issues.blockers!['16'] = [{ number: 500, inPrd: false, closed: true }]
  expect(plan(board, issues).ready.some(t => t.number === 16)).toBe(true)
})

test('without GitHub data the run log alone still sorts what it knows', () => {
  const { board } = fifty()
  const p = plan(board, null)
  expect(p.total).toBe(4)
  expect(p.inFlight.length).toBe(3)
  expect(p.attention.length).toBe(1)
})

test('the bar fills its width and never hides a non-empty group', () => {
  const { board, issues } = fifty()
  const cells = bar(plan(board, issues), 40)
  expect(cells.done + cells.flight + cells.attention + cells.rest).toBe(40)
  expect(cells.attention).toBeGreaterThan(0)
  expect(names([{ number: 1, title: 'a' }, { number: 2, title: 'b' }, { number: 3, title: '' }], 2)).toBe('#1 a · #2 b  +1 more')
})
