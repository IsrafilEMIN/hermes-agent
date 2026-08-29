import type { Usage, UsageQuotaAccount } from '../types.js'

export const ZERO: Usage = { calls: 0, input: 0, output: 0, total: 0 }

const remainingQuota = (used: number | undefined): string => {
  if (used === undefined || !Number.isFinite(used)) {
    return '?'
  }

  return String(Math.min(100, Math.max(0, Math.round(100 - used))))
}

const usageProviderLabel = (provider: string): string => {
  if (provider === 'openai-codex' || provider === 'openai') return 'GPT'
  if (provider === 'opencode-go') return 'GO'
  if (provider === 'cursor') return 'CURSOR'
  if (provider === 'xai' || provider === 'xai-oauth') return 'GROK'

  return provider
}

const quotaWindows = (account: UsageQuotaAccount): (number | undefined)[] => {
  if (account.provider === 'opencode-go') {
    return [account.five_hour, account.seven_day, account.monthly]
  }

  if (account.provider === 'cursor') {
    return account.monthly_other === undefined ? [account.monthly] : [account.monthly, account.monthly_other]
  }

  return [account.five_hour, account.seven_day]
}

export const formatQuotaChip = (accounts: UsageQuotaAccount[] | null | undefined): string => {
  if (!accounts || accounts.length === 0) return ''

  const label = usageProviderLabel(accounts[0].provider)
  const parts = accounts.map(
    account => `${account.active ? '●' : '○'} ${quotaWindows(account).map(remainingQuota).join('/')}`
  )

  return `${label} ${parts.join(' ')}`
}
