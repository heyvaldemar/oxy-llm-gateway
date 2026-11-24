import { describe, expect, it } from 'vitest'

import { paymentRequiredNotice } from './api'

describe('paymentRequiredNotice', () => {
  it('says a spent budget is spent, and that nothing resets it', () => {
    const notice = paymentRequiredNotice({
      error: { code: 'BUDGET_EXCEEDED', message: 'Budget limit of $5 exceeded.' },
    })
    expect(notice.title).toBe('Budget spent')
    expect(notice.billing).toBe(false)
    expect(notice.description).toContain('nothing resets it')
    expect(notice.description).not.toMatch(/subscription|billing/i)
  })

  it('keeps the billing notice for any other 402', () => {
    for (const body of [null, {}, { detail: 'No active subscription' }, { error: { code: 'PLAN_REQUIRED' } }, 'text']) {
      const notice = paymentRequiredNotice(body)
      expect(notice.title).toBe('Subscription Required')
      expect(notice.billing).toBe(true)
    }
  })
})
