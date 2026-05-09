export function logError(error: unknown): void {
  if (!process.env.SINOCLAW_INK_DEBUG_ERRORS) {
    return
  }

  console.error(error)
}
