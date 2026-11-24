/**
 * API utilities
 * Centralized API client with built-in CSRF protection, authentication, and error handling
 * 
 * SECURITY: All POST/PUT/DELETE requests include:
 * - credentials: 'include' → Sends cookies (CSRF token)
 * - X-CSRF-Token header → Double-submit cookie pattern
 * - Authorization header → JWT bearer token
 */

/**
 * Extract cookie value by name
 * Used for CSRF token extraction
 */
export const getCookie = (name: string): string => {
  const match = document.cookie.match('(^|;)\\s*' + name + '\\s*=\\s*([^;]+)')
  return match ? match.pop() || '' : ''
}

/**
 * Make authenticated API request with CSRF protection
 * 
 * @param url - Full API URL (including NEXT_PUBLIC_API_URL)
 * @param options - Fetch options
 * @param token - JWT access token from Supabase session
 * @returns Fetch response
 * 
 * @example
 * const response = await authenticatedFetch(
 *   `${process.env.NEXT_PUBLIC_API_URL}/management/onboard`,
 *   {
 *     method: 'POST',
 *     body: JSON.stringify({ full_name: 'John' })
 *   },
 *   session.access_token
 * )
 */
export async function authenticatedFetch(
  url: string,
  options: RequestInit = {},
  token: string
): Promise<Response> {
  const method = options.method?.toUpperCase() || 'GET'
  const isStateMutating = ['POST', 'PUT', 'DELETE', 'PATCH'].includes(method)

  // Build headers
  const headers = new Headers(options.headers || {})
  
  // Always include Authorization
  headers.set('Authorization', `Bearer ${token}`)
  
  // Set Content-Type if not already set and body exists
  if (options.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  
  // CRITICAL: Add CSRF token for state-mutating operations
  if (isStateMutating) {
    const csrfToken = getCookie('csrf_token')
    if (csrfToken) {
      headers.set('X-CSRF-Token', csrfToken)
    }
  }

  return fetch(url, {
    ...options,
    method,
    headers,
    // CRITICAL: Include credentials to send cookies (CSRF token)
    credentials: 'include',
  })
}

/**
 * Parse API error response into user-friendly message
 * 
 * @param response - Failed fetch response
 * @returns Error message string
 */
export async function handleApiError(response: Response): Promise<string> {
  let errorMsg = `Server returned ${response.status}. Please try again.`
  
  try {
    const errorData = await response.json()
    
    if (errorData.detail) {
      if (typeof errorData.detail === 'string') {
        errorMsg = errorData.detail
      } else if (Array.isArray(errorData.detail)) {
        errorMsg = errorData.detail.map((e: any) => e.msg || JSON.stringify(e)).join(', ')
      }
    } else if (errorData.error) {
      errorMsg = errorData.error
    } else if (errorData.message) {
      errorMsg = errorData.message
    }
  } catch {
    // If we can't parse JSON, use default message
  }
  
  return errorMsg
}

/**
 * What a 402 means to the person looking at the dashboard.
 *
 * The gateway answers 402 BUDGET_EXCEEDED when a project has spent its whole
 * budget, and nothing resets a budget, so that notice says to raise it. Any
 * other 402 is a plan or billing refusal (the optional billing module, the
 * SSO add-on) and keeps the billing notice.
 */
export interface PaymentRequiredNotice {
  title: string
  description: string
  billing: boolean
}

export function paymentRequiredNotice(body: unknown): PaymentRequiredNotice {
  const error = (body as { error?: { code?: unknown } } | null)?.error
  if (error && typeof error === 'object' && error.code === 'BUDGET_EXCEEDED') {
    return {
      title: 'Budget spent',
      description:
        "This project has spent its whole budget and nothing resets it. Raise the budget in the project's settings; until then its requests are refused.",
      billing: false,
    }
  }
  return {
    title: 'Subscription Required',
    description: 'Your subscription has expired or requires payment. Please update your billing.',
    billing: true,
  }
}

/**
 * Safe error message extractor for catch blocks
 * 
 * @param error - Unknown error from catch block
 * @returns User-friendly error message
 */
export function safeErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    if (error.name === 'AbortError') {
      return 'Request timed out. Please try again.'
    }
    if (error.message.includes('Failed to fetch')) {
      return 'Cannot connect to API server. Please ensure the engine is running.'
    }
    return error.message
  }
  return 'An unexpected error occurred.'
}

