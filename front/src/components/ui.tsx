import * as React from "react";
import { cn } from "@/lib/utils";

type ButtonVariant = "default" | "secondary" | "outline" | "ghost" | "destructive" | "success";
type ButtonSize = "default" | "sm" | "lg" | "icon";

export function buttonVariants({
  variant = "default",
  size = "default",
  className,
}: {
  variant?: ButtonVariant;
  size?: ButtonSize;
  className?: string;
} = {}) {
  const variants: Record<ButtonVariant, string> = {
    default: "bg-primary text-white shadow-sm hover:bg-sky-500 active:bg-sky-600",
    secondary: "border border-slate-200 bg-slate-100 text-slate-800 hover:bg-slate-200 active:bg-slate-200",
    outline: "border border-slate-200 bg-white text-slate-700 hover:border-slate-300 hover:bg-slate-50 hover:text-slate-950",
    ghost: "text-slate-700 hover:bg-slate-100 hover:text-slate-950",
    destructive: "bg-destructive text-destructive-foreground hover:bg-destructive/90",
    success: "bg-success text-success-foreground hover:bg-success/90",
  };
  const sizes: Record<ButtonSize, string> = {
    default: "min-h-10 px-4 py-2 sm:min-h-9",
    sm: "min-h-9 rounded-lg px-3 text-xs",
    lg: "min-h-12 rounded-xl px-6",
    icon: "h-11 w-11 p-0",
  };
  return cn(
    "inline-flex shrink-0 items-center justify-center gap-2 whitespace-nowrap rounded-lg text-sm font-medium transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:pointer-events-none disabled:opacity-45",
    variants[variant],
    sizes[size],
    className
  );
}

export function Card({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "rounded-2xl border border-slate-200 bg-card text-card-foreground",
        className
      )}
      {...props}
    />
  );
}

export function CardHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex flex-col gap-1.5 p-4 pb-3 sm:p-5 sm:pb-3", className)} {...props} />;
}

export function CardTitle({ className, ...props }: React.HTMLAttributes<HTMLHeadingElement>) {
  return <h2 className={cn("text-base font-semibold tracking-tight text-foreground", className)} {...props} />;
}

export function CardDescription({ className, ...props }: React.HTMLAttributes<HTMLParagraphElement>) {
  return <p className={cn("text-sm leading-6 text-muted-foreground", className)} {...props} />;
}

export function CardContent({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("p-4 pt-2 sm:p-5 sm:pt-2", className)} {...props} />;
}

export function Button({
  className,
  variant = "default",
  size = "default",
  type = "button",
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
}) {
  return (
    <button
      type={type}
      className={buttonVariants({ variant, size, className })}
      {...props}
    />
  );
}

export function Input({ className, ...props }: React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={cn(
        "flex min-h-10 w-full rounded-lg border border-input bg-card px-3 py-2 text-sm text-foreground outline-none transition duration-200 placeholder:text-muted-foreground focus:border-primary/60 focus:ring-2 focus:ring-ring/15 disabled:cursor-not-allowed disabled:bg-muted disabled:opacity-60 sm:min-h-9",
        className
      )}
      {...props}
    />
  );
}

export function Textarea({ className, ...props }: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      className={cn(
        "flex min-h-28 w-full rounded-lg border border-input bg-card px-3 py-2 text-sm text-foreground outline-none transition duration-200 placeholder:text-muted-foreground focus:border-primary/60 focus:ring-2 focus:ring-ring/15 disabled:cursor-not-allowed disabled:bg-muted disabled:opacity-60",
        className
      )}
      {...props}
    />
  );
}

export function Label({ className, ...props }: React.LabelHTMLAttributes<HTMLLabelElement>) {
  return (
    <label
      className={cn("text-sm font-medium leading-none text-foreground", className)}
      {...props}
    />
  );
}

