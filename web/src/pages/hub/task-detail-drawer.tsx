import { ArrowRight, CheckCircle2, CircleAlert, Download, FileText, ImageOff, LoaderCircle, RefreshCw, Sparkles, Type, Upload } from "lucide-react";
import { App, Button, Checkbox, Drawer, Empty, Image as AntdImage, Input, Progress, Tag, Tooltip } from "antd";
import { useCallback, useEffect, useRef, useState, type DragEvent } from "react";
import { Streamdown } from "streamdown";

import { badgeFits, boxRect, containRect, frameCaption, frameState, layoutLegend, otherBoxCount, stageSize } from "@/lib/hub/frame-overlay";

import {
    approveHubTask,
    confirmHubReplace,
    errorText,
    getHubTask,
    groupHubRows,
    hubOutputUrl,
    postHubAsset,
    retryHubTask,
    rollbackHubTask,
    startLapianRun,
    submitHubTask,
    ungroupHubRows,
    uploadHubImage,
    type HubAssetCard,
    type HubSelfCheckItem,
    type HubTaskDetail,
    type HubTaskSummary,
} from "./hub-api";
import { deleteGuard, HubStateTag } from "./hub-status";

/** 九宫格粗标配色（同帧多目标分色，AT-9 层1）。 */
const OVERLAY_COLORS = [
    "rgba(244,63,94,0.30)",
    "rgba(59,130,246,0.30)",
    "rgba(16,185,129,0.30)",
    "rgba(245,158,11,0.30)",
    "rgba(139,92,246,0.30)",
    "rgba(6,182,212,0.30)",
    "rgba(236,72,153,0.30)",
    "rgba(132,204,22,0.30)",
];

const POST_DECOMPOSE_STATES = ["awaiting_assets", "rendering", "awaiting_approval", "approved", "generating", "done", "failed"];

type BusyKind = "upload" | "confirm" | "empty" | "approve" | "submit" | "retry" | "rollback" | "group" | "ungroup" | "discuss" | null;

/**
 * 任务详情全屏 Drawer（AT-9 §五）：头部 → Agent 建议 → 资产置换 → 提示词 → 审批/生成/成片。
 * 操作合同 §四：每状态只渲染该表列出的动作；确认置换后立即关 Drawer（渲染由后端推进，前端零执行责任）。
 * 轮询治理：详情轮询仅 Drawer 开启时；切任务/关 Drawer 丢弃过期响应（序号守卫）；失联显示错误+上次更新时间。
 */
export function TaskDetailDrawer({
    task,
    open,
    onClose,
    onStateChange,
    onRefresh,
    onRequestDelete,
}: {
    task: HubTaskSummary | null;
    open: boolean;
    onClose: () => void;
    onStateChange: (localId: string, state: string) => void;
    onRefresh: () => void;
    onRequestDelete: (task: HubTaskSummary) => void;
}) {
    const { message } = App.useApp();
    const [detail, setDetail] = useState<HubTaskDetail | null>(null);
    const [loadError, setLoadError] = useState("");
    const [lastUpdated, setLastUpdated] = useState<number | null>(null);
    const [busy, setBusy] = useState<BusyKind>(null);
    const seqRef = useRef(0);
    const reportedStateRef = useRef("");

    const localId = task?.local_id || "";

    const load = useCallback(async () => {
        if (!localId) return;
        const seq = ++seqRef.current;
        try {
            const next = await getHubTask(localId);
            if (seq !== seqRef.current) return; // 过期响应（切任务/已关闭）→ 丢弃
            setDetail(next);
            setLoadError("");
            setLastUpdated(Date.now());
            if (next.state && next.state !== reportedStateRef.current) {
                reportedStateRef.current = next.state;
                onStateChange(localId, next.state);
            }
        } catch (error) {
            if (seq !== seqRef.current) return;
            setLoadError(errorText(error));
        }
    }, [localId, onStateChange]);

    // 详情轮询仅 Drawer 开启时；关闭瞬间作废在途响应。
    useEffect(() => {
        if (!open || !localId) return;
        reportedStateRef.current = "";
        void load();
        const timer = window.setInterval(() => void load(), 5000);
        return () => {
            window.clearInterval(timer);
            seqRef.current += 1;
        };
    }, [open, localId, load]);

    // Drawer 已关闭 → 在途响应一律丢弃。
    useEffect(() => {
        if (!open) seqRef.current += 1;
    }, [open]);

    const state = detail?.state || task?.state || "";
    const summary: HubTaskSummary | null = detail
        ? { ...detail, task_name: detail.task_name || task?.task_name || detail.local_id }
        : task;

    const reload = useCallback(() => void load(), [load]);

    const act = useCallback(
        async (kind: Exclude<BusyKind, null>, run: () => Promise<void>, okText?: string) => {
            if (busy) return;
            setBusy(kind);
            try {
                await run();
                if (okText) message.success(okText);
                await load();
                onRefresh();
            } catch (error) {
                message.error(errorText(error));
            } finally {
                setBusy(null);
            }
        },
        [busy, load, message, onRefresh],
    );

    // 确认置换：成功 → 立即关 Drawer（渲染由后端推进），列表刷新接住新状态。
    const handleConfirmed = useCallback(() => {
        onClose();
        onRefresh();
    }, [onClose, onRefresh]);

    const titleName = summary?.task_name || summary?.local_id || "任务详情";

    return (
        <Drawer
            open={open}
            onClose={onClose}
            width="100%"
            rootClassName="hub-detail-drawer"
            title={
                <div className="flex min-w-0 flex-wrap items-center gap-2 pr-8">
                    <span className="truncate text-base font-semibold">{titleName}</span>
                    <HubStateTag state={state} />
                </div>
            }
        >
            {!summary ? (
                <div className="flex min-h-[240px] items-center justify-center">
                    <Empty description="任务不存在或已被删除" />
                </div>
            ) : (
                <div className="mx-auto flex w-full max-w-[1100px] flex-col gap-5 pb-16">
                    {/* 失联：错误 + 上次更新时间（§五轮询治理） */}
                    {loadError ? (
                        <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-700 dark:border-amber-950 dark:bg-amber-950/20 dark:text-amber-300">
                            <span className="min-w-0 break-words">详情刷新失败（显示的是上次数据）：{loadError}</span>
                            <span className="text-xs text-amber-600/80 dark:text-amber-300/80">{lastUpdated ? `上次更新 ${new Date(lastUpdated).toLocaleTimeString("zh-CN", { hour12: false })}` : "尚未成功拉取"}</span>
                        </div>
                    ) : null}

                    {/* ① 头部：任务名 / 状态 / 意图原文完整展示 */}
                    <section className="rounded-lg border border-stone-200 bg-card p-4 dark:border-stone-800">
                        <div className="flex flex-wrap items-start justify-between gap-3">
                            <div className="min-w-0 flex-1">
                                <div className="flex flex-wrap items-center gap-2">
                                    <span className="font-mono text-sm font-semibold">{summary.local_id}</span>
                                    <HubStateTag state={state} />
                                    <span className="text-xs text-stone-400 dark:text-stone-500">{summary.video_name || "参考视频"}</span>
                                    {summary.created_at ? <span className="text-xs text-stone-400 dark:text-stone-500">建于 {formatTime(summary.created_at)}</span> : null}
                                </div>
                                <div className="mt-2 whitespace-pre-wrap break-words text-sm leading-6 text-stone-700 dark:text-stone-200">
                                    <span className="mr-1 font-medium text-stone-500 dark:text-stone-400">改造意图：</span>
                                    {summary.intent || "（未填写意图）"}
                                </div>
                            </div>
                            <HeaderActions
                                summary={summary}
                                busy={busy}
                                onRequestDelete={onRequestDelete}
                            />
                        </div>
                    </section>

                    {/* ② Agent 建议区（拆解后才有） */}
                    {POST_DECOMPOSE_STATES.includes(state) && detail ? <SuggestionSection detail={detail} /> : null}

                    {/* ③ 资产置换区（仅 awaiting_assets，§四：传图/清空/全沿用/成组/解组/确认置换） */}
                    {state === "awaiting_assets" && detail ? (
                        <AssetSection
                            detail={detail}
                            busy={busy}
                            setBusy={setBusy}
                            onWritten={reload}
                            onConfirmed={handleConfirmed}
                        />
                    ) : null}

                    {/* 状态进度（ingesting/discussing/rendering：只读+进度；discussing 显 stage+log_tail） */}
                    {(state === "discussing" || state === "ingesting" || state === "rendering") && detail ? <ProgressSection detail={detail} state={state} /> : null}

                    {/* ingested：发起拆解（§四：ingested → 发起拆解(含意图) → discussing） */}
                    {state === "ingested" ? (
                        <section className="rounded-lg border border-stone-200 bg-card p-4 dark:border-stone-800">
                            <div className="flex flex-wrap items-center justify-between gap-3">
                                <span className="text-sm text-stone-500 dark:text-stone-400">任务已建立但还没开始拆解（可能触发失败）。</span>
                                <Button
                                    type="primary"
                                    icon={<Sparkles className="size-4" />}
                                    loading={busy === "discuss"}
                                    onClick={() =>
                                        void act(
                                            "discuss",
                                            async () => {
                                                await startLapianRun(summary.local_id, summary.intent || "");
                                            },
                                            "已开始拆解（1–3 分钟）",
                                        )
                                    }
                                >
                                    发起拆解
                                </Button>
                            </div>
                        </section>
                    ) : null}

                    {/* ④ 提示词段（全文默认折叠可展开 + 自检结论占位；B5 才有自检数据） */}
                    {detail && POST_DECOMPOSE_STATES.includes(state) ? <PromptSection detail={detail} /> : null}

                    {/* ⑤ 审批/生成/成片段（§四各状态动作） */}
                    {detail ? <OutcomeSection detail={detail} busy={busy} act={act} /> : null}

                    {!detail && !loadError ? (
                        <div className="flex min-h-[200px] flex-col items-center justify-center gap-2 text-sm text-stone-500 dark:text-stone-400">
                            <LoaderCircle className="size-5 animate-spin" />
                            正在读取任务详情…
                        </div>
                    ) : null}
                </div>
            )}
        </Drawer>
    );
}

