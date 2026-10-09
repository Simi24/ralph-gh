export type Tone = 'run' | 'ok' | 'bad'

export type TicketRow = {
  number: number
  text: string
  pr?: number
  stage: number // index into STAGES reached so far
  firstAt: string
  at: string
  tone: Tone
}

export type Board = {
  session: string
  prd?: number
  prdStage: number // index into PRD_STAGES
  tickets: TicketRow[]
  integration?: string
  integrationTone?: Tone
  finalPr?: number
  ended?: string
  endedTone?: Tone
  recent: string[]
  lines: number
}

export type IssueTicket = { number: number; title: string; state: string; labels: string[] }

/** A blocker of a ticket: an issue number, whether it is in this PRD, and whether it is closed. */
export type Blocker = { number: number; inPrd: boolean; closed: boolean }

/** The PRD's sub-issues as GitHub has them, and each queued ticket's blockers (read once per PRD). */
export type Issues = {
  prd: number
  title: string
  tickets: IssueTicket[]
  blockers: Record<string, Blocker[]> | null
}

declare module 'claude-code' {
  interface PluginState {
    'ralph-gh': {
      repo: string | null
      board: Board | null
      stateDir: string | null
      issues: Issues | null
      showDone: boolean
      showWaiting: boolean
      now: number | null // the last poll's clock, so in-flight steps show a running time
    }
  }
}
