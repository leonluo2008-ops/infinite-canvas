import { ArrowRight, CheckCircle2, ChevronLeft, ChevronRight, CircleAlert, Download, FileText, ImageOff, LoaderCircle, RefreshCw, Sparkles, Type, Upload, Video } from "lucide-react";
import { App, Button, Image as AntdImage, Input, Progress, Tag } from "antd";
import { useCallback, useEffect, useRef, useState, type DragEvent } from "react";
import { Streamdown } from "streamdown";

import { approveHubTask, confirmHubReplace, errorText, getHubTask, hubOutputUrl, postHubAsset, renderHubTask, retryHubTask, submitHubTask, uploadHubAssets, uploadHubImage, type HubAssetCard, type HubPairLine, type HubRenderPreview, type HubTaskDetail, type HubTaskSummary } from "./hub-api";

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
            // B3 视觉卡片流：有 asset_cards 走逐卡置换（图片主载体）；旧 bridge 没有卡 → 退化回文字配对表。
            const cards = detail?.asset_cards || [];
            if (cards.length) {
                return (
                    <AssetCardFlow
                        taskId={task.local_id}
                        cards={cards}
                        onWritten={() => void loadDetail()}
                        onConfirm={() => void confirmReplace()}
                        confirming={busy === "confirm"}
                    />
                );
            }
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

            {detail?.summary_md ? <MarkdownDetails title="拆解摘要" markdown={detail.summary_md} defaultOpen={task.state === "awaiting_assets" && !(detail.asset_cards || []).length} /> : null}

            {detail?.checklist_md ? <MarkdownDetails title="资产清单" markdown={detail.checklist_md} defaultOpen={task.state === "awaiting_assets" && !pairLines.length && !(detail.asset_cards || []).length} /> : null}

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

/**
 * B3 视觉卡片流（awaiting_assets + asset_cards 有货时）：逐卡置换台。
 * 图片是主载体：左侧原片帧（点开放大），右侧换图/沿用；长文字全部折叠。
 */
function AssetCardFlow({ taskId, cards, onWritten, onConfirm, confirming }: { taskId: string; cards: HubAssetCard[]; onWritten: () => void; onConfirm: () => void; confirming: boolean }) {
    const sorted = [...cards].sort((a, b) => a.n - b.n);
    const coreCards = sorted.filter((card) => card.核心);
    const minorCards = sorted.filter((card) => !card.核心);
    const changedCount = sorted.filter((card) => card.已传图.length).length;
    return (
        <div className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-sm font-medium">逐卡置换（看图操作）</span>
                <span className="text-xs text-stone-500 dark:text-stone-400">看原片帧 → 传我们的图；没传的按原片沿用</span>
            </div>
            {coreCards.length ? (
                <div className="space-y-3">
                    {coreCards.map((card) => (
                        <AssetReplaceCard key={card.n} card={card} taskId={taskId} onWritten={onWritten} />
                    ))}
                </div>
            ) : (
                <div className="rounded-md border border-dashed border-stone-300 px-3 py-3 text-sm text-stone-500 dark:border-stone-700 dark:text-stone-400">清单里没有标「核心」的置换位，可直接确认全部沿用。</div>
            )}
            {minorCards.length ? (
                <details className="rounded-md border border-stone-200 dark:border-stone-800">
                    <summary className="flex cursor-pointer list-none items-center justify-between gap-2 px-3 py-2 text-sm font-medium [&::-webkit-details-marker]:hidden">
                        <span>{minorCards.length} 项按原样沿用（展开查看/操作）</span>
                        <span className="text-xs font-normal text-stone-400 dark:text-stone-500">展开 / 收起</span>
                    </summary>
                    <div className="space-y-3 border-t border-stone-200 px-3 py-3 dark:border-stone-800">
                        {minorCards.map((card) => (
                            <AssetReplaceCard key={card.n} card={card} taskId={taskId} onWritten={onWritten} />
                        ))}
                    </div>
                </details>
            ) : null}
            <div className="flex flex-wrap items-center justify-between gap-2 border-t border-stone-100 pt-3 dark:border-stone-800">
                <span className="text-xs text-stone-500 dark:text-stone-400">已换 {changedCount} / {sorted.length} 项，其余按原片沿用。</span>
                <Button type="primary" icon={<CheckCircle2 className="size-4" />} loading={confirming} onClick={onConfirm}>
                    确认置换
                </Button>
            </div>
        </div>
    );
}

