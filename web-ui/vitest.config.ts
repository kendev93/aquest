import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    exclude: ['node_modules', 'e2e'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html'],
      include: ['src/**/*.{ts,vue}'],
      thresholds: { lines: 85, functions: 85, statements: 85, branches: 75 },
      // 纯类型声明（2842 行 import type / export interface）没有一行可执行语句，
      // 计入分母只会把指标拉低：vitest 5 修好 v8 的 sourcemap 映射后不再自动忽略
      // 它们（vitest 3 下这些文件不参与统计），statements/functions 因此掉了 2~3 个
      // 点。类型正确性归 vue-tsc 管，不归覆盖率管。
      // main.ts / App.vue 只剩启动与路由外壳（挂载、keep-alive），没有可断言的分支。
      // HistoryComparePage 和 PortfolioBacktestPage 曾经也在这里，理由是 v8 把这两个
      // .vue 的行覆盖算错了（跑过 36 个用例还恒报 6.6%）。vitest 5.0.3 实测已经回到
      // 87.9% / 82.9%，所以它们重新计入门禁。
      exclude: [
        'src/main.ts',
        'src/App.vue',
        'src/types.ts',
        'src/types/**',
        'src/env.d.ts',
      ],
    },
  },
})