/** 头部动作：删除入口（运行中禁用+tooltip；awaiting_* 走任务名确认弹窗，由父级承载）。 */
function HeaderActions({ summary, busy, onRequestDelete }: { summary: HubTaskSummary; busy: BusyKind; onRequestDelete: (task: HubTaskSummary) => void }) {
    const guard = deleteGuard(summary);
    const button = (
        <Button
            danger
            ghost
            disabled={guard.disabled || busy !== null}
            onClick={() => onRequestDelete(summary)}
        >
            删除任务
        </Button>
    );
    return guard.disabled ? (
        <Tooltip title={guard.reason || "运行中不可删"}>
            {button}
        </Tooltip>
    ) : (
        button
    );
}

/** Agent 建议区：必须换/建议准备主展示带理由与镜次；可沿用全折叠；待核对显式标「尚无建议·待核对」，禁归可沿用。 */
function SuggestionSection({ detail }: { detail: HubTaskDetail }) {
    const suggestions = detail.suggestions;
    const cards = detail.asset_cards;
    const shortNameOf = (n: number) => cards.find((card) => card.n === n)?.短名 || "";

    if (!suggestions.length) {
        return (
            <section className="rounded-lg border border-amber-200 bg-amber-50 p-4 dark:border-amber-950 dark:bg-amber-950/20">
                <h2 className="text-sm font-semibold">Agent 建议</h2>
                <div className="mt-1.5 text-sm text-amber-700 dark:text-amber-300">尚无建议·待核对（意图建议缺失或解析失败，各行不自动归「可沿用」）。</div>
            </section>
        );
    }

    const must = suggestions.filter((item) => !item.待核对 && item.档位 === "必须换");
    const prepare = suggestions.filter((item) => !item.待核对 && item.档位 === "建议准备");
    const keep = suggestions.filter((item) => !item.待核对 && item.档位 === "可沿用");
    const unverified = suggestions.filter((item) => item.待核对 || !item.档位);

    return (
        <section className="rounded-lg border border-stone-200 bg-card p-4 dark:border-stone-800">
            <h2 className="text-sm font-semibold">Agent 建议</h2>
            <div className="mt-1 text-xs text-stone-500 dark:text-stone-400">结合你填的改造意图逐行给出；「必须换 / 建议准备」优先处理，其余折叠。</div>

            <div className="mt-3 space-y-2">
                {must.map((item) => (
                    <SuggestionRow key={item.n} item={item} name={shortNameOf(item.n)} tone="must" />
                ))}
                {prepare.map((item) => (
                    <SuggestionRow key={item.n} item={item} name={shortNameOf(item.n)} tone="prepare" />
                ))}
                {unverified.map((item) => (
                    <SuggestionRow key={item.n} item={item} name={shortNameOf(item.n)} tone="unverified" />
                ))}
            </div>

            {keep.length ? (
                <details className="mt-3 rounded-md border border-stone-200 dark:border-stone-800">
                    <summary className="flex cursor-pointer list-none items-center justify-between gap-2 px-3 py-2 text-sm [&::-webkit-details-marker]:hidden">
                        <span className="text-stone-600 dark:text-stone-300">{keep.length} 项不需要动(展开看)</span>
                        <span className="text-xs text-stone-400 dark:text-stone-500">展开 / 收起</span>
                    </summary>
                    <div className="space-y-2 border-t border-stone-200 px-3 py-2.5 dark:border-stone-800">
                        {keep.map((item) => (
                            <SuggestionRow key={item.n} item={item} name={shortNameOf(item.n)} tone="keep" />
                        ))}
                    </div>
                </details>
            ) : null}
        </section>
    );
}

