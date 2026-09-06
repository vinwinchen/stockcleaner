import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  CircleNotch, Database, Moon, Sun, WarningCircle, X, Play, Stop,
  Stack, GridFour, Scroll, ShieldCheck, ArrowsClockwise, Info,
} from '@phosphor-icons/react'
import { Button, Chip, EmptyState, FadeScroll, Segmented, SkeletonRows, Tooltip, cn } from './components/ui'
import { FileQueue } from './components/FileQueue'
import { DEFAULT_CONFIG, RulePanel, pickNative } from './components/RulePanel'
import { ColumnInspector } from './components/ColumnInspector'
import { DiffGrid } from './components/DiffGrid'
import { EMPTY_RUN, RunReport, reduceRunEvents, reportToText } from './components/RunReport'
import { BrowseDialog } from './components/BrowseDialog'
import { api, revealInExplorer, uploadDropped, pushJobEvents } from './api'
import { DASH, delimLabel, dirname, fmtInt } from './lib/format'
import type { Config, PreviewResult, QueueItem } from './types'

type Sources = { files: string[]; dirs: string[] }
type Tab = 'columns' | 'grid' | 'report'
type Theme = 'dark' | 'light'

// 没有"跟随系统"这一档: 启动时就把系统偏好解析成具体的暗/亮,
// 状态栏选中的永远就是你实际看到的主题 ("自动"不表达任何可见结果)。
const systemTheme = (): Theme =>
  window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'

