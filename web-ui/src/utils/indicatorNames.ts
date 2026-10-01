import type { FormulaScreenMetadata } from '../types'

export type IndicatorNameMap = ReadonlyMap<string, string>

/** Build one UI label map from the shared indicator catalog metadata. */
export function buildIndicatorNameMap(
  metadata: FormulaScreenMetadata | null | undefined,
): Map<string, string> {
  const names = new Map<string, string>()
  for (const indicator of metadata?.indicators ?? []) {
    for (const value of indicator.values ?? []) {
      names.set(value.id, value.display_name)
    }
    for (const signal of indicator.signals ?? []) {
      const displayName = signal.display_name === '始' || signal.display_name === '终'
        ? `${indicator.display_name} · ${signal.display_name}（${signal.description}）`
        : signal.display_name
      names.set(signal.id, displayName)
    }
  }
  return names
}

/** Keep unknown or user-defined IDs visible so a missing label never hides information. */
export function indicatorDisplayName(names: IndicatorNameMap, id: string): string {
  return names.get(id) ?? id
}
