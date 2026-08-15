const TERMUX_SAFE_PROMPT = '>'

export function composerPromptText(
  prompt: string,
  profileName?: null | string,
  shellMode = false,
  termuxMode = false,
  totalCols?: number
): string {
  if (shellMode) {
    return '$'
  }

  // profileName/totalCols are retained for signature compatibility with call
  // sites; the composer prompt no longer carries the active profile name.
  if (termuxMode) {
    // Termux fonts/terminal backends can render decorative prompt glyphs with
    // ambiguous width; keep the live composer marker strictly single-cell ASCII
    // so we never leave stale arrow artifacts while typing.
    return TERMUX_SAFE_PROMPT
  }

  return prompt
}
