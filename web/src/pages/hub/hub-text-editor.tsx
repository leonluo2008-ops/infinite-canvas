/**
 * AT-22 E3 §5.1：**一个编辑承载，两处复用**（红线 25）——行内「用我的描述」单元格（cell）
 * 与提示词全文编辑（doc，AT-21 用）。本文件只做三件事：输入/脏态/快捷键/字数 + 三态条 +
 * 保存状态机；**不发请求、不判任务状态、不解析自检证据**——接口调用与结果映射在各自 adapter
 * （页面文件内）注入 `onSave`，服务端返回什么就显示什么。
 *
 * 为什么新建而不是复用 `model-script-editor.tsx`：那是渠道/模型 JS 脚本编辑器（codemirror +
 * javascript 语法 + 全屏 Modal + 左侧插件步骤栏），语义/入参/形态三处都不合（E3 §5.4 代码证据）。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Input, Tooltip } from "antd";

export type SavePhase = "idle" | "dirty" | "saving" | "rendering" | "checking" | "done" | "failed";

/** 保存结果（两处同形）：`unchanged`=值没变、什么都没做；`重渲染`=服务端这次真做了没有。 */
export type SaveResult = {
    unchanged?: boolean;
    重渲染?: { 做了?: boolean; prompt_chars?: number; 阶段?: string[] };
    说明?: string;
};

export type SaveRerenderState = {
    draft: string;
    baseline: string;
    phase: SavePhase;
    error: string;
    result: SaveResult | null;
    dirty: boolean;
    begin: (baseline: string, initial?: string) => void;
    setDraft: (value: string) => void;
    /** 保存（next=draft, expect=baseline）→ 成功进 done / 失败进 failed（草稿不丢）。 */
    commit: () => Promise<void>;
    /** 失败后「再试一次」：用当前草稿重发（服务端按幂等/重渲染分支处理）。 */
    retry: () => Promise<void>;
    cancel: () => void;
};

/**
 * 保存→重渲染→自检 的统一状态机。`save` 由 adapter 注入（cell=改描述接口，doc=提示词接口）。
 *
 * 阶段点是**展示用**的（后端是一次同步请求，真实阶段由响应里的 `重渲染.阶段` 如实回传）；
 * 这里只按时间推进提示，不谎报任何一步已完成。
 */
export function useSaveRerender(
    save: (next: string, expect: string) => Promise<SaveResult>,
): SaveRerenderState {
    const [draft, setDraftState] = useState("");
    const [baseline, setBaseline] = useState("");
    const [phase, setPhase] = useState<SavePhase>("idle");
    const [error, setError] = useState("");
    const [result, setResult] = useState<SaveResult | null>(null);
    const timers = useRef<number[]>([]);
    const phaseRef = useRef<SavePhase>("idle");

    const clearTimers = useCallback(() => {
        timers.current.forEach((id) => window.clearTimeout(id));
        timers.current = [];
    }, []);
    useEffect(() => clearTimers, [clearTimers]);

    const setPhaseBoth = useCallback((next: SavePhase) => {
        phaseRef.current = next;
        setPhase(next);
    }, []);

    const begin = useCallback(
        (nextBaseline: string, initial?: string) => {
            clearTimers();
            setBaseline(nextBaseline);
            setDraftState(initial ?? nextBaseline);
            setError("");
            setResult(null);
            setPhaseBoth("idle");
        },
        [clearTimers, setPhaseBoth],
    );

    const setDraft = useCallback(
        (value: string) => {
            setDraftState(value);
            if (phaseRef.current === "saving" || phaseRef.current === "rendering" || phaseRef.current === "checking") return;
            setPhaseBoth(value === baseline ? "idle" : "dirty");
        },
        [baseline, setPhaseBoth],
    );

    const run = useCallback(
        async (next: string) => {
            clearTimers();
            setError("");
            setPhaseBoth("saving");
            // 进度点只是「还在跑」的可见性；真实阶段以响应里的 重渲染.阶段 为准。
            timers.current.push(window.setTimeout(() => setPhaseBoth("rendering"), 700));
            timers.current.push(window.setTimeout(() => setPhaseBoth("checking"), 1800));
            try {
                const out = await save(next, baseline);
                clearTimers();
                setResult(out);
                setPhaseBoth("done");
            } catch (err) {
                clearTimers();
                setError(err instanceof Error ? err.message : String(err));
                setPhaseBoth("failed");
            }
        },
        [baseline, clearTimers, save, setPhaseBoth],
    );

    const commit = useCallback(async () => {
        if (phaseRef.current === "saving" || phaseRef.current === "rendering" || phaseRef.current === "checking") return;
        await run(draft);
    }, [draft, run]);

    const retry = useCallback(async () => {
        if (phaseRef.current === "saving" || phaseRef.current === "rendering" || phaseRef.current === "checking") return;
        await run(draft);
    }, [draft, run]);

    const cancel = useCallback(() => {
        clearTimers();
        setDraftState(baseline);
        setError("");
        setResult(null);
        setPhaseBoth("idle");
    }, [baseline, clearTimers, setPhaseBoth]);

    return {
        draft, baseline, phase, error, result,
        dirty: draft !== baseline,
        begin, setDraft, commit, retry, cancel,
    };
}

