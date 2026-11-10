import { ArrowRight, CheckCircle2, CircleAlert, Download, FileText, ImageOff, LoaderCircle, RefreshCw, Sparkles, Type, Upload } from "lucide-react";
import { App, Button, Checkbox, Drawer, Empty, Image as AntdImage, Input, Modal, Progress, Radio, Select, Tag, Tooltip } from "antd";
import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { Streamdown } from "streamdown";

import { badgeFits, boxRect, containRect, formatSeconds, frameCaption, frameState, layoutLegend, otherBoxCount, ratioFromImage, stageSize } from "@/lib/hub/frame-overlay";
import { mergeBadge, mergeImageBranch, mergeMembershipChanged, mergeSelectable, mergeShotUnion, groupBoxRow, toggleMergeRow } from "@/lib/hub/merge-mode";

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
    type HubCardGroup,
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

    // ---- AT-11 M2 多选合并模式（§3.1/§3.6）：多选态=行号 Set（禁存卡对象引用） ----
    const [mergeMode, setMergeMode] = useState(false);
    const [selectedRows, setSelectedRows] = useState<Set<number>>(new Set());
    const [confirmOpen, setConfirmOpen] = useState(false);
    const [confirmRows, setConfirmRows] = useState<HubAssetCard[]>([]);
    // taskId 切换 → 多选态与选中一并清理（I2）；同任务 5 秒轮询不清（选中跨轮询保留）。
    useEffect(() => {
        setMergeMode(false);
        setSelectedRows(new Set());
        setConfirmOpen(false);
        setConfirmRows([]);
    }, [taskId]);

    // 逐卡可选性（已确认组置灰=先拆开；品牌视觉行置灰=品牌行单独处理）。
    const selectableMap = new Map<number, { selectable: boolean; reason: string }>();
    for (const card of cards) selectableMap.set(card.n, mergeSelectable(card));
    // 合并候选=选中且仍可选的行（轮询中变为不可选的行不计入，浮条数字诚实）。
    const mergeableSelected = cards.filter((card) => selectedRows.has(card.n) && selectableMap.get(card.n)?.selectable);
    const toggleRow = (card: HubAssetCard) => {
        const verdict = selectableMap.get(card.n);
        if (verdict && !verdict.selectable) {
            message.warning(verdict.reason);
            return;
        }
        setSelectedRows((prev) => toggleMergeRow(prev, card.n));
    };
    const exitMergeMode = () => {
        setMergeMode(false);
        setSelectedRows(new Set());
    };
    const openConfirm = () => {
        // 打开确认小窗时拍快照（提交时复核组成员未变，§3.6）。
        setConfirmRows(mergeableSelected);
        setConfirmOpen(true);
    };

    // 确认合并提交：先复核成员未变（变了报错可重选）→ 多图分支先清空落选行（既有单行清空路径，
    // 必须在成组前做——组内清空会整组联动）→ 既有 groupRows + 主行/组名落盘（『组元数据』）。
    // 成功清空选中退出多选（I2）；失败保留选中可重选；撞他组（409 文案）点名「先拆开」。
    const submitMerge = async (opts: { rows: number[]; primary: number; name: string; keepN: number | null }): Promise<boolean> => {
        if (busy) return false;
        const changed = mergeMembershipChanged(opts.rows, detail.asset_cards);
        if (changed) {
            message.error(`合并失败：${changed}`);
            return false;
        }
        setBusy("group");
        try {
            if (opts.keepN !== null) {
                for (const card of detail.asset_cards) {
                    if (opts.rows.includes(card.n) && card.n !== opts.keepN && card.已传图.length) {
                        await postHubAsset(taskId, { n: card.n, clear: true });
                    }
                }
            }
            await groupHubRows(taskId, opts.rows, { primary: opts.primary, name: opts.name });
            message.success(`已合并：#${opts.rows.join("、#")}（主行 #${opts.primary}，此后传一张全组生效）`);
            setSelectedRows(new Set());
            setMergeMode(false);
            setConfirmOpen(false);
            setConfirmRows([]);
            onWritten();
            return true;
        } catch (error) {
            const text = errorText(error);
            message.error(/已在他组|先解组|先拆开/.test(text) ? `其中一行已在他组，先拆开（${text}）` : `合并失败：${text}`);
            return false;
        } finally {
            setBusy(null);
        }
    };

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
                <div className="flex items-center gap-2">
                    <span className="text-xs text-stone-500 dark:text-stone-400">看原片帧 → 传我们的图（换图走图床公网直链）；没传的按原片沿用</span>
                    {/* AT-11 §3.1 常驻入口：点开进入多选模式，多张同一件 → 合并为一项 */}
                    {mergeMode ? (
                        <Button size="small" type="primary" ghost onClick={exitMergeMode} data-hub-merge-exit>
                            退出多选
                        </Button>
                    ) : (
                        <Button size="small" onClick={() => setMergeMode(true)} data-hub-merge-enter>
                            合并资产
                        </Button>
                    )}
                </div>
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
                    mergeMode={mergeMode}
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
                        mergeMode={mergeMode}
                        selected={selectedRows.has(card.n) && Boolean(selectableMap.get(card.n)?.selectable)}
                        disabledReason={mergeMode ? selectableMap.get(card.n)?.reason || "" : ""}
                        onToggleRow={() => toggleRow(card)}
                    />
                ))}
            </div>

            {/* 多选态底部浮条（§3.1：「已选 N 行 · 合并为一项 · [取消]」） */}
            {mergeMode ? (
                <div
                    className="fixed bottom-6 left-1/2 z-30 flex -translate-x-1/2 items-center gap-3 rounded-full border border-stone-200 bg-card px-5 py-2.5 shadow-lg dark:border-stone-700"
                    data-hub-merge-bar
                >
                    <span className="text-sm font-medium">已选 {mergeableSelected.length} 行 · 合并为一项</span>
                    <Button size="small" type="primary" disabled={mergeableSelected.length < 2 || busy !== null} loading={busy === "group"} onClick={openConfirm} data-hub-merge-submit>
                        合并
                    </Button>
                    <Button size="small" onClick={exitMergeMode} data-hub-merge-cancel>
                        取消
                    </Button>
                </div>
            ) : null}

            {/* 确认小窗（§3.1：名称/主行可改落盘 + 影响预览 + 已传图三分支） */}
            <MergeConfirmModal
                open={confirmOpen}
                rows={confirmRows}
                shotMap={shotMap}
                submitting={busy === "group"}
                onCancel={() => setConfirmOpen(false)}
                onSubmit={submitMerge}
            />

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

