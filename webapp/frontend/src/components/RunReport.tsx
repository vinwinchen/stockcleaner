import { useMemo, useState } from 'react'
import {
  CaretRightIcon, CheckCircleIcon, FolderOpenIcon, ArrowsClockwiseIcon,
  WarningCircleIcon, XCircleIcon, DownloadSimpleIcon,
} from '@phosphor-icons/react'
import { Button, Chip, SectionHead, cn } from './ui'
import { DASH, fmtInt } from '../lib/format'
import type { FileOutcome, JobEvent, PreviewResult, Report } from '../types'

export type RunSummary = {
  jobId: string | null
  total: number
  done: number
  ok: number
  failed: number
  status: 'idle' | 'running' | 'done' | 'cancelled'
  outputDir: string
  elapsed: number
  outcomes: FileOutcome[]
  /** 当前文件内的阶段进度 0..1; 文件完成即归零, 只在 running 时计入总进度 */
  fileFrac: number
  /** 当前文件的阶段标识 (read/prepare/numericize/.../write/save) */
  stage: string | null
  /** 正在处理的文件名 (file_start 带来, 完成后清空) */
  activeName: string | null
  /** 重连时缓冲被截断, 前面的事件没拿到 —— 报告只能算"最近这一段" */
  partial: boolean
  notes: string[]
}

export const EMPTY_RUN: RunSummary = {
  jobId: null, total: 0, done: 0, ok: 0, failed: 0,
  status: 'idle', outputDir: '', elapsed: 0, outcomes: [],
  fileFrac: 0, stage: null, activeName: null,
  partial: false, notes: [],
}

/** 内核阶段标识 -> 界面文案 */
const STAGE_LABEL: Record<string, string> = {
  read: '读取中', prepare: '预处理', fullwidth: '全角转半角', strip: '去字符',
  numericize: '数值化', dates: '日期统一', protect: '列保护回退',
  write: '写盘中', save: '收尾',
}

export function stageLabel(stage: string | null): string {
  return (stage && STAGE_LABEL[stage]) || '处理中'
}

/** 把 SSE 事件流折叠成一份运行汇总。事件可重放，所以这里必须幂等。
 *
 *  幂等靠的是 index (第几个文件), 不是 basename: 递归批量里 d1/x.csv 与 d2/x.csv
 *  同名, 用名字当键会让两条记录互相顶掉、React key 重复。
 */
export function reduceRunEvents(prev: RunSummary, e: JobEvent): RunSummary {
  const next: RunSummary = { ...prev, outcomes: prev.outcomes.slice() }
  const uid = e.index !== undefined ? String(e.index) : (e.name ?? '')
  const at = (list: FileOutcome[]) => list.findIndex((o) => o.uid === uid)
  switch (e.kind) {
    case 'job_start':
      next.jobId = e.job_id ?? prev.jobId
      next.total = e.total ?? prev.total
      next.outputDir = e.output_dir ?? prev.outputDir
      next.status = 'running'
      next.outcomes = []
      next.done = 0
      next.ok = 0
      next.failed = 0
      next.fileFrac = 0
      next.stage = null
      next.activeName = null
      next.partial = false
      next.notes = []
      break
    case 'file_start':
      next.fileFrac = 0
      next.stage = null
      next.activeName = e.name ?? null
      break
    case 'file_progress':
      next.fileFrac = Math.min(1, Math.max(0, e.frac ?? next.fileFrac))
      next.stage = e.stage ?? next.stage
      break
    case 'progress':
      next.done = e.done ?? next.done
      next.ok = e.ok ?? next.ok
      next.failed = e.failed ?? next.failed
      next.fileFrac = 0
      break
    case 'file_done': {
      const outcome: FileOutcome = {
        uid, name: e.name ?? DASH, ok: true, output: e.output,
        outputs: e.outputs, engine: e.engine,
        rows_out: e.rows_out, cols_out: e.cols_out, report: e.report,
      }
      const idx = at(next.outcomes)
      if (idx >= 0) next.outcomes[idx] = outcome
      else next.outcomes.push(outcome)
      next.fileFrac = 0
      next.stage = null
      next.activeName = null
      break
    }
    case 'file_error': {
      const outcome: FileOutcome = { uid, name: e.name ?? DASH, ok: false, error: e.error }
      const idx = at(next.outcomes)
      if (idx >= 0) next.outcomes[idx] = outcome
      else next.outcomes.push(outcome)
      next.fileFrac = 0
      next.stage = null
      next.activeName = null
      break
    }
    case 'replay_gap':
      next.partial = true
      next.notes = [...prev.notes,
        `进度流断过：前面 ${e.lost ?? '?'} 条事件已不在缓冲里，从 seq ${e.first_seq ?? '?'} 起重放`]
      break
    case 'note':
      if (e.message) next.notes = [...prev.notes, e.message]
      break
    case 'job_end':
      next.status = e.cancelled ? 'cancelled' : 'done'
      next.elapsed = e.elapsed ?? next.elapsed
      next.fileFrac = 0
      break
    default:
      break
  }
  return next
}

