import { atom, read, update } from 'claude-code'
import type { Engine, Register, Timer } from 'claude-code'

import type { Blocker, Board, Issues, IssueTicket, Tone } from '../types'
import { PRD_STAGES, STAGES, elapsed, notable, parseRun, since, summary } from './parse'
import { bar, names, plan } from './plan'

/**
 * the /ralph dashboard: watches a ralph-gh run from inside Claude Code.
 *
 * The conductor writes `run.log` in its per-repo state dir
 * (`<CLAUDE_CONFIG_DIR or ~/.claude>/ralph-gh/state/<owner>__<repo>/`). This mod polls it and
 * draws a control view in a pane (`/ralph`, or `/ralph owner/repo`): a progress bar, the tickets
 * in flight as pipelines of dots, the ones that need a human, what starts next, and one-line
 * counts for waiting and done tickets (expandable). It keeps a status line, toasts integrations
 * and failures, and offers Drain, which writes the STOP file exactly like `ralph-gh stop`.
 *
 * On demand only: loading the mod registers `/ralph` and does nothing else (no timer, no `gh`,
 * no file read). `/ralph` starts one poll timer; closing the pane or `/ralph off` cancels it.
 * While watching, GitHub (read-only `gh api`) is read when the pane opens and when run.log shows
 * a new event; each queued ticket's blockers once per PRD. It writes nothing but the STOP file.
 */

const PANE = 'ralph-board'
const POLL_MS = 3000
const REPO = /^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/

const repo = atom({ plugin: 'ralph-gh', key: 'repo' } as const, null)
const board = atom({ plugin: 'ralph-gh', key: 'board' } as const, null)
const stateDir = atom({ plugin: 'ralph-gh', key: 'stateDir' } as const, null)
const issues = atom({ plugin: 'ralph-gh', key: 'issues' } as const, null)
const showDone = atom({ plugin: 'ralph-gh', key: 'showDone' } as const, false)
const showWaiting = atom({ plugin: 'ralph-gh', key: 'showWaiting' } as const, false)
const clock = atom({ plugin: 'ralph-gh', key: 'now' } as const, null)

let timer: Timer | undefined // set only while watching
let watch = 0 // bumped when watching ends, so a poll still waiting on `gh` knows it is stale
let busy = false // a refresh that reads fifty tickets' blockers outlasts a poll interval

async function gh($: Engine, argv: string[]): Promise<string | null> {
  try {
    const out = await $.process.run(['gh', ...argv], { timeoutMs: 15000 })
    return out.exitCode === 0 ? out.stdout.trim() : null
  } catch {
    return null
  }
}

async function ghJson<T>($: Engine, argv: string[]): Promise<T | null> {
  const out = await gh($, argv)
  if (out === null) return null
  try {
    return JSON.parse(out) as T
  } catch {
    return null
  }
}

async function detectRepo($: Engine): Promise<string | null> {
  const name = await gh($, ['repo', 'view', '--json', 'nameWithOwner', '-q', '.nameWithOwner'])
  return name && REPO.test(name) ? name : null
}

async function stateDirOf($: Engine, name: string): Promise<string | null> {
  const config = (await $.env.get('CLAUDE_CONFIG_DIR')) ?? `${(await $.env.get('HOME')) ?? ''}/.claude`
  return config.startsWith('/') ? `${config}/ralph-gh/state/${name.replace('/', '__')}` : null
}