export function Badge({
  className,
  variant = "default",
  ...props
}: React.HTMLAttributes<HTMLSpanElement> & {
  variant?: "default" | "secondary" | "success" | "warning" | "destructive" | "outline";
}) {
  const variants = {
    default: "border-sky-200 bg-sky-50 text-sky-700",
    secondary: "border-slate-200 bg-slate-100 text-slate-700",
    success: "border-emerald-200 bg-emerald-50 text-emerald-700",
    warning: "border-amber-200 bg-amber-50 text-amber-700",
    destructive: "border-red-200 bg-red-50 text-red-700",
    outline: "border-border bg-card text-foreground",
  };
  return (
    <span
      className={cn(
        "inline-flex min-h-6 items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold leading-none",
        variants[variant],
        className
      )}
      {...props}
    />
  );
}

export function Select({ className, ...props }: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={cn(
        "flex min-h-10 w-full appearance-auto rounded-lg border border-input bg-card px-3 py-2 text-sm text-foreground outline-none transition duration-200 focus:border-primary/60 focus:ring-2 focus:ring-ring/15 disabled:cursor-not-allowed disabled:bg-muted disabled:opacity-60 sm:min-h-9",
        className
      )}
      {...props}
    />
  );
}

export function PaginationBar({
  page,
  pageSize,
  total,
  loading = false,
  onPageChange,
  onPageSizeChange,
  pageSizeOptions = [20, 50, 100],
}: {
  page: number;
  pageSize: number;
  total: number;
  loading?: boolean;
  onPageChange: (page: number) => void;
  onPageSizeChange?: (pageSize: number) => void;
  pageSizeOptions?: number[];
}) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const safePage = Math.min(Math.max(1, page), totalPages);
  const start = total ? (safePage - 1) * pageSize + 1 : 0;
  const end = Math.min(total, safePage * pageSize);
  const visibleCount = Math.min(5, totalPages);
  const firstVisiblePage = Math.max(1, Math.min(safePage - 2, totalPages - visibleCount + 1));
  const pageNumbers = Array.from({ length: visibleCount }, (_, index) => firstVisiblePage + index);
  return (
    <div className="flex flex-col gap-3 border-t border-slate-200 px-4 py-3 text-xs text-slate-500 sm:flex-row sm:items-center sm:justify-between sm:px-5">
      <div className="flex flex-wrap items-center gap-2">
        <span>共 {total} 条</span>
        <span className="text-slate-300">·</span>
        <span>{start}-{end}</span>
        {onPageSizeChange ? (
          <>
            <span className="text-slate-300">·</span>
            <Select
              className="h-8 min-h-8 w-[76px] py-1 text-xs sm:min-h-8"
              value={String(pageSize)}
              disabled={loading}
              onChange={(event) => onPageSizeChange(Number(event.target.value))}
              aria-label="每页数量"
            >
              {pageSizeOptions.map((value) => <option key={value} value={value}>{value} 条</option>)}
            </Select>
          </>
        ) : null}
      </div>
      <div className="flex items-center justify-between gap-2 sm:justify-end">
        <Button size="sm" variant="outline" disabled={loading || safePage <= 1} onClick={() => onPageChange(safePage - 1)}>上一页</Button>
        <span className="min-w-16 text-center tabular-nums text-slate-600 sm:hidden">{safePage} / {totalPages}</span>
        <div className="hidden items-center gap-1 sm:flex" aria-label="页码">
          {pageNumbers.map((pageNumber) => (
            <Button
              key={pageNumber}
              size="sm"
              variant={pageNumber === safePage ? "default" : "outline"}
              className="h-9 min-h-9 w-9 px-0"
              disabled={loading}
              onClick={() => onPageChange(pageNumber)}
              aria-current={pageNumber === safePage ? "page" : undefined}
            >
              {pageNumber}
            </Button>
          ))}
        </div>
        <Button size="sm" variant="outline" disabled={loading || safePage >= totalPages} onClick={() => onPageChange(safePage + 1)}>下一页</Button>
      </div>
    </div>
  );
}

