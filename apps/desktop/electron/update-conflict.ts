function describeConflictedPaths(paths: string[]): string {
  const shown = paths.slice(0, 5)
  const text = shown.join(', ')
  const remaining = paths.length - shown.length

  return remaining > 0 ? `${text} and ${remaining} more` : text
}

function parseUnmergedFileList(raw: string): string[] {
  const seen = new Set<string>()
  const paths: string[] = []

  for (const line of String(raw || '').split('\n')) {
    const tab = line.indexOf('\t')

    if (tab === -1) {
      continue
    }

    const filePath = line.slice(tab + 1).trim()

    if (filePath && !seen.has(filePath)) {
      seen.add(filePath)
      paths.push(filePath)
    }
  }

  return paths
}

function resolveUpdateConflictResult({
  branch,
  currentBranch,
  currentSha,
  fetchedAt,
  hermesRoot,
  rebaseInProgress,
  unmergedPaths = []
}: {
  branch?: string
  currentBranch?: string
  currentSha?: string
  fetchedAt?: number
  hermesRoot: string
  rebaseInProgress: boolean
  unmergedPaths?: string[]
}) {
  if (!rebaseInProgress && unmergedPaths.length === 0) {
    return null
  }

  if (rebaseInProgress) {
    const conflictPart =
      unmergedPaths.length > 0 ? ` with conflicts in ${describeConflictedPaths(unmergedPaths)}` : ''

    return {
      supported: true,
      branch,
      currentBranch,
      behind: null,
      updateAvailable: true,
      currentSha,
      dirty: unmergedPaths.length > 0,
      reason: 'rebase-in-progress',
      message:
        `A git rebase is paused in the source tree${conflictPart}. ` +
        'Run `git rebase --continue` to finish the update, or `git rebase --abort` to cancel it ' +
        'and restore your previous checkout. Check for updates again once the rebase is resolved.',
      hermesRoot,
      fetchedAt
    }
  }

  return {
    supported: true,
    branch,
    currentBranch,
    behind: null,
    updateAvailable: true,
    currentSha,
    dirty: true,
    reason: 'unmerged-index',
    message:
      `The source tree has unresolved merge conflicts in ${describeConflictedPaths(unmergedPaths)}. ` +
      'Resolve each conflicted file and commit the result, then check for updates again.',
    hermesRoot,
    fetchedAt
  }
}

export { parseUnmergedFileList, resolveUpdateConflictResult }