function SuggestionRow({ item, name, tone }: { item: HubTaskDetail["suggestions"][number]; name: string; tone: "must" | "prepare" | "keep" | "unverified" }) {
    const shots = item.镜次.length ? `第 ${item.镜次.join("、")} 镜` : "";
    const badge =
        tone === "must" ? (
            <Tag color="red" className="m-0">必须换</Tag>
        ) : tone === "prepare" ? (
            <Tag color="blue" className="m-0">建议准备</Tag>
        ) : tone === "unverified" ? (
            <Tag color="warning" className="m-0">尚无建议·待核对</Tag>
        ) : (
            <Tag className="m-0">可沿用</Tag>
        );
    return (
        <div className={`flex flex-wrap items-baseline gap-x-2 gap-y-0.5 rounded-md px-2 py-1.5 text-sm ${tone === "must" ? "bg-red-50/70 dark:bg-red-950/20" : tone === "prepare" ? "bg-blue-50/60 dark:bg-blue-950/20" : tone === "unverified" ? "bg-amber-50/70 dark:bg-amber-950/20" : ""}`}>
            {badge}
            <span className="font-mono text-xs text-stone-400 dark:text-stone-500">#{item.n}</span>
            {name ? <span className="font-medium">{name}</span> : null}
            {shots ? <span className="text-xs text-stone-400 dark:text-stone-500">{shots}</span> : null}
            {item.理由 ? <span className="min-w-0 flex-basis-full break-words text-xs text-stone-500 dark:text-stone-400">理由：{item.理由}</span> : null}
        </div>
    );
}

/** 拆解/入库进度（只读）：stage + log_tail 尾部（拆解进度必须界面可见）。 */
function ProgressSection({ detail, state }: { detail: HubTaskDetail; state: string }) {
    const stage = detail.lapian_stage || detail.stage || "";
    const logLines = (detail.log_tail || "").split("\n").filter(Boolean);
    return (
        <section className="rounded-lg border border-stone-200 bg-card p-4 dark:border-stone-800">
            <div className="flex flex-wrap items-center gap-2 text-sm">
                <LoaderCircle className="size-4 animate-spin text-stone-400" />
                <span className="font-medium">{state === "rendering" ? "正在渲染提示词（由后台推进，几秒完成）" : state === "ingesting" ? "参考片入库中…" : "拆解中（通常 1–3 分钟），完成后自动进入待置换"}</span>
                {stage ? <Tag color="blue" className="m-0">{stage}</Tag> : null}
            </div>
            {logLines.length ? (
                <pre className="thin-scrollbar mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded-md bg-stone-50 px-3 py-2 text-xs leading-5 text-stone-600 dark:bg-stone-900 dark:text-stone-300">
                    {logLines.slice(-10).join("\n")}
                </pre>
            ) : null}
        </section>
    );
}

// ---------------------------------------------------------------- 资产置换区（awaiting_assets）

/**
 * 资产置换区（§六层1层2层3）：确认组一卡联动、疑似组只提示不联动、九宫格粗标 overlay、
 * 传图/清空并排、必换未换锁点清单 + 全沿用（empty_confirm）。
 */
function AssetSection({
    detail,
    busy,
    setBusy,
    onWritten,
    onConfirmed,
}: {
    detail: HubTaskDetail;
    busy: BusyKind;
    setBusy: (kind: BusyKind) => void;
    onWritten: () => void;
    onConfirmed: () => void;
}) {
    const { message, modal } = App.useApp();
    const taskId = detail.local_id;
    const cards = [...detail.asset_cards].sort((a, b) => a.n - b.n);
    // 影响镜次（AT-10 C05 信息下沉）：意图建议的镜次，纯透出，不新增识别调用。
    const shotMap = new Map<number, number[]>(detail.suggestions.map((item) => [item.n, [...item.镜次].sort((a, b) => a - b)]));
    const shotsOf = (rows: number[]) => [...new Set(rows.flatMap((n) => shotMap.get(n) || []))].sort((a, b) => a - b);

    // 确认组：同实体确认 ≥2 行的成员归到一组；一卡展示，一传全组联动（后端单点写）。
    const groupMap = new Map<string, HubAssetCard[]>();
    for (const card of cards) {
        if (card.同实体确认.length >= 2) {
            const key = [...card.同实体确认].sort((a, b) => a - b).join("-");
            const bucket = groupMap.get(key) || [];
            bucket.push(card);
            groupMap.set(key, bucket);
        }
    }
    const groupedNs = new Set<number>();
    for (const bucket of groupMap.values()) for (const card of bucket) groupedNs.add(card.n);
    const groups = [...groupMap.values()].map((bucket) => [...bucket].sort((a, b) => a.n - b.n));
    const singles = cards.filter((card) => !groupedNs.has(card.n));

    const mustCards = cards.filter((card) => card.建议 === "必须换");
    const unfilledMust = mustCards.filter((card) => !card.已传图.length);
    const [acked, setAcked] = useState<Set<number>>(new Set());
    const toggleAck = (n: number, on: boolean) =>
        setAcked((prev) => {
            const next = new Set(prev);
            if (on) next.add(n);
            else next.delete(n);
            return next;
        });

    const anyUploaded = cards.some((card) => card.已传图.length) || Boolean(detail.has_pending_replace);
    const lockOk = unfilledMust.every((card) => acked.has(card.n));

    const confirmReplace = async () => {
        if (busy) return;
        // 全部行都没传图 = 空置换，后端硬拒 → 自动走 empty_confirm（合同放行通道）。
        const emptyConfirm = !anyUploaded;
        setBusy("confirm");
        try {
            const data = await confirmHubReplace(taskId, emptyConfirm, [...acked].sort((a, b) => a - b));
            message.success(data.全沿用 ? "已按全沿用确认，后台推进渲染" : "置换已确认，后台推进渲染；可关页等待");
            onConfirmed();
        } catch (error) {
            message.error(`确认置换失败：${errorText(error)}`);
        } finally {
            setBusy(null);
        }
    };

    const emptyConfirmAll = () => {
        modal.confirm({
            title: "全部按原片沿用？",
            content: `${cards.length} 项资产全部按原片沿用，不换任何图；确认后直接进入渲染（提示词由后台生成）。`,
            okText: "确认全沿用",
            onOk: async () => {
                try {
                    await confirmHubReplace(taskId, true);
                    message.success("已确认全沿用，后台推进渲染");
                    onConfirmed();
                } catch (error) {
                    message.error(`全沿用确认失败：${errorText(error)}`);
                }
            },
        });
    };

    const doGroup = async (rows: number[]) => {
        if (busy) return;
        setBusy("group");
        try {
            await groupHubRows(taskId, rows);
            message.success(`已成组：#${rows.join("、#")}（此后一传全组联动）`);
            onWritten();
        } catch (error) {
            message.error(`成组失败：${errorText(error)}`);
        } finally {
            setBusy(null);
        }
    };

    const doUngroup = async (rows: number[]) => {
        if (busy) return;
        setBusy("ungroup");
        try {
            await ungroupHubRows(taskId, rows);
            message.success(`已解组：#${rows.join("、#")}`);
            onWritten();
        } catch (error) {
            message.error(`解组失败：${errorText(error)}`);
        } finally {
            setBusy(null);
        }
    };

    return (
        <section className="rounded-lg border border-stone-200 bg-card p-4 dark:border-stone-800">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
                <h2 className="text-sm font-semibold">资产置换</h2>
                <span className="text-xs text-stone-500 dark:text-stone-400">看原片帧 → 传我们的图（换图走图床公网直链）；没传的按原片沿用</span>
            </div>

            {/* 确认组：同一件东西·共N行（一卡展示，一传全组联动） */}
            {groups.map((bucket, index) => (
                <GroupCard
                    key={`group-${bucket.map((c) => c.n).join("-")}`}
                    bucket={bucket}
                    color={OVERLAY_COLORS[index % OVERLAY_COLORS.length]}
                    shots={shotsOf(bucket.map((card) => card.n))}
                    busy={busy}
                    setBusy={setBusy}
                    taskId={taskId}
                    onWritten={onWritten}
                    onUngroup={() => void doUngroup(bucket.map((card) => card.n))}
                />
            ))}

            {/* 非组卡：逐卡置换 + 疑似组「可能是同一件？[成组]」（只提示不联动） */}
            <div className="mt-3 space-y-3">
                {singles.length ? null : <div className="text-xs text-stone-400 dark:text-stone-500">所有行都已确认成组。</div>}
                {singles.map((card, index) => (
                    <AssetCardUnit
                        key={card.n}
                        card={card}
                        color={OVERLAY_COLORS[(groups.length + index) % OVERLAY_COLORS.length]}
                        shots={shotsOf([card.n])}
                        busy={busy}
                        setBusy={setBusy}
                        taskId={taskId}
                        onWritten={onWritten}
                        onGroup={() => void doGroup([card.n, ...card.同实体疑似])}
                    />
                ))}
            </div>

            {/* 锁点清单 + 确认动作（§六层3） */}
            <div className="mt-4 space-y-2 border-t border-stone-100 pt-3 dark:border-stone-800">
                {mustCards.length ? (
                    <div className={`text-sm ${unfilledMust.length ? "text-amber-700 dark:text-amber-300" : "text-stone-500 dark:text-stone-400"}`}>
                        必换 {mustCards.length} 行：已换 {mustCards.length - unfilledMust.length}·未换 {unfilledMust.length}
                        {unfilledMust.length ? "（未换行将按原片沿用，逐行勾选确认）" : ""}
                    </div>
                ) : (
                    <div className="text-sm text-stone-500 dark:text-stone-400">清单里没有标「必须换」的行；不传图即全部按原片沿用。</div>
                )}
                {unfilledMust.length ? (
                    <div className="flex flex-col gap-1.5">
                        {unfilledMust.map((card) => (
                            <Checkbox key={card.n} checked={acked.has(card.n)} onChange={(event) => toggleAck(card.n, event.target.checked)}>
                                <span className="text-sm">
                                    <span className="font-mono text-xs text-stone-400">#{card.n}</span> {card.短名 || card.描述.slice(0, 24)} — 知道，按原样
                                </span>
                            </Checkbox>
                        ))}
                    </div>
                ) : null}

                <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
                    <Tooltip title={lockOk ? "" : "必换行还有未换也未勾「按原样」的，先处理再确认"}>
                        <Button
                            type="primary"
                            icon={<CheckCircle2 className="size-4" />}
                            loading={busy === "confirm"}
                            disabled={!lockOk || busy !== null}
                            onClick={() => void confirmReplace()}
                        >
                            确认置换
                        </Button>
                    </Tooltip>
                    <Button loading={busy === "empty"} disabled={busy !== null} onClick={emptyConfirmAll}>
                        全部按原样沿用
                    </Button>
                </div>
                <div className="text-xs text-stone-400 dark:text-stone-500">确认后由后台推进渲染（前端不再触发渲染调用），可关闭本页等待。</div>
            </div>
        </section>
    );
}