/** Sub-issues every call; blockers only for queued tickets not read yet (edges do not change in a run). */
async function readIssues($: Engine, name: string, prd: number, known: Issues | null): Promise<Issues | null> {
  const title = await gh($, ['api', `repos/${name}/issues/${prd}`, '--jq', '.title'])
  const tickets = await ghJson<IssueTicket[]>($, [
    'api', `repos/${name}/issues/${prd}/sub_issues?per_page=100`,
    '--jq', '[.[] | {number, title, state, labels: [.labels[].name]}]',
  ])
  if (title === null || tickets === null) return null

  const inPrd = new Set(tickets.map(t => t.number))
  const blockers: Record<string, Blocker[]> = { ...(known?.prd === prd ? (known.blockers ?? {}) : {}) }
  const repoUrl = `/repos/${name.toLowerCase()}`
  for (const ticket of tickets) {
    if (!ticket.labels.includes('ralph:queued') || blockers[String(ticket.number)]) continue
    const found = await ghJson<{ number: number; state: string; repo: string }[]>($, [
      'api', `repos/${name}/issues/${ticket.number}/dependencies/blocked_by?per_page=100`,
      '--jq', '[.[] | {number, state, repo: .repository_url}]',
    ])
    if (found === null) continue // unknown: read again next time, never guessed
    blockers[String(ticket.number)] = found.map(b => ({
      number: b.number,
      inPrd: b.repo.toLowerCase().endsWith(repoUrl) && inPrd.has(b.number),
      closed: b.state === 'closed',
    }))
  }
  return { prd, title, tickets, blockers }
}

/** One poll. `opening`: the pane just opened, so GitHub is read even if the log is unchanged. */
async function refresh($: Engine, opening = false): Promise<void> {
  if (busy) return
  busy = true
  const generation = watch
  try {
    let name = await read($, repo)
    if (!name) {
      name = await detectRepo($)
      if (!name) return
      await update($, repo, () => name)
    }
    const dir = await stateDirOf($, name)
    await update($, stateDir, () => dir)
    const log = dir ? `${dir}/run.log` : null
    const next = log && (await $.fs.exists(log)) ? parseRun(await $.fs.read(log)) : null
    const previous = await read($, board)

    const isUnchanged = !!next && !!previous && next.lines === previous.lines && next.session === previous.session
    const known = await read($, issues)
    if (next?.prd !== undefined && (opening || known?.prd !== next.prd || !isUnchanged)) {
      const fresh = await readIssues($, name, next.prd, known) // only on open or on a new event
      if (generation !== watch) return // watching ended meanwhile: touch nothing
      if (fresh) await update($, issues, () => fresh)
    }

    // Every poll moves the clock, so a step that has no new event still shows its running time.
    const tick = await $.clock.now()
    await update($, clock, () => tick)
    if (generation !== watch) return
    if (isUnchanged) return
    for (const line of next ? notable(previous, next) : []) $.ui.toast(`ralph ${line}`)
    $.ui.status(next ? summary(next) : undefined)
    await update($, board, () => next)
  } finally {
    busy = false
  }
}

const COLOR: Record<Tone, string> = { ok: 'green', bad: 'red', run: 'yellow' }
const MAX_LIST = 12 // rows an expanded group shows before "+N more"

type Dot = { glyph: string; color?: string; dim?: boolean }

/** One dot per step: done (green ●), current (yellow ◉ or red ✖), to do (dim ○). */
function dots(count: number, reached: number, tone: Tone): Dot[] {
  return Array.from({ length: count }, (_, i): Dot => {
    if (tone === 'ok' || i < reached) return { glyph: '●', color: 'green' }
    if (i === reached) return tone === 'bad' ? { glyph: '✖', color: 'red' } : { glyph: '◉', color: 'yellow' }
    return { glyph: '○', dim: true }
  })
}

function startWatching($: Engine): void {
  timer ??= $.clock.every(POLL_MS, () => void refresh($).catch(() => undefined))
}

