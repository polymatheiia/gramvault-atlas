import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

// RTL's automatic per-test cleanup relies on detecting a global test
// framework; this project doesn't enable vitest's `globals` option (tests
// import describe/it/expect explicitly instead), so register it by hand —
// without this, each test's rendered tree piles up in the jsdom body
// instead of being unmounted before the next test runs.
afterEach(() => {
  cleanup()
})