/** 确认组一卡通：『同一件东西·共N行』；代表行上传 → 后端单点整组联动。 */
function GroupCard({
    bucket,
    color,
    shots,
    busy,
    setBusy,
    taskId,
    onWritten,
    onUngroup,
}: {
    bucket: HubAssetCard[];
    color: string;
    shots: number[];
    busy: BusyKind;
    setBusy: (kind: BusyKind) => void;
    taskId: string;
    onWritten: () => void;
    onUngroup: () => void;
}) {
    const representative = bucket[0];
    const uploadedAll = [...new Set(bucket.flatMap((card) => card.已传图))];
    return (
        <div
            className="mt-3 rounded-lg border-2 border-dashed border-emerald-300 bg-emerald-50/40 p-3 dark:border-emerald-700 dark:bg-emerald-950/20"
            style={{ borderLeft: `4px solid ${color.replace(/0\.30\)$/, "0.85)")}` }}
        >
            <div className="flex flex-wrap items-center gap-2">
                <Tag color="success" className="m-0">同一件东西·共{bucket.length}行</Tag>
                <span className="text-sm font-semibold">{representative.短名 || `#${representative.n}`}</span>
                {representative.建议 === "必须换" ? <Tag color="red" className="m-0">必须换</Tag> : null}
                <span className="font-mono text-xs text-stone-400 dark:text-stone-500">#{bucket.map((card) => card.n).join("、#")}</span>
                {shots.length ? <span className="text-xs text-stone-400 dark:text-stone-500">影响镜次：第{shots.join("、")}镜</span> : null}
                <Button size="small" className="ml-auto" loading={busy === "ungroup"} disabled={busy !== null} onClick={onUngroup}>
                    解组
                </Button>
            </div>
            <div className="mt-1 text-xs text-emerald-700 dark:text-emerald-400">组内一行换图 = 全组生效（清单、流水、提交次序三处同步，由后端单点完成）。</div>
            <div className="mt-2 flex flex-wrap items-stretch gap-2">
                <FrameWithPosition card={representative} shots={shots} className="shrink-0" />

                {uploadedAll.length ? <ArrowRight className="mx-0.5 size-5 shrink-0 self-center text-stone-300 dark:text-stone-600" /> : null}
                <AssetUploadZone
                    taskId={taskId}
                    n={representative.n}
                    uploaded={uploadedAll}
                    busy={busy}
                    setBusy={setBusy}
                    onWritten={onWritten}
                    hint={`传一张，#${bucket.map((card) => card.n).join("、#")} 全组生效`}
                />
            </div>
            {/* 组内逐行回显（真源=清单，刷新后与组内其他行一致） */}
            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-stone-500 dark:text-stone-400">
                {bucket.map((card) => (
                    <span key={card.n}>
                        #{card.n} {card.短名}：{card.已传图.length ? `已换 ${card.已传图.length} 图` : "未换（随组联动）"}
                    </span>
                ))}
            </div>
        </div>
    );
}

