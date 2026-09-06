import type { Config, FilePlan, JobEvent, PreviewResult } from './types'

const BASE = ''

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(BASE + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  })
  const data = await res.json().catch(() => ({ error: `本地服务无响应 (${res.status})` }))
  if (!res.ok) throw new Error(data.error || `请求失败 (${res.status})`)
  return data as T
}

export type SkippedFile = { path: string; name: string; reason: 'output_dir' | 'manifest' }

export type CollectResult = {
  count: number
  plan: FilePlan[]
  unsupported: string[]
  skipped: SkippedFile[]
  suggested_output: string
}

export type BatchItem = {
  path: string
  name: string
  ok: boolean
  size?: number
  rows?: number | null
  cols?: number | null
  columns?: string[]
  encoding?: string | null
  delimiter?: string | null
  warnings?: string[]
  error?: string
}

export type Meta = {
  version: string
  native: boolean
  static_ready: boolean
  theme?: 'dark' | 'light' | 'system'
}

export type BrowseResult = {
  drives: { name: string; path: string }[]
  path: string | null
  parent: string | null
  dirs: { name: string; path: string }[]
  files?: { name: string; path: string }[]
  error?: string
  truncated?: boolean
}

export const api = {
  meta: () => fetch(BASE + '/api/meta').then((r) => r.json() as Promise<Meta>),

  collect: (body: {
    files: string[]; dirs: string[]; recursive: boolean; output_dir: string; config?: Config
  }) => post<CollectResult>('/api/collect', body),

  /** 只做表头级扫描 (行列数/编码/引擎), 不跑清洗 —— 所以不送 config, 免得看着像"按规则算过" */
  inspect: (paths: string[]) =>
    post<{ items: BatchItem[]; truncated: boolean; config_applied?: boolean }>('/api/inspect', { paths }),

  preview: (path: string, config: Config) =>
    post<PreviewResult>('/api/preview', { path, config }),

  dialog: (kind: 'files' | 'folder') =>
    post<{ ok: boolean; paths?: string[]; reason?: string }>('/api/dialog', { kind }),

  browse: (path: string | null, includeFiles = false) =>
    post<BrowseResult>('/api/fs/list', { path, include_files: includeFiles }),

  run: (paths: string[], output_dir: string, config: Config) =>
    post<{ job_id: string; total: number }>('/api/run', { paths, output_dir, config }),

  cancel: (job_id: string) => post<{ ok: boolean }>(`/api/jobs/${job_id}/cancel`, {}),
}

/** 进度流。SSE 断线时 EventSource 自带重连，服务端按 seq 重放，进度不会停住。 */
export function pushJobEvents(
  jobId: string,
  onEvent: (e: JobEvent) => void,
  onError?: () => void,
): () => void {
  const es = new EventSource(`${BASE}/api/jobs/${jobId}/events`)
  es.onmessage = (frame) => {
    try {
      onEvent(JSON.parse(frame.data) as JobEvent)
    } catch {
      /* keep-alive 帧不是 JSON, 忽略 */
    }
  }
  es.onerror = () => onError?.()
  return () => es.close()
}

export type UploadResult = { paths: string[]; dir: string; rejected?: string[] }

export async function uploadDropped(files: File[]): Promise<UploadResult> {
  const form = new FormData()
  files.forEach((f) => form.append('files', f))
  const res = await fetch('/api/upload', { method: 'POST', body: form })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.error || '拖拽接收失败')
  return { paths: data.paths as string[], dir: data.dir as string,
    rejected: (data.rejected as string[]) || undefined }
}

/** 在系统文件管理器里定位输出文件。只有桌面壳能用, 失败时前端退回提示。 */
export async function revealInExplorer(path: string): Promise<boolean> {
  try {
    const res = await post<{ ok: boolean }>('/api/reveal', { path })
    return Boolean(res.ok)
  } catch {
    return false
  }
}
