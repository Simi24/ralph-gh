import { expect, test } from 'claude-code/testing'

import { absolute, checkBash, checkWrite, opencodeWrites } from '../hooks/rules'

const refused = [
  'git push origin feat/x',
  'git -C /repo push --force origin main',
  'git commit --no-verify -m wip',
  'gh pr merge 4 --merge',
  'gh pr create --title t --body b',
  'gh issue edit 3 --add-label ralph:integrated',
  'gh label create ralph:queued',
  'gh api -X POST repos/o/r/issues/1/comments -f body=hi',
  'gh api repos/o/r/issues/1/labels -f labels[]=x',
  'npm test && git push',
  'gh workflow run ci.yml',
  'gh pr update-branch 4',
  'gh pr revert 4',
  'gh release upload v1 dist.zip',
  'gh repo sync owner/repo',
  'gh issue develop 3',
  'gh gist create f.txt',
  'gh gist edit abc',
  'gh gist delete abc',
  'gh cache delete 123',
  'gh project item-add 1 --url u',
  'gh project create --title t',
  'gh ruleset delete 5',
  'gh repo deploy-key add key.pub --allow-write',
  'gh repo deploy-key delete 1',
  'gh repo autolink create REF url',
  'gh repo autolink delete 1',
  'gh repo unarchive o/r',
  'gh ssh-key add k.pub',
  'gh ssh-key delete 1',
  'gh gpg-key add k.asc',
  'gh gpg-key delete 1',
]
const allowed = [
  'git status',
  'git commit -m "feat: add greet"',
  'git merge origin/feat/1-prd',
  'gh issue view 3 --json body',
  'gh pr diff 4',
  'gh pr checks 4',
  'gh pr status',
  'gh workflow view ci.yml',
  'gh workflow list',
  'gh gist view abc',
  'gh gist list',
  'gh cache list',
  'gh project list',
  'gh project view 1',
  'gh ruleset list',
  'gh repo deploy-key list',
  'gh repo autolink list',
  'gh ssh-key list',
  'gh api repos/o/r/issues/1/sub_issues',
  'gh api -X GET repos/o/r/issues -f state=open',
  'python3 -m unittest',
]

test('refuses pushes, --no-verify and GitHub writes', () => {
  for (const command of refused) expect(checkBash(command)).toBeDefined()
})

test('lets reads, commits and local merges through', () => {
  for (const command of allowed) expect(checkBash(command)).toBeUndefined()
})

test('writes stay inside the worktree, out of .git and away from manifests', () => {
  const roots = ['/state/prd-1/worktrees/ticket-2']
  expect(checkWrite('/state/prd-1/worktrees/ticket-2/greet.py', roots, false)).toBeUndefined()
  expect(checkWrite('/state/prd-1/worktrees/ticket-2-evil/x.py', roots, false)).toBeDefined()
  expect(checkWrite('/Users/me/.zshrc', roots, false)).toBeDefined()
  expect(checkWrite('/state/prd-1/worktrees/ticket-2/.git/config', roots, false)).toBeDefined()
  expect(checkWrite('/state/prd-1/worktrees/ticket-2/package.json', roots, false)).toBeDefined()
  expect(checkWrite('/state/prd-1/worktrees/ticket-2/requirements-dev.txt', roots, false)).toBeDefined()
  expect(checkWrite('/state/prd-1/worktrees/ticket-2/package.json', roots, true)).toBeUndefined()
})

test('absolute folds . and .. against the cwd', () => {
  expect(absolute('src/../greet.py', '/w')).toBe('/w/greet.py')
  expect(absolute('../../etc/passwd', '/w/t')).toBe('/etc/passwd')
  expect(absolute('/abs/./x', '/w')).toBe('/abs/x')
})

test('every refusal tells the session what to do instead', () => {
  const reasons = [
    ...[...refused].map(command => checkBash(command)),
    checkWrite('/Users/me/.zshrc', ['/w'], false),
    checkWrite('/w/.git/config', ['/w'], false),
    checkWrite('/w/package.json', ['/w'], false),
  ]
  for (const reason of reasons) expect(reason).toMatch(/RALPH:(DONE|BLOCKED)/)
})

test('opencode file tools name the files they write', () => {
  expect(opencodeWrites('write', { filePath: '/w/a.py', content: 'x' })).toEqual(['/w/a.py'])
  expect(opencodeWrites('edit', { filePath: 'src/a.py' })).toEqual(['src/a.py'])
  expect(opencodeWrites('multiedit', { filePath: '/w/a.py' })).toEqual(['/w/a.py'])
  expect(opencodeWrites('write', {})).toEqual([])
  expect(opencodeWrites('read', { filePath: '/etc/passwd' })).toBeUndefined()
  expect(opencodeWrites('bash', { command: 'ls' })).toBeUndefined()
})

test('file paths with control characters are refused', () => {
  for (const path of ['/w/a\rb', '/w/a\nb', '/w/a\u2028b', '/w/a\u0000b']) {
    expect(checkWrite(path, ['/w'], false)).toMatch(/control characters/)
  }
})

test('opencode patch paths are read the way OpenCode reads them: line by line, trimmed', () => {
  const patch = ['*** Begin Patch', '*** Add File: wt\r/../../out', '+x', '*** End Patch'].join('\n')
  expect(opencodeWrites('apply_patch', { patchText: patch })).toEqual(['wt\r/../../out'])
  const crlf = ['*** Begin Patch', '*** Add File: a.py ', '+x', '*** End Patch'].join('\r\n')
  expect(opencodeWrites('apply_patch', { patchText: crlf })).toEqual(['a.py'])
})

test('opencode patches name every file they add, update, delete or move to', () => {
  const patch = [
    '*** Begin Patch',
    '*** Add File: new.py',
    '+print(1)',
    '*** Update File: src/a.py',
    '*** Move to: /etc/evil',
    '@@',
    '-a',
    '+b',
    '  *** Delete File: package.json',
    '*** End Patch',
  ].join('\n')
  expect(opencodeWrites('apply_patch', { patchText: patch })).toEqual(['new.py', 'src/a.py', '/etc/evil', 'package.json'])
  expect(opencodeWrites('patch', { patchText: patch })).toHaveLength(4)
  expect(opencodeWrites('apply_patch', { patchText: '*** Begin Patch\n*** End Patch' })).toEqual([])
  expect(opencodeWrites('apply_patch', {})).toEqual([])
})
