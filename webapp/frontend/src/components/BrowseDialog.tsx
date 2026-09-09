import { useCallback, useEffect, useState } from 'react'
import {
  ArrowUpIcon, HardDrivesIcon, CaretRightIcon, XIcon, CheckIcon, FolderSimpleIcon,
} from '@phosphor-icons/react'
import { Button, cn } from './ui'
import { api, type BrowseResult } from '../api'
import { DASH } from '../lib/format'

/**
 * 内联目录浏览器。
 * 原生对话框(系统资源管理器那种)优先; 但浏览器模式、或用户取消原生对话框时,
 * 必须还有一条能走通的路, 否则输出目录就填不进去。
 */
export function BrowseDialog({
  kind,
  initialPath,
  onPick,
  onClose,
}: {
  kind: 'folder' | 'files'
  initialPath: string
  onPick: (paths: string[]) => void
  onClose: () => void
}) {
  const [data, setData] = useState<BrowseResult | null>(null)
  const [pickedFiles, setPickedFiles] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const go = useCallback(async (path: string | null) => {
    setLoading(true)
    setError(null)
    try {
      const res = await api.browse(path, kind === 'files')
      setData(res)
      if (res.error) setError(res.error)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [kind])

  useEffect(() => {
    void go(initialPath || null)
  }, [go, initialPath])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const toggleFile = (path: string) =>
    setPickedFiles((prev) => (prev.includes(path) ? prev.filter((p) => p !== path) : [...prev, path]))

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 p-6"
      role="dialog"
      aria-modal="true"
      aria-label={kind === 'folder' ? '选择输出目录' : '选择文件'}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div className="panel flex h-[min(620px,86vh)] w-[min(720px,94vw)] flex-col overflow-hidden anim-rise">
        <div className="flex h-11 shrink-0 items-center gap-2 border-b border-line px-3">
          <span className="text-[13px] font-semibold">{kind === 'folder' ? '选择目录' : '选择文件'}</span>
          <span className="num min-w-0 flex-1 truncate text-[11.5px] text-faint" title={data?.path ?? ''}>
            {data?.path ?? DASH}
          </span>
          <button type="button" onClick={onClose} className="rounded-[6px] p-1 text-faint hover:bg-surface2 hover:text-ink" aria-label="关闭">
            <XIcon size={15} />
          </button>
        </div>

        <div className="flex shrink-0 flex-wrap items-center gap-1.5 border-b border-line bg-surface2 px-3 py-2">
          <Button size="sm" variant="ghost" onClick={() => go(data?.parent ?? null)} disabled={!data?.parent}>
            <ArrowUpIcon size={13} /> 上一级
          </Button>
          {(data?.drives ?? []).map((d) => (
            <button
              key={d.path}
              type="button"
              onClick={() => go(d.path)}
              className={cn(
                'chip num cursor-pointer hover:border-line2 hover:text-ink',
                data?.path?.toLowerCase().startsWith(d.path.toLowerCase()) && 'chip-accent',
              )}
            >
              <HardDrivesIcon size={11} /> {d.name}
            </button>
          ))}
        </div>

        <div className="min-h-0 flex-1 overflow-auto">
          {error && (
            <div className="m-3 rounded-[var(--radius-control)] border border-err/40 bg-err/10 px-3 py-2 text-[12px] text-err">
              {error}
            </div>
          )}
          <ul>
            {(data?.dirs ?? []).map((d) => (
              <li key={d.path}>
                <button
                  type="button"
                  onClick={() => go(d.path)}
                  className="flex w-full items-center gap-2 border-b border-line/50 px-3 py-1.5 text-left text-[12.5px] hover:bg-surface2"
                >
                  <FolderSimpleIcon size={15} weight="fill" className="shrink-0 text-warn/70" />
                  <span className="truncate">{d.name}</span>
                  <CaretRightIcon size={11} className="ml-auto shrink-0 text-faint" />
                </button>
              </li>
            ))}
            {(data?.files ?? []).map((f) => {
              const on = pickedFiles.includes(f.path)
              return (
                <li key={f.path}>
                  <button
                    type="button"
                    onClick={() => toggleFile(f.path)}
                    className={cn(
                      'flex w-full items-center gap-2 border-b border-line/50 px-3 py-1.5 text-left text-[12.5px] hover:bg-surface2',
                      on && 'bg-[var(--changed)] text-accentsoft',
                    )}
                  >
                    {on ? <CheckIcon size={14} /> : <span className="w-[14px]" />}
                    <span className="truncate">{f.name}</span>
                  </button>
                </li>
              )
            })}
          </ul>
          {!loading && (data?.dirs?.length ?? 0) === 0 && (data?.files?.length ?? 0) === 0 && !error && (
            <div className="px-3 py-6 text-[12px] text-faint">这个目录里没有可显示的条目。</div>
          )}
        </div>

        <div className="flex h-12 shrink-0 items-center gap-2 border-t border-line px-3">
          {kind === 'folder' ? (
            <span className="num min-w-0 flex-1 truncate text-[11px] text-faint">
              将使用: {data?.path ?? DASH}
            </span>
          ) : (
            <span className="text-[11.5px] text-faint">已选 {pickedFiles.length} 个文件</span>
          )}
          <Button onClick={onClose}>取消</Button>
          <Button
            variant="primary"
            disabled={kind === 'files' && pickedFiles.length === 0}
            onClick={() => {
              if (kind === 'folder' && data?.path) onPick([data.path])
              else if (pickedFiles.length) onPick(pickedFiles)
              onClose()
            }}
          >
            {kind === 'folder' ? '用这个目录' : `加入 ${pickedFiles.length || ''} 个`}
          </Button>
        </div>
      </div>
    </div>
  )
}
