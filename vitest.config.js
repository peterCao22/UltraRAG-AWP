import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    environment: 'happy-dom',
    include: ['custom_app/frontend/__tests__/**/*.test.js'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html'],
      include: [
        'custom_app/frontend/main.js',
        'custom_app/frontend/components/**/*.js',
        'custom_app/frontend/services/**/*.js',
        'custom_app/frontend/store/**/*.js',
        'custom_app/frontend/utils/**/*.js',
      ],
      exclude: [
        'custom_app/frontend/admin.js',
        'custom_app/frontend/vendor/**',
        'custom_app/frontend/js/**',
        'custom_app/frontend/__tests__/**',
      ],
      thresholds: {
        branches: 80,
        functions: 80,
        lines: 80,
        statements: 80,
      },
    },
  },
})