/** 建议角标：必须换=红、建议准备=蓝；「可沿用」不标（保持画面干净）。 */
function AdviceBadge({ advice }: { advice: string }) {
    if (advice === "必须换") return <Tag color="red" className="m-0">必须换</Tag>;
    if (advice === "建议准备") return <Tag color="blue" className="m-0">建议准备</Tag>;
    return null;
}

/**
 * 单张资产卡：左=原片帧（多帧可切换，点开放大全屏 lightbox），右=已传图/上传区。
 * 「换/沿用」二选：有图=换（重传覆盖）；「沿用」= POST clear:true 回显空。
 * 品牌视觉卡（类别含「品牌视觉」）：品牌文字输入为主，logo 图可选。
 */
function AssetReplaceCard({ card, taskId, onWritten }: { card: HubAssetCard; taskId: string; onWritten: () => void }) {
    const { message } = App.useApp();
    const fileInputRef = useRef<HTMLInputElement>(null);
    const dragDepthRef = useRef(0);
    const [frameIdx, setFrameIdx] = useState(0);
    const [failedUrls, setFailedUrls] = useState<string[]>([]);
    const [dragActive, setDragActive] = useState(false);
    const [busy, setBusy] = useState<null | "upload" | "clear" | "text">(null);
    const [brandText, setBrandText] = useState("");
    const [previewOpen, setPreviewOpen] = useState(false);
    const [previewIndex, setPreviewIndex] = useState(0);

    const brand = card.类别.includes("品牌视觉");
    const frames = card.帧;
    const safeIdx = frames.length ? Math.min(Math.max(frameIdx, 0), frames.length - 1) : 0;
    const frame = frames[safeIdx] || null;
    const okFrames = frames.filter((item) => item.url && !failedUrls.includes(item.url));
    const uploaded = card.已传图;

    const uploadOne = async (file: File) => {
        if (busy) return;
        setBusy("upload");
        try {
            const url = await uploadHubImage(file);
            await postHubAsset(taskId, { n: card.n, image_url: url });
            message.success(`#${card.n} 已换图`);
            onWritten();
        } catch (error) {
            message.error(`#${card.n} 换图失败：${errorText(error)}`);
        } finally {
            setBusy(null);
            if (fileInputRef.current) fileInputRef.current.value = "";
        }
    };

    const pickFile = (files: FileList | null) => {
        // 一卡一张（重传覆盖），多张拖入取第一张，避免「传了 3 张只显示 1 张」的困惑。
        const image = Array.from(files || []).find((item) => item.type.startsWith("image/"));
        if (!image) {
            message.warning("请选图片文件");
            return;
        }
        void uploadOne(image);
    };

    const clearCard = async () => {
        if (busy) return;
        setBusy("clear");
        try {
            await postHubAsset(taskId, { n: card.n, clear: true });
            message.success(`#${card.n} 已按原片沿用`);
            onWritten();
        } catch (error) {
            message.error(`#${card.n} 沿用失败：${errorText(error)}`);
        } finally {
            setBusy(null);
        }
    };

    const saveText = async () => {
        const text = brandText.trim();
        if (!text) {
            message.warning("先填品牌文字再保存");
            return;
        }
        if (busy) return;
        setBusy("text");
        try {
            await postHubAsset(taskId, { n: card.n, text });
            message.success(`#${card.n} 品牌文字已写入清单`);
            onWritten();
        } catch (error) {
            message.error(`#${card.n} 写入失败：${errorText(error)}`);
        } finally {
            setBusy(null);
        }
    };

    const dropzoneClass = `flex h-16 min-w-40 flex-1 cursor-pointer items-center justify-center gap-1.5 rounded-md border border-dashed px-2 text-center text-xs transition ${
        dragActive ? "border-stone-900 bg-stone-100/80 dark:border-stone-100 dark:bg-stone-900/80" : "border-stone-300 bg-stone-50/60 hover:border-stone-400 dark:border-stone-700 dark:bg-stone-900/40 dark:hover:border-stone-500"
    }`;

    return (
        <div className="rounded-lg border border-stone-200 p-3 dark:border-stone-800">
            <header className="flex flex-wrap items-center gap-2">
                <AdviceBadge advice={card.建议} />
                <span className="text-sm font-semibold">{card.短名 || `#${card.n}`}</span>
                <span className="font-mono text-xs text-stone-400 dark:text-stone-500">#{card.n}</span>
                {card.出现时段.length ? <span className="text-xs text-stone-400 dark:text-stone-500">出现 {card.出现时段.map((span) => `${span.起}–${span.止}`).join("、")}</span> : null}
            </header>
            {card.同实体疑似.length ? (
                <div className="mt-1 text-xs text-amber-600 dark:text-amber-400">与 {card.同实体疑似.map((n) => `#${n}`).join("、")} 疑似同一件东西，换图二选一即可</div>
            ) : null}

            <div className="mt-2 flex flex-wrap items-stretch gap-2">
                {/* 左：原片帧（点开放大；多帧左右切换 + 第 x/N 处）；lightbox 复用 antd 受控 PreviewGroup（同 pages/home 既有模式） */}
                <div className="w-56 shrink-0">
                    <AntdImage.PreviewGroup
                        preview={{
                            open: previewOpen,
                            current: previewIndex,
                            onOpenChange: setPreviewOpen,
                            onChange: setPreviewIndex,
                        }}
                    >
                        <div className="relative overflow-hidden rounded-md border border-stone-200 bg-stone-100 dark:border-stone-800 dark:bg-stone-900">
                        <div className="flex aspect-video items-center justify-center">
                            {frame && frame.url && !failedUrls.includes(frame.url) ? (
                                <AntdImage
                                    src={frame.url}
                                    alt={`#${card.n} 原片帧`}
                                    className="size-full object-contain"
                                    onClick={() => setPreviewIndex(0)}
                                    onError={(event) => {
                                        event.currentTarget.setAttribute("data-fallback", "1");
                                        setFailedUrls((prev) => (prev.includes(frame.url) ? prev : [...prev, frame.url]));
                                    }}
                                />
                            ) : (
                                <div className="flex size-full flex-col items-center justify-center gap-1 px-2 text-center text-xs text-stone-400 dark:text-stone-500">
                                    {frame?.url ? <img src={frame.url} data-fallback="1" alt="" className="hidden" /> : null}
                                    <ImageOff className="size-4" />
                                    <span>未定位（点看原片）</span>
                                </div>
                            )}
                        </div>
                        {frames.length > 1 ? (
                            <>
                                <button
                                    type="button"
                                    aria-label="上一处"
                                    onClick={() => setFrameIdx((safeIdx - 1 + frames.length) % frames.length)}
                                    className="absolute left-1 top-1/2 -translate-y-1/2 rounded-full bg-black/45 p-1 text-white transition hover:bg-black/70"
                                >
                                    <ChevronLeft className="size-4" />
                                </button>
                                <button
                                    type="button"
                                    aria-label="下一处"
                                    onClick={() => setFrameIdx((safeIdx + 1) % frames.length)}
                                    className="absolute right-1 top-1/2 -translate-y-1/2 rounded-full bg-black/45 p-1 text-white transition hover:bg-black/70"
                                >
                                    <ChevronRight className="size-4" />
                                </button>
                                <span className="absolute bottom-1 left-1 rounded bg-black/45 px-1.5 py-0.5 text-[11px] text-white">
                                    第 {safeIdx + 1}/{frames.length} 处{frame && frame.秒 > 0 ? ` · ${frame.秒}s` : ""}
                                </span>
                            </>
                        ) : null}
                        </div>
                        {/* 源帧组（视觉隐藏：当前帧已作为第一个 child 可见渲染，这里补齐其余帧供放大层左右切换） */}
                        <div style={{ position: "absolute", width: 0, height: 0, overflow: "hidden", opacity: 0, pointerEvents: "none" }}>
                            {okFrames.filter((item) => item.url !== frame?.url).map((item) => (
                                <AntdImage key={item.url} src={item.url} alt={`#${card.n} 原片帧`} />
                            ))}
                        </div>
                    </AntdImage.PreviewGroup>
                    <div className="mt-1 text-center text-[11px] text-stone-400 dark:text-stone-500">原片画面（点图放大）</div>
                </div>

                {uploaded.length ? <ArrowRight className="mx-0.5 size-5 shrink-0 self-center text-stone-300 dark:text-stone-600" /> : null}

                {/* 右：已传图缩略图（有图时并排） + 上传区 */}
                <div className="flex min-w-0 flex-1 flex-col justify-center gap-1.5">
                    {uploaded.length ? (
                        <>
                            <div className="flex flex-wrap items-stretch gap-2">
                                {uploaded.map((url) => (
                                    <a key={url} href={url} target="_blank" rel="noreferrer" className="block">
                                        <img src={url} alt={`#${card.n} 换图`} className="aspect-video w-56 rounded-md border border-emerald-300 bg-stone-100 object-contain dark:border-emerald-700 dark:bg-stone-900" />
                                    </a>
                                ))}
                            </div>
                            <div className="text-xs text-emerald-600 dark:text-emerald-400">原片画面 → 换成这个（点图看大图）</div>
                        </>
                    ) : null}
                    <div className="flex flex-wrap items-center gap-2">
                        <div
                            role="button"
                            tabIndex={0}
                            className={dropzoneClass}
                            onClick={() => fileInputRef.current?.click()}
                            onKeyDown={(event) => {
                                if (event.key === "Enter" || event.key === " ") {
                                    event.preventDefault();
                                    fileInputRef.current?.click();
                                }
                            }}
                            onDragEnter={(event) => {
                                event.preventDefault();
                                dragDepthRef.current += 1;
                                if (event.dataTransfer.types.includes("Files")) setDragActive(true);
                            }}
                            onDragOver={(event) => {
                                event.preventDefault();
                                event.dataTransfer.dropEffect = "copy";
                            }}
                            onDragLeave={(event) => {
                                event.preventDefault();
                                dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
                                if (!dragDepthRef.current) setDragActive(false);
                            }}
                            onDrop={(event) => {
                                event.preventDefault();
                                dragDepthRef.current = 0;
                                setDragActive(false);
                                pickFile(event.dataTransfer.files);
                            }}
                        >
                            {busy === "upload" ? (
                                <>
                                    <LoaderCircle className="size-4 animate-spin text-stone-400" />
                                    <span className="text-stone-500 dark:text-stone-400">上传中…</span>
                                </>
                            ) : (
                                <>
                                    <Upload className="size-4 shrink-0 text-stone-400" />
                                    <span className="text-stone-600 dark:text-stone-300">{uploaded.length ? "重传（覆盖）" : dragActive ? "松开上传" : "拖图到这里 / 点击传图"}</span>
                                </>
                            )}
                        </div>
                        {uploaded.length ? (
                            <Button size="small" loading={busy === "clear"} onClick={() => void clearCard()}>
                                沿用原片
                            </Button>
                        ) : null}
                    </div>
                </div>
            </div>

            {/* 品牌视觉卡：文字为主（写清单「命名」列），logo 图走同一上传通道（可选） */}
            {brand ? (
                <div className="mt-2 flex flex-wrap items-center gap-2">
                    <Type className="size-4 shrink-0 text-stone-400" />
                    <Input
                        size="small"
                        value={brandText}
                        placeholder="品牌文字（品牌名 / 品类名 / slogan）"
                        className="max-w-xs"
                        disabled={busy !== null}
                        onChange={(event) => setBrandText(event.target.value)}
                        onPressEnter={() => void saveText()}
                    />
                    <Button size="small" type="primary" ghost loading={busy === "text"} onClick={() => void saveText()}>
                        写入品牌文字
                    </Button>
                </div>
            ) : null}

            {card.描述 ? (
                <details className="mt-2">
                    <summary className="cursor-pointer list-none text-xs text-stone-400 transition hover:text-stone-600 dark:text-stone-500 dark:hover:text-stone-300 [&::-webkit-details-marker]:hidden">查看描述</summary>
                    <div className="thin-scrollbar mt-1 max-h-32 overflow-auto rounded bg-stone-50 px-2 py-1.5 text-xs leading-5 text-stone-600 dark:bg-stone-900 dark:text-stone-300">
                        {card.描述}
                        {card.时段来源 ? <div className="mt-1 text-[11px] text-stone-400 dark:text-stone-500">时段依据：{card.时段来源}</div> : null}
                    </div>
                </details>
            ) : null}

            <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                className="hidden"
                onChange={(event) => {
                    pickFile(event.target.files);
                    event.target.value = "";
                }}
            />
        </div>
    );
}

function formatCreatedAt(value: string) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}
