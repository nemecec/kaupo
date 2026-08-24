const TOKEN_KEY = 'kaupo.api.token'

/** Fired on window after setToken, so listeners can re-check access. */
export const TOKEN_CHANGED_EVENT = 'kaupo:token-changed'

/** Bearer token for the API, persisted in localStorage. Empty/absent means anonymous. */
export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) {
      localStorage.setItem(TOKEN_KEY, token)
    } else {
      localStorage.removeItem(TOKEN_KEY)
    }
  } catch {
    // storage unavailable (private mode etc.) — token simply won't persist
  }
  window.dispatchEvent(new Event(TOKEN_CHANGED_EVENT))
}
