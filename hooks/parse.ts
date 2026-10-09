import type { Board, TicketRow, Tone } from '../types'

/**
 * Reads the last run out of ralph-gh's run.log (it appends across runs) into a Board.
 * Lines look like `<iso> [status] #2 PR #4 opened (...)` or `<iso> [status] PRD run ended: ...`,
 * and a run opens with `<iso> ralph-gh session: <id>`.
 */

export const STAGES = ['claim', 'implement', 'verify', 'PR', 'gate', 'merge', 'done'] as const
export const PRD_STAGES = ['explore', 'tickets', 'final review', 'done'] as const

const STATUS = /^(\S+) \[status\] (?:#(\d+)|PRD) (.*)$/
const SESSION = /^\S+ ralph-gh session: (\S+)/
const PRD = /^\S+ PRD: #(\d+)/
const EXITED = /^\S+ ralph-gh exited: (.*)$/
const BAD = /\b(failed|blocked|systemic|RED|broken|cascade)\b/i

/** The pipeline step a ticket status line belongs to; undefined for lines that move nothing. */
export function stageOf(text: string): number | undefined {
  if (/^integrated\b/.test(text)) return 6
  if (/\bPR #\d+ merged\b|merge-fix/.test(text)) return 5
  if (/ticket-gate session/.test(text)) return 4
  if (/\bPR #\d+ opened\b|^in review\b/.test(text)) return 3
  if (/^verify\b/.test(text)) return 2
  if (/implementer session/.test(text)) return 1
  if (/^dispatched\b/.test(text)) return 0
  return undefined
}

function toneOf(text: string): Tone {
  if (/^integrated\b/.test(text)) return 'ok'
  return BAD.test(text) ? 'bad' : 'run'
}

export function parseRun(log: string): Board | null {
  const all = log.split('\n')
  let start = -1
  all.forEach((line, i) => {
    if (SESSION.test(line)) start = i
  })
  if (start < 0) return null

  const board: Board = { session: SESSION.exec(all[start])![1], prdStage: 0, tickets: [], recent: [], lines: all.length, statusLines: 0 }
  const tickets = new Map<number, TicketRow>()
  for (const line of all.slice(start + 1)) {
    const prd = PRD.exec(line)
    if (prd) board.prd = Number(prd[1])
    const exited = EXITED.exec(line)
    if (exited) board.ended ??= exited[1]
    const status = STATUS.exec(line)
    if (!status) continue
    const [, at, number, text] = status
    const ticket = number && Number(number) !== board.prd ? number : undefined // `#<prd> verify started` is PRD-level
    board.statusLines++
    board.recent = [...board.recent, `${number ? `#${number}` : 'PRD'} ${text}`].slice(-6)
    if (ticket) {
      const n = Number(ticket)
      const old = tickets.get(n)
      const stage = Math.max(old?.stage ?? 0, stageOf(text) ?? 0)
      const row: TicketRow = { number: n, pr: old?.pr, stage, firstAt: old?.firstAt ?? at, at, text, tone: toneOf(text) }
      const pr = /\bPR #(\d+) opened\b/.exec(text)
      if (pr) row.pr = Number(pr[1])
      tickets.set(n, row)
      board.prdStage = Math.max(board.prdStage, 1)
      continue
    }
    if (/^integration verify|^integration branch/.test(text)) {
      board.integration = text
      board.integrationTone = /green|repaired/.test(text) ? 'ok' : /RED|broken/.test(text) ? 'bad' : 'run'
    }
    const final = /\bPR #(\d+) opened\b/.exec(text) // PRD-level: ticket PRs are logged under their ticket
    if (final) board.finalPr = Number(final[1])
    if (/^final-review session/.test(text)) board.prdStage = Math.max(board.prdStage, 2)
    const ended = /^run ended: (.*)$/.exec(text)
    if (ended) board.ended = ended[1]
  }
  if (board.ended !== undefined) {
    board.prdStage = 3
    board.endedTone = BAD.test(board.ended) || /incomplete|limit|stopped/.test(board.ended) ? 'bad' : 'ok'
  }
  board.tickets = [...tickets.values()].sort((a, b) => a.number - b.number)
  return board
}

/** The lines worth a toast among those added since the previous poll. */
export function notable(previous: Board | null, next: Board): string[] {
  if (!previous || previous.session !== next.session) return []
  const added = Math.min(next.recent.length, Math.max(0, next.statusLines - previous.statusLines))
  const fresh = added > 0 ? next.recent.slice(-added) : []
  return fresh.filter(line => /\b(integrated|failed|blocked|RED|repaired|run ended)\b/.test(line))
}

export function summary(board: Board): string {
  const done = board.tickets.filter(t => t.tone === 'ok').length
  const head = `ralph PRD #${board.prd ?? '?'} · ${done}/${board.tickets.length} integrated`
  if (board.ended) return `${head} · ended: ${board.ended}`
  return `${head} · ${board.recent.at(-1) ?? 'starting'}`
}

/** `2m05s` between two ISO timestamps; empty when either is unreadable. */
export function elapsed(from: string, to: string): string {
  return duration(Date.parse(to) - Date.parse(from))
}

/** `7m12s` from an ISO timestamp to a clock reading in ms (the poll's `now`): a time that runs. */
export function since(from: string, nowMs: number): string {
  return duration(nowMs - Date.parse(from))
}

function duration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return ''
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m${String(s % 60).padStart(2, '0')}s`
  return `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`
}
