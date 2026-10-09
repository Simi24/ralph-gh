import type { Board, Issues, TicketRow } from '../types'

/**
 * Sorts every ticket of the PRD into the few groups a person acts on, so a PRD of fifty tickets
 * still reads at a glance: what runs now, what needs a human, what starts next, what waits,
 * what is done. The run log knows what this run touched; GitHub's sub-issues know the rest.
 */

export type Named = { number: number; title: string }
export type Waiting = Named & { waitsFor: number[] }

export type Plan = {
  inFlight: TicketRow[]
  attention: (Named & { text: string })[]
  ready: Named[]
  waiting: Waiting[]
  done: Named[]
  other: Named[] // sub-issues never opted in (no ralph label) and still open
  total: number
}

const DONE_LABELS = ['ralph:integrated', 'ralph:done']
const HALTED_LABELS = ['ralph:failed:issue', 'ralph:failed:systemic', 'ralph:blocked']

export function plan(board: Board, issues: Issues | null): Plan {
  const known = issues && issues.prd === board.prd ? issues : null
  const titleOf = (n: number) => known?.tickets.find(t => t.number === n)?.title ?? ''
  const rows = new Map(board.tickets.map(row => [row.number, row]))
  const result: Plan = { inFlight: [], attention: [], ready: [], waiting: [], done: [], other: [], total: 0 }

  const isDone = (n: number): boolean => {
    const row = rows.get(n)
    if (row) return row.tone === 'ok'
    const issue = known?.tickets.find(t => t.number === n)
    return !!issue && (issue.state === 'closed' || issue.labels.some(l => DONE_LABELS.includes(l)))
  }

  const numbers = new Set<number>([...rows.keys(), ...(known?.tickets.map(t => t.number) ?? [])])
  for (const n of [...numbers].sort((a, b) => a - b)) {
    result.total += 1
    const row = rows.get(n)
    const issue = known?.tickets.find(t => t.number === n)
    const named = { number: n, title: titleOf(n) }
    if (row?.tone === 'run') {
      result.inFlight.push(row)
    } else if (row?.tone === 'bad') {
      result.attention.push({ ...named, text: row.text })
    } else if (isDone(n)) {
      result.done.push(named)
    } else if (issue && issue.labels.some(l => HALTED_LABELS.includes(l))) {
      result.attention.push({ ...named, text: issue.labels.find(l => HALTED_LABELS.includes(l)) ?? '' })
    } else if (issue && issue.labels.includes('ralph:queued')) {
      const blockers = known?.blockers?.[String(n)] ?? []
      const waitsFor = blockers.filter(b => (b.inPrd ? !isDone(b.number) : !b.closed)).map(b => b.number)
      if (waitsFor.length === 0) result.ready.push(named)
      else result.waiting.push({ ...named, waitsFor })
    } else if (issue && issue.state !== 'closed') {
      result.other.push(named)
    }
  }
  return result
}

/** A bar of `width` cells split by group size: done, in flight, attention, then the rest. */
export function bar(p: Plan, width: number): { done: number; flight: number; attention: number; rest: number } {
  if (p.total === 0) return { done: 0, flight: 0, attention: 0, rest: width }
  const cells = (n: number) => (n === 0 ? 0 : Math.max(1, Math.round((n / p.total) * width)))
  const done = cells(p.done.length)
  const flight = cells(p.inFlight.length)
  const attention = cells(p.attention.length)
  return { done, flight, attention, rest: Math.max(0, width - done - flight - attention) }
}

export function names(list: Named[], max: number): string {
  const shown = list.slice(0, max).map(t => `#${t.number}${t.title ? ` ${t.title}` : ''}`)
  return list.length > max ? `${shown.join(' · ')}  +${list.length - max} more` : shown.join(' · ')
}
