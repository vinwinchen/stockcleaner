import { useEffect, useMemo, useRef } from 'react'
import {
  createColumnHelper, flexRender, getCoreRowModel, useReactTable, type ColumnDef,
} from '@tanstack/react-table'
import { ArrowsLeftRight, LockSimple } from '@phosphor-icons/react'
import { Chip, SectionHead, cn } from './ui'
import { DASH, fmtInt } from '../lib/format'
import type { CellVal, GridCell, PreviewResult } from '../types'

type Row = { __row: number; __cells: GridCell[] }

const helper = createColumnHelper<Row>()

/** 只读差异矩阵: 数据量固定为「前 60 行 x 前 24 列」(与服务层 build_grid 的
 *  max_rows=60 同源), 不需要排序/分页, 用 TanStack 只为拿到稳定的列模型与
 *  可复用的列宽/顺序管理。
 *
 *  格子按**下标**存在 __cells 里, 不按列名存成 row[列名]: 表头是 `__proto__`、
 *  `constructor` 这类名字时, 按名存会写进原型链而不是这一行。
 */
export function DiffGrid({
  preview,
  stale,
  protectedColumns,
}: {
  preview: PreviewResult | null
  stale: boolean
  protectedColumns: string[]
}) {
  const grid = preview?.grid
  const scroller = useRef<HTMLDivElement>(null)

  const data = useMemo<Row[]>(() => (grid?.rows ?? []).map((r) => ({
    __row: r.row,
    __cells: grid!.columns.map((_c, i) => r.cells[i] ?? [{ s: '', t: 'empty' }, { s: '', t: 'empty' }, false]),
  })), [grid])

  const columns = useMemo<ColumnDef<Row>[]>(() => {
    if (!grid) return []
    const widthOf = (idx: number) => {
      const longest = (grid.rows ?? []).reduce((max, r) => {
        const cell = r.cells[idx]
        return Math.max(max, cell ? Math.max(cell[0].s.length, cell[1].s.length) : 0)
      }, (grid.columns[idx] ?? '').length)
      return Math.min(260, Math.max(96, longest * 7.4 + 26))
    }
    return grid.columns.map((name, i) =>
      helper.accessor((row) => row.__cells[i], {
        id: `${i}:${name}`,
        header: () => (
          <span className="flex items-center gap-1.5">
            <span className="truncate">{name}</span>
            {protectedColumns.includes(name) && (
              <LockSimple size={11} weight="fill" className="shrink-0 text-accentsoft" aria-label="该列已保护" />
            )}
          </span>
        ),
        size: widthOf(i),
        minSize: 88,
        cell: ({ getValue }) => {
          const [before, after, changed] = getValue() as GridCell
          return <DiffCell before={before} after={after} changed={changed} />
        },
      }) as ColumnDef<Row>,
    )
  }, [grid, protectedColumns])

  const table = useReactTable({ data, columns, getCoreRowModel: getCoreRowModel() })

  // 键盘横向滚动: 表格在 pywebview 里可能拿到焦点, Shift+滚轮是主要手段
  useEffect(() => {
    const el = scroller.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      if (!e.shiftKey || Math.abs(e.deltaY) === 0) return
      e.preventDefault()
      el.scrollLeft += e.deltaY
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  const changedCells = useMemo(
    () => (grid?.rows ?? []).reduce((sum, r) => sum + r.cells.filter((c) => c[2]).length, 0),
    [grid],
  )

  return (
    <div className={cn('flex min-h-0 flex-1 flex-col', stale && 'stale')}>
      <SectionHead
        title="数据差异"
        count={grid ? `${fmtInt(grid.rows.length)} 行 x ${fmtInt(grid.columns.length)} 列` : DASH}
        right={
          grid ? (
            <>
              <Chip tone={changedCells ? 'accent' : 'neutral'}>
                <ArrowsLeftRight size={11} /> 改动 {fmtInt(changedCells)} 格
              </Chip>
              {(grid.truncated_columns > 0 || grid.truncated_rows > 0) && (
                <Chip title="超出预览上限的部分只在正式运行时处理">
                  {grid.truncated_columns > 0 ? `另有 ${fmtInt(grid.truncated_columns)} 列未显示` : `另有 ${fmtInt(grid.truncated_rows)} 行未显示`}
                </Chip>
              )}
            </>
          ) : undefined
        }
      />
      {!grid || data.length === 0 ? (
        <div className="px-4 py-6 text-[12.5px] text-faint">选择左侧文件后显示逐格差异。</div>
      ) : (
        <div ref={scroller} className="min-h-0 flex-1 overflow-auto">
          <table className="w-max border-separate border-spacing-0 text-[11.5px]">
            <thead className="sticky top-0 z-20">
              {table.getHeaderGroups().map((hg) => (
                <tr key={hg.id}>
                  {hg.headers.map((header) => (
                    <th
                      key={header.id}
                      style={{ width: header.getSize() }}
                      className="sticky top-0 z-20 truncate border-b border-line bg-surface2 px-2 py-1.5 text-left text-[11px] font-medium text-muted"
                    >
                      {flexRender(header.column.columnDef.header, header.getContext())}
                    </th>
                  ))}
                </tr>
              ))}
            </thead>
            <tbody>
              {table.getRowModel().rows.map((row) => (
                <tr key={row.id} className="group">
                  {row.getVisibleCells().map((cell) => (
                    <td
                      key={cell.id}
                      style={{ width: cell.column.getSize() }}
                      className="max-w-[260px] border-b border-line/60 px-0 py-0 align-top transition-colors group-hover:bg-surface2"
                    >
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          <div className="hint sticky left-0 px-3 py-2">
            有底色的格子 = 这一格会被改写，划线的是它原来的样子。
          </div>
        </div>
      )}
    </div>
  )
}

function DiffCell({ before, after, changed }: { before: CellVal; after: CellVal; changed: boolean }) {
  const shown = changed ? after : before
  if (shown.t === 'empty') {
    return (
      <div className={cn('px-2 py-1', changed && 'cell-changed')}>
        <span className="text-[11px] italic text-faint">(空)</span>
      </div>
    )
  }
  const isNum = after.t === 'int' || after.t === 'float'
  return (
    <div className={cn('px-2 py-1', changed && 'cell-changed')}>
      {changed && (
        <div className="num truncate text-[10.5px] text-faint line-through decoration-line2">
          {before.s || DASH}
        </div>
      )}
      <div className={cn('truncate', isNum ? 'num text-ink' : 'text-ink')} title={shown.s}>
        {shown.s}
        {shown.padded && <span className="ml-1 text-[10px] text-faint">含空格</span>}
      </div>
    </div>
  )
}
