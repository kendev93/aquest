import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  cancelStrategyDiscovery,
  cancelStrategyReplay,
  createStrategyDiscovery,
  createStrategyReplay,
  deleteStrategyDiscoveryHistory,
  fetchStrategyDiscoveryCapabilities,
  fetchStrategyDiscoveryHistory,
  getStrategyDiscovery,
  getStrategyDiscoveryResults,
  getStrategyReplay,
  getStrategyReplayHistory,
  getStrategyReplayResults,
} from './strategyDiscovery'

describe('strategy discovery API', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('creates a task, reads results and history through the local API', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { algorithm: 'optuna_tpe_lightgbm_surrogate' } }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { job_id: 'discovery-1', status: 'queued' } }), { status: 202 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { job_id: 'discovery-1', status: 'completed', progress: 1, total_candidates: 2, total_scanned: 2, errors: 0, skipped: 0, error: null, result: null } }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { results_by_sharpe: [], results_by_return: [], request: {}, search_metadata: {}, validation_metadata: {} } }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: [{ history_id: 'discovery-1' }] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { job_id: 'discovery-1', status: 'cancelling', cancel_requested: true } }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { deleted: true } }), { status: 200 }))

    await expect(fetchStrategyDiscoveryCapabilities()).resolves.toMatchObject({ algorithm: 'optuna_tpe_lightgbm_surrogate' })
    await expect(createStrategyDiscovery({ start_date: '2024-01-01', end_date: '2024-12-31', max_positions: 3, universe: 'all' })).resolves.toEqual({ job_id: 'discovery-1', status: 'queued' })
    await expect(getStrategyDiscovery('discovery-1')).resolves.toMatchObject({ status: 'completed' })
    await expect(getStrategyDiscoveryResults('discovery-1')).resolves.toMatchObject({ results_by_sharpe: [] })
    await expect(fetchStrategyDiscoveryHistory()).resolves.toEqual([{ history_id: 'discovery-1' }])
    await expect(cancelStrategyDiscovery('discovery-1')).resolves.toEqual({ job_id: 'discovery-1', status: 'cancelling', cancel_requested: true })
    await expect(deleteStrategyDiscoveryHistory('discovery-1')).resolves.toEqual({ deleted: true })

    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method ?? 'GET'])).toEqual([
      ['/api/v1/strategy-discovery/capabilities', 'GET'],
      ['/api/v1/strategy-discovery', 'POST'],
      ['/api/v1/strategy-discovery/discovery-1', 'GET'],
      ['/api/v1/strategy-discovery/discovery-1/results', 'GET'],
      ['/api/v1/strategy-discovery/history', 'GET'],
      ['/api/v1/strategy-discovery/discovery-1/cancel', 'POST'],
      ['/api/v1/strategy-discovery/history/discovery-1', 'DELETE'],
    ])
  })

  it('surfaces API error messages', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(new Response(JSON.stringify({ error: { message: '策略实验失败' } }), { status: 422 }))

    await expect(fetchStrategyDiscoveryCapabilities()).rejects.toThrow('策略实验失败')
  })

  it('retries GET requests on non-JSON gateway failures but does not retry POST', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response('<html>bad gateway</html>', { status: 502 }))
      .mockResolvedValueOnce(new Response('<html>bad gateway</html>', { status: 502 }))
      .mockResolvedValueOnce(new Response('<html>bad gateway</html>', { status: 502 }))

    await expect(fetchStrategyDiscoveryHistory()).rejects.toThrow('HTTP 502')
    expect(fetchMock).toHaveBeenCalledTimes(3)

    fetchMock.mockReset()
    fetchMock.mockResolvedValueOnce(new Response('<html>bad gateway</html>', { status: 502 }))
    await expect(createStrategyDiscovery({ universe: 'all', max_positions: 2 })).rejects.toThrow('HTTP 502')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('creates, polls, cancels, and reads a full-range replay', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { job_id: 'replay-1', status: 'queued' } }), { status: 202 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { job_id: 'replay-1', status: 'running' } }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { candidate_count: 10 } }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { job_id: 'replay-1', status: 'cancelling', cancel_requested: true } }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { replay_id: 'replay-1' } }), { status: 200 }))

    await expect(createStrategyReplay('experiment-1', 'sharpe')).resolves.toEqual({ job_id: 'replay-1', status: 'queued' })
    await expect(getStrategyReplay('replay-1')).resolves.toEqual({ job_id: 'replay-1', status: 'running' })
    await expect(getStrategyReplayResults('replay-1')).resolves.toEqual({ candidate_count: 10 })
    await expect(cancelStrategyReplay('replay-1')).resolves.toEqual({ job_id: 'replay-1', status: 'cancelling', cancel_requested: true })
    await expect(getStrategyReplayHistory('experiment-1', 'sharpe')).resolves.toEqual({ replay_id: 'replay-1' })
    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method ?? 'GET'])).toEqual([
      ['/api/v1/strategy-discovery/experiment-1/replays', 'POST'],
      ['/api/v1/strategy-discovery/replay-jobs/replay-1', 'GET'],
      ['/api/v1/strategy-discovery/replay-jobs/replay-1/results', 'GET'],
      ['/api/v1/strategy-discovery/replay-jobs/replay-1/cancel', 'POST'],
      ['/api/v1/strategy-discovery/history/experiment-1/replays?leaderboard=sharpe', 'GET'],
    ])
  })
})
