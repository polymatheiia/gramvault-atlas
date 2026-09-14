import { defineConfig, mergeConfig } from 'vitest/config'
import viteConfig from './vite.config'

// Separate from vite.config.ts because vite's own `defineConfig` doesn't
// know about vitest's `test` option — merging keeps the dev/build config
// (the react plugin, the /api proxy) as the single source of truth rather
// than duplicating it here.
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: 'jsdom',
      setupFiles: ['./src/test/setup.ts'],
      css: false,
      restoreMocks: true,
      unstubGlobals: true,
      unstubEnvs: true,
    },
  }),
)
