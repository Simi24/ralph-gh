import { expect, test } from 'claude-code/testing'

import { absolute, checkBash, checkWrite } from '../hooks/rules'

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
]
const allowed = [
  'git status',
  'git commit -m "feat: add greet"',
  'git merge origin/feat/1-prd',
  'gh issue view 3 --json body',
  'gh pr diff 4',
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
