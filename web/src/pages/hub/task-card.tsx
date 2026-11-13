import { Activity, ArrowRight, CheckCircle2, ChevronLeft, ChevronRight, CircleAlert, CircleStop, Download, FileText, ImageOff, LoaderCircle, RefreshCw, Sparkles, TriangleAlert, Type, Upload, Video } from "lucide-react";
import { App, Button, Image as AntdImage, Input, Modal, Progress, Tag } from "antd";
import { useCallback, useEffect, useRef, useState, type DragEvent } from "react";
import { Streamdown } from "streamdown";

import { approveHubTask, confirmHubReplace, errorText, forceFinishHubTask, getHubTask, hubOutputUrl, postHubAsset, renderHubTask, retryHubTask, submitHubTask, uploadHubAssets, uploadHubImage, type HubActivity, type HubAssetCard, type HubFailedView, type HubHeartbeat, type HubLostRun, type HubPairLine, type HubRemoteWait, type HubRenderPreview, type HubTaskDetail, type HubTaskSummary } from "./hub-api";
import { forceFinishGuard } from "./hub-status";
import { TaskDeleteButton } from "./task-delete-confirm";
import { useHubMessage } from "./use-hub-message";

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

// ---------------------------------------------------------------- AT-18 运行态可观测 + 人工兜底口

/**
 * 强制结束的二次确认文案（冻结）。改这三句 = 改合同：它们同时是界面文案与探针断言点。
 *
 * 为什么必须写「不删任何东西 / 不杀进程」：这是一颗**看起来像破坏性操作、实际上只改状态**的按钮，
 * 用户按下之前得知道代价是什么（老任务产物还在、进程可能继续跑），否则会以为点了就丢掉两小时产物。
 */
export const FORCE_FINISH_CONFIRM_TITLE = "强制结束这个任务？";
export const FORCE_FINISH_CONFIRM_SCOPE = "只把状态置为「失败」：任务目录、拆解产物、参考视频全部保留，不删除任何文件，也不杀进程。";
export const FORCE_FINISH_CONFIRM_SIDE_EFFECT = "如果它其实还活着，进程会继续跑完，但状态仍是失败——之后可点「重试」续跑，或「删除」清掉。";
/**
 * 远程语义（所有者 2026-11-12 裁定，必须出现在终止动作的确认与回执里）：
 * 已经提交到远程的拆解**无法取消**，会继续跑到返回；结果按作废丢弃；再提交同一条任务
 * 必须等它返回。少这一句，用户就会以为「强制结束 = 远程也停了」。
 */
export const FORCE_FINISH_CONFIRM_REMOTE = "已经提交到远程（聚鑫）的拆解无法取消，会继续跑到返回；返回的结果按作废丢弃，不会覆盖这次失败。要再提交同一条任务，得等它返回后再点重试。";

/** 失败原因兜底（与既有文案一致，改它会让「没拿到原因」和「拿到了空原因」长得一样）。 */
export const FAILED_REASON_FALLBACK = "任务失败，错误原文未返回。";

/** 秒 → 人话时长。非有限数 → 「—」（不知道就说不知道，不编一个 0 秒出来）。 */
export function hubDurationText(seconds: number | null | undefined): string {
    if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "—";
    const total = Math.round(seconds);
    if (total < 60) return `${total} 秒`;
    if (total < 3600) return `${Math.floor(total / 60)} 分钟`;
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    return minutes ? `${hours} 小时 ${minutes} 分` : `${hours} 小时`;
}

/**
 * ISO → 本地「MM-DD HH:mm:ss」，**手写补零**而不是 `toLocaleString`。
 *
 * 为什么不用 toLocaleString：同一个 zh-CN 在 Node（ICU）与浏览器里格式不一致（实测 Node 出 `11/12 15:29:41`），
 * 探针与真机就会对不上同一句人话。心跳/静默这类「是不是还活着」的读数必须两边逐字一致。
 */
