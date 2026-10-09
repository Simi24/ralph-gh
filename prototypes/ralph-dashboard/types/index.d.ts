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
  final?: FinalPhase
}

/** The PRD's final review as this run's log tells it: rounds, the last verdict, what runs now. */
export type FinalPhase = {
  round: number // final reviews started in this run
  verdict: 'pending' | 'pass' | 'fail' // of the last finished review
  activity: 'review' | 'fix' | 'verify' | null // what runs now ('verify': the fix is verified and pushed)
  since?: string // when that activity started
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
    'ralph-dashboard': {
      repo: string | null
      board: Board | null
      stateDir: string | null
      issues: Issues | null
      showDone: boolean
      showWaiting: boolean
      now: number | null // the last poll's clock, so in-flight steps show a running time
      limits: PlanLimits | null // the account's plan windows, as this session last read them
    }
  }
}

/** The subscription's rate-limit windows (five_hour, seven_day), as the last API reply reported them. */
export type PlanLimits = {
  windows: { kind: string; percentUsed: number; resetsAt?: string }[]
  readAt: number // clock ms when the dashboard read them
}
