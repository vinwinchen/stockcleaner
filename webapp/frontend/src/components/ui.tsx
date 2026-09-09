import { forwardRef, useEffect, useRef, useState, type ReactNode } from 'react'
import * as SwitchPrimitive from '@radix-ui/react-switch'
import * as TooltipPrimitive from '@radix-ui/react-tooltip'
import { clsx, type ClassValue } from 'clsx'
import { CircleNotchIcon } from '@phosphor-icons/react'

export function cn(...inputs: ClassValue[]) {
  return clsx(inputs)
}

/* ---------------------------------------------------------------- 布局骨架 */

export function SectionHead({
  title,
  count,
  right,
  className,
}: {
  title: string
  count?: string
  right?: ReactNode
  className?: string
}) {
  return (
    <div className={cn('flex h-9 shrink-0 items-center justify-between px-3', className)}>
      <div className="flex items-baseline gap-2 min-w-0">
        <span className="text-[12.5px] font-semibold text-ink truncate">{title}</span>
        {count && <span className="num text-[11px] text-faint shrink-0">{count}</span>}
      </div>
      {right && <div className="flex items-center gap-1.5 shrink-0">{right}</div>}
    </div>
  )
}

/* ---------------------------------------------------------------- 开关 */

export function Toggle({
  checked,
  onChange,
  disabled,
  label,
  hint,
  danger,
}: {
  checked: boolean
  onChange: (v: boolean) => void
  disabled?: boolean
  label: string
  hint?: ReactNode
  danger?: boolean
}) {
  return (
    <label
      className={cn(
        'flex cursor-pointer select-none items-start gap-2.5 py-1.5 group',
        disabled && 'cursor-not-allowed opacity-45',
      )}
    >
      <SwitchPrimitive.Root
        className={cn(
          'mt-[3px] shrink-0 h-[18px] w-[32px] rounded-full border transition-colors duration-150',
          'border-line2 bg-surface3',
          'data-[state=checked]:border-accent data-[state=checked]:bg-accent',
          disabled && 'pointer-events-none',
        )}
        checked={checked}
        disabled={disabled}
        onCheckedChange={onChange}
        aria-label={label}
      >
        <SwitchPrimitive.Thumb
          className={cn(
            'block h-[12px] w-[12px] translate-x-[2px] rounded-full transition-transform duration-150',
            checked ? 'bg-onaccent' : 'bg-faint group-hover:bg-muted',
            'data-[state=checked]:translate-x-[16px]',
          )}
        />
      </SwitchPrimitive.Root>
      <span className="min-w-0 flex-1">
        <span className={cn('block text-[12.5px] leading-5', danger && checked && 'text-err')}>
          {label}
        </span>
        {hint && <span className="hint block">{hint}</span>}
      </span>
    </label>
  )
}

/* ---------------------------------------------------------------- 按钮 */

export function Button({
  children,
  variant = 'default',
  size = 'md',
  loading,
  className,
  ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'default' | 'primary' | 'ghost'
  size?: 'sm' | 'md'
  loading?: boolean
}) {
  return (
    <button
      {...rest}
      className={cn(
        'btn',
        variant === 'primary' && 'btn-primary',
        variant === 'ghost' && 'btn-ghost',
        size === 'sm' && 'h-7 px-2 text-[12px]',
        className,
      )}
    >
      {loading && <CircleNotchIcon size={14} weight="bold" className="animate-spin" aria-hidden />}
      {children}
    </button>
  )
}

/* ---------------------------------------------------------------- 图标按钮 */

export function IconButton({
  children,
  label,
  className,
  ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <button
      {...rest}
      type="button"
      title={label}
      aria-label={label}
      className={cn(
        'flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-[var(--radius-chip)]',
        'border border-transparent text-muted transition-colors',
        'hover:border-line hover:bg-surface2 hover:text-ink',
        'active:translate-y-[1px] disabled:opacity-40 disabled:pointer-events-none',
        className,
      )}
    >
      {children}
    </button>
  )
}

/* ---------------------------------------------------------------- 徽标 */

export function Chip({
  children,
  tone = 'neutral',
  className,
  title,
}: {
  children: ReactNode
  tone?: 'neutral' | 'accent' | 'warn' | 'err' | 'ok'
  className?: string
  title?: string
}) {
  return (
    <span
      title={title}
      className={cn(
        'chip',
        tone === 'accent' && 'chip-accent',
        tone === 'warn' && 'chip-warn',
        tone === 'err' && 'chip-err',
        tone === 'ok' && 'chip-accent',
        className,
      )}
    >
      {children}
    </span>
  )
}

/* ---------------------------------------------------------------- 输入 */

export function Field({
  label,
  hint,
  error,
  children,
  className,
  htmlFor,
}: {
  label: string
  hint?: ReactNode
  error?: string
  children: ReactNode
  className?: string
  htmlFor?: string
}) {
  return (
    <div className={cn('flex flex-col gap-1.5', className)}>
      <label className="label" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
      {hint && !error && <span className="hint">{hint}</span>}
      {error && (
        <span className="text-[11.5px] text-err" role="alert">
          {error}
        </span>
      )}
    </div>
  )
}