const METRICS: { key: keyof Report; label: string; unit: string }[] = [
  { key: 'numeric_cells', label: '数值化', unit: '格' },
  { key: 'unit_cells', label: '单位换算', unit: '格' },
  { key: 'date_cells', label: '日期统一', unit: '格' },
  { key: 'fullwidth_cells', label: '全角转半角', unit: '格' },
  { key: 'stripped_cells', label: '去字符', unit: '格' },
  { key: 'protected_cells', label: '保护回退', unit: '格' },
  // 内核把"行裁剪"和"删全空行"合并进同一个计数, 标签必须两件事都说到
  { key: 'dropped_rows', label: '裁剪/删空行', unit: '行' },
]

function ReportBody({ report }: { report: Report }) {
  const shown = METRICS.filter((m) => (report[m.key] as number) > 0)
  return (
    <div className="flex flex-col gap-2 px-3 pb-3">
      {shown.length > 0 ? (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-3">
          {shown.map((m) => (
            <div key={m.key} className="flex items-baseline justify-between gap-2 border-b border-line pb-1">
              <dt className="text-[11.5px] text-faint">{m.label}</dt>
              <dd className="num text-[12px] text-ink">
                {fmtInt(report[m.key] as number)}
                <span className="ml-0.5 text-[10px] text-faint">{m.unit}</span>
              </dd>
            </div>
          ))}
        </dl>
      ) : (
        <div className="text-[11.5px] text-faint">没有任何单元格被改动。</div>
      )}

      {report.id_columns.length > 0 && (
        <div className="flex flex-wrap items-start gap-1.5">
          <span className="text-[11.5px] text-faint">标识符列</span>
          {report.id_columns.map((c) => (
            <Chip key={c} tone="warn" title="按前导零判定，整列保留文本">
              {c}
            </Chip>
          ))}
        </div>
      )}

      {report.warnings.map((w, i) => (
        <div key={i} className="flex items-start gap-1.5 text-[11.5px] text-warn">
          <WarningCircleIcon size={13} weight="fill" className="mt-[2px] shrink-0" />
          <span>{w}</span>
        </div>
      ))}
    </div>
  )
}

