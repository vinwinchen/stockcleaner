import { useState } from 'react'
import {
  FileXls, FileCsv, BracketsSquare, FolderOpen, TrayArrowDown, X, UploadSimple,
  WarningCircle, MagnifyingGlass, ArrowRight,
} from '@phosphor-icons/react'
import { Button, Chip, IconButton, SectionHead, TextInput, cn } from './ui'
import { DASH, extOf, fmtBytes, fmtInt } from '../lib/format'
import type { QueueItem } from '../types'

function KindIcon({ name }: { name: string }) {
  const ext = extOf(name)
  if (ext === 'xlsx' || ext === 'xls' || ext === 'xlsm')
    return <FileXls size={15} className="shrink-0 text-faint" />
  if (ext === 'txt') return <BracketsSquare size={15} className="shrink-0 text-faint" />
  return <FileCsv size={15} className="shrink-0 text-faint" />
}

export function FileQueue({
  items,
  focus,
  dirs,
  onSelect,
  onRemove,
  onClear,
  onPickFiles,
  onPickFolder,
  onDropFiles,
  onAddPaths,
  busy,
}: {
  items: QueueItem[]
  focus: string | null
  dirs: string[]
  onSelect: (path: string) => void
  onRemove: (path: string) => void
  onClear: () => void
  onPickFiles: () => void
  onPickFolder: () => void
  onDropFiles: (files: File[]) => void
  onAddPaths: (paths: string[]) => void
  busy: boolean
}) {
  const [dragOver, setDragOver] = useState(false)
  const [pasteValue, setPasteValue] = useState('')
  const totalBytes = items.reduce((sum, i) => sum + (i.size || 0), 0)
  const failed = items.filter((i) => i.error).length

  const submitPaste = () => {
    // 单行 input 会把粘贴进来的换行吃掉, 所以同时支持 | 分隔 (Windows 文件名不允许 |)
    const paths = pasteValue
      .split(/[\n|]/)
      .map((p) => p.trim().replace(/^"|"$/g, ''))
      .filter(Boolean)
    if (!paths.length) return
    onAddPaths(paths)
    setPasteValue('')
  }

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault()
        setDragOver(true)
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        e.preventDefault()
        setDragOver(false)
        // 拖拽拿不到本机绝对路径 (WebView 的安全边界), 所以把文件字节交给本地服务
        // 落到临时目录, 再走同一套内核; 绝不在前端伪造路径。
        const files = Array.from(e.dataTransfer.files || [])
        if (files.length) onDropFiles(files)
      }}
      className={cn('flex shrink-0 flex-col', dragOver && 'bg-[var(--changed)]')}
    >
      <SectionHead
        title="待处理文件"
        count={items.length ? `${fmtInt(items.length)} 个 · ${fmtBytes(totalBytes)}` : DASH}
        right={
          <>
            <IconButton label="选择文件 (可多选)" onClick={onPickFiles} disabled={busy}>
              <UploadSimple size={15} />
            </IconButton>
            <IconButton label="递归扫描文件夹" onClick={onPickFolder} disabled={busy}>
              <FolderOpen size={15} />
            </IconButton>
            {items.length > 0 && (
              <IconButton label="清空列表" onClick={onClear} disabled={busy}>
                <X size={15} />
              </IconButton>
            )}
          </>
        }
      />

      {dirs.length > 0 && (
        <div className="flex flex-wrap gap-1 px-3 pb-2">
          {dirs.map((d) => (
            <Chip key={d} title={d}>
              <FolderOpen size={11} />
              <span className="max-w-[180px] truncate">{d}</span>
            </Chip>
          ))}
        </div>
      )}

      {items.length === 0 ? (
        <div
          className={cn(
            'mx-3 mb-3 flex min-h-[136px] flex-col items-center justify-center gap-2 rounded-[var(--radius-panel)] border border-dashed px-4 py-6 text-center',
            dragOver ? 'border-accent bg-[var(--changed)]' : 'border-line2',
          )}
        >
          <TrayArrowDown size={22} className="text-faint" />
          <div className="text-[12.5px] text-muted">拖入 CSV / TXT / XLSX，或用上方的按钮选择</div>
          <div className="hint max-w-[30ch]">
            文件夹模式会递归扫描全部子目录；同名文件的输出名会自动加目录前缀，不会互相覆盖。
          </div>
        </div>
      ) : (
        <div className="max-h-[252px] min-h-[92px] overflow-auto px-1.5 pb-2">
          <table className="w-full table-collapse border-separate border-spacing-0 text-[12px]">
            <thead className="sticky top-0 z-10 bg-surface">
              <tr className="text-left text-[11px] text-faint">
                <th className="py-1.5 pl-2 pr-2 font-medium">文件</th>
                <th className="w-[54px] py-1.5 px-1 text-right font-medium">行</th>
                <th className="w-[46px] py-1.5 px-1 text-right font-medium">列</th>
                <th className="w-[28px] py-1.5" />
              </tr>
            </thead>
            <tbody>
              {items.map((item) => {
                const active = item.path === focus
                return (
                  <tr
                    key={item.path}
                    title={`${item.path}\n${fmtBytes(item.size)} -> ${item.out_name}`}
                    onClick={() => onSelect(item.path)}
                    tabIndex={0}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault()
                        onSelect(item.path)
                      }
                    }}
                    aria-current={active}
                    className={cn(
                      'group cursor-pointer',
                      active ? 'bg-[var(--changed)]' : 'hover:bg-surface2',
                    )}
                  >
                    <td className="max-w-0 py-1.5 pl-2 pr-2">
                      <div className="flex items-center gap-1.5">
                        <KindIcon name={item.name} />
                        <span className={cn('truncate', active && 'text-accentsoft font-medium')}>
                          {item.name}
                        </span>
                        {item.merged_stem && (
                          <Chip title="与另一个同名文件冲突，输出名已加目录前缀">改名</Chip>
                        )}
                        {item.exists && (
                          <Chip tone="warn" title={item.out_path ?? ''}>
                            将覆盖
                          </Chip>
                        )}
                        {item.error && (
                          <Chip tone="err" title={item.error}>
                            <WarningCircle size={11} weight="fill" /> 读不了
                          </Chip>
                        )}
                      </div>
                      <div className="num mt-0.5 truncate text-[10.5px] text-faint">
                        {item.out_name}
                      </div>
                    </td>
                    <td className="num py-1.5 px-1 text-right text-muted">
                      {item.error ? DASH : fmtInt(item.rows)}
                    </td>
                    <td className="num py-1.5 px-1 text-right text-muted">
                      {item.error ? DASH : fmtInt(item.cols)}
                    </td>
                    <td className="py-1.5 pr-1 text-right">
                      <button
                        type="button"
                        title="移出列表"
                        disabled={busy}
                        onClick={(e) => {
                          e.stopPropagation()
                          onRemove(item.path)
                        }}
                        className="rounded-[4px] p-0.5 text-faint opacity-0 transition group-hover:opacity-100 hover:text-ink disabled:hidden"
                      >
                        <X size={13} />
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          {failed > 0 && (
            <div className="mx-2 mt-2 flex items-center gap-1.5 text-[11.5px] text-err">
              <WarningCircle size={13} weight="fill" />
              {failed} 个文件读不了，运行时会跳过并记入报告
            </div>
          )}
          {items.length > 0 && (
            <div className="mx-2 mt-2 flex items-center gap-1.5 text-[11px] text-faint">
              <MagnifyingGlass size={12} />
              选中一个文件即可在右侧核对每一列的改动
            </div>
          )}
        </div>
      )}

      {/* 原生对话框之外的第二条入口: 从资源管理器复制路径过来即可, 不用在对话框里翻目录 */}
      <div className="flex shrink-0 items-center gap-1.5 border-t border-line px-3 py-2">
        <TextInput
          value={pasteValue}
          placeholder="粘贴绝对路径 (多个用 | 分隔)，回车加入"
          aria-label="粘贴文件或文件夹的绝对路径"
          onChange={(e) => setPasteValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              submitPaste()
            }
          }}
          className="h-[28px] text-[11.5px]"
        />
        <Button size="sm" onClick={submitPaste} disabled={!pasteValue.trim()} title="加入列表">
          <ArrowRight size={13} />
        </Button>
      </div>
    </div>
  )
}
