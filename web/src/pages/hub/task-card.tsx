import { CheckCircle2, CircleAlert, Download, FileText, LoaderCircle, RefreshCw, Sparkles, Upload, Video } from "lucide-react";
import { App, Button, Progress, Tag } from "antd";
import { useCallback, useEffect, useRef, useState, type DragEvent } from "react";
import { Streamdown } from "streamdown";

import { approveHubTask, confirmHubReplace, errorText, getHubTask, hubOutputUrl, renderHubTask, retryHubTask, submitHubTask, uploadHubAssets, type HubPairLine, type HubRenderPreview, type HubTaskDetail, type HubTaskSummary } from "./hub-api";

const STATE_META: Record<string, { label: string; dot: string; pill: string }> = {
    ingested: { label: "已建任务", dot: "bg-stone-400", pill: "bg-stone-100 text-stone-600 dark:bg-stone-800 dark:text-stone-300" },
    discussing: { label: "拆解中", dot: "bg-blue-500", pill: "bg-blue-50 text-blue-700 dark:bg-blue-950/40 dark:text-blue-300" },
    awaiting_assets: { label: "待置换", dot: "bg-amber-500", pill: "bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300" },
    rendering: { label: "渲染中", dot: "bg-violet-500", pill: "bg-violet-50 text-violet-700 dark:bg-violet-950/40 dark:text-violet-300" },
    awaiting_approval: { label: "待审批", dot: "bg-cyan-500", pill: "bg-cyan-50 text-cyan-700 dark:bg-cyan-950/40 dark:text-cyan-300" },
    approved: { label: "已批准·待提交", dot: "bg-teal-500", pill: "bg-teal-50 text-teal-700 dark:bg-teal-950/40 dark:text-teal-300" },
    generating: { label: "生成中", dot: "bg-indigo-500", pill: "bg-indigo-50 text-indigo-700 dark:bg-indigo-950/40 dark:text-indigo-300" },
    done: { label: "已交付", dot: "bg-emerald-500", pill: "bg-emerald-50 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300" },
    failed: { label: "失败", dot: "bg-red-500", pill: "bg-red-50 text-red-700 dark:bg-red-950/40 dark:text-red-300" },
};

const DOWNLOAD_LINK_CLASS = "inline-flex h-8 items-center gap-1.5 rounded-md bg-stone-900 px-3 text-sm font-medium text-white transition hover:bg-stone-700 dark:bg-stone-100 dark:text-stone-900 dark:hover:bg-stone-300";

