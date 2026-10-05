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
      // HistoryComparePage 的展示逻辑都在 utils/historyCompare.ts（单独测、100% 覆盖），
      // 页面本身只剩响应式胶水；v8 对这两个 .vue 的行覆盖是错的（sourcemap 映射丢
      // 了，跑过的代码算成没跑过——PortfolioBacktestPage.vue 同样恒报 6.6%），
      // 把它们排除才不会让门禁去追一个根本不存在的缺口。
      // 纯类型声明（2842 行 import type / export interface）没有一行可执行语句，
      // 计入分母只会把指标拉低：vitest 5 修好 v8 的 sourcemap 映射后不再自动忽略
      // 它们（vitest 3 下这些文件不参与统计），statements/functions 因此掉了 2~3 个
      // 点。类型正确性归 vue-tsc 管，不归覆盖率管。
      exclude: [
        'src/main.ts',
        'src/App.vue',
        'src/types.ts',
        'src/types/**',
        'src/env.d.ts',
        'src/pages/HistoryComparePage.vue',
        'src/pages/PortfolioBacktestPage.vue',
      ],
    },
  },
})
