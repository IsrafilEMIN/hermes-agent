/**
 * Pure helpers for choosing a remote URL during passive update checks.
 *
 * A public install can end up with `origin=git@github.com:NousResearch/hermes-agent.git`.
 * If the user's GitHub SSH key is FIDO2/passkey-backed, a background `git fetch
 * origin` triggers an unexplained hardware-touch prompt. For passive checks
 * against the official repo we substitute the public HTTPS `ls-remote` path,
 * which needs no auth and cannot prompt. Active update/apply flows are left
 * unchanged.
 *
 * Extracted from main.ts so the security-critical remote detection is unit
 * testable without booting Electron (main.ts requires('electron') at load).
 */

const OFFICIAL_REPO_HTTPS_URL = 'https://github.com/NousResearch/hermes-agent.git'
const OFFICIAL_REPO_CANONICAL = 'github.com/nousresearch/hermes-agent'

// Normalize common GitHub remote URL forms to `host/owner/repo` (lowercased,
// no trailing slash, no .git suffix) so SSH and HTTPS forms of the same repo
// compare equal.
function canonicalGitHubRemote(url) {
  if (!url) {
    return ''
  }

  let value = String(url).trim()

  if (value.startsWith('git@github.com:')) {
    value = `github.com/${value.slice('git@github.com:'.length)}`
  } else if (value.startsWith('ssh://git@github.com/')) {
    value = `github.com/${value.slice('ssh://git@github.com/'.length)}`
  } else {
    try {
      const parsed = new URL(value)

      if (parsed.hostname && parsed.pathname) {
        value = `${parsed.hostname}${parsed.pathname}`
      }
    } catch {
      // Leave non-URL forms unchanged.
    }
  }

  value = value.trim().replace(/\/+$/, '')

  if (value.endsWith('.git')) {
    value = value.slice(0, -4)
  }

  return value.toLowerCase()
}

function isSshRemote(url) {
  const value = String(url || '')
    .trim()
    .toLowerCase()

  return value.startsWith('git@') || value.startsWith('ssh://')
}

function isOfficialSshRemote(url) {
  return isSshRemote(url) && canonicalGitHubRemote(url) === OFFICIAL_REPO_CANONICAL
}

/**
 * Pick the remote Desktop should compare against. Matches CLI
 * `hermes update --check`: on `main`, prefer `upstream` when that remote
 * exists so a fork that is current with origin still sees official commits.
 * Non-main branches stay on origin — a fork's `bb/gui` typically has no
 * upstream counterpart.
 */
function resolveUpdateCompareRemote({
  branch,
  originUrl,
  upstreamUrl
}: {
  branch: string
  originUrl?: string
  upstreamUrl?: string
}): { remote: 'origin' | 'upstream'; compareUrl: string } {
  const origin = String(originUrl || '').trim()
  const upstream = String(upstreamUrl || '').trim()

  if (branch === 'main' && upstream) {
    return { remote: 'upstream', compareUrl: upstream }
  }

  return { remote: 'origin', compareUrl: origin }
}

export {
  canonicalGitHubRemote,
  isOfficialSshRemote,
  isSshRemote,
  OFFICIAL_REPO_CANONICAL,
  OFFICIAL_REPO_HTTPS_URL,
  resolveUpdateCompareRemote
}
