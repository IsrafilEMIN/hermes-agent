import assert from 'node:assert/strict'

import { test } from 'vitest'

import { stopBackendChild, stopBackendTreesForUpdate } from './backend-child'

test('stopBackendChild does not signal a safehouse-attached backend', () => {
  const kills: string[] = []
  const trees: number[] = []

  stopBackendChild(
    {
      pid: 4242,
      ownsProcess: false,
      kill(signal) {
        kills.push(signal)
      }
    },
    {
      forceKillProcessTree: pid => {
        trees.push(pid)
      }
    }
  )

  assert.deepEqual(kills, [])
  assert.deepEqual(trees, [])
})

test('stopBackendTreesForUpdate skips a backend Desktop does not own', () => {
  const trees: number[] = []
  let poolStops = 0

  stopBackendTreesForUpdate(
    { pid: 4242, ownsProcess: false },
    {
      forceKillProcessTree: pid => {
        trees.push(pid)
      },
      stopAllPoolBackends: () => {
        poolStops += 1
      }
    }
  )

  assert.deepEqual(trees, [])
  assert.equal(poolStops, 1)
})
