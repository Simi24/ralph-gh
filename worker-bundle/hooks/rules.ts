/**
 * What a ralph-gh worker session may not do. Pure functions: a reason string means "refuse".
 *
 * The conductor is the only writer of GitHub state and the only pusher; a session writes code
 * inside its own worktree and reports an outcome line. These rules make that a hard limit
 * instead of a prompt instruction. Shell parsing is best effort: the conductor's own checks
 * (verify, gate, head-pinned merges) stay the authority.
 */

const GIT_PUSH = /\bgit\b(?:\s+-[cC]\s+\S+)*\s+push\b/
const NO_VERIFY = /--no-verify\b/
const GH_WRITE =
  /\bgh\s+(?:(?:pr|issue|label|release|repo|workflow|run|secret|variable|gist|cache|project|ruleset)\s+(?:create|merge|close|reopen|edit|ready|comment|review|delete|transfer|lock|unlock|pin|unpin|archive|rename|fork|set|enable|disable|cancel|rerun|clone|run|upload|update-branch|revert|develop|sync|add|remove|link|unlink|copy|mark-template|field-create|field-delete|item-add|item-archive|item-create|item-delete|item-edit)|release\s+delete-asset|repo\s+(?:deploy-key|autolink)\s+(?:add|create|delete)|repo\s+unarchive|(?:ssh-key|gpg-key)\s+(?:add|delete))\b/
const GH_API = /\bgh\s+api\b([^\n;&|]*)/g
const API_METHOD = /(?:-X\s*|--method[=\s]+)([A-Za-z]+)/
const API_FIELDS = /\s(?:-f|-F|--field|--raw-field|--input)\b/

export function checkBash(command: string): string | undefined {
  if (GIT_PUSH.test(command)) {
    return 'only the conductor pushes; commit your work and report RALPH:DONE'
  }
  if (NO_VERIFY.test(command)) {
    return '--no-verify is not allowed; fix what the hooks report, then commit, or report RALPH:BLOCKED if you cannot'
  }
  if (GH_WRITE.test(command)) {
    return 'GitHub writes (PRs, issues, labels, comments, merges) belong to the conductor; commit your work and report RALPH:DONE, or RALPH:BLOCKED if you need a human'
  }
  for (const match of command.matchAll(GH_API)) {
    const rest = match[1] ?? ''
    const method = API_METHOD.exec(rest)?.[1]?.toUpperCase()
    const writes = method !== undefined ? method !== 'GET' : API_FIELDS.test(rest)
    if (writes) {
      return 'gh api writes belong to the conductor; read-only gh calls are fine. Commit your work and report RALPH:DONE, or RALPH:BLOCKED if you need a human'
    }
  }
  return undefined
}

const MANIFESTS = new Set([
  'package.json', 'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock',
  'pyproject.toml', 'poetry.lock', 'Pipfile', 'Pipfile.lock', 'setup.py', 'setup.cfg',
  'go.mod', 'go.sum', 'Cargo.toml', 'Cargo.lock', 'Gemfile', 'Gemfile.lock',
  'composer.json', 'composer.lock',
])
const REQUIREMENTS = /^requirements(?:[-.][\w.-]*)?\.txt$/

function isInside(path: string, root: string): boolean {
  const base = root.endsWith('/') ? root.slice(0, -1) : root
  return path === base || path.startsWith(`${base}/`)
}

/** `path` is the resolved real path of the file to write; `roots` the resolved writable roots. */
export function checkWrite(path: string, roots: readonly string[], allowManifests: boolean): string | undefined {
  if (!roots.some(root => isInside(path, root))) {
    return `writes outside the worktree are not allowed: ${path}; write inside your worktree, or report RALPH:BLOCKED if the ticket needs it`
  }
  if (path.includes('/.git/') || path.endsWith('/.git')) {
    return 'the repository metadata (.git) is not yours to edit; use git commands to commit, then report RALPH:DONE, or report RALPH:BLOCKED'
  }
  const name = path.slice(path.lastIndexOf('/') + 1)
  if (!allowManifests && (MANIFESTS.has(name) || REQUIREMENTS.test(name))) {
    return `dependency manifests are not edited by sessions (${name}); report RALPH:BLOCKED if the ticket needs it`
  }
  return undefined
}

/** Lexical absolute path: `path` against `cwd`, with `.` and `..` folded. */
export function absolute(path: string, cwd: string): string {
  const parts: string[] = []
  for (const part of (path.startsWith('/') ? path : `${cwd}/${path}`).split('/')) {
    if (part === '' || part === '.') continue
    if (part === '..') parts.pop()
    else parts.push(part)
  }
  return `/${parts.join('/')}`
}
