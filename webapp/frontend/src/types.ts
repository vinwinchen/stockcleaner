export type CellVal = { s: string; t: string; nan?: boolean; padded?: boolean }

export type SamplePair = {
  row: number
  before: CellVal
  after: CellVal
  changed: boolean
}

export type ColumnKind = 'text' | 'number' | 'date' | 'identifier' | 'protected' | 'added'

export type ColumnInfo = {
  name: string
  kind: ColumnKind
  cells: number
  changed: number
  protected: boolean
  id_protected: boolean
  /** 同一列里两种写法会洗成同一个数值 (2023.1 与 2023.10), 内核已写入警告 */
  collision?: boolean
  /** 该列是"日期/时间拆列"新造的列, 原表没有; 值是来源列名 */
  split_from?: string | null
  samples: SamplePair[]
}

export type Report = {
  numeric_cells: number
  unit_cells: number
  date_cells: number
  stripped_cells: number
  fullwidth_cells?: number
  protected_cells?: number
  dropped_rows: number
  id_columns: string[]
  date_columns: string[]
  fullwidth_columns?: string[]
  protected_columns?: string[]
  collision_columns?: string[]
  date_time_columns?: { from: string; to: string; cells: number }[]
  warnings: string[]
  output_ext?: string
  output_extras?: string[]
}

export type GridCell = [CellVal, CellVal, boolean]

export type PreviewResult = {
  ok: true
  path: string
  name: string
  size: number
  encoding: string
  delimiter?: string | null
  engine?: string | null
  rows_in: number
  cols_in: number
  rows_out: number
  cols_out: number
  sampled: boolean
  sample_rows: number
  columns: ColumnInfo[]
  removed_columns: string[]
  grid: {
    columns: string[]
    rows: { row: number; cells: GridCell[] }[]
    truncated_columns: number
    truncated_rows: number
  }
  report: Report
  cached?: boolean
}

export type PreviewError = { ok: false; error: string }

export type FilePlan = {
  path: string
  name: string
  out_name: string
  out_path: string | null
  extra_names: string[]
  merged_stem: boolean
  exists: boolean
}

export type QueueItem = FilePlan & {
  size: number
  rows: number | null
  cols: number | null
  error: string | null
  warnings: string[]
}

export type OutputFormat = 'keep' | 'xlsx' | 'csv' | 'both'

export type Config = {
  strip_tokens: string[]
  strip_column_mode: boolean
  strip_column: string
  numericize: boolean
  convert_units: boolean
  head_cut: number
  tail_cut: number
  drop_empty_rows: boolean
  drop_empty_cols: boolean
  normalize_dates: boolean
  fullwidth: boolean
  output_format: OutputFormat
  column_overrides: Record<string, { protect: boolean }>
  sample_rows: number
}

export type JobEvent = {
  seq: number
  kind: 'hello' | 'job_start' | 'file_start' | 'file_done' | 'file_error' | 'progress'
    | 'job_end' | 'note' | 'replay_gap'
  at: number
  job_id?: string
  total?: number
  output_dir?: string
  index?: number
  name?: string
  path?: string
  /** progress 事件里是"已成功文件数"; file_done 的 ok 由 kind 本身表达, 不进类型 */
  ok?: number
  failed?: number
  done?: number
  error?: string
  elapsed?: number
  status?: string
  rows_out?: number
  cols_out?: number
  output?: string
  outputs?: string[]
  engine?: string
  report?: Report
  cancelled?: boolean
  /** 重连时缓冲已被截断: 丢了前面多少条、回放从哪个 seq 开始 */
  lost?: number
  first_seq?: number | null
  message?: string
}

export type FileOutcome = {
  /** 队列里第几个文件; 重名文件 (递归批量) 靠它区分, 不能只用 name */
  uid: string
  name: string
  ok: boolean
  error?: string
  output?: string
  outputs?: string[]
  engine?: string
  rows_out?: number
  cols_out?: number
  report?: Report
}