/** 单卡置换单元：帧+overlay、传图/清空并排、疑似组提示按钮、品牌文字（品牌视觉卡）。 */
function AssetCardUnit({
    card,
    color,
    shots,
    busy,
    setBusy,
    taskId,
    onWritten,
    onGroup,
}: {
    card: HubAssetCard;
    color: string;
    shots: number[];
    busy: BusyKind;
    setBusy: (kind: BusyKind) => void;
    taskId: string;
    onWritten: () => void;
    onGroup: () => void;
}) {
    const { message } = App.useApp();
    const [brandText, setBrandText] = useState("");
    const brand = card.类别.includes("品牌视觉");

    const saveText = async () => {
        const text = brandText.trim();
        if (!text || busy) return;
        setBusy("upload");
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

    return (
        <div
            className="rounded-lg border border-stone-200 p-3 dark:border-stone-800"
            style={{ borderLeft: `4px solid ${color.replace(/0\.30\)$/, "0.85)")}` }}
        >
            <header className="flex flex-wrap items-center gap-2">
                {card.建议 === "必须换" ? <Tag color="red" className="m-0">必须换</Tag> : card.建议 === "建议准备" ? <Tag color="blue" className="m-0">建议准备</Tag> : null}
                <span className="text-sm font-semibold">{card.短名 || `#${card.n}`}</span>
                <span className="font-mono text-xs text-stone-400 dark:text-stone-500">#{card.n}</span>
                <span className="text-xs text-stone-400 dark:text-stone-500">{card.类别}</span>
                {card.已传图.length ? <Tag color="success" className="m-0">已换图</Tag> : <Tag className="m-0">未换·沿用原片</Tag>}
                {card.同实体确认.length >= 2 ? (
                    <Tag color="cyan" className="m-0">确认组 #{card.同实体确认.join("、#")}</Tag>
                ) : null}
                {shots.length ? <span className="text-xs text-stone-400 dark:text-stone-500">影响镜次：第{shots.join("、")}镜</span> : null}
            </header>

            <div className="mt-2 flex flex-wrap items-stretch gap-2">
                <FrameWithPosition card={card} shots={shots} className="shrink-0" />
                {card.已传图.length ? <ArrowRight className="mx-0.5 size-5 shrink-0 self-center text-stone-300 dark:text-stone-600" /> : null}
                <AssetUploadZone taskId={taskId} n={card.n} uploaded={card.已传图} busy={busy} setBusy={setBusy} onWritten={onWritten} />
            </div>

            {/* 疑似组：只提示 + 一键成组，绝不自动联动（#3↔#5 误连实证反例） */}
            {card.同实体疑似.length ? (
                <div className="mt-2 flex flex-wrap items-center gap-2 rounded-md bg-amber-50/70 px-2 py-1.5 text-xs text-amber-700 dark:bg-amber-950/20 dark:text-amber-300">
                    <span>可能是同一件？与 #{card.同实体疑似.join("、#")} 疑似同实体（现不联动，各自独立换图）</span>
                    <Button size="small" loading={busy === "group"} disabled={busy !== null} onClick={onGroup}>
                        成组
                    </Button>
                </div>
            ) : null}

            {brand ? (
                <div className="mt-2 flex flex-wrap items-center gap-2">
                    <Type className="size-4 shrink-0 text-stone-400" />
                    <Input size="small" value={brandText} placeholder="品牌文字（品牌名 / 品类名 / slogan）" className="max-w-xs" disabled={busy !== null} onChange={(event) => setBrandText(event.target.value)} onPressEnter={() => void saveText()} />
                    <Button size="small" type="primary" ghost loading={busy === "upload"} onClick={() => void saveText()}>
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
        </div>
    );
}

/** 角标（建议列原文）配色：与 AT-8 卡片角标同源，禁新增识别调用。 */
const BOX_ACCENT: Record<string, string> = {
    必须换: "rgb(225,29,72)",
    建议准备: "rgb(37,99,235)",
    可沿用: "rgb(120,113,108)",
};
const accentOf = (label: string) => BOX_ACCENT[label] || BOX_ACCENT["可沿用"];

/**
 * 共享帧 + 同帧多红框（AT-10 C3）：overlay 与图片共用 contain 后**实际内容盒**（不是外框），
 * 九宫格百分比直接落内容盒；全部框保留红色边界，本卡加粗实红、其他行红半透明；
 * 图例 rail 逐行号排布（行号·短名·角标），引线汇聚到各自框——同格多框也逐行号可见可分。
 * 三态诚实：未定位→不画框不标镜次+「未能定位到该资产出现的镜头·暂用整片画面」；
 * 无自身位置→帧与镜次照给、他人框照画、描述首句+「未能标注位置」。旧字段（帧[]/画面位置）仍驱动 legacy 分支。
 */
function FrameWithPosition({ card, className = "", shots = [] }: { card: HubAssetCard; className?: string; shots?: number[] }) {
    const state = frameState(card);
    const legacyUrl = card.帧.map((frame) => (frame.url || "").trim()).find(Boolean) || "";
    const url = (card.代表帧.url || "").trim() || legacyUrl;
    const [ratio, setRatio] = useState<number | null>(null);
    const [hoverRow, setHoverRow] = useState<number | null>(null);

    const stage = stageSize(ratio);
    const content = containRect(ratio, stage);
    // 未定位态：不画任何框（合同：帧内框=[] 时也不画）；legacy 只画旧字段那一格。
    const boxes = state === "precise" || state === "no-own-box" ? card.帧内框 : [];
    const laid = boxes
        .map((box) => ({ box, rect: boxRect(box, content) }))
        .sort((a, b) => Number(a.box.本卡) - Number(b.box.本卡));
    const legend = layoutLegend(laid.map(({ box, rect }) => ({ 行: box.行, box: rect })), stage);

    const legacyPosition = state === "legacy" ? card.画面位置 : null;
    const legacyRect =
        legacyPosition && legacyPosition.格 >= 1 && legacyPosition.格 <= 9
            ? boxRect(
                  {
                      "x%": ((legacyPosition.格 - 1) % 3) * (100 / 3),
                      "y%": Math.floor((legacyPosition.格 - 1) / 3) * (100 / 3),
                      "w%": 100 / 3,
                      "h%": 100 / 3,
                  },
                  content,
              )
            : null;

    const caption = frameCaption(card, state);
    const firstDesc = firstSentence(card.描述);
    const others = otherBoxCount(laid.map(({ box }) => box));
    const shotText = shots.length ? `第${shots.join("、")}镜` : "";
    const wrapperW = legend.chips.length ? legend.x + legend.w : stage.w;
    const wrapperH = Math.max(stage.h, legend.height);

    return (
        <div className={className} data-hub-frame-state={state}>
            <div className="relative flex items-start" style={{ width: wrapperW, height: wrapperH }}>
                {/* 舞台：尺寸按帧真实宽高比定（竖屏窄、横屏宽）；overflow 只裁图片圆角，标签全在舞台之外 */}
                <div
                    className="relative shrink-0 overflow-hidden rounded-md border border-stone-200 bg-stone-100 dark:border-stone-800 dark:bg-stone-900"
                    style={{ width: stage.w, height: stage.h }}
                    data-hub-frame-stage
                    data-hub-frame-state={state}
                >
                    {url ? (
                        <AntdImage
                            src={url}
                            width={stage.w}
                            height={stage.h}
                            style={{ objectFit: "contain", display: "block" }}
                            alt={`#${card.n} 原片帧`}
                            onLoad={(event) => {
                                const image = event.currentTarget;
                                if (image.naturalWidth > 0 && image.naturalHeight > 0) setRatio(image.naturalWidth / image.naturalHeight);
                            }}
                        />
                    ) : (
                        <div className="flex size-full flex-col items-center justify-center gap-1 px-2 text-center text-xs text-stone-400 dark:text-stone-500">
                            <ImageOff className="size-4" />
                            <span>未定位（无可用帧）</span>
                        </div>
                    )}

                    {/* 内容盒：与 contain 后图片实际内容区重合，框百分比只落在它里面 */}
                    {url ? (
                        <div
                            className="pointer-events-none absolute"
                            style={{ left: content.x, top: content.y, width: content.w, height: content.h }}
                            data-hub-frame-content-box
                        >
                            {/* 九宫格参考线（极淡，帮助读格） */}
                            <div aria-hidden className="absolute inset-0">
                                <div className="absolute inset-y-0 left-1/3 w-px bg-white/25" />
                                <div className="absolute inset-y-0 left-2/3 w-px bg-white/25" />
                                <div className="absolute inset-x-0 top-1/3 h-px bg-white/25" />
                                <div className="absolute inset-x-0 top-2/3 h-px bg-white/25" />
                            </div>

                            {laid.map(({ box, rect }) => {
                                const active = hoverRow === box.行;
                                return (
                                    <div
                                        key={box.行}
                                        data-hub-box-row={box.行}
                                        data-hub-box-own={box.本卡 ? "1" : "0"}
                                        className="absolute rounded-[2px]"
                                        style={{
                                            left: rect.x,
                                            top: rect.y,
                                            width: rect.w,
                                            height: rect.h,
                                            border: box.本卡 ? "2px solid rgb(225,29,72)" : "1px solid rgba(244,63,94,0.45)",
                                            backgroundColor: box.本卡 ? "rgba(244,63,94,0.14)" : "rgba(244,63,94,0.06)",
                                            outline: active ? "2px solid rgba(225,29,72,0.6)" : undefined,
                                            opacity: active || box.本卡 ? 1 : 0.8,
                                            zIndex: active ? 30 : box.本卡 ? 20 : 10,
                                        }}
                                    >
                                        {badgeFits(rect) ? (
                                            <span
                                                className="absolute left-0.5 top-0.5 rounded px-1 text-[10px] leading-4"
                                                style={
                                                    box.本卡
                                                        ? { backgroundColor: "rgb(225,29,72)", color: "#fff" }
                                                        : { backgroundColor: "rgba(255,255,255,0.9)", color: "rgb(190,18,60)" }
                                                }
                                            >
                                                #{box.行}
                                            </span>
                                        ) : null}
                                    </div>
                                );
                            })}

                            {legacyRect ? (
                                <div
                                    aria-hidden
                                    className="absolute border-2"
                                    style={{
                                        left: legacyRect.x,
                                        top: legacyRect.y,
                                        width: legacyRect.w,
                                        height: legacyRect.h,
                                        borderColor: "rgba(244,63,94,0.85)",
                                        backgroundColor: "rgba(244,63,94,0.30)",
                                    }}
                                />
                            ) : null}
                        </div>
                    ) : null}
                </div>

                {/* 图例 rail + 引线：同一格的多行各占一条，不按格去重身份 */}
                {legend.chips.length ? (
                    <>
                        <svg className="pointer-events-none absolute inset-0" width={wrapperW} height={wrapperH} aria-hidden data-hub-frame-links>
                            {legend.links.map((link) => (
                                <line
                                    key={link.行}
                                    x1={link.x1}
                                    y1={link.y1}
                                    x2={link.x2}
                                    y2={link.y2}
                                    stroke="rgba(225,29,72,0.45)"
                                    strokeWidth={1}
                                    strokeDasharray="3 2"
                                />
                            ))}
                        </svg>
                        <ul className="absolute flex flex-col gap-1" style={{ left: legend.x, top: 0, width: legend.w }} data-hub-frame-legend>
                            {legend.chips.map((chip) => {
                                const item = laid.find(({ box }) => box.行 === chip.行);
                                if (!item) return null;
                                const { box } = item;
                                return (
                                    <li
                                        key={chip.行}
                                        data-hub-box-row={chip.行}
                                        data-hub-box-own={box.本卡 ? "1" : "0"}
                                        onMouseEnter={() => setHoverRow(box.行)}
                                        onMouseLeave={() => setHoverRow(null)}
                                        className={`flex cursor-default flex-col justify-center overflow-hidden rounded border px-1 leading-tight ${
                                            box.本卡
                                                ? "border-rose-500 bg-rose-50 text-rose-700 dark:border-rose-400 dark:bg-rose-950/40 dark:text-rose-200"
                                                : "border-stone-200 bg-white/85 text-stone-500 dark:border-stone-700 dark:bg-stone-900/70 dark:text-stone-400"
                                        }`}
                                        style={{ height: chip.rect.h, opacity: box.本卡 ? 1 : 0.85 }}
                                        title={`#${box.行} ${box.短名}｜${box.角标}`}
                                    >
                                        <span className="flex items-center gap-1">
                                            <span className={`font-mono text-[10px] ${box.本卡 ? "font-bold" : ""}`}>#{box.行}</span>
                                            <span className={`truncate ${chip.compact ? "text-[10px]" : "text-[11px]"} ${box.本卡 ? "font-semibold" : ""}`}>
                                                {box.短名 || "（未命名）"}
                                            </span>
                                            <span
                                                aria-hidden
                                                className="ml-auto size-1.5 shrink-0 rounded-full"
                                                style={{ backgroundColor: accentOf(box.角标) }}
                                            />
                                        </span>
                                        {chip.compact ? null : (
                                            <span className="truncate text-[10px]" style={{ color: accentOf(box.角标) }}>
                                                {box.角标}
                                            </span>
                                        )}
                                    </li>
                                );
                            })}
                        </ul>
                    </>
                ) : null}
            </div>

            <div className="mt-1 max-w-full px-0.5 text-left text-[11px] text-stone-500 dark:text-stone-400">
                {state === "unlocated" ? (
                    <span className="text-amber-700 dark:text-amber-300">{caption.main}</span>
                ) : (
                    <>
                        <div>
                            {caption.main}
                            {caption.hint ? <span className="text-stone-400 dark:text-stone-500">（{caption.hint}）</span> : null}
                        </div>
                        {caption.honest ? (
                            <Tag color="warning" className="m-0 mt-0.5 !text-[10px]">
                                {caption.honest}
                            </Tag>
                        ) : null}
                        {state === "no-own-box" && firstDesc ? (
                            <div className="line-clamp-2 text-stone-400 dark:text-stone-500">{firstDesc}</div>
                        ) : null}
                        {others ? <div className="text-stone-400 dark:text-stone-500">同画面其他资产 {others} 项（图上红框，非本行换图对象）</div> : null}
                        {shotText ? <div className="text-stone-400 dark:text-stone-500">影响镜次：{shotText}</div> : null}
                    </>
                )}
            </div>
        </div>
    );
}

/** 传图/清空并排的上传区（图床公网直链 → 定向写清单；组卡传代表行=全组联动）。 */
function AssetUploadZone({
    taskId,
    n,
    uploaded,
    busy,
    setBusy,
    onWritten,
    hint,
}: {
    taskId: string;
    n: number;
    uploaded: string[];
    busy: BusyKind;
    setBusy: (kind: BusyKind) => void;
    onWritten: () => void;
    hint?: string;
}) {
    const { message } = App.useApp();
    const fileInputRef = useRef<HTMLInputElement>(null);
    const dragDepthRef = useRef(0);
    const [dragActive, setDragActive] = useState(false);

    const uploadOne = async (file: File) => {
        if (busy) return;
        setBusy("upload");
        try {
            const url = await uploadHubImage(file);
            const result = await postHubAsset(taskId, { n, image_url: url });
            message.success(result.组?.length ? `已换图，#${result.组.join("、#")} 全组生效` : `#${n} 已换图`);
            onWritten();
        } catch (error) {
            message.error(`#${n} 换图失败：${errorText(error)}`);
        } finally {
            setBusy(null);
            if (fileInputRef.current) fileInputRef.current.value = "";
        }
    };

    const pickFile = (files: FileList | null) => {
        const image = Array.from(files || []).find((item) => item.type.startsWith("image/"));
        if (!image) {
            message.warning("请选图片文件");
            return;
        }
        void uploadOne(image);
    };

    const clearRow = async () => {
        if (busy) return;
        setBusy("upload");
        try {
            const result = await postHubAsset(taskId, { n, clear: true });
            message.success(result.组?.length ? `已清空，#${result.组.join("、#")} 全组按原片` : `#${n} 已按原片沿用`);
            onWritten();
        } catch (error) {
            message.error(`#${n} 清空失败：${errorText(error)}`);
        } finally {
            setBusy(null);
        }
    };

    const dropzoneClass = `flex h-16 min-w-40 flex-1 cursor-pointer items-center justify-center gap-1.5 rounded-md border border-dashed px-2 text-center text-xs transition ${
        dragActive ? "border-stone-900 bg-stone-100/80 dark:border-stone-100 dark:bg-stone-900/80" : "border-stone-300 bg-stone-50/60 hover:border-stone-400 dark:border-stone-700 dark:bg-stone-900/40 dark:hover:border-stone-500"
    }`;

    return (
        <div className="flex min-w-0 flex-1 flex-col justify-center gap-1.5">
            {uploaded.length ? (
                <div className="flex flex-wrap items-stretch gap-2">
                    {uploaded.map((url) => (
                        <a key={url} href={url} target="_blank" rel="noreferrer" className="block">
                            <img src={url} alt={`#${n} 换图`} className="aspect-video w-40 rounded-md border border-emerald-300 bg-stone-100 object-contain dark:border-emerald-700 dark:bg-stone-900" />
                        </a>
                    ))}
                </div>
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
                    onDragEnter={(event: DragEvent<HTMLDivElement>) => {
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
                <Button size="small" disabled={busy !== null || !uploaded.length} onClick={() => void clearRow()}>
                    清空
                </Button>
            </div>
            <div className="text-[11px] text-stone-400 dark:text-stone-500">{hint || (uploaded.length ? "换图走图床公网直链；清空=按原片沿用" : "没传就按原片沿用")}</div>
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

// ---------------------------------------------------------------- 提示词段 + 审批/生成/成片段

/** 提示词段：全文默认折叠可展开 + 自检结论（AT-9 B5：渲染完成自动跑；无文件=「未检查」）。 */
function PromptSection({ detail }: { detail: HubTaskDetail }) {
    const prompt = detail.prompt_md || "";
    return (
        <section className="rounded-lg border border-stone-200 bg-card p-4 dark:border-stone-800">
            <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="flex items-center gap-1.5 text-sm font-semibold">
                    <FileText className="size-4" />
                    提示词
                </h2>
                <SelfCheckBadge items={detail.自检 ?? null} />
            </div>
            {prompt ? (
                <details className="mt-2 rounded-md border border-stone-200 dark:border-stone-800">
                    <summary className="flex cursor-pointer list-none items-center justify-between gap-2 px-3 py-2 text-sm [&::-webkit-details-marker]:hidden">
                        <span className="text-stone-600 dark:text-stone-300">提示词全文（默认折叠，展开查看）</span>
                        <span className="text-xs text-stone-400 dark:text-stone-500">展开 / 收起</span>
                    </summary>
                    <div className="agent-streamdown thin-scrollbar max-h-96 overflow-auto border-t border-stone-200 px-3 py-2 text-sm dark:border-stone-800">
                        <Streamdown>{prompt}</Streamdown>
                    </div>
                </details>
            ) : (
                <div className="mt-2 rounded-md border border-dashed border-stone-300 px-3 py-4 text-sm text-stone-500 dark:border-stone-700 dark:text-stone-400">提示词尚未生成（确认置换后由后台渲染产出）。</div>
            )}
        </section>
    );
}

/** 自检结论（AT-9 B5 数据合同）：✓N 项全绿；有异常 ⚠N/M 必显，可展开逐项证据。
 *  FAIL 不拦截审批（可 override——图像内容是否真对仍属人工走查），但结论与证据必须可见。 */
function SelfCheckBadge({ items }: { items: HubSelfCheckItem[] | null }) {
    if (!items || !items.length) {
        return (
            <Tooltip title="这条任务渲染于自检上线前，没有自检结果文件（自检.json）。">
                <Tag className="m-0">自检结论：未检查</Tag>
            </Tooltip>
        );
    }
    const failed = items.filter((item) => item.结论 !== "PASS");
    return (
        <details className="min-w-0">
            <summary className="flex cursor-pointer list-none items-center [&::-webkit-details-marker]:hidden">
                <Tag color={failed.length ? "red" : "green"} className="m-0">
                    {failed.length
                        ? `自检 ⚠ ${failed.length}/${items.length} 项异常（点开看证据）`
                        : `自检 ✓ ${items.length} 项通过`}
                </Tag>
            </summary>
            <div className="mt-2 w-[min(560px,80vw)] space-y-2 rounded-md border border-stone-200 bg-stone-50 p-2 text-left dark:border-stone-800 dark:bg-stone-900">
                {items.map((item) => (
                    <div key={item.项} className="text-xs leading-5">
                        <div className="flex items-center gap-1.5">
                            {item.结论 === "PASS" ? (
                                <CheckCircle2 className="size-3.5 shrink-0 text-emerald-600" />
                            ) : (
                                <CircleAlert className="size-3.5 shrink-0 text-red-600" />
                            )}
                            <span className="font-medium">{item.项}</span>
                            <Tag color={item.结论 === "PASS" ? "green" : "red"} className="mr-0 px-1 py-0 text-[10px]">
                                {item.结论 === "PASS" ? "通过" : "异常"}
                            </Tag>
                        </div>
                        {item.证据 ? (
                            <div className="mt-0.5 break-words whitespace-pre-wrap pl-5 text-stone-500 dark:text-stone-400">{item.证据}</div>
                        ) : null}
                    </div>
                ))}
            </div>
        </details>
    );
}

/** 审批/生成/成片段（§四）：批准出片/退回置换；生成进度；成片预览+下载；失败重试。 */
function OutcomeSection({
    detail,
    busy,
    act,
}: {
    detail: HubTaskDetail;
    busy: BusyKind;
    act: (kind: Exclude<BusyKind, null>, run: () => Promise<void>, okText?: string) => Promise<void>;
}) {
    const state = detail.state;
    const localId = detail.local_id;

    const progress = typeof detail.gen_progress === "number" && Number.isFinite(detail.gen_progress) ? Math.max(0, Math.min(100, Math.round(detail.gen_progress))) : null;

    if (state === "awaiting_approval") {
        return (
            <section className="rounded-lg border border-stone-200 bg-card p-4 dark:border-stone-800">
                <div className="text-sm text-stone-500 dark:text-stone-400">提示词已出（上方可展开全文）。批准后将自动提交生成——生成会调用付费接口。</div>
                <ApprovalActions detail={detail} busy={busy} act={act} />
            </section>
        );
    }

    if (state === "approved") {
        return (
            <section className="rounded-lg border border-stone-200 bg-card p-4 dark:border-stone-800">
                <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="text-sm text-stone-500 dark:text-stone-400">已批准，等待提交生成。</span>
                    <Button
                        type="primary"
                        loading={busy === "submit"}
                        disabled={busy !== null}
                        onClick={() =>
                            void act(
                                "submit",
                                async () => {
                                    await submitHubTask(localId);
                                },
                                "已提交生成，等待成片",
                            )
                        }
                    >
                        提交生成
                    </Button>
                </div>
            </section>
        );
    }

    if (state === "generating") {
        return (
            <section className="rounded-lg border border-stone-200 bg-card p-4 dark:border-stone-800">
                <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
                    <span className="flex items-center gap-1.5 text-stone-500 dark:text-stone-400">
                        <LoaderCircle className="size-3.5 animate-spin" />
                        生成中，云端通常 5 分钟以上；前端超时≠云端失败，重查可找回成片。
                    </span>
                    <span className="text-stone-600 dark:text-stone-300">{progress !== null ? `${progress}%` : "等待进度"}</span>
                </div>
                {progress !== null ? (
                    <Progress percent={progress} showInfo={false} size="small" status="active" className="mt-2" />
                ) : (
                    <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-stone-200 dark:bg-stone-800">
                        <div className="h-full w-1/3 animate-pulse rounded-full bg-indigo-500" />
                    </div>
                )}
            </section>
        );
    }

    if (state === "done") {
        return (
            <section className="rounded-lg border border-stone-200 bg-card p-4 dark:border-stone-800">
                <h2 className="text-sm font-semibold">成片</h2>
                <video src={hubOutputUrl(localId)} controls preload="metadata" className="mt-2 aspect-video w-full rounded-md border border-stone-200 bg-black object-contain dark:border-stone-800" />
                <div className="mt-2 flex flex-wrap justify-end">
                    <a className="inline-flex h-8 items-center gap-1.5 rounded-md bg-stone-900 px-3 text-sm font-medium text-white transition hover:bg-stone-700 dark:bg-stone-100 dark:text-stone-900 dark:hover:bg-stone-300" href={hubOutputUrl(localId)} download={`${localId}.mp4`}>
                        <Download className="size-4" />
                        下载成片
                    </a>
                </div>
            </section>
        );
    }

    if (state === "failed") {
        return (
            <section className="rounded-lg border border-red-200 bg-red-50 p-4 dark:border-red-950 dark:bg-red-950/20">
                <div className="flex items-start gap-2 text-sm text-red-700 dark:text-red-300">
                    <CircleAlert className="mt-0.5 size-4 shrink-0" />
                    <div className="min-w-0 break-words">{detail.error || "任务失败，错误原文未返回。"}</div>
                </div>
                <div className="mt-2 flex justify-end">
                    <RetryButton localId={localId} busy={busy} act={act} />
                </div>
            </section>
        );
    }

    return null;
}

/** 重试（§四 failed 动作）：POST /api/hub/task/<id>/retry（AT-9 B5 上桥）——failed → discussing
 *  续跑，产物优先；非 failed 桥端 409，错误原文透出（不再吞错降级）。 */
function RetryButton({ localId, busy, act }: { localId: string; busy: BusyKind; act: (kind: Exclude<BusyKind, null>, run: () => Promise<void>, okText?: string) => Promise<void> }) {
    return (
        <Button
            danger
            size="small"
            icon={<RefreshCw className="size-3.5" />}
            loading={busy === "retry"}
            onClick={() =>
                void act("retry", async () => {
                    await retryHubTask(localId);
                }, "已触发重试（后台续跑，可关页等待）")
            }
        >
            重试
        </Button>
    );
}

/** awaiting_approval 的两条动作（§四）：批准出片（付费停点）/ 退回置换（确认弹窗）。 */
function ApprovalActions({ detail, busy, act }: { detail: HubTaskDetail; busy: BusyKind; act: (kind: Exclude<BusyKind, null>, run: () => Promise<void>, okText?: string) => Promise<void> }) {
    const { message, modal } = App.useApp();
    const localId = detail.local_id;
    const promptReady = Boolean(detail.prompt_md);

    const approveAndSubmit = () =>
        void act(
            "approve",
            async () => {
                await approveHubTask(localId);
                try {
                    await submitHubTask(localId);
                    message.success("已批准并提交生成，等待成片");
                } catch (error) {
                    message.warning(`已批准，但提交生成失败：${errorText(error)}。可在「提交生成」重试`);
                }
            },
        );

    const rollback = () => {
        modal.confirm({
            title: "退回置换？",
            content: "丢弃当前提示词，回到待置换状态重新传图/确认（原提示词不保留）。",
            okText: "退回置换",
            onOk: () =>
                act("rollback", async () => {
                    await rollbackHubTask(localId);
                }),
        });
    };

    return (
        <div className="mt-2 flex flex-wrap items-center justify-end gap-2">
            <Button danger ghost loading={busy === "rollback"} disabled={busy !== null} onClick={rollback}>
                退回置换
            </Button>
            <Button type="primary" icon={<Sparkles className="size-4" />} loading={busy === "approve"} disabled={!promptReady || busy !== null} onClick={approveAndSubmit}>
                批准出片
            </Button>
        </div>
    );
}

/** 描述首句（位置缺失时常驻展示）。 */
function firstSentence(text: string) {
    const head = (text || "").split(/[。；;.!！?？\n]/).find((part) => part.trim()) || "";
    return head.trim().slice(0, 42);
}

function formatTime(value: string) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}
