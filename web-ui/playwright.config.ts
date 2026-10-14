import { defineConfig, devices } from '@playwright/test'

const chromeExecutable = process.env.PLAYWRIGHT_CHROME_PATH
// E2E 用独立端口，默认就避开 5173 上常驻的人为 dev server（本机那个端口常年被容器占着）。
const playwrightPort = process.env.PLAYWRIGHT_PORT ?? '5199'

export default defineConfig({
  testDir: './e2e',
  use: {
    baseURL: `http://127.0.0.1:${playwrightPort}`,
    trace: 'retain-on-failure',
    ...(chromeExecutable ? { launchOptions: { executablePath: chromeExecutable } } : {}),
  },
  webServer: {
    // --strictPort：端口被占时 vite 默认自己挪到 5174，而 Playwright 仍然去轮询配置里
    // 的端口——于是它连上的是"住在默认端口上的那个别人"，测试跑的到底是哪份代码没人知道。
    command: `npm run dev -- --host 127.0.0.1 --port ${playwrightPort} --strictPort`,
    url: `http://127.0.0.1:${playwrightPort}`,
    // E2E 是提交前的门禁，必须测本次工作区构建出来的那套代码，所以从不复用已在跑的
    // 服务器（旧 dev server 可能来自另一个分支/worktree）。端口被占时宁可当场报错，
    // 让用 `sh scripts/verify_ci.sh frontend` 的人换个 PLAYWRIGHT_PORT 再来一次。
    reuseExistingServer: false,
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
})
