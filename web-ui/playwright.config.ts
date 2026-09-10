import { defineConfig, devices } from '@playwright/test'

const chromeExecutable = process.env.PLAYWRIGHT_CHROME_PATH
const playwrightPort = process.env.PLAYWRIGHT_PORT ?? '5173'

export default defineConfig({
  testDir: './e2e',
  use: {
    baseURL: `http://127.0.0.1:${playwrightPort}`,
    trace: 'retain-on-failure',
    ...(chromeExecutable ? { launchOptions: { executablePath: chromeExecutable } } : {}),
  },
  webServer: {
    command: `npm run dev -- --host 127.0.0.1 --port ${playwrightPort}`,
    url: `http://127.0.0.1:${playwrightPort}`,
    reuseExistingServer: true,
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
})