export const TextInput = forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement> & { mono?: boolean }>(
  function TextInput({ className, mono, ...rest }, ref) {
    return <input ref={ref} {...rest} className={cn('input', mono && 'input-mono', className)} />
  },
)

export function NumberInput({
  value,
  onChange,
  min = 0,
  max = 999999,
  className,
  ...rest
}: {
  value: number
  onChange: (n: number) => void
  min?: number
  max?: number
  className?: string
} & Omit<React.InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange' | 'min' | 'max'>) {
  return (
    <input
      {...rest}
      type="number"
      min={min}
      max={max}
      value={Number.isFinite(value) ? value : 0}
      onChange={(e) => {
        const n = Number.parseInt(e.target.value, 10)
        onChange(Number.isNaN(n) ? min : Math.min(max, Math.max(min, n)))
      }}
      className={cn('input input-mono', className)}
    />
  )
}

/* ---------------------------------------------------------------- 分段控件 */

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  className,
}: {
  value: T
  options: { value: T; label: string; icon?: ReactNode; title?: string }[]
  onChange: (v: T) => void
  className?: string
}) {
  return (
    <div className={cn('flex items-center gap-0.5 rounded-[var(--radius-control)] bg-surface2 p-0.5 border border-line', className)}>
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          onClick={() => onChange(opt.value)}
          aria-pressed={value === opt.value}
          aria-label={opt.title || opt.label}
          title={opt.title || opt.label}
          className={cn(
            'flex h-[26px] items-center gap-1.5 rounded-[6px] px-2.5 text-[12px] font-medium transition-colors',
            value === opt.value
              ? 'bg-surface3 text-ink shadow-[inset_0_1px_0_rgba(255,255,255,0.05)]'
              : 'text-muted hover:text-ink',
          )}
        >
          {opt.icon}
          {opt.label}
        </button>
      ))}
    </div>
  )
}

/* ---------------------------------------------------------------- 骨架与空态 */

export function SkeletonRows({ rows = 4, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn('flex flex-col gap-2 p-3', className)} aria-busy="true" aria-live="polite">
      {Array.from({ length: rows }).map((_, i) => (
        <div
          key={i}
          className="h-[34px] rounded-[var(--radius-chip)] bg-surface2 relative overflow-hidden"
          style={{ opacity: 1 - i * 0.13 }}
        >
          <div className="absolute inset-y-0 w-1/3 anim-sweep bg-gradient-to-r from-transparent via-white/[0.04] to-transparent" />
        </div>
      ))}
    </div>
  )
}

export function EmptyState({
  icon,
  title,
  body,
  action,
}: {
  icon: ReactNode
  title: string
  body?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 px-8 py-10 text-center">
      <div className="text-faint">{icon}</div>
      <div className="text-[13.5px] font-semibold text-muted">{title}</div>
      {body && <div className="hint max-w-[46ch]">{body}</div>}
      {action}
    </div>
  )
}

/* ---------------------------------------------------------------- 滚动容器 */

/**
 * 只在"真的还能往下滚"时才在底部渐隐。
 * 不加这个判断的话, 内容刚好放得下时最后一行也会被淡掉, 那是新的错觉。
 *
 * 观察的是内层内容而不是滚动容器: 容器高度是固定的, 内容变长时它的盒子尺寸不变,
 * ResizeObserver 挂在容器上根本不会触发。
 */
export function FadeScroll({
  children,
  className,
}: {
  children: ReactNode
  className?: string
}) {
  const box = useRef<HTMLDivElement>(null)
  const inner = useRef<HTMLDivElement>(null)
  const [more, setMore] = useState(false)

  useEffect(() => {
    const b = box.current
    const i = inner.current
    if (!b || !i) return
    const measure = () => {
      const canScroll = i.offsetHeight - b.clientHeight > 4
      const atBottom = b.scrollTop + b.clientHeight >= i.offsetHeight - 4
      setMore(canScroll && !atBottom)
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(i)
    b.addEventListener('scroll', measure, { passive: true })
    return () => {
      ro.disconnect()
      b.removeEventListener('scroll', measure)
    }
  }, [])

  return (
    <div ref={box} className={cn('overflow-auto', more && 'scroll-fade', className)}>
      <div ref={inner}>{children}</div>
    </div>
  )
}

/* ---------------------------------------------------------------- 提示气泡 */

/* Provider 已在 main.tsx 根部挂过一次, 这里只放实例, 不重复套 Provider */
export function Tooltip({ content, children }: { content: ReactNode; children: ReactNode }) {
  return (
    <TooltipPrimitive.Root>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          sideOffset={6}
          className="z-50 max-w-[320px] rounded-[var(--radius-chip)] border border-line2 bg-surface3 px-2.5 py-1.5 text-[11.5px] leading-[1.5] text-ink shadow-lg anim-rise"
        >
          {content}
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  )
}
