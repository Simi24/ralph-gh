import { expect, test } from 'claude-code/testing'

import { age, cells, resetsIn, toneOf } from '../hooks/limits'

const NOW = Date.parse('2026-10-09T12:20:00+02:00')

test('the 5-hour and weekly windows read as one line, in that order, with their resets', () => {
  const line = cells(
    {
      windows: [
        { kind: 'seven_day', percentUsed: 61, resetsAt: '2026-10-12T16:20:00+02:00' },
        { kind: 'five_hour', percentUsed: 42, resetsAt: '2026-10-09T13:40:00+02:00' },
      ],
      readAt: NOW,
    },
    NOW,
  )
  expect(line.map(c => `${c.label} ${c.percent} ${c.resets}`)).toEqual(['5h 42% in 1h20m', 'week 61% in 3d4h'])
})

test('colours turn yellow at 70% and red at 90%', () => {
  expect([toneOf(42), toneOf(70), toneOf(89.9), toneOf(90), toneOf(104)]).toEqual(['ok', 'run', 'run', 'bad', 'bad'])
})

test('an unknown or past reset shows nothing, and the reading says how old it is', () => {
  expect(resetsIn(undefined, NOW)).toBe('')
  expect(resetsIn('2026-10-09T12:00:00+02:00', NOW)).toBe('')
  expect(resetsIn('2026-10-09T12:45:00+02:00', NOW)).toBe('in 25m')
  expect(age(NOW, NOW)).toBe('just now')
  expect(age(NOW - 7 * 60000, NOW)).toBe('7m ago')
  expect(age(NOW - 125 * 60000, NOW)).toBe('2h ago')
})