export function RunReport({
  run,
  preview,
  onOpenDir,
  onRetry,
  onExport,
}: {
  run: RunSummary
  preview: PreviewResult | null
  onOpenDir: (path: string) => void
  onRetry: () => void
  onExport: () => void
}) {
  const [open, setOpen] = useState<string | null>(null)
  const totals = useMemo(() => {
    const acc: Record<string, number> = {}
    for (const m of METRICS) acc[m.key] = 0
    for (const o of run.outcomes) {
      if (!o.report) continue
      for (const m of METRICS) acc[m.key] += (o.report[m.key] as number) || 0
    }
    return acc
  }, [run.outcomes])

  if (run.status === 'idle') {
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        <SectionHead title="运行报告" count={DASH} />
        {preview ? (
          <div className="px-3 pb-3">
            <div className="hint mb-2">
              下面是 {preview.name} 的干跑预估（样本 {fmtInt(preview.sample_rows)} 行）。正式运行处理全量行，报告逐文件留痕。
            </div>
            <ReportBody report={preview.report} />
          </div>
        ) : (
          <div className="px-3 text-[12px] text-faint">选择文件后这里显示预估。</div>
        )}
      </div>
    )
  }

  const pct = run.total ? Math.round((run.done / run.total) * 100) : 0

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <SectionHead
        title="运行报告"
        count={`${fmtInt(run.done)}/${fmtInt(run.total)} · ${pct}%`}
        right={
          <>
            {run.failed > 0 && <Chip tone="err"><XCircleIcon size={11} />{fmtInt(run.failed)} 失败</Chip>}
            <Chip tone={run.status === 'running' ? 'accent' : 'neutral'}>
              {run.status === 'running' ? stageLabel(run.stage)
                : run.status === 'cancelled' ? '已中止' : '完成'}
            </Chip>
          </>
        }
      />

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-y border-line bg-surface2 px-3 py-2 text-[11.5px]">
        {METRICS.map((m) => (
          <span key={m.key} className="flex items-baseline gap-1">
            <span className="text-faint">{m.label}</span>
            <span className="num text-ink">{fmtInt(totals[m.key])}</span>
            <span className="text-[10px] text-faint">{m.unit}</span>
          </span>
        ))}
        {run.elapsed > 0 && <span className="num ml-auto text-faint">{run.elapsed.toFixed(2)}s</span>}
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {run.partial && (
          <div className="m-3 flex items-start gap-1.5 rounded-[var(--radius-chip)] border border-warn/40 bg-warn/10 px-3 py-2 text-[11.5px] text-warn">
            <WarningCircleIcon size={13} weight="fill" className="mt-[2px] shrink-0" />
            <span>进度流断过一次，下面的清单只覆盖重放窗口内的部分；完整产出请以输出目录和登记为准。</span>
          </div>
        )}
        {run.notes.length > 0 && (
          <div className="mx-3 mb-2 flex flex-col gap-1 text-[11.5px] text-faint">
            {run.notes.map((n, i) => (<span key={i}>· {n}</span>))}
          </div>
        )}
        {run.outcomes.map((o) => {
          const isOpen = open === o.uid
          return (
            <div key={o.uid} className="border-b border-line last:border-b-0">
              <button
                type="button"
                onClick={() => setOpen(isOpen ? null : o.uid)}
                className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-surface2"
              >
                <CaretRightIcon size={12} className={cn('shrink-0 text-faint transition-transform', isOpen && 'rotate-90')} />
                {o.ok
                  ? <CheckCircleIcon size={14} weight="fill" className="shrink-0 text-ok" />
                  : <XCircleIcon size={14} weight="fill" className="shrink-0 text-err" />}
                <span className="truncate text-[12.5px] text-ink">{o.name}</span>
                <span className="num ml-auto shrink-0 text-[11px] text-faint">
                  {o.ok ? `${fmtInt(o.rows_out)} x ${fmtInt(o.cols_out)}` : '失败'}
                </span>
              </button>
              {isOpen && (
                <div className="anim-rise">
                  {o.ok && o.report ? <ReportBody report={o.report} /> : (
                    <div className="px-3 pb-3 text-[11.5px] text-err">{o.error}</div>
                  )}
                  {o.ok && (o.outputs?.length || o.output) && (
                    <div className="flex flex-col gap-1 px-3 pb-3">
                      {(o.outputs?.length ? o.outputs : [o.output!]).map((p) => (
                        <div key={p} className="flex items-center gap-2">
                          <span className="num min-w-0 flex-1 truncate text-[11px] text-faint" title={p}>
                            {p}
                          </span>
                          <Button size="sm" variant="ghost" onClick={() => onOpenDir(p)}>
                            <FolderOpenIcon size={13} /> 定位
                          </Button>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>

      <div className="flex shrink-0 items-center gap-1.5 border-t border-line px-3 py-2">
        <Button size="sm" variant="ghost" onClick={onExport} disabled={!run.outcomes.length}>
          <DownloadSimpleIcon size={13} /> 导出报告
        </Button>
        <Button size="sm" variant="ghost" onClick={onRetry} disabled={run.status === 'running'}>
          <ArrowsClockwiseIcon size={13} /> 再跑一次
        </Button>
        <span className="num ml-auto min-w-0 truncate text-[11px] text-faint" title={run.outputDir}>
          {run.outputDir}
        </span>
      </div>
    </div>
  )
}

export function reportToText(run: RunSummary): string {
  const lines = [
    `StockCleaner 运行报告`,
    `输出目录: ${run.outputDir}`,
    `结果: 成功 ${run.ok} / 失败 ${run.failed} / 共 ${run.total}`,
    '',
  ]
  for (const o of run.outcomes) {
    if (!o.ok) {
      lines.push(`[失败] ${o.name}: ${o.error}`)
      continue
    }
    const r = o.report!
    const parts = METRICS.filter((m) => (r[m.key] as number) > 0)
      .map((m) => `${m.label} ${fmtInt(r[m.key] as number)}${m.unit}`)
    lines.push(`[完成] ${o.name} -> ${o.output}`)
    lines.push(`       ${fmtInt(o.rows_out)} 行 x ${fmtInt(o.cols_out)} 列; ${parts.join('; ') || '无改动'}`)
    if (r.id_columns.length) lines.push(`       标识符列: ${r.id_columns.join(', ')}`)
    for (const w of r.warnings) lines.push(`       警告: ${w}`)
  }
  return lines.join('\n')
}
