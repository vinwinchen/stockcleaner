import { useState } from 'react'
import {
  ArrowRightIcon, LockSimpleIcon, LockSimpleOpenIcon, ShieldCheckIcon, CaretRightIcon, CheckIcon,
} from '@phosphor-icons/react'
import { Chip, SectionHead, cn } from './ui'
import { DASH, fmtInt } from '../lib/format'
import type { ColumnInfo, ColumnKind, PreviewResult } from '../types'

const KIND_LABEL: Record<ColumnKind, string> = {
  text: '文本',
  number: '数值',
  date: '日期',
  identifier: '标识符',
  protected: '已保护',
  added: '新增列',
}

const KIND_TONE: Record<ColumnKind, 'neutral' | 'accent' | 'warn'> = {
  text: 'neutral',
  number: 'accent',
  date: 'accent',
  identifier: 'warn',
  protected: 'warn',
  added: 'accent',
}

function SampleValue({ s, t, faded }: { s: string; t: string; faded?: boolean }) {
  const empty = t === 'empty' || s === ''
  return (
    <span
      className={cn(
        'num inline-block max-w-full truncate rounded-[4px] px-1.5 py-0.5 text-[11.5px]',
        empty ? 'text-faint italic' : faded ? 'text-faint line-through decoration-line2' : 'text-ink',
      )}
    >
      {empty ? '(空)' : s}
    </span>
  )
}

export function ColumnInspector({
  preview,
  stale,
  protectedColumns,
  onToggleProtect,
}: {
  preview: PreviewResult | null
  stale: boolean
  protectedColumns: string[]
  onToggleProtect: (column: string, protect: boolean) => void
}) {
  const [open, setOpen] = useState<string | null>(null)
  const columns = preview?.columns ?? []
  const changedTotal = columns.reduce((sum, c) => sum + c.changed, 0)

  return (
    <div className={cn('flex min-h-0 flex-1 flex-col', stale && 'stale')}>
      <SectionHead
        title="列检视"
        count={preview ? `${fmtInt(columns.length)} 列 · 样本内改动 ${fmtInt(changedTotal)} 格` : DASH}
        right={
          preview?.sampled ? (
            <Chip title={`全文件 ${fmtInt(preview.rows_in)} 行，预览只扫描前 ${fmtInt(preview.sample_rows)} 行`}>
              样本 {fmtInt(preview.sample_rows)} 行
            </Chip>
          ) : undefined
        }
      />

      {columns.length === 0 ? (
        <div className="px-4 py-6 text-[12.5px] text-faint">没有可显示的列。</div>
      ) : (
        <div className="min-h-0 flex-1 overflow-auto">
          {columns.map((col) => {
            const expanded = open === col.name
            const protectedNow = protectedColumns.includes(col.name) || col.protected
            return (
              <div key={col.name} className="border-t border-line first:border-t-0">
                <div className="flex items-center gap-2 px-3 py-[7px]">
                  <button
                    type="button"
                    onClick={() => setOpen(expanded ? null : col.name)}
                    aria-expanded={expanded}
                    className="flex min-w-0 flex-1 items-center gap-2 text-left"
                  >
                    <CaretRightIcon
                      size={13}
                      weight="bold"
                      className={cn('shrink-0 text-faint transition-transform duration-150', expanded && 'rotate-90')}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-2">
                        <span className="truncate text-[12.5px] font-medium text-ink">{col.name}</span>
                        <Chip
                          tone={KIND_TONE[col.kind]}
                          title={col.id_protected
                            ? '按前导零判定为标识符列，整列保留文本，未做数值化'
                            : col.split_from
                              ? `日期含时间分量，这一列是从 "${col.split_from}" 拆出来的；原列只留日期`
                              : undefined}
                        >
                          {col.id_protected && <ShieldCheckIcon size={11} />}
                          {KIND_LABEL[col.kind]}
                        </Chip>
                        {col.collision && (
                          <Chip tone="warn" title="同一列里两种写法会洗成同一个数值，洗完全分不出谁是谁">
                            写法碰撞
                          </Chip>
                        )}
                      </span>
                    </span>
                    <span className="num shrink-0 text-[11.5px]">
                      {col.changed ? (
                        <span className="text-accentsoft">改动 {fmtInt(col.changed)} 格</span>
                      ) : (
                        <span className="text-faint">无改动</span>
                      )}
                      <span className="ml-1.5 text-faint">/ 共 {fmtInt(col.cells)}</span>
                    </span>
                  </button>
                  <button
                    type="button"
                    onClick={() => onToggleProtect(col.name, !protectedNow)}
                    title={protectedNow ? '取消保护：该列重新参与清洗' : '保护该列：整列回退为原值'}
                    className={cn(
                      'shrink-0 rounded-[var(--radius-chip)] border p-1 transition-colors',
                      protectedNow
                        ? 'border-[var(--changed-line)] bg-[var(--changed)] text-accentsoft'
                        : 'border-line text-faint hover:border-line2 hover:text-muted',
                    )}
                  >
                    {protectedNow ? <LockSimpleIcon size={13} weight="fill" /> : <LockSimpleOpenIcon size={13} />}
                  </button>
                </div>

                {expanded && <SampleTable col={col} />}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

function SampleTable({ col }: { col: ColumnInfo }) {
  if (!col.samples.length) {
    return (
      <div className="mx-3 mb-2 flex items-center gap-1.5 rounded-[var(--radius-chip)] bg-surface2 px-2.5 py-2 text-[11.5px] text-faint">
        <CheckIcon size={13} /> 该列没有任何单元格会被改动
      </div>
    )
  }
  return (
    <table className="mb-2 ml-3 mr-3 w-[calc(100%-24px)] table-fixed border-separate border-spacing-0 text-[11.5px]">
      <thead>
        <tr className="text-left text-[10.5px] text-faint">
          <th className="w-[46px] py-1 pl-2 font-medium">行</th>
          <th className="w-[45%] py-1 px-2 font-medium">原值</th>
          <th className="w-[16px] py-1" />
          <th className="py-1 pr-2 font-medium">清洗结果</th>
        </tr>
      </thead>
      <tbody>
        {col.samples.map((s, i) => (
          <tr key={`${s.row}-${i}`} className={cn(s.changed ? 'bg-[var(--changed)]' : '')}>
            <td className="num py-1 pl-2 text-faint">{s.row + 1}</td>
            <td className="max-w-0 py-1 px-2 align-middle">
              <SampleValue s={s.before.s} t={s.before.t} faded={s.changed} />
            </td>
            <td className="py-1 text-center">{s.changed ? <ArrowRightIcon size={11} className="text-faint" /> : null}</td>
            <td className="max-w-0 py-1 pr-2">
              {s.changed ? (
                <SampleValue s={s.after.s} t={s.after.t} />
              ) : (
                <span className="num text-[11.5px] text-faint">{DASH} 保持原值</span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
