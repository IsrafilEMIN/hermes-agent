import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import { parseUnmergedFileList, resolveUpdateConflictResult } from './update-conflict'

function createTempGitRepo() {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-update-conflict-'))
  const git = (...args: string[]) => execFileSync('git', args, { cwd, encoding: 'utf8', timeout: 10_000 }).trim()

  try {
    git('init', '--quiet')
    git('config', 'commit.gpgSign', 'false')
    git('config', 'core.hooksPath', '.git/no-hooks')
    git('config', 'user.name', 'Hermes Test')
    git('config', 'user.email', 'hermes@example.invalid')
    git('branch', '-M', 'main')

    return { cwd, git }
  } catch (error) {
    fs.rmSync(cwd, { recursive: true, force: true })
    throw error
  }
}

function writeFile(cwd: string, name: string, content: string) {
  fs.writeFileSync(path.join(cwd, name), content)
}

function commitFile(cwd: string, git: (...args: string[]) => string, name: string, content: string, message: string) {
  writeFile(cwd, name, content)
  git('add', name)
  git('commit', '--quiet', '-m', message)
}

function rebasePaused(cwd: string): boolean {
  return fs.existsSync(path.join(cwd, '.git', 'rebase-merge')) || fs.existsSync(path.join(cwd, '.git', 'rebase-apply'))
}

const ROW_BASE = { branch: 'main', hermesRoot: '/tmp/hermes-root', fetchedAt: 123 }

test('clean checkout reports no conflict, leaving the normal up-to-date path untouched', () => {
  const { cwd, git } = createTempGitRepo()

  try {
    commitFile(cwd, git, 'app.ts', 'clean', 'root')

    assert.equal(
      resolveUpdateConflictResult({ ...ROW_BASE, rebaseInProgress: false, unmergedPaths: [] }),
      null
    )
  } finally {
    fs.rmSync(cwd, { recursive: true, force: true })
  }
})

test('dirty-but-unconflicted checkout reports no conflict (up-to-date verdict unchanged)', () => {
  const { cwd, git } = createTempGitRepo()

  try {
    commitFile(cwd, git, 'app.ts', 'clean', 'root')
    writeFile(cwd, 'app.ts', 'dirty worktree edit')

    assert.equal(
      resolveUpdateConflictResult({ ...ROW_BASE, rebaseInProgress: false, unmergedPaths: [] }),
      null
    )
  } finally {
    fs.rmSync(cwd, { recursive: true, force: true })
  }
})

test('parseUnmergedFileList dedupes stages and ignores non-entry lines', () => {
  const raw = [
    '100644 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa 1\tsrc/a.ts',
    '100644 bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb 2\tsrc/a.ts',
    '100644 cccccccccccccccccccccccccccccccccccccccc 3\tsrc/b.ts',
    '',
    'garbage without a tab'
  ].join('\n')

  assert.deepEqual(parseUnmergedFileList(raw), ['src/a.ts', 'src/b.ts'])
  assert.deepEqual(parseUnmergedFileList(''), [])
})