export default function App() {
  const [sources, setSources] = useState<Sources>({ files: [], dirs: [] })
  const [excluded, setExcluded] = useState<string[]>([])
  const [items, setItems] = useState<QueueItem[]>([])
  const [outputDir, setOutputDir] = useState('')
  const [suggested, setSuggested] = useState('')
  const [outputTouched, setOutputTouched] = useState(false)
  const [config, setConfig] = useState<Config>(DEFAULT_CONFIG)
  const [focus, setFocus] = useState<string | null>(null)
  const [preview, setPreview] = useState<PreviewResult | null>(null)
  const [previewErr, setPreviewErr] = useState<string | null>(null)
  const [previewBusy, setPreviewBusy] = useState(false)
  const [stale, setStale] = useState(false)
  const [tab, setTab] = useState<Tab>('columns')
  const [run, setRun] = useState(EMPTY_RUN)
  const [browse, setBrowse] = useState<null | 'in' | 'out'>(null)
  const [theme, setTheme] = useState<Theme>(systemTheme)
  const [nativeShell, setNativeShell] = useState(true)
  const [version, setVersion] = useState('')
  const [notice, setNotice] = useState<string | null>(null)

  const configRef = useRef(config)
  configRef.current = config
  const closeStream = useRef<null | (() => void)>(null)
  const reqSeq = useRef(0)

  useEffect(() => {
    api.meta()
      .then((m) => {
        setNativeShell(Boolean(m?.native))
        if (m?.version) setVersion(m.version)
        // 打包时可用 SC_THEME=dark|light 钉死默认主题; 'system' 表示跟随系统
        if (m?.theme === 'dark' || m?.theme === 'light') setTheme(m.theme)
      })
      .catch(() => setNativeShell(false))
    return () => closeStream.current?.()
  }, [])

  useEffect(() => {
    // 只有暗/亮两档, 显式写 data-theme。初始值就是系统偏好解析出来的,
    // 和 CSS 的 prefers-color-scheme 首帧同色, 所以这一写不会造成翻转。
    document.documentElement.dataset.theme = theme
  }, [theme])

  useEffect(() => {
    // 桌面壳建窗时 pywebview 就会 Focus() 控件, 窗口被激活的那一瞬间 Chromium 会把初始
    // 焦点塞给文档里第一个可聚焦元素 —— 实测是右上角主题档的"暗"按钮, 而且命中
    // :focus-visible 描出一圈 2px 实线, 看着像"程序一启动就停在暗/亮上"。
    // 这个时机在 React 挂载之后, 所以挂载时 blur 一次是来不及的 (实测如此):
    // 改成"用户还没碰过界面之前出现的 focusin 一律退回 body"。
    // 用户一旦 pointerdown / keydown 就摘掉这个兜底, 键盘 Tab 的焦点框不受影响。
    let touched = false
    const mark = () => { touched = true }
    const sink = (e: FocusEvent) => {
      if (touched) return
      const el = e.target as HTMLElement | null
      if (el && el !== document.body) el.blur()
    }
    window.addEventListener('pointerdown', mark, true)
    window.addEventListener('keydown', mark, true)
    document.addEventListener('focusin', sink, true)
    return () => {
      window.removeEventListener('pointerdown', mark, true)
      window.removeEventListener('keydown', mark, true)
      document.removeEventListener('focusin', sink, true)
    }
  }, [])

  /* ------------------------------------------------ 队列 */

  const rebuild = useCallback(
    async (next: Sources, drop: string[], outDir: string) => {
      try {
        const collected = await api.collect({
          files: next.files, dirs: next.dirs, recursive: true, output_dir: outDir,
          config: configRef.current,
        })
        setSuggested(collected.suggested_output || '')
        const paths = collected.plan.filter((p) => !drop.includes(p.path)).map((p) => p.path)
        const info = paths.length
          ? await api.inspect(paths)
          : { items: [], truncated: false, config_applied: false }
        const meta = new Map(info.items.map((i) => [i.path, i]))
        setItems(
          collected.plan
            .filter((p) => !drop.includes(p.path))
            .map((p) => {
              const m = meta.get(p.path)
              return {
                ...p,
                size: m?.size ?? 0,
                rows: m?.rows ?? null,
                cols: m?.cols ?? null,
                error: m && !m.ok ? (m.error ?? '读不了') : null,
                warnings: m?.warnings ?? [],
              }
            }),
        )
        if (collected.unsupported.length) {
          setNotice(
            `已忽略 ${collected.unsupported.length} 个不支持的类型: ${collected.unsupported.slice(0, 3).join('、')}`,
          )
        }
        const skipped = collected.skipped ?? []
        if (skipped.length) {
          const byDir = skipped.filter((s) => s.reason === 'output_dir').length
          const byReg = skipped.length - byDir
          const why = [
            byDir ? `${byDir} 个在输出目录内` : '',
            byReg ? `${byReg} 个是已登记的产出` : '',
          ].filter(Boolean).join(', ')
          setNotice(`文件夹扫描跳过 ${skipped.length} 个文件 (${why})。要重洗自己的输出, 请逐个点选文件。`)
        }
        if (info.truncated) setNotice(`队列过长，仅前 ${info.items.length} 个文件读取了行列数`)
      } catch (e) {
        setNotice(msg(e))
      }
    },
    [],
  )

  const addSources = useCallback(
    async (patch: Partial<Sources>) => {
      // 只改来源; 队列重建统一交给下面的 effect, 避免同一份清单被算两遍
      setSources((prev) => ({
        files: [...prev.files, ...(patch.files ?? [])],
        dirs: [...prev.dirs, ...(patch.dirs ?? [])],
      }))
    },
    [],
  )

  useEffect(() => {
    if (!sources.files.length && !sources.dirs.length) return
    const t = window.setTimeout(() => void rebuild(sources, excluded, outputDir), 200)
    return () => window.clearTimeout(t)
    // excluded 只在删除时用, 删除本身已直接更新 items, 不需要重扫
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sources, outputDir, rebuild])

  useEffect(() => {
    if (!items.length) {
      setFocus(null)
      return
    }
    if (!focus || !items.some((i) => i.path === focus)) setFocus(items[0].path)
  }, [items, focus])

  useEffect(() => {
    // 输出目录默认跟随输入 (输入父目录下的 cleaned/); 用户手改过就不再动
    if (!outputTouched && suggested) setOutputDir(suggested)
  }, [suggested, outputTouched])

  /* ------------------------------------------------ 预览 (只读干跑) */

  const runPreview = useCallback(async (path: string, cfg: Config) => {
    const seq = ++reqSeq.current
    setPreviewBusy(true)
    try {
      const res = await api.preview(path, cfg)
      if (seq !== reqSeq.current) return
      setPreview(res)
      setPreviewErr(null)
      setStale(false)
    } catch (e) {
      if (seq !== reqSeq.current) return
      setPreview(null)
      setPreviewErr(msg(e))
      setStale(false)
    } finally {
      if (seq === reqSeq.current) setPreviewBusy(false)
    }
  }, [])

  useEffect(() => {
    if (!focus) return
    setStale(true)
    const t = window.setTimeout(() => void runPreview(focus, config), 300)
    return () => window.clearTimeout(t)
  }, [focus, config, runPreview])

  /* ------------------------------------------------ 动作 */

  const chooseFiles = async () => {
    const paths = await pickNative('files')
    if (paths) await addSources({ files: paths })
    else setBrowse('in')
  }

  const chooseFolder = async () => {
    const paths = await pickNative('folder')
    if (paths) await addSources({ dirs: paths })
    else setBrowse('in')
  }

  const addPaths = async (paths: string[]) => {
    // 粘贴进来的路径要自己分类: 同时塞进 files 和 dirs 会让后端把目录名报成"不支持的类型"
    const isFile = (p: string) => /\.(csv|tsv|txt|xlsx|xls|xlsm)$/i.test(p.trim())
    await addSources({
      files: paths.filter(isFile),
      dirs: paths.filter((p) => !isFile(p)),
    })
  }

  const removeOne = async (path: string) => {
    const drop = [...excluded, path]
    setExcluded(drop)
    setItems((prev) => prev.filter((i) => i.path !== path))
    if (sources.files.includes(path)) setSources((prev) => ({ ...prev, files: prev.files.filter((f) => f !== path) }))
  }

  const clearAll = () => {
    setSources({ files: [], dirs: [] })
    setExcluded([])
    setItems([])
    setNotice(null)
  }

  const startRun = async () => {
    if (!items.length || !outputDir) return
    setTab('report')
    setRun({ ...EMPTY_RUN, status: 'running', total: items.length, outputDir })
    closeStream.current?.()
    try {
      const { job_id } = await api.run(items.map((i) => i.path), outputDir, config)
      closeStream.current = pushJobEvents(
        job_id,
        (e) => {
          setRun((prev) => reduceRunEvents(prev, e))
          if (e.kind === 'job_end') {
            closeStream.current?.()
            closeStream.current = null
          }
        },
        () => setNotice('进度连接中断，稍后自动重连'),
      )
    } catch (e) {
      setRun((prev) => ({ ...prev, status: 'done' }))
      setNotice(msg(e))
    }
  }

  const cancelRun = async () => {
    if (run.jobId) await api.cancel(run.jobId)
  }

  const toggleProtect = (column: string, protect: boolean) => {
    setStale(true)
    setConfig((prev) => {
      const overrides = { ...prev.column_overrides }
      if (protect) overrides[column] = { protect: true }
      else delete overrides[column]
      return { ...prev, column_overrides: overrides }
    })
  }

  const patchConfig = (patch: Partial<Config>) => {
    setStale(true)
    setConfig((prev) => ({ ...prev, ...patch }))
  }

  const dropFiles = async (files: File[]) => {
    setNotice(`正在接收拖入的 ${files.length} 个文件`)
    try {
      const { paths, dir } = await uploadDropped(files)
      await addSources({ files: paths })
      setNotice(`拖入的文件落在 ${dir}`)
    } catch (e) {
      setNotice(msg(e))
    }
  }

  const exportReport = async () => {
    const text = reportToText(run)
    try {
      await navigator.clipboard.writeText(text)
      setNotice('运行报告已复制到剪贴板')
    } catch {
      setNotice('剪贴板不可用；报告内容见浏览器控制台')
      console.log(text)
    }
  }

  /* ------------------------------------------------ 派生值 */

  const protectedColumns = useMemo(
    () => Object.keys(config.column_overrides).filter((c) => config.column_overrides[c]?.protect),
    [config.column_overrides],
  )
  const sampleChanges = (preview?.columns ?? []).reduce((sum, c) => sum + c.changed, 0)
  const running = run.status === 'running'
  const progress = run.total ? run.done / run.total : 0
  const overwriteCount = items.filter((i) => i.exists).length
  const busy = previewBusy || running

  return (
    <div className="flex h-full flex-col">
      <header className="flex h-[52px] shrink-0 items-center gap-3 border-b border-line bg-surface px-4">
        <div className="flex shrink-0 items-center gap-2">
          <Database size={17} weight="duotone" className="text-accent" aria-hidden />
          <span className="text-[14px] font-semibold tracking-[-0.01em]">StockCleaner</span>
          <Chip title="清洗内核版本 (来自 /api/meta, 与 APP_VERSION 同步)">
            内核 {version ? `v${version}` : '…'}
          </Chip>
        </div>

        <div className="h-5 w-px shrink-0 bg-line" aria-hidden />

        <div className="flex min-w-0 flex-1 items-center gap-2 text-[12px] text-muted">
          <span className="num shrink-0">{fmtInt(items.length)} 个文件</span>
          {preview && (
            <>
              <span className="shrink-0 text-faint">/</span>
              <span className="truncate" title={preview.path}>{preview.name}</span>
              <span className="num shrink-0 text-faint">
                {fmtInt(preview.rows_in)} 行 x {fmtInt(preview.cols_in)} 列
              </span>
              {preview.encoding && preview.encoding !== 'excel' && <Chip>{preview.encoding}</Chip>}
              {preview.engine && (
                <Chip title="Excel 读取引擎。calamine 比 openpyxl 快 4-5 倍, 且带 dtype=object 时仍保住前导零">
                  {preview.engine}
                </Chip>
              )}
              {preview.delimiter && (
                <Chip title={'分隔符按"能不能把每行切成同样多列"判定，引号里的字符不算票'}>
                  分隔符 {delimLabel(preview.delimiter)}
                </Chip>
              )}
              <Tooltip content={preview.sampled
                ? `全文件 ${fmtInt(preview.rows_in)} 行，预览只算前 ${fmtInt(preview.sample_rows)} 行；正式运行处理全部行`
                : '预览覆盖全文件的全部行'}>
                <span className="shrink-0">
                  <Chip tone={preview.sampled ? 'warn' : 'accent'}>
                    {preview.sampled ? `样本 ${fmtInt(preview.sample_rows)} 行` : '全量核对'}
                  </Chip>
                </span>
              </Tooltip>
              {sampleChanges > 0 && (
                <Chip tone="accent" className="shrink-0"
                  title={preview.sampled
                    ? `样本 ${fmtInt(preview.sample_rows)} 行内会计 ${fmtInt(preview.cols_in)} 列改动的格数；全量运行的总数见运行报告`
                    : '按当前规则会被改写的单元格数'}>
                  {preview.sampled ? `样本内 ${fmtInt(sampleChanges)} 格将被改写`
                    : `${fmtInt(sampleChanges)} 格将被改写`}
                </Chip>
              )}
              {(preview.report.date_time_columns?.length ?? 0) > 0 && (
                <Chip tone="warn" className="shrink-0"
                  title="这些列含时间分量，已拆成日期列 + 时间列">
                  拆出 {preview.report.date_time_columns!.length} 个时间列
                </Chip>
              )}
            </>
          )}
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {previewBusy && <CircleNotch size={14} weight="bold" className="animate-spin text-faint" aria-label="正在重算预览" />}
          {!nativeShell && (
            <Chip title="未接管到桌面壳，原生对话框不可用时退回内联目录浏览">浏览器模式</Chip>
          )}
          <Segmented
            value={theme}
            onChange={(v) => setTheme(v)}
            options={[
              { value: 'dark', label: '暗', icon: <Moon size={13} />, title: '暗色' },
              { value: 'light', label: '亮', icon: <Sun size={13} />, title: '亮色' },
            ]}
          />
        </div>
      </header>

      {notice && (
        <div className="flex shrink-0 items-center gap-2 border-b border-line bg-surface2 px-4 py-2 text-[12px]">
          <Info size={14} className="shrink-0 text-accent" aria-hidden />
          <span className="min-w-0 flex-1">{notice}</span>
          <button type="button" className="shrink-0 text-faint hover:text-ink" onClick={() => setNotice(null)} aria-label="关闭提示">
            <X size={13} />
          </button>
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        <aside className="flex w-[344px] shrink-0 flex-col border-r border-line bg-surface">
          <FileQueue
            items={items}
            focus={focus}
            dirs={sources.dirs}
            busy={running}
            onSelect={setFocus}
            onRemove={(p) => void removeOne(p)}
            onClear={clearAll}
            onPickFiles={() => void chooseFiles()}
            onPickFolder={() => void chooseFolder()}
            onDropFiles={(files) => void dropFiles(files)}
            onAddPaths={(paths) => void addPaths(paths)}
          />
          <FadeScroll className="min-h-0 flex-1 border-t border-line">
            <RulePanel
              config={config}
              columns={(preview?.columns ?? []).map((c) => c.name)}
              focusName={preview?.name ?? null}
              idColumns={preview?.report.id_columns ?? []}
              onChange={patchConfig}
            />
          </FadeScroll>
        </aside>

        <main className="flex min-w-0 flex-1 flex-col">
          <div className="flex h-11 shrink-0 items-center gap-2 border-b border-line bg-surface px-3">
            <Segmented
              value={tab}
              onChange={setTab}
              options={[
                { value: 'columns', label: '列检视', icon: <Stack size={13} /> },
                { value: 'grid', label: '数据差异', icon: <GridFour size={13} /> },
                { value: 'report', label: '运行报告', icon: <Scroll size={13} /> },
              ]}
            />
            {stale && focus && <Chip tone="warn">规则已改，正在重算</Chip>}
            <span className="hint ml-auto hidden items-center gap-1.5 xl:flex">
              <ShieldCheck size={13} className="shrink-0 text-faint" />
              解析失败一律保留原值，标识符列不会被吃成数字
            </span>
          </div>

          {!items.length ? (
            <EmptyState
              icon={<Database size={30} weight="duotone" />}
              title="还没有待处理的文件"
              body="在左侧加入 CSV / TXT / XLSX。这里会先给出逐列的「原值 → 结果」对照，确认无误再写出文件。预览只读，不碰原始数据。"
              action={
                <div className="mt-1 flex gap-2">
                  <Button variant="primary" onClick={() => void chooseFiles()}>选择文件</Button>
                  <Button onClick={() => void chooseFolder()}>选择文件夹</Button>
                </div>
              }
            />
          ) : (
            <div className="flex min-h-0 flex-1 flex-col">
              {previewErr && (
                <div className="m-3 flex shrink-0 items-start gap-2 rounded-[var(--radius-control)] border border-err/40 bg-err/10 px-3 py-2 text-[12px] text-err">
                  <WarningCircle size={14} weight="fill" className="mt-[1px] shrink-0" />
                  <span className="min-w-0 flex-1">{previewErr}</span>
                  <Button size="sm" variant="ghost" onClick={() => focus && void runPreview(focus, config)}>
                    <ArrowsClockwise size={13} /> 重试
                  </Button>
                </div>
              )}
              {previewBusy && !preview && !previewErr && <SkeletonRows rows={6} />}
              {preview && tab === 'columns' && (
                <ColumnInspector
                  preview={preview}
                  stale={stale}
                  protectedColumns={protectedColumns}
                  onToggleProtect={toggleProtect}
                />
              )}
              {preview && tab === 'grid' && (
                <DiffGrid preview={preview} stale={stale} protectedColumns={protectedColumns} />
              )}
              {tab === 'report' && (
                <RunReport
                  run={run}
                  preview={preview}
                  onOpenDir={(p) => void revealInExplorer(p)}
                  onRetry={() => void startRun()}
                  onExport={() => void exportReport()}
                />
              )}
            </div>
          )}
        </main>
      </div>

      <div className="flex h-10 shrink-0 items-center gap-2 border-t border-line bg-surface px-4">
        <span className="label shrink-0">输出目录</span>
        <span className="num min-w-0 flex-1 truncate text-[11.5px] text-muted" title={outputDir || DASH}>
          {outputDir || DASH}
        </span>
        {overwriteCount > 0 && <Chip tone="warn">{fmtInt(overwriteCount)} 个输出已存在，会被覆盖</Chip>}
        {items.some((i) => i.merged_stem) && <Chip tone="accent">同名文件已加目录前缀</Chip>}
        {outputDir && !suggested.startsWith(outputDir) && !outputTouched && (
          <Chip title="输出目录跟随输入位置">默认</Chip>
        )}
        <div className="flex shrink-0 items-center gap-1.5">
          <span className="label">格式</span>
          <Segmented
            value={config.output_format}
            onChange={(v) => patchConfig({ output_format: v })}
            options={[
              { value: 'keep', label: '跟随输入', title: 'Excel 出 xlsx，文本出 csv（默认）' },
              { value: 'xlsx', label: 'xlsx', title: '一律写 xlsx' },
              { value: 'csv', label: 'csv', title: '一律写 utf-8-sig 的 csv，实测比写 xlsx 快约 28 倍' },
              { value: 'both', label: '两种都写', title: '同一份数据写 xlsx 与 csv 各一份' },
            ]}
          />
        </div>
        <Button size="sm" onClick={() => setBrowse('out')} disabled={running}>更改</Button>
      </div>

      <footer className="flex h-[56px] shrink-0 items-center gap-4 border-t border-line bg-surface px-4">
        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
          <div className="flex items-baseline gap-2 text-[11.5px] text-muted">
            <span className="num">
              {run.status === 'idle'
                ? `${fmtInt(items.length)} 个文件待处理`
                : `${fmtInt(run.done)} / ${fmtInt(run.total)} 完成，失败 ${fmtInt(run.failed)}`}
            </span>
            {run.status !== 'idle' && run.elapsed > 0 && (
              <span className="num text-faint">{run.elapsed.toFixed(2)}s</span>
            )}
          </div>
          <div
            className="h-[3px] w-full overflow-hidden rounded-full bg-surface3"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(progress * 100)}
          >
            <div
              className={cn('h-full w-full origin-left bg-accent transition-transform duration-300 ease-out',
                running && progress === 0 && 'anim-pulse')}
              style={{ transform: `scaleX(${progress})` }}
            />
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {running ? (
            <Button onClick={() => void cancelRun()}>
              <Stop size={14} weight="fill" /> 中止
            </Button>
          ) : (
            <Tooltip content={items.length && outputDir ? '按当前规则写出 _cleaned 文件' : '先加入文件并选择输出目录'}>
              <span>
                {/* 上一版是 `... || busy && !items.length`: && 优先于 ||, 有文件排队时
                    busy 这一项恒不生效 —— 跑批中途还能再点一次开始, 两个批次写同一个输出
                    目录互相顶掉进度流, 还会并发抢写产出登记。 */}
                <Button variant="primary" disabled={!items.length || !outputDir || busy}
                  onClick={() => void startRun()}>
                  <Play size={14} weight="fill" /> 开始清洗
                </Button>
              </span>
            </Tooltip>
          )}
        </div>
      </footer>

      {browse && (
        <BrowseDialog
          kind={browse === 'out' ? 'folder' : 'files'}
          initialPath={browse === 'out' ? outputDir : dirname(items[0]?.path ?? '')}
          onClose={() => setBrowse(null)}
          onPick={async (paths) => {
            setBrowse(null)
            if (!paths.length) return
            if (browse === 'out') {
              setOutputTouched(true)
              setOutputDir(paths[0])
            }
            else await addSources({ files: paths })
          }}
        />
      )}
    </div>
  )
}

function msg(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}
