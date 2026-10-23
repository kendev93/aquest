/** 上游版本状态：后端每天查一次发布记录，前端只读结论。 */

export interface ReleaseInfo {
  /** 发布标签，形如 `v0.1.8`。 */
  tag: string
  version: string
  /** 发布页地址，提示点进去就是它。 */
  url: string
  published_at: string | null
}

export interface ReleaseStatus {
  /** 本机正在跑的版本。 */
  current: string
  repository: string
  /** `AQUEST_UPDATE_CHECK=off` 时为 false，此时 `latest` 永远是 null。 */
  enabled: boolean
  update_available: boolean
  /** 没查到、被判为不可比较或检查关闭时都是 null。 */
  latest: ReleaseInfo | null
  checked_at: string | null
  error: string | null
}
