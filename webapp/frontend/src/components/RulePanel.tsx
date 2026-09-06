import { useEffect, useMemo, useState } from 'react'
import { Funnel, ShieldCheck, Tag, CalendarBlank, Scissors, Trash, WarningCircle, TextAa } from '@phosphor-icons/react'
import { Field, NumberInput, SectionHead, TextInput, Toggle } from './ui'
import { api } from '../api'
import { DASH } from '../lib/format'
import type { Config } from '../types'

export const DEFAULT_CONFIG: Config = {
  strip_tokens: [],
  strip_column_mode: false,
  strip_column: '',
  numericize: true,
  convert_units: true,
  head_cut: 0,
  tail_cut: 0,
  drop_empty_rows: true,
  drop_empty_cols: false,
  normalize_dates: true,
  fullwidth: true,
  output_format: 'keep',
  column_overrides: {},
  sample_rows: 500,
}

const UNIT_TOKENS = ['万亿', '千亿', '千万', '百万', '十万', '亿', '万', '千', '元']

function Group({ icon, title, children }: { icon: React.ReactNode; title: string; children: React.ReactNode }) {
  return (
    <div className="px-3 py-2.5">
      <div className="mb-1 flex items-center gap-1.5 text-muted">
        <span className="text-faint">{icon}</span>
        <span className="text-[12px] font-semibold">{title}</span>
      </div>
      <div className="flex flex-col gap-0.5">{children}</div>
    </div>
  )
}

