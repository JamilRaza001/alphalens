import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    // splitFrames and parseFrame are pure string functions — no DOM needed.
    environment: 'node',
    include: ['lib/**/*.test.ts'],
  },
})