export function TaskCard({ task, onStateChange, onRefresh }: { task: HubTaskSummary; onStateChange: (localId: string, state: string) => void; onRefresh: () => void }) {
    const { message } = App.useApp();
    const fileInputRef = useRef<HTMLInputElement>(null);
    const assetDragDepthRef = useRef(0);
    const renderJobRef = useRef<Promise<void> | null>(null);
    const [detail, setDetail] = useState<HubTaskDetail | null>(null);
    const [detailError, setDetailError] = useState("");
    const [busy, setBusy] = useState<"upload" | "confirm" | "approve" | "submit" | "retry" | null>(null);
    const [assetDragActive, setAssetDragActive] = useState(false);
    const [renderError, setRenderError] = useState("");
    const [preview, setPreview] = useState<HubRenderPreview | null>(null);
    const [renderPrompt, setRenderPrompt] = useState("");

    const loadDetail = useCallback(async () => {
        try {
            const next = await getHubTask(task.local_id);
            setDetail(next);
            setDetailError("");
            if (next.state && next.state !== task.state) onStateChange(task.local_id, next.state);
        } catch (error) {
            setDetailError(errorText(error));
        }
    }, [task.local_id, task.state, onStateChange]);

    useEffect(() => {
        void loadDetail();
    }, [loadDetail]);

    // 拆解中：stage 实时刷新（5s，与列表轮询同频）——拆解进度可见（2026-11-09 用户要求）
    useEffect(() => {
        if (task.state !== "discussing") return;
        const timer = window.setInterval(() => void loadDetail(), 5000);
        return () => window.clearInterval(timer);
    }, [task.state, loadDetail]);

    // 渲染是秒级动作：由前端触发 GET render，成功后状态迁到待审批；失败下个 tick 再试。
    const ensureRender = useCallback(() => {
        if (renderJobRef.current) return;
        const job = renderHubTask(task.local_id)
            .then((data) => {
                setRenderError("");
                if (data.preview) setPreview(data.preview);
                if (data.prompt_md) setRenderPrompt(data.prompt_md);
                if (data.state) onStateChange(task.local_id, data.state);
                void loadDetail();
            })
            .catch((error) => {
                setRenderError(errorText(error));
            })
            .finally(() => {
                renderJobRef.current = null;
            });
        renderJobRef.current = job;
    }, [task.local_id, onStateChange, loadDetail]);

    useEffect(() => {
        if (task.state !== "rendering") return;
        void ensureRender();
        const timer = window.setInterval(() => {
            void ensureRender();
            void loadDetail();
        }, 2000);
        return () => window.clearInterval(timer);
    }, [task.state, ensureRender, loadDetail]);

    const uploadAssets = async (files: FileList | null) => {
        const images = Array.from(files || []).filter((item) => item.type.startsWith("image/"));
        if (!images.length) {
            message.warning("请选择图片文件");
            return;
        }
        if (busy) return;
        setBusy("upload");
        try {
            const data = await uploadHubAssets(task.local_id, images);
            setDetail((prev) => (prev && data.filled.length ? { ...prev, pair_lines: data.filled } : prev));
            if (data.state) onStateChange(task.local_id, data.state);
            message.success(`已上传 ${images.length} 张替换图，核对配对后确认`);
            void loadDetail();
        } catch (error) {
            message.error(`上传替换图失败：${errorText(error)}`);
        } finally {
            setBusy(null);
            if (fileInputRef.current) fileInputRef.current.value = "";
        }
    };

    const handleAssetDragEnter = (event: DragEvent<HTMLDivElement>) => {
        event.preventDefault();
        assetDragDepthRef.current += 1;
        if (event.dataTransfer.types.includes("Files")) setAssetDragActive(true);
    };

    const handleAssetDragLeave = (event: DragEvent<HTMLDivElement>) => {
        event.preventDefault();
        assetDragDepthRef.current = Math.max(0, assetDragDepthRef.current - 1);
        if (!assetDragDepthRef.current) setAssetDragActive(false);
    };

    const handleAssetDrop = (event: DragEvent<HTMLDivElement>) => {
        event.preventDefault();
        assetDragDepthRef.current = 0;
        setAssetDragActive(false);
        void uploadAssets(event.dataTransfer.files);
    };

    const confirmReplace = async () => {
        setBusy("confirm");
        try {
            const data = await confirmHubReplace(task.local_id);
            onStateChange(task.local_id, data.state || "rendering");
            message.success("置换已确认，开始渲染提示词");
        } catch (error) {
            message.error(`确认置换失败：${errorText(error)}`);
        } finally {
            setBusy(null);
            onRefresh();
        }
    };

    const submit = async () => {
        setBusy("submit");
        try {
            const data = await submitHubTask(task.local_id);
            onStateChange(task.local_id, "generating");
            message.success(data.job_id ? `已提交生成（job ${data.job_id}）` : "已提交生成，等待成片");
        } catch (error) {
            message.error(`提交生成失败：${errorText(error)}`);
        } finally {
            setBusy(null);
            onRefresh();
        }
    };

    const approveAndSubmit = async () => {
        setBusy("approve");
        try {
            const data = await approveHubTask(task.local_id);
            onStateChange(task.local_id, data.state || "approved");
            try {
                const submitted = await submitHubTask(task.local_id);
                onStateChange(task.local_id, "generating");
                message.success(submitted.job_id ? `已批准并提交生成（job ${submitted.job_id}）` : "已批准并提交生成，等待成片");
            } catch (error) {
                message.warning(`已批准，但提交生成失败：${errorText(error)}。可点「提交生成」重试`);
            }
        } catch (error) {
            message.error(`批准失败：${errorText(error)}`);
        } finally {
            setBusy(null);
            onRefresh();
        }
    };

    const retry = async () => {
        setBusy("retry");
        try {
            await retryHubTask(task.local_id);
            message.success("已触发重试");
        } catch {
            // 重试端点可能尚未上桥：按合同退化为「仅刷新任务状态」。
            message.info("已刷新任务状态");
        } finally {
            setBusy(null);
            onRefresh();
            void loadDetail();
        }
    };

    const meta = STATE_META[task.state] || { label: task.state || "未知状态", dot: "bg-stone-400", pill: "bg-stone-100 text-stone-600 dark:bg-stone-800 dark:text-stone-300" };
    const pairLines = detail?.pair_lines || [];
    const filledLines = pairLines.filter((line) => Boolean(line.url));
    const promptMd = renderPrompt || detail?.prompt_md || "";
    const rawProgress = typeof task.gen_progress === "number" ? task.gen_progress : detail?.gen_progress;
    const progress = typeof rawProgress === "number" && Number.isFinite(rawProgress) ? Math.max(0, Math.min(100, Math.round(rawProgress))) : null;
    const snapshot = preview?.snapshot;
    const snapshotSrc = typeof snapshot === "string" && /^(https?:\/\/|\/|data:image\/)/.test(snapshot) ? snapshot : "";

    const operation = (() => {
        if (task.state === "ingested" || task.state === "discussing") {
            const stage = detail?.lapian_stage || "";
            return (
                <div className="space-y-1 text-sm text-stone-500 dark:text-stone-400">
                    <div className="flex items-center gap-2">
                        <LoaderCircle className="size-4 animate-spin" />
                        {task.state === "ingested" ? "任务已建立，等待开始拆解…" : stage || "拆解中（通常 1–3 分钟），完成后自动进入待置换。"}
                    </div>
                    {stage ? (
                        <div className="pl-6 text-xs text-stone-400 dark:text-stone-500">
                            拆解中，完成后自动进入待置换。当前阶段实时更新；完整过程见任务目录「拆解日志.txt」。
                        </div>
                    ) : null}
                </div>
            );
        }

        if (task.state === "awaiting_assets") {
            return (
                <div className="space-y-3">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="text-sm font-medium">替换图（可多张）</span>
                        <span className="text-xs text-stone-500 dark:text-stone-400">{pairLines.length ? `已配对 ${filledLines.length} / ${pairLines.length} 行` : "等待资产清单…"}</span>
                    </div>

                    <div
                        role="button"
                        tabIndex={0}
                        className={`flex min-h-20 cursor-pointer flex-col items-center justify-center gap-1.5 rounded-lg border border-dashed px-3 py-3 text-center text-sm transition ${
                            assetDragActive ? "border-stone-900 bg-stone-100/80 dark:border-stone-100 dark:bg-stone-900/80" : "border-stone-300 bg-stone-50/60 hover:border-stone-400 dark:border-stone-700 dark:bg-stone-900/40 dark:hover:border-stone-500"
                        }`}
                        onClick={() => fileInputRef.current?.click()}
                        onKeyDown={(event) => {
                            if (event.key === "Enter" || event.key === " ") {
                                event.preventDefault();
                                fileInputRef.current?.click();
                            }
                        }}
                        onDragEnter={handleAssetDragEnter}
                        onDragOver={(event) => {
                            event.preventDefault();
                            event.dataTransfer.dropEffect = "copy";
                        }}
                        onDragLeave={handleAssetDragLeave}
                        onDrop={handleAssetDrop}
                    >
                        {busy === "upload" ? (
                            <>
                                <LoaderCircle className="size-5 animate-spin text-stone-400" />
                                <span className="text-stone-500 dark:text-stone-400">正在上传替换图…</span>
                            </>
                        ) : (
                            <>
                                <Upload className="size-5 text-stone-400" />
                                <span className="font-medium">{assetDragActive ? "松开上传替换图" : "拖入替换图到这里，或点击选择（可多张）"}</span>
                                <span className="text-xs text-stone-500 dark:text-stone-400">按清单行顺序自动配对；上传后在下方核对</span>
                            </>
                        )}
                    </div>

                    {pairLines.length ? <PairTable lines={pairLines} /> : null}

                    {filledLines.length ? (
                        <div className="flex flex-wrap items-center justify-between gap-2">
                            <span className="text-xs text-stone-500 dark:text-stone-400">核对配对无误后确认；未配对的行沿用原片资产。</span>
                            <Button type="primary" icon={<CheckCircle2 className="size-4" />} loading={busy === "confirm"} onClick={() => void confirmReplace()}>
                                确认置换
                            </Button>
                        </div>
                    ) : null}
                </div>
            );
        }

        if (task.state === "rendering") {
            return (
                <div className="space-y-1.5">
                    <div className="flex items-center gap-2 text-sm text-stone-500 dark:text-stone-400">
                        <LoaderCircle className="size-4 animate-spin" />
                        渲染中（通常几秒），生成提示词与预演快照…
                    </div>
                    {renderError ? <div className="text-xs text-amber-600 dark:text-amber-400">渲染调用未成功：{renderError}。仍在继续轮询任务状态。</div> : null}
                </div>
            );
        }

        if (task.state === "awaiting_approval") {
            return (
                <div className="space-y-3">
                    <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-medium">提示词（全文）</span>
                        {preview?.duration ? <Tag className="m-0">时长 {String(preview.duration)}</Tag> : null}
                        {preview?.ratio ? <Tag className="m-0">比例 {String(preview.ratio)}</Tag> : null}
                        {snapshotSrc ? <img src={snapshotSrc} alt="渲染快照" className="h-9 rounded border border-stone-200 object-cover dark:border-stone-800" /> : null}
                    </div>
                    {promptMd ? (
                        <div className="agent-streamdown thin-scrollbar max-h-96 overflow-auto rounded-md border border-stone-200 bg-stone-50 px-3 py-2 text-sm dark:border-stone-800 dark:bg-stone-900">
                            <Streamdown>{promptMd}</Streamdown>
                        </div>
                    ) : (
                        <div className="rounded-md border border-dashed border-stone-300 px-3 py-4 text-sm text-stone-500 dark:border-stone-700 dark:text-stone-400">提示词还没返回，刷新任务后再看。</div>
                    )}
                    <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="text-xs text-stone-500 dark:text-stone-400">批准后自动提交生成；生成会调用付费接口。</span>
                        <Button type="primary" icon={<Sparkles className="size-4" />} loading={busy === "approve"} disabled={!promptMd} onClick={() => void approveAndSubmit()}>
                            批准出片
                        </Button>
                    </div>
                </div>
            );
        }

        if (task.state === "approved") {
            return (
                <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="text-sm text-stone-600 dark:text-stone-300">已批准，等待提交生成。</span>
                    <Button type="primary" loading={busy === "submit"} onClick={() => void submit()}>
                        提交生成
                    </Button>
                </div>
            );
        }

        if (task.state === "generating") {
            return (
                <div className="space-y-2">
                    <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-stone-500 dark:text-stone-400">
                        <span className="flex items-center gap-1.5">
                            <LoaderCircle className="size-3.5 animate-spin" />
                            生成中，云端通常 5 分钟以上，请保持本页打开。
                        </span>
                        <span>{progress !== null ? `${progress}%` : "等待进度"}</span>
                    </div>
                    {progress !== null ? (
                        <Progress percent={progress} showInfo={false} size="small" status="active" />
                    ) : (
                        <div className="h-1.5 w-full overflow-hidden rounded-full bg-stone-200 dark:bg-stone-800">
                            <div className="h-full w-1/3 animate-pulse rounded-full bg-indigo-500" />
                        </div>
                    )}
                </div>
            );
        }

        if (task.state === "done") {
            return (
                <div className="space-y-2">
                    <video src={hubOutputUrl(task.local_id)} controls preload="metadata" className="aspect-video w-full rounded-md border border-stone-200 bg-black object-contain dark:border-stone-800" />
                    <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="text-xs text-stone-500 dark:text-stone-400">成片已落盘，可直接下载交付。</span>
                        <a className={DOWNLOAD_LINK_CLASS} href={hubOutputUrl(task.local_id)} download={`${task.local_id}.mp4`}>
                            <Download className="size-4" />
                            下载成片
                        </a>
                    </div>
                </div>
            );
        }

        if (task.state === "failed") {
            return (
                <div className="rounded-md border border-red-200 bg-red-50 p-3 dark:border-red-950 dark:bg-red-950/20">
                    <div className="flex items-start gap-2 text-sm text-red-700 dark:text-red-300">
                        <CircleAlert className="mt-0.5 size-4 shrink-0" />
                        <div className="min-w-0 break-words">{detail?.error || "任务失败，错误原文未返回。"}</div>
                    </div>
                    <div className="mt-2 flex justify-end">
                        <Button danger size="small" icon={<RefreshCw className="size-3.5" />} loading={busy === "retry"} onClick={() => void retry()}>
                            重试
                        </Button>
                    </div>
                </div>
            );
        }

        return <div className="text-sm text-stone-500 dark:text-stone-400">未知状态：{task.state || "（空）"}</div>;
    })();

    return (
        <article className="flex min-w-0 flex-col rounded-lg border border-stone-200 bg-card p-4 shadow-sm dark:border-stone-800">
            <header className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                        <span className={`size-2.5 shrink-0 rounded-full ${meta.dot}`} />
                        <span className="font-mono text-sm font-semibold tracking-tight">{task.local_id}</span>
                        <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${meta.pill}`}>{meta.label}</span>
                    </div>
                    <div className="mt-1.5 line-clamp-2 text-sm font-medium text-stone-900 dark:text-stone-100">{task.intent || "（未填写意图）"}</div>
                    <div className="mt-1 flex min-w-0 items-center gap-1.5 text-xs text-stone-500 dark:text-stone-400">
                        <Video className="size-3.5 shrink-0" />
                        <span className="truncate">{task.video_name || "参考视频"}</span>
                    </div>
                </div>
                <time className="shrink-0 text-xs text-stone-400 dark:text-stone-500">{formatCreatedAt(task.created_at)}</time>
            </header>

            <div className="mt-3 border-t border-stone-100 pt-3 dark:border-stone-800">
                {detailError ? <div className="mb-2 text-xs text-amber-600 dark:text-amber-400">详情刷新失败：{detailError}</div> : null}
                {operation}
            </div>

            {detail?.summary_md ? <MarkdownDetails title="拆解摘要" markdown={detail.summary_md} defaultOpen={task.state === "awaiting_assets"} /> : null}

            {detail?.checklist_md ? <MarkdownDetails title="资产清单" markdown={detail.checklist_md} defaultOpen={task.state === "awaiting_assets" && !pairLines.length} /> : null}

            <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                multiple
                className="hidden"
                onChange={(event) => {
                    void uploadAssets(event.target.files);
                    event.target.value = "";
                }}
            />
        </article>
    );
}

function MarkdownDetails({ title, markdown, defaultOpen = false }: { title: string; markdown: string; defaultOpen?: boolean }) {
    const [open, setOpen] = useState(defaultOpen);
    useEffect(() => {
        setOpen(defaultOpen);
    }, [defaultOpen]);

    return (
        <details className="mt-3 rounded-md border border-stone-200 dark:border-stone-800" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
            <summary className="flex cursor-pointer list-none items-center justify-between gap-2 px-3 py-2 text-sm font-medium [&::-webkit-details-marker]:hidden">
                <span className="flex items-center gap-1.5">
                    <FileText className="size-3.5" />
                    {title}
                </span>
                <span className="text-xs font-normal text-stone-400 dark:text-stone-500">展开 / 收起</span>
            </summary>
            <div className="agent-streamdown thin-scrollbar max-h-72 overflow-auto border-t border-stone-200 px-3 py-2 text-sm dark:border-stone-800">
                <Streamdown>{markdown}</Streamdown>
            </div>
        </details>
    );
}

function PairTable({ lines }: { lines: HubPairLine[] }) {
    return (
        <div className="thin-scrollbar max-h-64 overflow-y-auto rounded-md border border-stone-200 dark:border-stone-800">
            <table className="w-full table-fixed text-xs">
                <thead className="sticky top-0 bg-stone-50 dark:bg-stone-900">
                    <tr className="text-left text-stone-500 dark:text-stone-400">
                        <th className="w-12 px-2 py-1.5 font-medium">序号</th>
                        <th className="px-2 py-1.5 font-medium">描述</th>
                        <th className="w-20 px-2 py-1.5 font-medium">资产图</th>
                    </tr>
                </thead>
                <tbody>
                    {lines.map((line, index) => {
                        const assetLabel = line.asset || line.img || line.name || "";
                        return (
                            <tr key={`${line.no}-${index}`} className="border-t border-stone-200 dark:border-stone-800">
                                <td className="px-2 py-1.5 align-top text-stone-500 dark:text-stone-400">{line.no}</td>
                                <td className="px-2 py-1.5 align-top">
                                    <div className="line-clamp-2 text-stone-700 dark:text-stone-200">{line.desc || "—"}</div>
                                    {assetLabel ? <div className="mt-0.5 truncate text-stone-400 dark:text-stone-500">{assetLabel}</div> : null}
                                </td>
                                <td className="px-2 py-1.5 align-top">
                                    {line.url ? (
                                        <a href={line.url} target="_blank" rel="noreferrer">
                                            <img src={line.url} alt={`资产 ${line.no}`} className="size-12 rounded border border-stone-200 object-cover dark:border-stone-800" />
                                        </a>
                                    ) : (
                                        <span className="text-stone-400 dark:text-stone-500">未配对</span>
                                    )}
                                </td>
                            </tr>
                        );
                    })}
                </tbody>
            </table>
        </div>
    );
}

function formatCreatedAt(value: string) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}