/** 三态条：进行中（阶段文案 + 细进度点）/ 成功（一句话）/ 失败（原文 + 再试一次）。 */
export function SaveRerenderBar({
    phase,
    error,
    okText,
    onRetry,
    stageText,
}: {
    phase: SavePhase;
    error?: string;
    okText?: string;
    onRetry?: () => void;
    stageText?: string;
}) {
    if (phase === "saving" || phase === "rendering" || phase === "checking") {
        const line = stageText
            || (phase === "saving" ? "正在保存并重渲染…" : phase === "rendering" ? "正在重出提示词…" : "正在重跑自检…");
        return (
            <div className="flex min-w-0 items-center gap-1.5 text-[11px] text-stone-500 dark:text-stone-400" data-hub-desc-phase={phase}>
                <span className="inline-flex gap-0.5" aria-hidden>
                    <span className="size-1 animate-pulse rounded-full bg-stone-400" />
                    <span className="size-1 animate-pulse rounded-full bg-stone-400 [animation-delay:150ms]" />
                    <span className="size-1 animate-pulse rounded-full bg-stone-400 [animation-delay:300ms]" />
                </span>
                <span className="min-w-0 break-words">{line}</span>
            </div>
        );
    }
    if (phase === "done") {
        return (
            <div className="break-words text-[11px] text-emerald-700 dark:text-emerald-400" data-hub-desc-phase="done">
                {okText || "已保存"}
            </div>
        );
    }
    if (phase === "failed") {
        return (
            <div className="flex min-w-0 flex-wrap items-start gap-2 text-[11px] text-red-700 dark:text-red-300" data-hub-desc-phase="failed">
                <span className="min-w-0 flex-1 break-words" data-hub-desc-error>{error || "保存失败（没拿到原因）"}</span>
                {onRetry ? (
                    <button type="button" className="shrink-0 underline decoration-dotted" onClick={onRetry} data-hub-desc-retry>
                        再试一次
                    </button>
                ) : null}
            </div>
        );
    }
    return null;
}