function stopWatching($: Engine): void {
  watch += 1
  timer?.cancel()
  timer = undefined
  $.ui.status(undefined)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    // Registering the command is all a session start does: nothing polls until `/ralph`.
    await $.command.register({
      name: 'ralph',
      description: 'Watch the live ralph-gh board (/ralph [owner/repo], /ralph off to stop)',
    })
    return next(e)
  })

  on('command.run', { command: 'ralph' }, async ($, e) => {
    const wanted = e.args.trim()
    if (wanted === 'off') {
      stopWatching($)
      await $.ui.close({ id: PANE })
      return { text: 'Stopped watching ralph-gh.' }
    }
    if (wanted) {
      if (!REPO.test(wanted)) return { text: `Not a repository name: ${wanted} (expected owner/repo, or off).` }
      await update($, repo, () => wanted)
      await update($, board, () => null)
      await update($, issues, () => null)
    } else {
      watch += 1 // an in-flight poll of the old repo discards its result
      const current = await detectRepo($)
      await update($, repo, () => current) // a bare /ralph returns to the current repo
      await update($, board, () => null)
      await update($, issues, () => null)
    }
    await refresh($, true)
    // No repository found here: show why, but start no timer (it would retry `gh` every poll).
    if (await read($, repo)) startWatching($)
    else stopWatching($)
    await $.ui.open({ id: PANE, title: 'ralph-gh' })
    const name = await read($, repo)
    return { text: name ? `Watching ralph-gh runs of ${name}.` : 'No GitHub repository found here; use /ralph owner/repo.' }
  })

  on('ui.close', async ($, e, next) => {
    if (e.id === PANE) stopWatching($) // closing the pane ends all polling
    return next(e)
  }).catch(($, e, next) => next(e))

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const name = await read($, repo)
    const run: Board | null = await read($, board)
    const known: Issues | null = await read($, issues)
    const dir = await read($, stateDir)
    const isDoneOpen = await read($, showDone)
    const isWaitingOpen = await read($, showWaiting)
    const now: number | null = await read($, clock)
    const columns = Math.max(30, (e.props as { bodyColumns?: number }).bodyColumns ?? e.viewport?.columns ?? 60)

    // A pipeline never shrinks: in a narrow row only the title gives way.
    const Flow = ({ list }: { list: Dot[] }) => (
      <Box flexShrink={0}>
        {list.map((dot, i) => (
          <Text color={dot.color} dimColor={dot.dim}>
            {dot.glyph}
            {i < list.length - 1 ? <Text dimColor>─</Text> : ''}
          </Text>
        ))}
      </Box>
    )

    if (!run) {
      return (
        <Box flexDirection="column">
          <Text bold>ralph-gh · {name ?? 'no repository'}</Text>
          {name ? (
            <Text dimColor>No ralph-gh run recorded for this repository yet. Watching for one: start it with `ralph-gh run --prd N`.</Text>
          ) : (
            <Text dimColor>This directory is not a GitHub repository, so nothing is watched. Use /ralph owner/repo.</Text>
          )}
        </Box>
      )
    }

    const isRunning = run.ended === undefined
    const p = plan(run, known)
    const prdTitle = known?.prd === run.prd ? known.title.replace(/^\s*PRD\s*[:\-–—]\s*/i, '') : undefined
    const prdTone: Tone = run.endedTone ?? 'run'
    const cells = bar(p, Math.min(48, columns - 4))

    return (
      <Box flexDirection="column">
        <Text bold>
          PRD #{run.prd ?? '?'}
          {prdTitle ? ` ${prdTitle}` : ''}
        </Text>
        <Text dimColor>
          {name}
          {run.finalPr ? ` · final PR #${run.finalPr}` : ''} · {isRunning ? `running (${run.session})` : 'ended'}
        </Text>
        <Box>
          <Flow list={dots(PRD_STAGES.length, run.prdStage, prdTone)} />
          <Text dimColor>  {PRD_STAGES.join(' → ')}</Text>
        </Box>
        {run.ended && <Text color={COLOR[prdTone]}>{run.ended}</Text>}
        {run.integration && (
          <Text color={COLOR[run.integrationTone ?? 'run']}>
            {run.integrationTone === 'ok' ? '●' : run.integrationTone === 'bad' ? '✖' : '◉'} integration branch:{' '}
            {run.integration.replace(/^integration /, '')}
          </Text>
        )}

        <Text> </Text>
        <Box>
          <Text color="green">{'█'.repeat(cells.done)}</Text>
          <Text color="yellow">{'█'.repeat(cells.flight)}</Text>
          <Text color="red">{'█'.repeat(cells.attention)}</Text>
          <Text dimColor>{'░'.repeat(cells.rest)}</Text>
        </Box>
        <Text>
          <Text color="green">{p.done.length} done</Text>
          <Text dimColor> · </Text>
          <Text color="yellow">{p.inFlight.length} in flight</Text>
          <Text dimColor> · </Text>
          <Text color={p.attention.length ? 'red' : undefined} dimColor={!p.attention.length}>
            {p.attention.length} need attention
          </Text>
          <Text dimColor>
            {' '}· {p.ready.length} ready · {p.waiting.length} waiting{p.other.length ? ` · ${p.other.length} not queued` : ''}
          </Text>
        </Text>

        {p.attention.length > 0 && (
          <Box flexDirection="column" marginTop={1}>
            <Text bold color="red">Needs attention</Text>
            {p.attention.slice(0, MAX_LIST).map(t => (
              <Text color="red">
                ✖ #{t.number} {t.title} <Text dimColor>— {t.text}</Text>
              </Text>
            ))}
          </Box>
        )}

        <Box flexDirection="column" marginTop={1}>
          <Text bold>In flight</Text>
          {p.inFlight.length === 0 && <Text dimColor>nothing running</Text>}
          {p.inFlight.map(t => (
            <Box flexDirection="column">
              <Box>
                <Flow list={dots(STAGES.length, t.stage, t.tone)} />
                <Box flexShrink={0}>
                  <Text bold>{`  #${t.number} `}</Text>
                </Box>
                <Box flexShrink={1} flexGrow={1}>
                  <Text wrap="truncate-end">{known?.tickets.find(k => k.number === t.number)?.title ?? ''}</Text>
                </Box>
                {t.pr && (
                  <Box flexShrink={0}>
                    <Text dimColor>{`  PR #${t.pr}`}</Text>
                  </Box>
                )}
              </Box>
              <Text>
                {'              '}
                <Text color="yellow">{STAGES[t.stage]}</Text>
                <Text dimColor>
                  {now !== null
                    ? ` · ${since(t.at, now)} in this step · ${since(t.firstAt, now)} total — ${t.text}`
                    : ` · ${elapsed(t.firstAt, t.at)} — ${t.text}`}
                </Text>
              </Text>
            </Box>
          ))}
        </Box>

        {p.ready.length > 0 && (
          <Box flexDirection="column" marginTop={1}>
            <Text bold>Up next</Text>
            <Text dimColor>{names(p.ready, 3)}</Text>
          </Box>
        )}

        <Box flexDirection="column" marginTop={1}>
          <Text dimColor>
            {isWaitingOpen ? '▾' : '▸'} {p.waiting.length} waiting on blockers
          </Text>
          {isWaitingOpen &&
            p.waiting.slice(0, MAX_LIST).map(t => (
              <Text dimColor>
                {'  '}#{t.number} {t.title} — waits for {t.waitsFor.map(n => `#${n}`).join(', ')}
              </Text>
            ))}
          {isWaitingOpen && p.waiting.length > MAX_LIST && <Text dimColor>  +{p.waiting.length - MAX_LIST} more</Text>}
          <Text color="green" dimColor={!isDoneOpen}>
            {isDoneOpen ? '▾' : '▸'} {p.done.length} integrated
          </Text>
          {isDoneOpen && <Text dimColor>  {names(p.done, MAX_LIST)}</Text>}
        </Box>

        <Box flexDirection="column" marginTop={1}>
          <Text dimColor>recent:</Text>
          {run.recent.slice(-4).map(line => (
            <Text dimColor>  {line}</Text>
          ))}
        </Box>
        <Box gap={1}>
          {isRunning && dir && (
            <Button
              key="drain"
              label="Drain (STOP)"
              hotkey="s"
              onPress={async () => {
                await $.fs.write(`${dir}/STOP`, '')
                $.ui.toast('ralph: STOP written — in-flight tickets finish, nothing new starts')
              }}
            />
          )}
          <Button key="waiting" label={isWaitingOpen ? 'Hide waiting' : 'Waiting'} hotkey="w" onPress={() => update($, showWaiting, v => !v)} />
          <Button key="done" label={isDoneOpen ? 'Hide done' : 'Done'} hotkey="d" onPress={() => update($, showDone, v => !v)} />
          <Button key="refresh" label="Refresh" hotkey="r" onPress={() => refresh($)} />
        </Box>
      </Box>
    )
  })
}