/**
 * 确认组一卡通（AT-11 M2 合并卡）：徽标「#1+#3+#5 已合并」+组名+镜次并集；
 * 代表帧与唯一框**同源自主行**（组.代表帧/组.单框，单帧单框）——主行无框 → 诚实文案「主行未定位」，
 * 绝不搬组内其他行的框；旧 bridge 无组投影 → 回退 AT-10 共享帧渲染。
 * 「拆开合并」=解除联动，各行保留当前图与流水（§3.7，不是历史撤销）。
 */
function GroupCard({
    bucket,
    color,
    shots,
    busy,
    setBusy,
    taskId,
    onWritten,
    onUngroup,
    mergeMode = false,
}: {
    bucket: HubAssetCard[];
    color: string;
    shots: number[];
    busy: BusyKind;
    setBusy: (kind: BusyKind) => void;
    taskId: string;
    onWritten: () => void;
    onUngroup: () => void;
    mergeMode?: boolean;
}) {
    const { modal } = App.useApp();
    // 组卡投影（AT-11 M1）：成员卡挂同一『组』对象；代表卡=主行（投影缺省=最小行）。
    const group = bucket.find((card) => card.组)?.组 ?? null;
    const members = group ? group.成员 : bucket.map((card) => card.n);
    const representative = (group ? bucket.find((card) => card.n === group.主行) : undefined) ?? bucket[0];
    const uploadedAll = [...new Set(bucket.flatMap((card) => card.已传图))];
    const unionShots = group && group.镜次并集.length ? group.镜次并集 : shots;
    const groupName = group?.组名 || "";
    const badge = group ? mergeBadge(members) : `同一件东西·共${bucket.length}行`;

    const confirmUngroup = () =>
        modal.confirm({
            title: `拆开合并 #${members.join("、#")}？`,
            content: "解除联动，各行保留当前图与流水（不是恢复合并前的历史值）。",
            okText: "拆开合并",
            onOk: onUngroup,
        });

    return (
        <div
            className={`mt-3 rounded-lg border-2 border-dashed border-emerald-300 bg-emerald-50/40 p-3 dark:border-emerald-700 dark:bg-emerald-950/20 ${mergeMode ? "opacity-60" : ""}`}
            style={{ borderLeft: `4px solid ${color.replace(/0\.30\)$/, "0.85)")}` }}
            data-hub-group-card={group ? group.键 : ""}
            data-hub-merge-disabled={mergeMode ? "已确认组·先拆开" : ""}
        >
            <div className="flex flex-wrap items-center gap-2">
                <Tag color="success" className="m-0" data-hub-group-badge>
                    {badge}
                </Tag>
                <span className="text-sm font-semibold" data-hub-group-name>
                    {groupName || representative.短名 || `#${representative.n}`}
                </span>
                {representative.建议 === "必须换" ? <Tag color="red" className="m-0">必须换</Tag> : null}
                <span className="font-mono text-xs text-stone-400 dark:text-stone-500">#{members.map((n) => (n === representative.n ? `${n}（主行）` : n)).join("、#")}</span>
                {unionShots.length ? (
                    <span className="text-xs text-stone-400 dark:text-stone-500" data-hub-group-shots>
                        影响镜次：第{unionShots.join("、")}镜
                    </span>
                ) : null}
                {mergeMode ? <Tag className="m-0">已确认组·先拆开（不可并入其他合并）</Tag> : null}
                <Button size="small" className="ml-auto" loading={busy === "ungroup"} disabled={busy !== null || mergeMode} onClick={confirmUngroup} data-hub-group-ungroup>
                    拆开合并
                </Button>
            </div>
            <div className="mt-1 text-xs text-emerald-700 dark:text-emerald-400">
                {group ? `合并后一体：传一张图 · 一个图号替换 #${members.join("、#")} 全部（解除联动各自保留当前图）。` : "组内一行换图 = 全组生效（清单、流水、提交次序三处同步，由后端单点完成）。"}
            </div>
            <div className="mt-2 flex flex-wrap items-stretch gap-2">
                {group ? <GroupFrame group={group} className="shrink-0" /> : <FrameWithPosition card={representative} className="shrink-0" />}

                {uploadedAll.length ? <ArrowRight className="mx-0.5 size-5 shrink-0 self-center text-stone-300 dark:text-stone-600" /> : null}
                <AssetUploadZone
                    taskId={taskId}
                    n={representative.n}
                    uploaded={uploadedAll}
                    busy={busy}
                    setBusy={setBusy}
                    onWritten={onWritten}
                    hint={`传一张，#${members.join("、#")} 全组生效`}
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

/**
 * AT-11 M2 合并卡代表帧（§3.1.2 单帧单框）：帧与唯一框同源自主行投影（组.代表帧/组.单框），
 * 帧上**恰一枚框**（data-hub-box-row=主行）；主行无自身框 → 不画任何框+「主行未定位」诚实文案
 * （绝不搬组内其他行的框充数、不跨镜拼框）。几何口径与 AT-10 相同：contain 内容盒 + 百分比框。
 */
function GroupFrame({ group, className = "" }: { group: HubCardGroup; className?: string }) {
    const url = (group.代表帧.url || "").trim();
    const [ratio, setRatio] = useState<number | null>(null);
    const stageRef = useRef<HTMLDivElement | null>(null);

    // 比例探针同 FrameWithPosition（antd Image 不透传 onLoad + 缓存早挂载，挂载期主动查一次）。
    useEffect(() => {
        setRatio(null);
        if (!url) return;
        let alive = true;
        const apply = (img: HTMLImageElement | null) => {
            const next = ratioFromImage(img);
            if (alive && next !== null) setRatio(next);
        };
        apply(stageRef.current?.querySelector("img") ?? null);
        const probe = new Image();
        probe.onload = () => apply(probe);
        probe.onerror = () => apply(null);
        probe.src = url;
        apply(probe);
        return () => {
            alive = false;
            probe.onload = null;
            probe.onerror = null;
        };
    }, [url]);

    const stage = stageSize(ratio);
    const content = containRect(ratio, stage);
    const boxRow = groupBoxRow(group); // 恰一枚框（行号=主行）；null=主行未定位（诚实降级）
    const rect = boxRow && group.单框 ? boxRect(group.单框, content) : null;
    const located = Boolean(url) && (group.代表帧.定位 || "").trim() === "精确";
    const shot = group.代表帧.镜次;
    const caption = located
        ? [typeof shot === "number" && shot > 0 ? `第${shot}镜` : "", formatSeconds(group.代表帧.秒), "AI粗标"].filter(Boolean).join(" · ")
        : "未能定位到该资产出现的镜头 · 暂用整片画面";

    return (
        <div className={className} data-hub-group-frame data-hub-frame-state={boxRow ? "precise" : "unlocated"}>
            <div className="relative">
                <div
                    ref={stageRef}
                    className="relative shrink-0 overflow-hidden rounded-md bg-stone-100 dark:bg-stone-900"
                    style={{ width: stage.w, height: stage.h }}
                    data-hub-frame-stage
                    data-hub-frame-state={boxRow ? "precise" : "unlocated"}
                >
                    {url ? (
                        <AntdImage src={url} width={stage.w} height={stage.h} style={{ objectFit: "contain", display: "block" }} alt={`#${group.主行} 主行代表帧`} />
                    ) : (
                        <div className="flex size-full flex-col items-center justify-center gap-1 px-2 text-center text-xs text-stone-400 dark:text-stone-500">
                            <ImageOff className="size-4" />
                            <span>未定位（无可用帧）</span>
                        </div>
                    )}

                    {/* 内容盒：与 contain 后图片实际内容区重合；组卡只放主行那一枚框 */}
                    {url ? (
                        <div className="pointer-events-none absolute" style={{ left: content.x, top: content.y, width: content.w, height: content.h }} data-hub-frame-content-box>
                            {boxRow && rect ? (
                                <div
                                    data-hub-box-row={boxRow.row}
                                    data-hub-box-own="1"
                                    className="absolute rounded-[2px]"
                                    style={{
                                        left: rect.x,
                                        top: rect.y,
                                        width: rect.w,
                                        height: rect.h,
                                        border: "2px solid rgb(225,29,72)",
                                        backgroundColor: "rgba(244,63,94,0.14)",
                                    }}
                                >
                                    {badgeFits(rect) ? (
                                        <span className="absolute left-0.5 top-0.5 rounded px-1 text-[10px] leading-4" data-hub-box-badge style={{ backgroundColor: "rgb(225,29,72)", color: "#fff" }}>
                                            #{boxRow.row}
                                        </span>
                                    ) : null}
                                </div>
                            ) : null}
                        </div>
                    ) : null}
                </div>
            </div>
            <div className="mt-1 max-w-full px-0.5 text-left text-[11px] text-stone-500 dark:text-stone-400">
                <div>{caption}{located ? "（点图放大）" : ""}</div>
                {url && !boxRow ? (
                    <Tag color="warning" className="m-0 mt-0.5 !text-[10px]" data-hub-group-degraded>
                        主行未定位
                    </Tag>
                ) : null}
            </div>
        </div>
    );
}

/** 单卡置换单元：帧+overlay、传图/清空并排、疑似组提示按钮、品牌文字（品牌视觉卡）。
 *  AT-11 M2：多选态下整卡可点选（行号入 Set）；置灰行（确认组/品牌行）不可选并注明原因。 */
function AssetCardUnit({
    card,
    color,
    shots,
    busy,
    setBusy,
    taskId,
    onWritten,
    onGroup,
    mergeMode = false,
    selected = false,
    disabledReason = "",
    onToggleRow,
}: {
    card: HubAssetCard;
    color: string;
    shots: number[];
    busy: BusyKind;
    setBusy: (kind: BusyKind) => void;
    taskId: string;
    onWritten: () => void;
    onGroup: () => void;
    mergeMode?: boolean;
    selected?: boolean;
    disabledReason?: string;
    onToggleRow?: () => void;
}) {
    const { message } = App.useApp();
    const [brandText, setBrandText] = useState("");
    const brand = card.类别.includes("品牌视觉");
    const clickBlocked = mergeMode && Boolean(disabledReason);

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
            role={mergeMode && !disabledReason ? "button" : undefined}
            aria-disabled={mergeMode ? Boolean(disabledReason) : undefined}
            onClick={mergeMode && !disabledReason ? onToggleRow : undefined}
            className={`rounded-lg border p-3 dark:border-stone-800 ${
                mergeMode
                    ? disabledReason
                        ? "cursor-not-allowed border-stone-200 opacity-60 dark:border-stone-800"
                        : "cursor-pointer border-stone-200 transition hover:border-stone-400 dark:border-stone-800 dark:hover:border-stone-500"
                    : ""
            } ${selected ? "!border-indigo-500 ring-2 ring-indigo-200 dark:ring-indigo-900" : ""}`}
            style={{ borderLeft: `4px solid ${color.replace(/0\.30\)$/, "0.85)")}` }}
            data-hub-merge-row={mergeMode ? card.n : undefined}
            data-hub-merge-selected={mergeMode && selected ? "1" : "0"}
            data-hub-merge-disabled={mergeMode ? disabledReason : ""}
        >
            <header className="flex flex-wrap items-center gap-2">
                {mergeMode ? (
                    selected ? (
                        <Tag color="geekblue" className="m-0" data-hub-merge-selected-tag>
                            已选
                        </Tag>
                    ) : disabledReason ? (
                        <Tag className="m-0" data-hub-merge-disabled-tag>
                            {disabledReason}
                        </Tag>
                    ) : (
                        <Tag className="m-0">点选参与合并</Tag>
                    )
                ) : null}
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

            <div className="mt-2 flex flex-wrap items-stretch gap-2" onClick={mergeMode ? (event) => event.stopPropagation() : undefined}>
                <FrameWithPosition card={card} className="shrink-0" />
                {card.已传图.length ? <ArrowRight className="mx-0.5 size-5 shrink-0 self-center text-stone-300 dark:text-stone-600" /> : null}
                <AssetUploadZone taskId={taskId} n={card.n} uploaded={card.已传图} busy={busy} setBusy={setBusy} onWritten={onWritten} />
            </div>

            {/* 疑似组：只提示 + 一键成组，绝不自动联动（#3↔#5 误连实证反例）；多选态下禁用（走浮条合并） */}
            {card.同实体疑似.length ? (
                <div className="mt-2 flex flex-wrap items-center gap-2 rounded-md bg-amber-50/70 px-2 py-1.5 text-xs text-amber-700 dark:bg-amber-950/20 dark:text-amber-300" onClick={mergeMode ? (event) => event.stopPropagation() : undefined}>
                    <span>可能是同一件？与 #{card.同实体疑似.join("、#")} 疑似同实体（现不联动，各自独立换图）</span>
                    <Button size="small" loading={busy === "group"} disabled={busy !== null || clickBlocked} onClick={onGroup}>
                        成组
                    </Button>
                </div>
            ) : null}

            {brand ? (
                <div className="mt-2 flex flex-wrap items-center gap-2" onClick={mergeMode ? (event) => event.stopPropagation() : undefined}>
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

/**
 * AT-11 M2 确认小窗（§3.1）：合并后名称（默认主行短名可改）、主行（默认行号最小可改——落盘『组元数据』）、
 * 影响预览（镜次并集 + 传 1 张图 1 个图号替换全部）、已传图三分支（B3）：
 * 零图=直接组；一张唯一图=提示「沿用 #N 已传图统一到全组」；多张不同图=列出点名保留一张（其余行图
 * 清空走既有清空路径，流水如实记录）或取消合并不悄悄任选。
 * 提交失败不关窗（可重选）；父层负责提交编排与成员复核。
 */
function MergeConfirmModal({
    open,
    rows,
    shotMap,
    submitting,
    onCancel,
    onSubmit,
}: {
    open: boolean;
    rows: HubAssetCard[];
    shotMap: Map<number, number[]>;
    submitting: boolean;
    onCancel: () => void;
    onSubmit: (opts: { rows: number[]; primary: number; name: string; keepN: number | null }) => Promise<boolean>;
}) {
    // rows=打开确认窗时的快照（父层拍定），此处排序仅展示用。
    const sortedRows = useMemo(() => [...rows].sort((a, b) => a.n - b.n), [rows]);
    const branch = useMemo(() => mergeImageBranch(sortedRows), [sortedRows]);
    const [primary, setPrimary] = useState<number>(sortedRows[0]?.n ?? 0);
    const [name, setName] = useState(sortedRows[0]?.短名 || "");
    const [keepN, setKeepN] = useState<number | null>(branch.branch === "multi" ? branch.candidates[0].n : null);
    const [error, setError] = useState("");

    const shotUnion = mergeShotUnion(sortedRows.map((card) => shotMap.get(card.n) || []));

    const submit = async () => {
        if (!sortedRows.length) return;
        if (!primary || !sortedRows.some((card) => card.n === primary)) {
            setError("请选主行（主行必须是本次合并的行之一）");
            return;
        }
        if (branch.branch === "multi" && keepN === null) {
            setError("多张不同图：请点名保留哪一张，或取消合并");
            return;
        }
        setError("");
        await onSubmit({ rows: sortedRows.map((card) => card.n), primary, name: name.trim(), keepN: branch.branch === "multi" ? keepN : null });
    };

    return (
        <Modal
            title={`合并为一项（已选 ${sortedRows.length} 行）`}
            open={open}
            onCancel={onCancel}
            footer={null}
            width={560}
            destroyOnHidden
        >
            <div data-hub-merge-modal>
            {sortedRows.length ? (
                <div className="space-y-4 py-1">
                    {/* 影响预览（§3.1）：镜次并集 + 一图一号替换全部 */}
                    <div className="rounded-md bg-stone-50 px-3 py-2 text-sm dark:bg-stone-900" data-hub-merge-preview>
                        <div className="font-medium">影响预览</div>
                        <div className="mt-1 text-xs text-stone-500 dark:text-stone-400">
                            合并 #{sortedRows.map((card) => card.n).join("、#")}
                            {shotUnion.length ? ` · 镜次并集：第 ${shotUnion.join("、")} 镜` : ""}
                            {branch.branch === "single" ? " · 沿用既有图" : " · 传 1 张图 · 1 个图号替换全部"}
                        </div>
                    </div>

                    {/* 已传图三分支（B3） */}
                    {branch.branch === "empty" ? (
                        <div className="rounded-md border border-stone-200 px-3 py-2 text-xs text-stone-500 dark:border-stone-800 dark:text-stone-400" data-hub-merge-branch="empty">
                            组内暂无已传图：直接合并，之后传一张全组生效。
                        </div>
                    ) : branch.branch === "single" ? (
                        <div className="rounded-md border border-emerald-200 bg-emerald-50/60 px-3 py-2 text-xs text-emerald-700 dark:border-emerald-800 dark:bg-emerald-950/20 dark:text-emerald-300" data-hub-merge-branch="single">
                            沿用 #{branch.keepN} 已传图统一到全组（合并后全组同图同图号）。
                        </div>
                    ) : (
                        <div className="rounded-md border border-amber-200 bg-amber-50/60 px-3 py-2 text-xs text-amber-700 dark:border-amber-800 dark:bg-amber-950/20 dark:text-amber-300" data-hub-merge-branch="multi">
                            <div className="font-medium">所选行有多张不同图，保留哪一张？</div>
                            <Radio.Group className="mt-1.5 flex flex-col gap-1" value={keepN} onChange={(event) => setKeepN(event.target.value)}>
                                {branch.candidates.map((pair) => (
                                    <Radio key={`${pair.n}-${pair.url}`} value={pair.n}>
                                        保留 #{pair.n} 的图
                                    </Radio>
                                ))}
                            </Radio.Group>
                            <div className="mt-1 text-[11px] text-amber-600/80 dark:text-amber-300/80">其余行已传图将清空（流水如实记录）；也可取消合并不动任何图。</div>
                        </div>
                    )}

                    {/* 主行（默认行号最小可改——落盘『组元数据』，代表帧/单框/图号锚定它） */}
                    <div className="flex flex-wrap items-center gap-2 text-sm">
                        <span className="shrink-0">主行</span>
                        <span data-hub-merge-primary className="inline-flex">
                            <Select
                                size="small"
                                className="min-w-40"
                                value={primary}
                                onChange={(value) => {
                                    setPrimary(value);
                                    setName(sortedRows.find((card) => card.n === value)?.短名 || name);
                                }}
                                options={sortedRows.map((card) => ({ value: card.n, label: `#${card.n} ${card.短名 || card.描述.slice(0, 16)}` }))}
                            />
                        </span>
                        <span className="text-xs text-stone-400 dark:text-stone-500">代表帧与合并卡取主行</span>
                    </div>

                    {/* 合并后名称（默认主行短名可改） */}
                    <div className="flex flex-wrap items-center gap-2 text-sm">
                        <span className="shrink-0">合并后名称</span>
                        <Input size="small" className="min-w-60 flex-1" value={name} onChange={(event) => setName(event.target.value)} placeholder="合并后叫什么（默认主行短名）" data-hub-merge-name />
                    </div>

                    {error ? <div className="text-xs text-red-600 dark:text-red-400">{error}</div> : null}

                    <div className="flex justify-end gap-2">
                        <Button size="small" onClick={onCancel}>
                            取消
                        </Button>
                        <Button size="small" type="primary" loading={submitting} onClick={() => void submit()} data-hub-merge-confirm>
                            确认合并
                        </Button>
                    </div>
                </div>
            ) : (
                <div className="py-6 text-center text-sm text-stone-500 dark:text-stone-400">没有可合并的行（请先在多选态选中 ≥2 行）。</div>
            )}
            </div>
        </Modal>
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
function FrameWithPosition({ card, className = "" }: { card: HubAssetCard; className?: string }) {
    const state = frameState(card);
    const legacyUrl = card.帧.map((frame) => (frame.url || "").trim()).find(Boolean) || "";
    const url = (card.代表帧.url || "").trim() || legacyUrl;
    const [ratio, setRatio] = useState<number | null>(null);
    const [hoverRow, setHoverRow] = useState<number | null>(null);
    const stageRef = useRef<HTMLDivElement | null>(null);

    /**
     * 帧真实宽高比（C4 修 C3 真机缺陷）：只靠 onLoad 必挂——antd Image（@rc-component/image）内部 `<img>`
     * 只吃 COMMON_PROPS 白名单（无 onLoad）＋自己的 srcAndOnload，外部 onLoad 根本不透传；命中缓存时
     * load 事件又早于 React 挂载。两种情形 ratio 恒 null → 竖屏帧按 16:9 画，框落到 letterbox 黑边。
     * 所以挂载/换图时主动查一次：① DOM 里已渲染的 img（缓存命中 immediate complete）；
     * ② 自带 Image 探针（未命中时由 probe.onload 回来，命中时同 URL 亦走缓存）。
     */
    useEffect(() => {
        setRatio(null);
        if (!url) return;
        let alive = true;
        const apply = (img: HTMLImageElement | null) => {
            const next = ratioFromImage(img);
            if (alive && next !== null) setRatio(next);
        };
        apply(stageRef.current?.querySelector("img") ?? null);
        const probe = new Image();
        probe.onload = () => apply(probe);
        probe.onerror = () => apply(null);
        probe.src = url;
        apply(probe);
        return () => {
            alive = false;
            probe.onload = null;
            probe.onerror = null;
        };
    }, [url]);

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
    const wrapperW = legend.chips.length ? legend.x + legend.w : stage.w;
    const wrapperH = Math.max(stage.h, legend.height);

    return (
        <div className={className} data-hub-frame-state={state}>
            <div className="relative flex items-start" style={{ width: wrapperW, height: wrapperH }}>
                {/* 舞台：尺寸按帧真实宽高比定（竖屏窄、横屏宽）；overflow 只裁图片圆角，标签全在舞台之外 */}
                <div
                    ref={stageRef}
                    className="relative shrink-0 overflow-hidden rounded-md bg-stone-100 dark:bg-stone-900"
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