test('paused conflicted rebase stays an available update even when HEAD equals the fetched upstream tip', () => {
  const { cwd, git } = createTempGitRepo()

  try {
    commitFile(cwd, git, 'feature.txt', 'base\n', 'root')

    git('checkout', '-q', '-b', 'upstream-src')
    commitFile(cwd, git, 'feature.txt', 'upstream\n', 'upstream tip')
    const upstreamSha = git('rev-parse', 'HEAD')
    git('update-ref', 'refs/remotes/upstream/main', upstreamSha)
    git('checkout', '-q', 'main')

    commitFile(cwd, git, 'feature.txt', 'local fork commit\n', 'local fork work')
    try {
      git('rebase', 'refs/remotes/upstream/main')
    } catch {}

    assert.equal(rebasePaused(cwd), true)
    assert.equal(git('rev-parse', 'HEAD'), upstreamSha)
    assert.equal(git('rev-parse', '--abbrev-ref', 'HEAD'), 'HEAD')
    assert.equal(git('rev-list', 'HEAD..refs/remotes/upstream/main', '--count'), '0')

    const currentSha = git('rev-parse', 'HEAD')
    const unmergedPaths = parseUnmergedFileList(
      execFileSync('git', ['ls-files', '--unmerged'], { cwd, encoding: 'utf8', timeout: 10_000 })
    )
    assert.deepEqual(unmergedPaths, ['feature.txt'])

    const result = resolveUpdateConflictResult({
      ...ROW_BASE,
      currentBranch: 'HEAD',
      currentSha,
      rebaseInProgress: true,
      unmergedPaths
    })

    assert.notEqual(result, null)
    assert.equal(result?.updateAvailable, true)
    assert.equal(result?.reason, 'rebase-in-progress')
    assert.equal(result?.behind, null)
    assert.equal(result?.dirty, true)
    assert.equal('error' in (result ?? {}), false)
    assert.match(result?.message ?? '', /feature\.txt/)
    assert.match(result?.message ?? '', /git rebase --continue/)
    assert.match(result?.message ?? '', /git rebase --abort/)

    git('rebase', '--abort')
    assert.equal(rebasePaused(cwd), false)
    assert.equal(
      resolveUpdateConflictResult({ ...ROW_BASE, rebaseInProgress: false, unmergedPaths: [] }),
      null
    )
  } finally {
    fs.rmSync(cwd, { recursive: true, force: true })
  }
}, 30_000)

test('unmerged index without a paused rebase reports an unresolved-conflict recovery row', () => {
  const { cwd, git } = createTempGitRepo()

  try {
    commitFile(cwd, git, 'feature.txt', 'base\n', 'root')

    git('checkout', '-q', '-b', 'side')
    commitFile(cwd, git, 'feature.txt', 'side\n', 'side work')
    git('checkout', '-q', 'main')
    commitFile(cwd, git, 'feature.txt', 'main\n', 'main work')

    try {
      git('merge', 'side')
    } catch {}

    assert.equal(rebasePaused(cwd), false)
    assert.notEqual(
      execFileSync('git', ['ls-files', '--unmerged'], { cwd, encoding: 'utf8', timeout: 10_000 }).trim(),
      ''
    )

    const currentSha = git('rev-parse', 'HEAD')
    const unmergedPaths = parseUnmergedFileList(
      execFileSync('git', ['ls-files', '--unmerged'], { cwd, encoding: 'utf8', timeout: 10_000 })
    )

    const result = resolveUpdateConflictResult({
      ...ROW_BASE,
      currentBranch: 'main',
      currentSha,
      rebaseInProgress: false,
      unmergedPaths
    })

    assert.notEqual(result, null)
    assert.equal(result?.updateAvailable, true)
    assert.equal(result?.reason, 'unmerged-index')
    assert.equal(result?.behind, null)
    assert.equal('error' in (result ?? {}), false)
    assert.match(result?.message ?? '', /feature\.txt/)

    git('merge', '--abort')
    assert.equal(
      resolveUpdateConflictResult({ ...ROW_BASE, rebaseInProgress: false, unmergedPaths: [] }),
      null
    )
  } finally {
    fs.rmSync(cwd, { recursive: true, force: true })
  }
}, 30_000)

test('conflict copy caps the file list and names the pause without conflicts', () => {
  const manyPaths = ['a.ts', 'b.ts', 'c.ts', 'd.ts', 'e.ts', 'f.ts', 'g.ts']

  const unmerged = resolveUpdateConflictResult({
    ...ROW_BASE,
    rebaseInProgress: false,
    unmergedPaths: manyPaths
  })

  assert.match(unmerged?.message ?? '', /a\.ts, b\.ts, c\.ts, d\.ts, e\.ts and 2 more/)

  const rebase = resolveUpdateConflictResult({
    ...ROW_BASE,
    rebaseInProgress: true,
    unmergedPaths: []
  })

  assert.notEqual(rebase, null)
  assert.equal(rebase?.dirty, false)
  assert.equal(rebase?.reason, 'rebase-in-progress')
  assert.doesNotMatch(rebase?.message ?? '', /conflicts in/)
  assert.match(rebase?.message ?? '', /git rebase --continue/)
})
