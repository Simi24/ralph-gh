import type { PlanLimits, Tone } from '../types'

/**
 * The subscription's rate-limit windows as one short line: `5h 42% (resets in 1h20m) · week 61% (resets in 3d4h)`.
 * Pure: the dashboard reads the windows with `$.session.usage()`, which costs nothing, and passes them here.
 */

const LABELS: Record<string, string> = { five_hour: '5h', seven_day: 'week', spend_limit: 'spend' }
const ORDER = ['five_hour', 'seven_day', 'spend_limit']

export type LimitCell = { label: string; percent: string; resets: string; tone: Tone }

export function toneOf(percentUsed: number): Tone {
  if (percentUsed >= 90) return 'bad'
  if (percentUsed >= 70) return 'run'
  return 'ok'
}

/** `in 1h20m`, `in 3d4h`, or '' when the reset is unknown or already past. */
export function resetsIn(resetsAt: string | undefined, nowMs: number): string {
  if (!resetsAt) return ''
  const ms = Date.parse(resetsAt) - nowMs
  if (!Number.isFinite(ms) || ms <= 0) return ''
  const m = Math.round(ms / 60000)
  if (m < 60) return `in ${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `in ${h}h${String(m % 60).padStart(2, '0')}m`
  return `in ${Math.floor(h / 24)}d${h % 24}h`
}

export function cells(limits: PlanLimits, nowMs: number): LimitCell[] {
  return [...limits.windows]
    .sort((a, b) => (ORDER.indexOf(a.kind) + 99) % 99 - (ORDER.indexOf(b.kind) + 99) % 99)
    .map(w => ({
      label: LABELS[w.kind] ?? w.kind,
      percent: `${w.percentUsed}%`,
      resets: resetsIn(w.resetsAt, nowMs),
      tone: toneOf(w.percentUsed),
    }))
}

/** How old the reading is, for the dim "as of" note: the windows only move when this session talks to the API. */
export function age(readAt: number, nowMs: number): string {
  const m = Math.round((nowMs - readAt) / 60000)
  return m <= 0 ? 'just now' : m < 60 ? `${m}m ago` : `${Math.floor(m / 60)}h ago`
}
