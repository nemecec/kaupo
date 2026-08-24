import { describe, expect, it } from 'vitest'
import { formatNumber } from './format'

describe('formatNumber', () => {
  it('normalizes -0 to 0', () => {
    expect(formatNumber(-0)).toBe('0')
    expect(formatNumber(0)).toBe('0')
  })

  it('renders dust values with significant digits instead of "0"', () => {
    expect(formatNumber(0.0000001)).toBe('1.0e-7')
    expect(formatNumber(-0.0000001)).toBe('-1.0e-7')
    expect(formatNumber(0.000000499)).toBe('5.0e-7')
  })

  it('keeps fixed-fraction formatting for regular values', () => {
    expect(formatNumber(0.5)).toBe('0.5')
    expect(formatNumber(0.000001)).toBe('0.000001')
    expect(formatNumber(12.3456)).toBe('12.3456')
    expect(formatNumber(1234.567)).toBe('1,234.57')
  })
})