export function Switch({
  checked,
  onCheckedChange,
  disabled,
  label,
}: {
  checked: boolean;
  onCheckedChange: (value: boolean) => void;
  disabled?: boolean;
  label?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onCheckedChange(!checked)}
      className={cn(
        "relative inline-flex h-7 w-12 shrink-0 items-center rounded-full border-2 border-transparent transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50",
        checked ? "bg-primary" : "bg-slate-300"
      )}
    >
      <span
        className={cn(
          "pointer-events-none block h-5 w-5 rounded-full bg-white shadow-sm transition-transform duration-200",
          checked ? "translate-x-5" : "translate-x-0"
        )}
      />
    </button>
  );
}

export function StatCard({
  title,
  value,
  hint,
  accent = "primary",
  icon,
}: {
  title: string;
  value: React.ReactNode;
  hint?: string;
  accent?: "primary" | "success" | "warning" | "destructive" | "secondary";
  icon?: React.ReactNode;
}) {
  const accents = {
    primary: { line: "bg-sky-500", icon: "bg-sky-50 text-sky-600" },
    success: { line: "bg-emerald-500", icon: "bg-emerald-50 text-emerald-600" },
    warning: { line: "bg-amber-500", icon: "bg-amber-50 text-amber-600" },
    destructive: { line: "bg-red-500", icon: "bg-red-50 text-red-600" },
    secondary: { line: "bg-slate-500", icon: "bg-slate-100 text-slate-700" },
  };
  const style = accents[accent];
  return (
    <Card className="overflow-hidden">
      <CardContent className="p-4 pt-4 sm:p-5 sm:pt-5">
        <div className="mb-3 flex items-center justify-between gap-3">
          <p className="text-sm font-medium text-muted-foreground">{title}</p>
          {icon ? (
            <span className={cn("flex h-9 w-9 items-center justify-center rounded-xl", style.icon)}>
              {icon}
            </span>
          ) : null}
        </div>
        <div className="text-2xl font-bold tabular-nums tracking-tight text-foreground sm:text-3xl">{value}</div>
        {hint ? <p className="mt-1.5 text-xs leading-5 text-muted-foreground">{hint}</p> : null}
      </CardContent>
    </Card>
  );
}

export function EmptyState({ title, description }: { title: string; description?: string }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed bg-muted/35 px-5 py-10 text-center sm:py-14">
      <div className="text-base font-medium text-foreground">{title}</div>
      {description ? <p className="mt-2 max-w-md text-sm leading-6 text-muted-foreground">{description}</p> : null}
    </div>
  );
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold tracking-tight text-foreground">{title}</h1>
        {description ? (
          <p className="mt-1 max-w-3xl text-sm leading-6 text-muted-foreground">{description}</p>
        ) : null}
      </div>
      {actions ? (
        <div className="flex w-full flex-wrap gap-2 sm:w-auto sm:justify-end [&>*]:min-w-0 [&>*]:flex-1 sm:[&>*]:flex-none">
          {actions}
        </div>
      ) : null}
    </div>
  );
}

export function Toast({
  message,
  tone = "default",
}: {
  message: string;
  tone?: "default" | "success" | "error";
}) {
  if (!message) return null;
  const tones = {
    default: "border-border bg-card text-foreground",
    success: "border-emerald-200 bg-emerald-50 text-emerald-800",
    error: "border-red-200 bg-red-50 text-red-800",
  };
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        "fixed inset-x-4 bottom-[calc(5rem+env(safe-area-inset-bottom))] z-[80] rounded-xl border px-4 py-3 text-sm font-medium shadow-lg sm:inset-x-auto sm:bottom-6 sm:right-6 sm:max-w-sm",
        tones[tone]
      )}
    >
      {message}
    </div>
  );
}