export function hubClockText(value: string): string {
    const text = (value || "").trim();
    if (!text) return "";
    const date = new Date(text);
    if (Number.isNaN(date.getTime())) return text;
    const pad = (n: number) => String(n).padStart(2, "0");
    return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/**
 * 运行态行的「最近活动」+「疑似失联」红字（AT-18 §4.6）。
 *
 * 数据源只有后端透出的三键：`activity`（六源取 max）/`heartbeat`（心跳侧车）/`lost_run`（判定）。
 * 三条纪律写在这里：
 *  1. `activity` 缺失 → 明说「活动时间不可用」，**不拿 created_at 或日志反推**（会假装活着）；
 *  2. 红字只认 `lost_run.suspect === true`，**绝不按 state 猜**——阈值判定只有后端有；
 *  3. 心跳/静默时间都给绝对值 + 人话，让用户自己判断「这条是不是还活着」。
 */
export function RunningActivityLine({ activity, heartbeat, lost_run }: { activity?: HubActivity | null; heartbeat?: HubHeartbeat | null; lost_run?: HubLostRun | null }) {
    const at = activity?.last_activity_at || heartbeat?.at || "";
    const silence = activity?.silence_seconds ?? heartbeat?.age_seconds ?? null;
    if (!at) {
        return (
            <div className="flex items-center gap-1.5 text-xs text-stone-400 dark:text-stone-500" data-hub-activity="missing">
                <Activity className="size-3.5 shrink-0" />
                活动时间不可用
            </div>
        );
    }
    const suspect = lost_run?.suspect === true;
    return (
        <div className="space-y-1.5">
            <div
                className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-stone-500 dark:text-stone-400"
                data-hub-activity="ok"
                data-hub-activity-source={activity?.source || ""}
                data-hub-activity-at={at}
                data-hub-silence-seconds={silence === null ? "" : String(silence)}
            >
                <Activity className="size-3.5 shrink-0" />
                <span>
                    最近活动 {hubClockText(at)}
                    {activity?.source ? <span className="text-stone-400 dark:text-stone-500">（{activity.source}）</span> : null}
                </span>
                <span data-hub-silence-text="1">已静默 {hubDurationText(silence)}</span>
                {heartbeat?.phase ? <span className="min-w-0 truncate text-stone-400 dark:text-stone-500">· {heartbeat.phase}</span> : null}
            </div>
            {suspect ? (
                <div
                    className="flex items-start gap-1.5 rounded-md border border-red-200 bg-red-50 px-2 py-1.5 text-xs text-red-700 dark:border-red-950 dark:bg-red-950/20 dark:text-red-300"
                    data-hub-lost-run="suspect"
                    data-hub-lost-silence={lost_run?.silence_seconds === null || lost_run?.silence_seconds === undefined ? "" : String(lost_run.silence_seconds)}
                >
                    <TriangleAlert className="mt-0.5 size-3.5 shrink-0" />
                    <span className="min-w-0 break-words">
                        疑似失联：静默 {hubDurationText(lost_run?.silence_seconds ?? silence)} 已超过阈值 {hubDurationText(lost_run?.threshold_seconds)}
                        {lost_run?.reason ? `（${lost_run.reason}）` : ""}。可点「强制结束」把它置为失败，再重试或删除。
                    </span>
                </div>
            ) : null}
        </div>
    );
}

/** 强制结束按钮：非运行态整颗不渲染（可见性判定在 hub-status.forceFinishGuard，唯一来源）。 */
export function ForceFinishButton({ task, busy = false, onAsk }: { task: HubTaskSummary; busy?: boolean; onAsk: () => void }) {
    const guard = forceFinishGuard(task);
    if (!guard.visible) return null;
    return (
        <Button
            danger
            ghost
            size="small"
            icon={<CircleStop className="size-3.5" />}
            loading={busy}
            data-hub-force-finish={task.local_id}
            title={guard.reason || "把这条卡住的任务置为失败（不删产物），失败后可重试或删除"}
            onClick={(event) => {
                event.stopPropagation();
                onAsk();
            }}
        >
            强制结束
        </Button>
    );
}

/** 二次确认弹层正文（正文独立成件：真机走查与探针都按这段文案核对代价说明）。 */
export function ForceFinishConfirmBody({ task }: { task: HubTaskSummary }) {
    return (
        <div className="space-y-2 text-xs" data-hub-force-confirm={task.local_id}>
            <div className="break-words text-stone-600 dark:text-stone-300">{FORCE_FINISH_CONFIRM_SCOPE}</div>
            <div className="break-words text-stone-600 dark:text-stone-300">{FORCE_FINISH_CONFIRM_SIDE_EFFECT}</div>
            <div className="break-words text-amber-700 dark:text-amber-300">{FORCE_FINISH_CONFIRM_REMOTE}</div>
            <div className="text-stone-500 dark:text-stone-400">
                任务号 <span className="font-mono">{task.local_id}</span> · 当前状态 {task.state || "未知"}
            </div>
        </div>
    );
}

/**
 * 远程半边（强制终止后）：本地已终止 ≠ 远程已停。
 *
 * 只渲染后端 `remote.message`——那句话（还在跑 / 预计还需 / 结果按作废丢弃）的唯一文案源是
 * automation/remote_wait.py，界面三处出口（强制结束回执、卡片、抽屉）说同一句，不自造第二套。
 * `running` 只决定配色（在跑=琥珀，已释放=灰），不参与措辞。
 */
export function RemoteWaitLine({ remote, compact = false }: { remote?: HubRemoteWait | null; compact?: boolean }) {
    if (!remote || !remote.message) return null;
    const running = Boolean(remote.running);
    const tone = running
        ? "border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900 dark:bg-amber-950/20 dark:text-amber-200"
        : "border-stone-200 bg-stone-50 text-stone-600 dark:border-stone-700 dark:bg-stone-800/40 dark:text-stone-300";
    return (
        <div
            className={`rounded-md border px-2.5 py-1.5 text-xs ${tone}`}
            data-hub-remote-wait={running ? "running" : "released"}
        >
            <div className="flex items-start gap-1.5">
                <CircleAlert className="mt-0.5 size-3.5 shrink-0" />
                <span className={`min-w-0 flex-1 break-words ${compact ? "line-clamp-3" : ""}`}>{remote.message}</span>
            </div>
        </div>
    );
}

/**
 * 失败原因（AT-18 §4.6 顺序写死）：`failed.reason`（后端唯一文案源）→ 既有 `error`（history 尾注）→ 兜底句。
 * 前端**不自造**失败原因：新任务的原因由后端 fail_orphan_task 落盘，老任务回落到原来的 error 字段。
 */
export function hubFailedReason(failed?: HubFailedView | null, fallbackError?: string): string {
    return (failed?.reason || "").trim() || (fallbackError || "").trim() || FAILED_REASON_FALLBACK;
}

/**
 * 列表行摘要（纯函数，探针直测）：后端原文**首行** + `cloud_raw` 徽标值。
 *
 * 为什么只取首行：后端 `discuss._failure_detail` 把「云端原文：{message}」摆在首行，后面
 * ［附属］/［转述］/［原始响应］是诊断附属（含 raw_response JSON）。表格行是横向比对用的，
 * 四行原文平铺会把单行撑到 673px（真机实测），所以行内只留首行 + 一个落盘徽标，
 * 完整原文交给详情页 `CloudFailurePanel`（读同一份 `failed.reason`，逐字不裁剪）。
 *
 * 红线：不解析错误语义、不改写文案、**不读 `evidence.cloud_error.message` 二次渲染**
 * （那正是「云端原文在同一行出现两次」的重复来源）；徽标只认 `cloud_raw` 的字面值，
 * 其它值不造占位。
 */
export type FailedRowSummary = { firstLine: string; cloudRaw: "on_disk" | "missing" | "" };

/**
 * `evidence.cloud_raw` 的字面值（后端 discuss.py 落的落盘标记，前端只认值不重判）。
 * 缺字段 / 坏值一律返回空串——不造占位、不推断「应该落盘了」。
 */
export function cloudRawOf(failed?: HubFailedView | null): "on_disk" | "missing" | "" {
    const raw = failed?.evidence && typeof failed.evidence === "object" ? failed.evidence.cloud_raw : "";
    return raw === "on_disk" ? "on_disk" : raw === "missing" ? "missing" : "";
}

export function failedRowSummary(failed?: HubFailedView | null, fallbackError = ""): FailedRowSummary {
    const firstLine = (hubFailedReason(failed, fallbackError).split("\n")[0] || "").trimEnd();
    return { firstLine, cloudRaw: cloudRawOf(failed) };
}

/**
 * 云端原文证据块（直渲 failed_evidence，冻结 schema 原样展示）：
 * · cloud_error.message 主行，error_type / finish_reason / attempts 有值才跟附属行；
 * · cloud_raw 给 on_disk/missing 徽标（discuss.py 落的标记，前端只认值不重判）。
 * 红线 §7.25：不解析 failed_reason 文案、不改 evidence 结构，只做直渲；
 * 字段缺失就不渲染对应行/徽标，不造假占位。
 */
function CloudErrorEvidence({ evidence }: { evidence: Record<string, unknown> }) {
    const cloud = evidence.cloud_error && typeof evidence.cloud_error === "object" && evidence.cloud_error !== null
        ? (evidence.cloud_error as Record<string, unknown>) : null;
    const cloudRaw = typeof evidence.cloud_raw === "string" ? evidence.cloud_raw : "";
    const message = cloud ? String(cloud.message ?? "").trim() : "";
    const meta: string[] = [];
    if (cloud) {
        const errorType = String(cloud.error_type ?? "").trim();
        const finish = cloud.finish_reason;
        if (errorType) meta.push(`错误类型 ${errorType}`);
        if (finish !== null && finish !== undefined && String(finish).trim()) meta.push(`finish_reason=${String(finish).trim()}`);
        if (typeof cloud.attempts === "number" && cloud.attempts > 0) meta.push(`尝试 ${cloud.attempts} 次`);
    }
    const onDisk = cloudRaw === "on_disk";
    const missing = cloudRaw === "missing";
    if (!message && !onDisk && !missing) return null;
    return (
        <div
            className="mt-2 rounded-md border border-red-200/70 bg-white/60 px-2.5 py-2 text-xs dark:border-red-900/60 dark:bg-red-950/30"
            data-hub-cloud-error={message ? "1" : ""}
            data-hub-cloud-raw={onDisk ? "on_disk" : missing ? "missing" : ""}
        >
            <div className="flex items-center justify-between gap-2">
                <span className="text-red-600/80 dark:text-red-400/80">云端原文</span>
                {onDisk || missing ? (
                    <span className={onDisk
                        ? "rounded border border-emerald-300 bg-emerald-50 px-1.5 py-0.5 font-mono text-emerald-700 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300"
                        : "rounded border border-stone-300 bg-stone-100 px-1.5 py-0.5 font-mono text-stone-500 dark:border-stone-600 dark:bg-stone-800/60 dark:text-stone-400"}>
                        {onDisk ? "on_disk 已落盘" : "missing 未落盘"}
                    </span>
                ) : null}
            </div>
            {message ? <div className="mt-1 min-w-0 whitespace-pre-wrap break-words font-medium text-red-700 dark:text-red-300">{message}</div> : null}
            {meta.length ? <div className="mt-1 break-words font-mono text-red-600/80 dark:text-red-400/80">{meta.join(" · ")}</div> : null}
        </div>
    );
}

/**
 * 失败原因行（两种密度）：
 * · 默认（详情抽屉 / `?legacy=1` 卡片流）：全文 + 云端证据块，原因不折叠；
 * · `compact`（默认页 `/hub` 表格行）：**只长首行**（视觉最多 2 行，`line-clamp-2`）+ 落盘徽标，
 *   不再叠 `CloudErrorEvidence` 的 message（同一句云端原文会在同一行出现两次），
 *   ［原始响应］JSON 也不进表格单元格——完整原文在详情页 `CloudFailurePanel`。
 */
export function FailedReasonLine({ failed, fallbackError = "", compact = false }: { failed?: HubFailedView | null; fallbackError?: string; compact?: boolean }) {
    const evidence = failed?.evidence || null;
    const hook = failed?.reason ? "backend" : fallbackError ? "history" : "empty";
    if (compact) {
        const { firstLine, cloudRaw } = failedRowSummary(failed, fallbackError);
        // 失败时间不进第二行（真机实测：多一行就顶到 120px 上限），改成悬停提示 + 详情页正文
        const tip = [failed?.at ? `失败时间 ${hubClockText(failed.at)}` : "", "完整原文见「打开」详情"].filter(Boolean).join(" · ");
        return (
            <div
                className="rounded-md border border-red-200 bg-red-50 px-2.5 py-0.5 dark:border-red-950 dark:bg-red-950/20"
                data-hub-failed-reason={hook}
                data-hub-failed-row="1"
                title={tip || undefined}
            >
                <div className="flex items-start gap-1.5 text-xs text-red-700 dark:text-red-300">
                    <CircleAlert className="mt-0.5 size-3.5 shrink-0" />
                    {firstLine ? (
                        <span className="line-clamp-2 min-w-0 flex-1 break-words leading-4" data-hub-failed-first-line>{firstLine}</span>
                    ) : null}
                    {cloudRaw ? (
                        <span
                            className={cloudRaw === "on_disk"
                                ? "shrink-0 rounded border border-emerald-300 bg-emerald-50 px-1 font-mono text-[10px] leading-4 text-emerald-700 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300"
                                : "shrink-0 rounded border border-stone-300 bg-stone-100 px-1 font-mono text-[10px] leading-4 text-stone-500 dark:border-stone-600 dark:bg-stone-800/60 dark:text-stone-400"}
                            data-hub-cloud-raw={cloudRaw}
                        >
                            {cloudRaw === "on_disk" ? "on_disk 已落盘" : "missing 未落盘"}
                        </span>
                    ) : null}
                </div>
            </div>
        );
    }
    return (
        <div className="rounded-md border border-red-200 bg-red-50 p-3 dark:border-red-950 dark:bg-red-950/20" data-hub-failed-reason={hook}>
            <div className="flex items-start gap-2 text-sm text-red-700 dark:text-red-300">
                <CircleAlert className="mt-0.5 size-4 shrink-0" />
                {/* whitespace-pre-wrap：failed_reason 是多行文本（云端原文主位+附属行），不许折成一长句 */}
                <div className="min-w-0 whitespace-pre-wrap break-words">{hubFailedReason(failed, fallbackError)}</div>
            </div>
            {evidence ? <CloudErrorEvidence evidence={evidence} /> : null}
            {failed?.at ? <div className="mt-1 pl-6 text-xs text-red-600/80 dark:text-red-400/80">失败时间 {hubClockText(failed.at)}</div> : null}
        </div>
    );
}

// ---------------------------------------------------------------- 云端原文失败详情（后端原文置顶）

/**
 * 附属信息行标记（逐字对齐后端 `discuss._failure_detail` 的固定行前缀）。
 *
 * 后端把「云端原文：{message}」摆首行，退出码 / finish_reason / 错误类型 / 原转述 / 原始响应
 * 逐行跟在后面并带这些标记。前端**只按标记挑样式**：不改字、不解析语义、不推断原因、不丢行
 * （逐字等式由 `splitFailedReason` 的探针锁定）。
 */
export const FAILED_REASON_AUX_MARKERS = ["［附属］", "［转述］", "［原始响应］"] as const;

/** 分段结果：main=主展示（云端原文行 / 无标记行），aux=附属信息（带标记的行，次要样式）。 */
export type FailedReasonSegment = { kind: "main" | "aux"; text: string };

/**
 * 把后端失败详情按行分段（纯函数，探针直测）。
 *
 * 铁律：`splitFailedReason(r).map(s => s.text).join("\n") === String(r ?? "")`——只换样式不换字：
 * 不 trim、不截断、不合并、不改写。未知前缀 / 后端新增行一律落在 main 里原样显示，所以加新字段
 * 不会被吞掉（只会以主样式出现），也不会让页面报错。
 */
export function splitFailedReason(reason?: string | null): FailedReasonSegment[] {
    const text = String(reason ?? "");
    if (!text) return [];
    return text.split("\n").map((line) => ({
        kind: FAILED_REASON_AUX_MARKERS.some((marker) => line.startsWith(marker)) ? "aux" : "main",
        text: line,
    }));
}

/**
 * 失败详情面板（任务详情页 failed 态的**唯一主展示件**）：云端原文等宽 + 可换行 + 全文不截断置顶，
 * 转述 / 退出码 / finish_reason / 原始响应降为附属次要样式。
 *
 * 只渲染后端给的东西：正文取 `failed.reason`（后端唯一文案源；老任务回落 history 尾注 `error`），
 * 不根据错误文本推断原因、不覆盖 / 改写后端文案、不丢未知行。「云端原文：未落盘（…）」这类文案
 * 同样原样展示（它没有附属标记，落在主展示区）。
 * `data-hub-cloud-failure` 是渲染钩子：backend=后端 failed.reason / history=老任务 error 尾注。
 *
 * 右上角 `on_disk/missing` 徽标取 `evidence.cloud_raw` 字面值（`cloudRawOf`）：这是详情页唯一的落盘
 * 徽标落点——原先由 FailedReasonLine 的证据块出，那一件在详情页已不再挂载（同一句云端原文会被
 * 渲染第二遍），徽标随之迁到这里，`data-hub-cloud-raw` 钩子不变。徽标只认 cloud_raw 字面值，
 * **不碰** evidence 里的云端 error 结构（正文已由 `failed.reason` 全量承载，再渲染一遍就是重复）。
 */
export function CloudFailurePanel({ failed, fallbackError = "" }: { failed?: HubFailedView | null; fallbackError?: string }) {
    const backend = (failed?.reason || "").trim();
    const history = (fallbackError || "").trim();
    const cloudRaw = cloudRawOf(failed);
    const segments = splitFailedReason(backend || history);
    if (!segments.length) return null; // 两处都空 → 交回既有兜底句（这里不造第二套文案）
    // 主展示：无标记行（按契约就是首行的云端原文；未落盘文案也在这里）
    const main = segments
        .filter((segment) => segment.kind === "main")
        .map((segment) => segment.text)
        .join("\n")
        .trim();
    const aux = segments.filter((segment) => segment.kind === "aux");
    return (
        <section
            className="rounded-lg border border-red-200 bg-red-50 p-3 dark:border-red-950 dark:bg-red-950/20"
            data-hub-cloud-failure={backend ? "backend" : "history"}
            data-hub-cloud-failure-aux={aux.length ? "1" : "0"}
        >
            <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-1.5 text-xs font-medium text-red-700 dark:text-red-300">
                    <CircleAlert className="size-3.5 shrink-0" />
                    失败详情（后端原文，原样展示）
                </div>
                {cloudRaw ? (
                    <span
                        className={cloudRaw === "on_disk"
                            ? "shrink-0 rounded border border-emerald-300 bg-emerald-50 px-1.5 py-0.5 font-mono text-[10px] leading-4 text-emerald-700 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300"
                            : "shrink-0 rounded border border-stone-300 bg-stone-100 px-1.5 py-0.5 font-mono text-[10px] leading-4 text-stone-500 dark:border-stone-600 dark:bg-stone-800/60 dark:text-stone-400"}
                        data-hub-cloud-raw={cloudRaw}
                    >
                        {cloudRaw === "on_disk" ? "on_disk 已落盘" : "missing 未落盘"}
                    </span>
                ) : null}
            </div>
            {main ? (
                <div
                    className="mt-1.5 min-w-0 whitespace-pre-wrap break-words font-mono text-[13px] leading-6 text-red-800 dark:text-red-200"
                    data-hub-cloud-failure-main
                >
                    {main}
                </div>
            ) : null}
            {aux.length ? (
                <div className="mt-2 space-y-1 border-t border-red-200/70 pt-2 dark:border-red-900/60" data-hub-cloud-failure-auxlist>
                    {aux.map((segment, index) => (
                        <div
                            key={`aux-${index}`}
                            className="min-w-0 whitespace-pre-wrap break-words font-mono text-[11px] leading-5 text-red-700/75 dark:text-red-300/75"
                            data-hub-cloud-failure-line={index}
                        >
                            {segment.text}
                        </div>
                    ))}
                </div>
            ) : null}
            {failed?.at ? <div className="mt-1.5 text-xs text-red-600/70 dark:text-red-400/70">失败时间 {hubClockText(failed.at)}</div> : null}
        </section>
    );
}

export function TaskCard({ task, onStateChange, onRefresh }: { task: HubTaskSummary; onStateChange: (localId: string, state: string) => void; onRefresh: () => void }) {
    const message = useHubMessage();
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
    // ---- AT-18：强制结束二次确认 + 行内错误（行内文案比一闪而过的 toast 活得久）----
    const [forceOpen, setForceOpen] = useState(false);
    const [forceBusy, setForceBusy] = useState(false);
    const [actionError, setActionError] = useState("");

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
        // 远程还在跑：按钮已禁用，这里再兜一层（真机双击/键盘回车也不会打出无意义的排队请求）
        if (detail?.remote?.running) {
            setActionError(`重试暂缓：${detail.remote.message}`);
            return;
        }
        setBusy("retry");
        setActionError("");
        try {
            const data = await retryHubTask(task.local_id);
            message.success(data.previous_reason ? "已触发重试（上一次失败原因留在任务历史里）" : "已触发重试");
        } catch (error) {
            // 缺陷 R5 根治：桥端原文透出（「重试需 failed 状态…」/「任务已有拆解任务在跑」），
            // 不再吞成一句「已刷新任务状态」——那正是「点了没反应」的来源。
            const text = errorText(error);
            setActionError(`重试失败：${text}`);
            message.error(`重试失败：${text}`);
        } finally {
            setBusy(null);
            onRefresh();
            void loadDetail();
        }
    };

    /**
     * AT-18 §4.2 强制结束：**只有用户在本卡的二次确认弹层里按下「确认强制结束」才会走到这里**。
     * 取消 = 只关弹层（`setForceOpen(false)`），零请求。
     * 成功后行内状态立即刷新为 failed（`onStateChange` + 拉详情），不删任何文件。
     */
    const forceFinish = async () => {
        if (forceBusy) return;
        setForceBusy(true);
        setActionError("");
        try {
            const data = await forceFinishHubTask(task.local_id);
            setForceOpen(false);
            onStateChange(task.local_id, data.state || "failed");
            // 回执三分：本地已终止 / 远程无法取消仍在跑（后端算好预计剩余）/ 结果返回后作废丢弃。
            // 后半句原样来自 data.remote.message（唯一文案源），前端不另写一套。
            const remoteNote = String(data.remote?.message || "");
            message.success(
                (data.already ? `任务 ${task.local_id} 已是失败状态` : `已强制结束：${task.local_id} 置为失败（产物全保留，可重试或删除）`)
                + (remoteNote ? `；${remoteNote}` : ""),
            );
            if (remoteNote) setActionError("");
            void loadDetail();
        } catch (error) {
            // 行内留痕：409「任务当前不是运行态…」/400「需二次确认…」都原样透出，不吞、不白屏
            const text = errorText(error);
            setForceOpen(false);
            setActionError(`强制结束失败：${text}`);
            message.error(`强制结束失败：${text}`);
        } finally {
            setForceBusy(false);
            onRefresh();
        }
    };

    const meta = STATE_META[task.state] || { label: task.state || "未知状态", dot: "bg-stone-400", pill: "bg-stone-100 text-stone-600 dark:bg-stone-800 dark:text-stone-300" };
    // 强制结束可见性（唯一判定在 hub-status.forceFinishGuard；本卡不另写状态枚举）
    const forceGuard = forceFinishGuard(task);
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
                <div className="space-y-2">
                    {/* AT-18 §4.6：原因只认后端 failed.reason，老任务回落既有 error，都没有才兜底 */}
                    <FailedReasonLine failed={task.failed || detail?.failed} fallbackError={detail?.error || ""} />
                    {/* 远程半边：已终止的 run 若远程还在跑，这里说清「无法取消、还需等多久、结果作废」 */}
                    <RemoteWaitLine remote={detail?.remote} compact />
                    <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="text-xs text-stone-500 dark:text-stone-400">重试=重新拆解（已有产物优先复用）；删除=写墓碑防任务号复用，不可恢复。</span>
                        <div className="flex items-center gap-1.5">
                            <Button
                                danger
                                size="small"
                                icon={<RefreshCw className="size-3.5" />}
                                loading={busy === "retry"}
                                disabled={Boolean(detail?.remote?.running)}
                                title={detail?.remote?.running ? "上一轮远程拆解仍在跑（无法取消），返回后拆解槽自动释放，再点重试" : undefined}
                                data-hub-retry={task.local_id}
                                onClick={() => void retry()}
                            >
                                {detail?.remote?.running ? "等远程返回后重试" : "重试"}
                            </Button>
                            {/* 删除走既有二键确认件（同承接、同墓碑说明），成功即刷新列表；拒绝原因同时进 toast 与行内 */}
                            <TaskDeleteButton task={task} size="small" onDeleted={() => onRefresh()} onError={(text) => setActionError(`删除失败：${text}`)} />
                        </div>
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
                {/* AT-18 §4.6 运行态尾部：最近活动（列表摘要优先，详情更新）+ 人工兜底口「强制结束」。
                    只有 discussing/rendering/generating 三种真实运行态才渲染（可见性判定在 hub-status.forceFinishGuard）。 */}
                {forceGuard.visible ? (
                    <div className="mt-2 space-y-1.5 border-t border-stone-100 pt-2 dark:border-stone-800" data-hub-running-footer={task.local_id}>
                        <RunningActivityLine activity={detail?.activity || task.activity} heartbeat={detail?.heartbeat || task.heartbeat} lost_run={detail?.lost_run || task.lost_run} />
                        <div className="flex justify-end">
                            <ForceFinishButton task={task} busy={forceBusy} onAsk={() => setForceOpen(true)} />
                        </div>
                    </div>
                ) : null}
                {/* 接口报错一律在行内留一句（不吞错、不白屏）；点右侧可主动关掉 */}
                {actionError ? (
                    <div className="mt-2 flex items-start gap-2 rounded-md border border-red-200 bg-red-50 px-2 py-1.5 text-xs text-red-700 dark:border-red-950 dark:bg-red-950/20 dark:text-red-300" data-hub-action-error={task.local_id}>
                        <CircleAlert className="mt-0.5 size-3.5 shrink-0" />
                        <span className="min-w-0 flex-1 break-words">{actionError}</span>
                        <button type="button" className="shrink-0 text-red-500 underline decoration-dotted dark:text-red-400" onClick={() => setActionError("")}>
                            知道了
                        </button>
                    </div>
                ) : null}
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

            {/* AT-18 §4.2 二次确认：取消只关弹层（零请求），确认才调 force-finish；成功即把行内状态刷成 failed */}
            <Modal
                open={forceOpen}
                title={FORCE_FINISH_CONFIRM_TITLE}
                okText="确认强制结束"
                cancelText="取消"
                okButtonProps={{ danger: true, "data-hub-force-confirm-ok": task.local_id }}
                cancelButtonProps={{ "data-hub-force-confirm-cancel": task.local_id }}
                confirmLoading={forceBusy}
                onOk={() => void forceFinish()}
                onCancel={() => setForceOpen(false)}
            >
                <ForceFinishConfirmBody task={task} />
            </Modal>
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
    const message = useHubMessage();
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
            const uploadedRef = await uploadHubImage(file, taskId);
            await postHubAsset(taskId, { n: card.n, image_url: uploadedRef.ref });
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