/** 编辑控件：cell=行内单元格（正文字体 3–8 行）；doc=提示词全文（等宽 + 更高）。 */
export function HubTextEditor({
    value,
    baseline,
    variant,
    placeholder,
    hint,
    before,
    maxLength = 500,
    softMax = 200,
    saveLabel,
    cancelLabel = "取消",
    onSave,
    onCancel,
    disabled = false,
    autoFocus = true,
    onSaved,
}: {
    value: string;
    baseline: string;
    variant: "cell" | "doc";
    placeholder?: string;
    hint?: string;
    /** 输入框上方的只读参照（如「原物描述」），与 hint 分开是为了它可被选中复制。 */
    before?: { label: string; text: string } | null;
    maxLength?: number;
    softMax?: number;
    saveLabel: string;
    cancelLabel?: string;
    onSave: (next: string, expect: string) => Promise<SaveResult>;
    onCancel: () => void;
    disabled?: boolean;
    autoFocus?: boolean;
    /** 保存成功后回调（调用方刷新详情/收起编辑器/切基线）。 */
    onSaved?: (result: SaveResult, next: string) => void;
}) {
    const store = useSaveRerender(onSave);
    const { begin, setDraft, commit, retry, cancel, dirty, phase, error, result, draft } = store;
    const [escWarn, setEscWarn] = useState(false);

    useEffect(() => {
        begin(baseline, value);
        setEscWarn(false);
        // 只在打开/基线变化时重置草稿（轮询回填不该冲掉正在编辑的草稿，见 dirty 判断）
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [baseline]);

    useEffect(() => {
        if (phase === "done" && result) onSaved?.(result, draft);
        // 只在进入 done 的这一跳回调一次
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [phase]);

    const busy = phase === "saving" || phase === "rendering" || phase === "checking";
    const tooLong = draft.length > softMax;
    const overHard = draft.length > maxLength;

    return (
        <div className="w-full min-w-0 space-y-1.5 rounded-md border border-indigo-200 bg-indigo-50/40 p-2 dark:border-indigo-900 dark:bg-indigo-950/20" data-hub-text-editor={variant}>
            {before?.text ? (
                <div className="break-words text-[11px] text-stone-500 dark:text-stone-400" data-hub-desc-origin>
                    <span className="font-medium">{before.label}：</span>
                    {before.text}
                </div>
            ) : null}
            <Input.TextArea
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                placeholder={placeholder}
                autoFocus={autoFocus}
                autoSize={variant === "cell" ? { minRows: 3, maxRows: 8 } : { minRows: 6, maxRows: 18 }}
                disabled={disabled || busy}
                maxLength={maxLength}
                data-hub-desc-input
                className={variant === "doc" ? "font-mono text-xs" : "text-sm"}
                onKeyDown={(event) => {
                    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                        event.preventDefault();
                        if (dirty && !overHard) void commit();
                        return;
                    }
                    if (event.key === "Escape") {
                        event.preventDefault();
                        if (busy) return;
                        // 有改动先要一次「二次确认」——用行内两步（第一次 Esc 只提示，第二次才丢弃），
                        // 不弹阻塞式 confirm（阻塞弹窗会掐断自动化脚本与键盘流）。
                        if (dirty && !escWarn) {
                            setEscWarn(true);
                            return;
                        }
                        cancel();
                        onCancel();
                    }
                }}
            />
            {escWarn && dirty ? (
                <div className="break-words text-[11px] text-amber-700 dark:text-amber-400" data-hub-desc-esc-warn>
                    有没保存的改动：再按一次 Esc 放弃，或点「{saveLabel}」。
                </div>
            ) : null}
            <div className="flex flex-wrap items-center gap-2">
                {hint ? <span className="min-w-0 flex-1 break-words text-[11px] text-stone-500 dark:text-stone-400">{hint}</span> : <span className="flex-1" />}
                <Tooltip title={overHard ? `上限 ${maxLength} 字` : ""}>
                    <span
                        className={`shrink-0 text-[11px] tabular-nums ${overHard ? "text-red-600" : tooLong ? "text-amber-600" : "text-stone-400"}`}
                        data-hub-desc-counter
                    >
                        {draft.length}/{maxLength}
                    </span>
                </Tooltip>
                <Button size="small" onClick={() => { cancel(); onCancel(); }} disabled={busy} data-hub-desc-cancel>
                    {cancelLabel}
                </Button>
                <Button
                    size="small"
                    type="primary"
                    loading={busy}
                    disabled={(!dirty && phase !== "failed") || overHard || disabled}
                    onClick={() => void commit()}
                    data-hub-desc-save
                >
                    {saveLabel}
                </Button>
            </div>
            <SaveRerenderBar
                phase={phase}
                error={error}
                onRetry={() => void retry()}
                okText={result?.unchanged ? "值没变，什么都没做（没重跑渲染）" : "已保存并重渲染"}
            />
        </div>
    );
}