export function RulePanel({
  config,
  onChange,
  columns,
  focusName,
  idColumns,
}: {
  config: Config
  onChange: (patch: Partial<Config>) => void
  columns: string[]
  focusName: string | null
  idColumns: string[]
}) {
  const tokens = config.strip_tokens.join(',')
  const [tokenText, setTokenText] = useState(tokens)
  useEffect(() => setTokenText(config.strip_tokens.join(',')), [config.strip_tokens])

  const parsedTokens = useMemo(
    () => tokenText.split(/[,，]/).map((t) => t.trim()).filter(Boolean),
    [tokenText],
  )
  const clash = config.convert_units
    ? parsedTokens.filter((t) => UNIT_TOKENS.includes(t))
    : []

  const commitTokens = () => onChange({ strip_tokens: parsedTokens })

  return (
    <div className="flex flex-col divide-y divide-line border-t border-line">
      <SectionHead title="清洗规则" count={focusName ?? DASH} />

      <Group icon={<Funnel size={14} />} title="数值化">
        <Toggle
          label="智能数值化"
          hint="千分位、全角、货币符号、会计负数、科学计数法"
          checked={config.numericize}
          onChange={(v) => onChange({ numericize: v })}
        />
        <Toggle
          label="中文单位换算"
          hint="1.5 亿写成 150000000；关掉后保留原样"
          checked={config.convert_units}
          disabled={!config.numericize}
          onChange={(v) => onChange({ convert_units: v })}
        />
        {config.convert_units && (
          <div className="hint pl-[42px] pt-0.5">
            前导零列（如 000001）自动识别为代码列，整列不数值化。
          </div>
        )}
      </Group>

      <Group icon={<CalendarBlank size={14} />} title="日期">
        <Toggle
          label="统一为 YYYY-MM-DD"
          hint="按内容识别，不按列名猜；非法日期保留原值"
          checked={config.normalize_dates}
          onChange={(v) => onChange({ normalize_dates: v })}
        />
        {config.normalize_dates && (
          <div className="hint pl-[42px]">
            带时间的列（Excel 的日期时间、或 2023-01-05 14:30:00）会拆成“日期”＋“列名_时间”两列。
          </div>
        )}
      </Group>

      <Group icon={<TextAa size={14} />} title="全角转半角">
        <Toggle
          label="全角字符转半角"
          hint="逐格作用于文本单元格，在去字符与数值化之前执行"
          checked={config.fullwidth}
          onChange={(v) => onChange({ fullwidth: v })}
        />
        {config.fullwidth && (
          <div className="mt-1 flex items-start gap-1.5 rounded-[var(--radius-chip)] border border-line bg-surface2 px-2 py-1.5">
            <WarningCircle size={14} weight="fill" className="mt-[1px] shrink-0 text-warn" />
            <span className="hint text-warn">
              这会改写文本列：名称、备注里的全角字母与标点（ＨＫＣ／（）→ HKC/()）一并转半角。
              不想动的列在右侧点锁保护。全角代码转完仍是前导零，代码列照旧受标识符保护。
            </span>
          </div>
        )}
        {!config.fullwidth && (
          <div className="hint pl-[42px]">
            关掉后全角日期（２０２３．１．８）按“解析失败”保留原值，文本列一律不动。
          </div>
        )}
      </Group>

      <Group icon={<Tag size={14} />} title="去字符">
        <Field
          label="要删除的字符"
          htmlFor="strip-tokens"
          hint="多个用逗号分隔，例如：* , # , 待定"
        >
          <TextInput
            id="strip-tokens"
            value={tokenText}
            placeholder="留空表示不去字符"
            onChange={(e) => setTokenText(e.target.value)}
            onBlur={commitTokens}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault()
                commitTokens()
              }
            }}
          />
        </Field>
        {clash.length > 0 && (
          <div className="mt-1 flex items-start gap-1.5 rounded-[var(--radius-chip)] border border-line bg-surface2 px-2 py-1.5">
            <WarningCircle size={14} weight="fill" className="mt-[1px] shrink-0 text-warn" />
            <span className="hint text-warn">
              {clash.join('、')} 属于单位字符，已由单位换算处理，不会被当作去字符删掉。
            </span>
          </div>
        )}
        <Toggle
          label="仅针对指定列"
          checked={config.strip_column_mode}
          onChange={(v) => onChange({ strip_column_mode: v })}
        />
        {config.strip_column_mode && (
          <Field label="目标列名" hint="留空则本次不去字符并记入警告；列名找不到时按全表处理并记入警告">
            <TextInput
              list="known-columns"
              value={config.strip_column}
              placeholder={columns[0] ?? '列名'}
              onChange={(e) => onChange({ strip_column: e.target.value })}
            />
            <datalist id="known-columns">
              {columns.map((c) => (
                <option key={c} value={c} />
              ))}
            </datalist>
          </Field>
        )}
      </Group>

      <Group icon={<Scissors size={14} />} title="行裁剪">
        <div className="flex items-end gap-2">
          <Field label="删头部行数" className="flex-1">
            <NumberInput value={config.head_cut} onChange={(n) => onChange({ head_cut: n })} />
          </Field>
          <Field label="删尾部行数" className="flex-1">
            <NumberInput value={config.tail_cut} onChange={(n) => onChange({ tail_cut: n })} />
          </Field>
        </div>
        {(config.head_cut > 0 || config.tail_cut > 0) && (
          <div className="hint">裁剪作用于每个文件的表头之后，多文件批量时按同一行数处理。</div>
        )}
      </Group>

      <Group icon={<Trash size={14} />} title="删除空行空列">
        <Toggle
          label="删除全空行"
          checked={config.drop_empty_rows}
          onChange={(v) => onChange({ drop_empty_rows: v })}
        />
        <Toggle
          label="删除全空列"
          checked={config.drop_empty_cols}
          onChange={(v) => onChange({ drop_empty_cols: v })}
        />
      </Group>

      <Group icon={<ShieldCheck size={14} />} title="逐列保护">
        {idColumns.length > 0 ? (
          <div className="hint">
            内核自动识别为标识符列：{idColumns.join('、')}。可在列检视里再手动保护任意列。
          </div>
        ) : (
          <div className="hint">在“列检视”里对任意列打开保护，该列整列回退为原值。</div>
        )}
      </Group>
    </div>
  )
}

/** 输出目录选择：原生对话框优先，退回内联目录浏览 */
export async function pickNative(kind: 'files' | 'folder'): Promise<string[] | null> {
  try {
    const res = await api.dialog(kind)
    if (res.ok && res.paths?.length) return res.paths
    return null
  } catch {
    return null
  }
}
