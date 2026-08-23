const TOKEN_KEY = 'kaupo.api.token'

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
}
