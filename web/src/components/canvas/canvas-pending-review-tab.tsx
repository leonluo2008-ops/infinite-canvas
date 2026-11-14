import { useCallback, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { App, Empty, Popconfirm, Spin } from "antd";
import { AlertTriangle, Ban, ChevronRight, Download, Film, Image as ImageIcon, MapPin, Pause, Play, RotateCcw, ShieldCheck, X } from "lucide-react";
import { Streamdown } from "streamdown";
import { useTranslation } from "react-i18next";
import { useParams } from "react-router-dom";

import { readNodeFullText, buildGenerationStatus } from "@/lib/agent/agent-node-text";
import type { CanvasAgentSnapshot } from "@/lib/canvas/canvas-agent-ops";
import type { CanvasTheme } from "@/lib/canvas-theme";
import { findBatchMember, findWorkflowBatch, type WorkflowBatchMember, type WorkflowBatchRecord } from "@/lib/canvas/workflow-batch";
import { queryBatchMemberTasks, splitEvidenceByMember, type StatusQuery } from "@/lib/canvas/workflow-batch-query";
import type { MemberTaskEvidence } from "@/lib/canvas/workflow-batch-status";
import { getWorkflowBatchRuntime, pauseBatch, rerunInstance, startBatch } from "@/lib/canvas/workflow-batch-runtime";
import { readApprovedSignature, readGenDraft } from "@/lib/hub/gen-settings";
import { buildRepairTimeline, buildReplaceSummary, buildReviewList, buildSlotRows, canShowRollback, chunkRows, filterReviewInstances, isReviewTabEmpty, REVIEW_FILTER_ALL, REVIEW_FILTER_REPLACED, reviewFilterIdOf, reviewReplacedCount, selfcheckItemsOf, vetoRecordForSlot, type ReviewBatchRow, type ReviewInstanceRow, type ReviewSlotRow } from "@/lib/canvas/workflow-pending-review";
import { upsertAssetVeto, type WorkflowAssetVetoRecord } from "@/lib/canvas/workflow-asset-match";
import { workflowTemplateDisplayName } from "@/lib/canvas/workflow-builtin-template";
import { approveHubTask, errorText, getHubTask, hubQuickAction, hubQuickChannels, hubQuickPhaseText, rollbackHubTask, runHubQuickAction, type HubQuickPhase, type HubTaskDetail } from "@/pages/hub/hub-api";
import { resolveMediaUrl } from "@/services/file-storage";
import { previewUrlFor } from "@/services/image-storage";
import { useCanvasStore } from "@/stores/canvas/use-canvas-store";
import { useConfigStore } from "@/stores/use-config-store";
import { useWorkbenchAgentStore } from "@/stores/use-workbench-agent-store";
import { CanvasNodeType, type CanvasNodeData } from "@/types/canvas";

/** 语义色（与侧栏既有 `STATUS_COLOR` 同口径；色值不落 `stone/slate` 类名）。 */
const TONE_COLOR: Record<string, string> = {
    wait: "#f59e0b",
    run: "#3b82f6",
    human: "#8b5cf6",
    fail: "#ef4444",
    done: "#22c55e",
    unknown: "#a8a29e",
};

// ---------------------------------------------------------------------------
// hub 现查（徽标与列表共用一份；10s 内复用，避免两处重复打 GET）
// ---------------------------------------------------------------------------

type HubStateRow = { state: string | null; error?: string };
const HUB_STATE_TTL_MS = 10_000;
const hubStateCache = new Map<string, { at: number; row: HubStateRow }>();
const hubStateInflight = new Map<string, Promise<HubStateRow>>();

function fetchHubState(localId: string): Promise<HubStateRow> {
    const cached = hubStateCache.get(localId);
    if (cached && Date.now() - cached.at < HUB_STATE_TTL_MS) return Promise.resolve(cached.row);
    const inflight = hubStateInflight.get(localId);
    if (inflight) return inflight;
    const promise = getHubTask(localId)
        .then((task): HubStateRow => ({ state: String(task?.state || "") }))
        .catch((error): HubStateRow => ({ state: null, error: errorText(error) }))
        .then((row) => {
            // 失败只短存（1s）：查不到 ≠ 待批，但也不该让一次网络抖动把整屏锁死 10s。
            hubStateCache.set(localId, { at: row.state === null ? Date.now() - HUB_STATE_TTL_MS + 1_000 : Date.now(), row });
            return row;
        })
        .finally(() => hubStateInflight.delete(localId));
    hubStateInflight.set(localId, promise);
    return promise;
}

/** 批次成员 hub 现查态（instanceId → state；null = 查不到）。 */
export function useReviewHubStates(batches: WorkflowBatchRecord[]) {
    const [states, setStates] = useState<Record<string, string | null>>({});
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState("");
    const [tick, setTick] = useState(0);
    const localIds = useMemo(() => [...new Set(batches.flatMap((batch) => batch.members.map((member) => String(member.hub?.localId || "")).filter(Boolean)))], [batches]);

    useEffect(() => {
        let alive = true;
        if (!localIds.length) {
            setStates({});
            setLoading(false);
            setError("");
            return;
        }
        setLoading(true);
        setError("");
        void (async () => {
            const rows: Record<string, HubStateRow> = {};
            let firstError = "";
            let failed = 0;
            for (let cursor = 0; cursor < localIds.length; cursor += 6) {
                const slice = localIds.slice(cursor, cursor + 6);
                const chunk = await Promise.all(slice.map(async (id) => [id, await fetchHubState(id)] as const));
                for (const [id, row] of chunk) {
                    rows[id] = row;
                    if (row.state === null) {
                        failed += 1;
                        firstError = firstError || String(row.error || "");
                    }
                }
            }
            if (!alive) return;
            const byInstance: Record<string, string | null> = {};
            for (const batch of batches) {
                for (const member of batch.members) {
                    const localId = String(member.hub?.localId || "");
                    if (localId) byInstance[member.instanceId] = rows[localId]?.state ?? null;
                }
            }
            setStates(byInstance);
            // 只有「全部查不到」才当错误显示（部分失败该条派生 unknown，不冒充待批也不炸整屏）
            setError(failed && failed === localIds.length ? firstError || "hub 状态查询失败" : "");
            setLoading(false);
        })();
        return () => {
            alive = false;
        };
    }, [batches, localIds, tick]);

    const reload = useCallback(() => setTick((value) => value + 1), []);
    return { states, loading, error, reload };
}

// ---------------------------------------------------------------------------
// P5 任务证据（批次成员分组查询 → 派生层 evidenceOf）
// ---------------------------------------------------------------------------

const EVIDENCE_TTL_MS = 15_000;
const evidenceCache = new Map<string, { at: number; rows: Map<string, MemberTaskEvidence> }>();

/** 缓存键：成员身份 + 后端任务号 + 成员节点集（任何一处变了就必须重查）。 */
function evidenceKeyOf(members: WorkflowBatchMember[]): string {
    return members.map((member) => [member.instanceId, member.backend?.videoTaskId || "", member.backend?.lapianTaskId || "", (member.nodes?.all || []).join(",")].join("~")).join("|");
}

/**
 * 成员任务证据（instanceId → `MemberTaskEvidence`）。
 *
 * 为什么必须接：成员声明了 `videoTaskId` 却没人查 → 派生层 R3 一律判「视频任务状态未知」，
 * 真机上已完成（视频任务 succeeded + 产物 success）的实例会被错标 unknown。
 *
 * 接法（全部复用既有实现，禁第二份）：`buildGenerationStatus` 包成 `StatusQuery` →
 * `queryBatchMemberTasks` 分块（≤100）+ 截断二分 → `splitEvidenceByMember` 切成每成员一份。
 * 口径：查询失败/截断/查不到 → 该成员证据缺失（派生层 unknown + missing），绝不造成功。
 * 刷新：与 `fetchHubState` 同路数，15s 内复用同一份结果，不新增轮询。
 */
export function useReviewEvidence(batches: WorkflowBatchRecord[], nodes: CanvasNodeData[], projectId: string, projectTitle: string) {
    const [rows, setRows] = useState<Map<string, MemberTaskEvidence>>(() => new Map());
    const members = useMemo(() => batches.flatMap((batch) => batch.members || []), [batches]);
    const key = useMemo(() => evidenceKeyOf(members), [members]);

    useEffect(() => {
        let alive = true;
        if (!members.length) {
            setRows(new Map());
            return () => {
                alive = false;
            };
        }
        const cached = evidenceCache.get(key);
        if (cached && Date.now() - cached.at < EVIDENCE_TTL_MS) {
            setRows(cached.rows);
            return () => {
                alive = false;
            };
        }
        const snapshot: CanvasAgentSnapshot = { projectId, title: projectTitle, nodes, connections: [], selectedNodeIds: [], viewport: { x: 0, y: 0, k: 1 } };
        const query: StatusQuery = async (input) => {
            const nodeIds = input.nodeIds || [];
            // 节点查询走画布口径；纯 taskId 查询（videoTaskId / lapianTaskId）只有任务口径能给状态。
            const scope = nodeIds.length ? "canvas" : "video";
            const result = buildGenerationStatus({ scope, nodeIds, taskId: input.taskId, limit: input.limit }, snapshot, useWorkbenchAgentStore.getState().tasks);
            return { total: Number(result.total || 0), tasks: (result.tasks || []).map((task) => ({ id: task.id, status: task.status, source: task.source, kind: task.kind, title: task.title })) };
        };
        void (async () => {
            try {
                const result = await queryBatchMemberTasks({ members, query, maxLimit: 100 });
                const split = splitEvidenceByMember({ members, result });
                evidenceCache.set(key, { at: Date.now(), rows: split });
                if (alive) setRows(split);
            } catch {
                // 整体查询失败：清空证据（派生层落 unknown + missing），不当成功
                if (alive) setRows(new Map());
            }
        })();
        return () => {
            alive = false;
        };
    }, [key, members, nodes, projectId, projectTitle]);

    return useCallback((member: WorkflowBatchMember) => rows.get(member.instanceId), [rows]);
}

/** 侧栏页签徽标数字（与列表同一派生链；只多一个 reduce）。 */
export function useReviewBadgeCount(nodes?: CanvasNodeData[]): number {
    const { id: projectId } = useParams();
    const project = useCanvasStore((state) => state.projects.find((item) => item.id === projectId));
    const batches = useMemo(() => project?.workflowBatches || [], [project?.workflowBatches]);
    const liveNodes = nodes && nodes.length ? nodes : project?.nodes || [];
    const { states } = useReviewHubStates(batches);
    const evidenceOf = useReviewEvidence(batches as WorkflowBatchRecord[], liveNodes, String(projectId || ""), String(project?.title || ""));
    return useMemo(() => {
        const rows = buildReviewList({ batches: batches as WorkflowBatchRecord[], nodes: liveNodes, hubStates: states, evidenceOf, templateNameOf: (templateId) => workflowTemplateDisplayName(project, templateId) });
        return rows.reduce((sum, row) => sum + row.badgeCount, 0);
    }, [batches, evidenceOf, liveNodes, project, states]);
}

// ---------------------------------------------------------------------------
// 待审批页签：批次头 + 实例行（只读派生；动作全部走既有闸）
// ---------------------------------------------------------------------------

type TabProps = {
    nodes: CanvasNodeData[];
    onFocusNode: (nodeId: string) => void;
    theme: CanvasTheme;
};

export function CanvasPendingReviewTab({ nodes, onFocusNode, theme }: TabProps) {
    const { t } = useTranslation();
    const { message } = App.useApp();
    const { id: projectId } = useParams();
    const project = useCanvasStore((state) => state.projects.find((item) => item.id === projectId));
    const batches = useMemo(() => (project?.workflowBatches || []) as WorkflowBatchRecord[], [project?.workflowBatches]);
    const vetoes = project?.workflowAssetVetoes;
    const liveNodes = nodes && nodes.length ? nodes : project?.nodes || [];
    const { states, loading, error, reload } = useReviewHubStates(batches);
    const evidenceOf = useReviewEvidence(batches, liveNodes, String(projectId || ""), String(project?.title || ""));
    const [pages, setPages] = useState(1);
    const [filter, setFilter] = useState<string>(REVIEW_FILTER_ALL);
    const [opened, setOpened] = useState<{ batchId: string; instanceId: string } | null>(null);
    const [busy, setBusy] = useState("");
    const [runtimeReady, setRuntimeReady] = useState(false);

    useEffect(() => {
        setRuntimeReady(Boolean(getWorkflowBatchRuntime()));
    }, [batches]);

    const rows = useMemo(() => buildReviewList({ batches, nodes: liveNodes, hubStates: states, evidenceOf, templateNameOf: (templateId) => workflowTemplateDisplayName(project, templateId) }), [batches, evidenceOf, liveNodes, project, states]);
    // 状态筛选（P0c 图2 ②）：键只是手势，行仍是派生结果；筛选只影响显示，不改批次状态。
    const filteredRows = useMemo(() => rows.map((batch) => ({ ...batch, instances: filterReviewInstances(batch.instances, filter) })), [rows, filter]);
    const flat = useMemo(() => filteredRows.flatMap((batch) => batch.instances), [filteredRows]);
    const chunks = useMemo(() => chunkRows(flat), [flat]);
    const shown = useMemo(() => chunks.slice(0, pages).flat(), [chunks, pages]);
    const pickFilter = useCallback((next: string) => {
        setFilter(next);
        setPages(1);
    }, []);
    const filterLabel = useMemo(() => {
        if (filter === REVIEW_FILTER_ALL) return t("canvas.review.filter.all");
        if (filter === REVIEW_FILTER_REPLACED) return t("canvas.review.filter.replaced");
        const hit = rows.flatMap((batch) => batch.instances).find((row) => reviewFilterIdOf(row.status) === filter);
        return hit ? t(hit.statusI18nKey, { defaultValue: hit.statusLabel }) : filter;
    }, [filter, rows, t]);

    const startOrPause = async (batchId: string, action: "start" | "pause") => {
        setBusy(`${action}:${batchId}`);
        try {
            const out = action === "start" ? await startBatch(batchId) : await pauseBatch(batchId);
            if (!out.ok) {
                message.error(out.reason);
                return;
            }
            message.success(action === "start" ? t("canvas.review.toast.batchStarted") : t("canvas.review.toast.batchPaused"));
            reload();
        } catch (failure) {
            message.error(errorText(failure));
        } finally {
            setBusy("");
        }
    };

    const rerun = async (row: ReviewInstanceRow) => {
        setBusy(`rerun:${row.instanceId}`);
        try {
            const out = await rerunInstance(row.batchId, row.instanceId);
            if (!out.ok) message.warning(out.reason || t("canvas.review.row.rerunFailed"));
            else message.success(t("canvas.review.toast.rerunQueued"));
            reload();
        } catch (failure) {
            message.error(errorText(failure));
        } finally {
            setBusy("");
        }
    };

    if (isReviewTabEmpty(batches)) {
        return (
            <div className="grid h-full place-items-center px-6 text-center" data-review-empty>
                <div className="space-y-2">
                    <span className="block text-sm font-medium opacity-60">{t("canvas.review.empty")}</span>
                    <span className="block text-xs leading-relaxed opacity-40">{t("canvas.review.emptyHint")}</span>
                    <span className="block text-[11px] opacity-30">{t("canvas.review.emptyHint2")}</span>
                </div>
            </div>
        );
    }

    return (
        <div className="flex h-full flex-col" data-review-list>
            {error ? (
                <div className="mx-3 mb-2 rounded-lg border px-2.5 py-2 text-xs leading-relaxed" style={{ borderColor: "#ef4444", color: "#ef4444" }} data-review-error>
                    <div className="break-words">{error}</div>
                    <button type="button" onClick={reload} className="mt-1 font-semibold underline">
                        {t("canvas.review.retry")}
                    </button>
                </div>
            ) : null}
            {loading && !flat.length ? (
                <div className="grid flex-1 place-items-center" data-review-loading>
                    <Spin size="small" />
                </div>
            ) : (
                <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-4">
                    {!flat.length ? (
                        <div className="mx-1 mt-2 rounded-lg border px-2.5 py-3 text-[11px] leading-relaxed" style={{ borderColor: theme.toolbar.border }} data-review-filter-empty>
                            <div className="opacity-60">{t("canvas.review.filter.empty", { label: filterLabel })}</div>
                            <div className="mt-1 opacity-40">{t("canvas.review.filter.emptyHint")}</div>
                            <button type="button" onClick={() => pickFilter(REVIEW_FILTER_ALL)} className="mt-1 font-semibold underline opacity-70" data-review-filter-reset>
                                {t("canvas.review.filter.reset")}
                            </button>
                        </div>
                    ) : null}
                    {rows.map((batch) => {
                        const batchRows = shown.filter((row) => row.batchId === batch.batchId);
                        // 筛选后为空 / 尚未翻到这一批 → 整批不渲染（含批次头），避免空头挂空列表
                        if (!batchRows.length) return null;
                        return <BatchBlock key={batch.batchId} batch={batch} rows={batchRows} filter={filter} onFilter={pickFilter} theme={theme} busy={busy} runtimeReady={runtimeReady} onOpen={(instanceId) => setOpened({ batchId: batch.batchId, instanceId })} onFocus={onFocusNode} onRerun={rerun} onBatchAction={startOrPause} />;
                    })}
                    {chunks.length > pages ? (
                        <button type="button" onClick={() => setPages((value) => value + 1)} className="mt-2 w-full rounded-lg border py-2 text-xs font-medium opacity-70 transition hover:opacity-100" style={{ borderColor: theme.toolbar.border }} data-review-more>
                            {t("canvas.review.showMore", { count: flat.length - shown.length })}
                        </button>
                    ) : null}
                </div>
            )}
            {opened ? <ReviewDrawer batchId={opened.batchId} instanceId={opened.instanceId} nodes={liveNodes} vetoes={vetoes} theme={theme} onClose={() => setOpened(null)} onFocusNode={onFocusNode} onChanged={reload} /> : null}
        </div>
    );
}

function BatchBlock({
    batch,
    rows,
    filter,
    onFilter,
    theme,
    busy,
    runtimeReady,
    onOpen,
    onFocus,
    onRerun,
    onBatchAction,
}: {
    batch: ReviewBatchRow;
    rows: ReviewInstanceRow[];
    filter: string;
    onFilter: (next: string) => void;
    theme: CanvasTheme;
    busy: string;
    runtimeReady: boolean;
    onOpen: (instanceId: string) => void;
    onFocus: (nodeId: string) => void;
    onRerun: (row: ReviewInstanceRow) => void;
    onBatchAction: (batchId: string, action: "start" | "pause") => void;
}) {
    const { t } = useTranslation();
    const startBusy = busy === `start:${batch.batchId}`;
    const pauseBusy = busy === `pause:${batch.batchId}`;
    const replacedCount = reviewReplacedCount(batch.instances);
    return (
        <section className="mb-3" data-review-batch={batch.batchId}>
            <header className="sticky top-0 z-10 -mx-1 mb-1.5 rounded-lg px-2 py-1.5 backdrop-blur" style={{ background: theme.toolbar.panel }}>
                <div className="flex items-center gap-1.5">
                    <span className="min-w-0 flex-1 truncate text-xs font-semibold" title={batch.title}>
                        {batch.title}
                    </span>
                    <span className="shrink-0 text-[11px] opacity-45" data-review-member-count>
                        {t("canvas.review.batch.memberCount", { count: batch.memberCount })}
                    </span>
                    <button
                        type="button"
                        disabled={!runtimeReady || startBusy}
                        onClick={() => onBatchAction(batch.batchId, "start")}
                        className="flex shrink-0 items-center gap-1 rounded-md px-1.5 py-1 text-[11px] font-semibold transition hover:bg-black/5 disabled:cursor-not-allowed disabled:opacity-40 dark:hover:bg-white/10"
                        style={{ color: theme.toolbar.activeText }}
                        title={runtimeReady ? undefined : t("canvas.review.batch.runtimeMissing")}
                        data-review-batch-start
                    >
                        <Play className="size-3" />
                        {startBusy ? t("canvas.review.batch.starting") : t("canvas.review.batch.start")}
                    </button>
                    <button
                        type="button"
                        disabled={!runtimeReady || pauseBusy || batch.queuePhase !== "running"}
                        onClick={() => onBatchAction(batch.batchId, "pause")}
                        className="flex shrink-0 items-center gap-1 rounded-md px-1.5 py-1 text-[11px] font-semibold opacity-80 transition hover:bg-black/5 disabled:cursor-not-allowed disabled:opacity-35 dark:hover:bg-white/10"
                        data-review-batch-pause
                    >
                        <Pause className="size-3" />
                        {pauseBusy ? t("canvas.review.batch.pausing") : t("canvas.review.batch.pause")}
                    </button>
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-1.5">
                    <span className="text-[11px] opacity-45">{batch.templateName}</span>
                    <ReviewFilterChip id={REVIEW_FILTER_ALL} label={t("canvas.review.filter.all")} count={batch.memberCount} active={filter === REVIEW_FILTER_ALL} theme={theme} onSelect={onFilter} />
                    {batch.chips.map((chip) => {
                        const id = reviewFilterIdOf(chip.status);
                        return <ReviewFilterChip key={chip.status} id={id} chip={chip.status} label={t(chip.i18nKey, { defaultValue: chip.label })} count={chip.count} tone={chip.tone} active={filter === id} theme={theme} onSelect={onFilter} />;
                    })}
                    {replacedCount > 0 ? (
                        <ReviewFilterChip id={REVIEW_FILTER_REPLACED} label={t("canvas.review.filter.replaced")} count={replacedCount} active={filter === REVIEW_FILTER_REPLACED} theme={theme} onSelect={onFilter} title={t("canvas.review.filter.replacedHint")} />
                    ) : null}
                    {batch.queue ? (
                        <span className="inline-flex items-center gap-1 text-[11px] opacity-50" data-review-queue={batch.queuePhase}>
                            {t(`canvas.review.queuePhase.${batch.queuePhase}`, { defaultValue: batch.queuePhase })} · {t("canvas.review.batch.queueNumbers", { concurrency: batch.queue.concurrency, attempted: batch.queue.attempted, awaiting: batch.queue.awaitingApproval, failed: batch.queue.failed })}
                        </span>
                    ) : null}
                </div>
            </header>
            <div className="space-y-1">
                {rows.map((row) => (
                    <InstanceRow key={row.instanceId} row={row} theme={theme} busy={busy} onOpen={() => onOpen(row.instanceId)} onFocus={onFocus} onRerun={() => onRerun(row)} />
                ))}
            </div>
        </section>
    );
}

/**
 * 状态筛选 chip（P0c 图2 ②：批次头 = 批次名 + 实例数 + 状态筛选）。
 *
 * - 选中态只用 `theme` token（`toolbar.activeBg / activeText`），禁硬编码黑白色；
 * - 未选中按语义 tone 上浅底（色值仍来自 `TONE_COLOR`）；再次点已选中的 chip = 回到「全部」；
 * - `data-review-chip` 保留（沿用原计数徽标的选择器），`data-review-filter` 是筛选手势。
 */
function ReviewFilterChip({ id, chip, label, count, tone, active, theme, onSelect, title }: { id: string; chip?: string; label: string; count: number; tone?: string; active: boolean; theme: CanvasTheme; onSelect: (next: string) => void; title?: string }) {
    const tint = tone ? TONE_COLOR[tone] || theme.node.muted : theme.node.muted;
    const style = active
        ? { background: theme.toolbar.activeBg, borderColor: theme.toolbar.activeText, color: theme.toolbar.activeText }
        : tone
          ? { background: `${tint}1f`, borderColor: "transparent", color: tint }
          : { background: "transparent", borderColor: theme.toolbar.border, color: theme.node.muted };
    return (
        <button
            type="button"
            onClick={() => onSelect(active ? REVIEW_FILTER_ALL : id)}
            title={title}
            aria-pressed={active}
            className={`inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[11px] font-medium transition ${active ? "" : "opacity-85 hover:opacity-100"}`}
            style={style}
            data-review-filter={id}
            data-review-filter-active={active ? "true" : "false"}
            data-review-chip={chip}
        >
            {label} {count}
        </button>
    );
}

/**
 * 素材缩略（32×32）：视频 → `<video src=…#t=0.1 muted preload="metadata">`，图片 → `<img>`；
 * 拿不到就渲染带图标的占位（不空白、不造假图）。URL 只走既有 `resolveMediaUrl`（禁另写存储读取）。
 */
function ReviewThumb({ row, theme }: { row: ReviewInstanceRow; theme: CanvasTheme }) {
    const [url, setUrl] = useState(row.materialUrl);
    useEffect(() => {
        let alive = true;
        setUrl(row.materialUrl);
        if (!row.materialStorageKey) {
            return () => {
                alive = false;
            };
        }
        void resolveMediaUrl(row.materialStorageKey, row.materialUrl)
            .then((next) => {
                if (alive && next) setUrl(next);
            })
            .catch(() => undefined);
        return () => {
            alive = false;
        };
    }, [row.materialStorageKey, row.materialUrl]);
    const frame = { borderColor: theme.toolbar.border, background: theme.toolbar.activeBg };
    if (url && row.materialKind === "video") {
        return (
            <span className="grid size-8 shrink-0 place-items-center overflow-hidden rounded-md border" style={frame}>
                <video data-review-thumb="video" src={`${url}#t=0.1`} muted preload="metadata" className="size-full object-cover" />
            </span>
        );
    }
    if (url) {
        return (
            <span className="grid size-8 shrink-0 place-items-center overflow-hidden rounded-md border" style={frame}>
                <img data-review-thumb="image" src={url} alt={row.materialTitle} className="size-full object-cover" />
            </span>
        );
    }
    return (
        <span className="grid size-8 shrink-0 place-items-center rounded-md border" style={{ ...frame, color: theme.node.muted }} title={row.materialTitle} data-review-thumb="placeholder">
            {row.materialKind === "video" ? <Film className="size-4" /> : <ImageIcon className="size-4" />}
        </span>
    );
}

function InstanceRow({ row, theme, busy, onOpen, onFocus, onRerun }: { row: ReviewInstanceRow; theme: CanvasTheme; busy: string; onOpen: () => void; onFocus: (nodeId: string) => void; onRerun: () => void }) {
    const { t } = useTranslation();
    const tone = TONE_COLOR[row.statusTone] || TONE_COLOR.unknown;
    const rerunning = busy === `rerun:${row.instanceId}`;
    return (
        <div className="group flex items-start gap-2 rounded-lg px-1.5 py-1.5 transition hover:bg-black/5 dark:hover:bg-white/5" data-review-instance={row.instanceId} data-review-status={row.status}>
            <ReviewThumb row={row} theme={theme} />
            <button type="button" onClick={onOpen} className="min-w-0 flex-1 text-left">
                <div className="flex items-center gap-1.5">
                    <span className="shrink-0 text-[11px] opacity-40">#{row.instanceIndex}</span>
                    <span className="min-w-0 flex-1 truncate text-xs font-medium" title={row.materialTitle}>
                        {row.materialTitle || row.instanceId}
                    </span>
                    <span className="shrink-0 rounded px-1 py-0.5 text-[10px] font-semibold" style={{ background: `${tone}1f`, color: tone }} data-review-status-label>
                        {t(row.statusI18nKey, { defaultValue: row.statusLabel })}
                    </span>
                </div>
                <div className="mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[11px] leading-snug opacity-50">
                    <span>{t("canvas.review.row.rule", { rule: row.rule })}</span>
                    {row.repairBadge.tone !== "none" ? (
                        <span style={{ color: row.repairBadge.tone === "fail" ? TONE_COLOR.fail : undefined }} data-review-repair>
                            {row.repairBadge.text}
                        </span>
                    ) : null}
                    <span data-review-selfcheck={row.selfcheckFailCount === null ? "unknown" : String(row.selfcheckFailCount)}>
                        {row.selfcheckFailCount === null ? t("canvas.review.row.selfcheckUnknown") : t("canvas.review.row.selfcheckFail", { count: row.selfcheckFailCount })}
                    </span>
                    {row.nodesPresent < row.nodesExpected ? <span style={{ color: TONE_COLOR.fail }}>{t("canvas.review.row.nodesMissing", { present: row.nodesPresent, total: row.nodesExpected })}</span> : null}
                    {row.missingEvidence.length ? <span style={{ color: TONE_COLOR.human }}>{t("canvas.review.row.missingEvidence", { count: row.missingEvidence.length })}</span> : null}
                </div>
                <div className="mt-0.5 truncate text-[11px] leading-snug opacity-35">{row.reason}</div>
            </button>
            <div className="flex shrink-0 items-center gap-0.5 pt-0.5">
                <button
                    type="button"
                    disabled={!row.locateNodeId}
                    onClick={() => row.locateNodeId && onFocus(row.locateNodeId)}
                    className="grid size-6 place-items-center rounded-md opacity-55 transition hover:bg-black/10 hover:opacity-100 disabled:cursor-not-allowed disabled:opacity-25 dark:hover:bg-white/10"
                    title={row.locateNodeId ? t("canvas.review.row.focus") : t("canvas.review.row.nodesDeleted")}
                    aria-label={t("canvas.review.row.focus")}
                    data-review-focus
                >
                    <MapPin className="size-3.5" />
                </button>
                <button
                    type="button"
                    disabled={rerunning || row.status === "done"}
                    onClick={onRerun}
                    className="grid size-6 place-items-center rounded-md opacity-55 transition hover:bg-black/10 hover:opacity-100 disabled:cursor-not-allowed disabled:opacity-25 dark:hover:bg-white/10"
                    title={row.status === "done" ? t("canvas.review.row.doneNoRerun") : t("canvas.review.row.rerun")}
                    aria-label={t("canvas.review.row.rerun")}
                    data-review-rerun
                >
                    <RotateCcw className="size-3.5" />
                </button>
                <button type="button" onClick={onOpen} className="grid size-6 place-items-center rounded-md opacity-45 transition hover:bg-black/10 hover:opacity-100 dark:hover:bg-white/10" aria-label={t("canvas.review.row.open")} data-review-open>
                    <ChevronRight className="size-3.5" />
                </button>
            </div>
            <span className="hidden" style={{ color: theme.node.muted }} />
        </div>
    );
}

// ---------------------------------------------------------------------------
// 详情抽屉：两态条 / 提示词全文 / 挂图位 / 置换摘要 / 自检 / 自修痕迹 / 动作位
// ---------------------------------------------------------------------------

/** 自检条目字段（生产者原样：`项/项显示名/结论/证据/说明`；不重判、不拆证据串）。 */
function selfcheckVerdictOf(item: Record<string, unknown>): string {
    return String(item.结论 ?? "");
}

function selfcheckNameOf(item: Record<string, unknown>, index: number): string {
    return String(item.项显示名 || item.项 || `#${index + 1}`);
}

function Section({ title, extra, children, theme }: { title: string; extra?: React.ReactNode; children: React.ReactNode; theme: CanvasTheme }) {
    return (
        <section className="border-t px-4 py-3" style={{ borderColor: theme.toolbar.border }}>
            <div className="mb-2 flex items-center gap-2">
                <h4 className="text-xs font-semibold opacity-70">{title}</h4>
                {extra}
            </div>
            {children}
        </section>
    );
}

function ReviewDrawer({
    batchId,
    instanceId,
    nodes,
    vetoes,
    theme,
    onClose,
    onFocusNode,
    onChanged,
}: {
    batchId: string;
    instanceId: string;
    nodes: CanvasNodeData[];
    vetoes?: WorkflowAssetVetoRecord[];
    theme: CanvasTheme;
    onClose: () => void;
    onFocusNode: (nodeId: string) => void;
    onChanged: () => void;
}) {
    const { t } = useTranslation();
    const { message } = App.useApp();
    const { id: projectId } = useParams();
    const project = useCanvasStore((state) => state.projects.find((item) => item.id === projectId));
    const updateProject = useCanvasStore((state) => state.updateProject);
    const flushProjects = useCanvasStore((state) => state.flushProjects);
    const config = useConfigStore((state) => state.config);
    const batch = findWorkflowBatch(project?.workflowBatches, batchId);
    const member = findBatchMember(batch, instanceId);
    const localId = String(member?.hub?.localId || "");
    const evidenceOf = useReviewEvidence((project?.workflowBatches || []) as WorkflowBatchRecord[], nodes, String(projectId || ""), String(project?.title || ""));

    const [task, setTask] = useState<HubTaskDetail | null>(null);
    const [taskError, setTaskError] = useState("");
    const [taskLoading, setTaskLoading] = useState(false);
    const [confirming, setConfirming] = useState(false);
    const [working, setWorking] = useState("");
    const [phase, setPhase] = useState<{ phase: HubQuickPhase; state: string } | null>(null);

    const reloadTask = useCallback(async () => {
        if (!localId) {
            setTask(null);
            setTaskError(t("canvas.review.drawer.noHub"));
            return;
        }
        setTaskLoading(true);
        try {
            setTask(await getHubTask(localId));
            setTaskError("");
        } catch (failure) {
            setTask(null);
            setTaskError(errorText(failure));
        } finally {
            setTaskLoading(false);
        }
    }, [localId, t]);

    useEffect(() => {
        void reloadTask();
    }, [reloadTask]);

    useEffect(() => {
        const onKey = (event: KeyboardEvent) => {
            if (event.key === "Escape") onClose();
        };
        window.addEventListener("keydown", onKey);
        return () => window.removeEventListener("keydown", onKey);
    }, [onClose]);

    const row = useMemo(() => {
        const list = buildReviewList({ batches: project?.workflowBatches, nodes, hubStates: task ? { [instanceId]: String(task.state || "") } : {}, evidenceOf });
        return list.flatMap((item) => item.instances).find((item) => item.instanceId === instanceId);
    }, [evidenceOf, instanceId, nodes, project?.workflowBatches, task]);

    const slotRows = useMemo(() => (member ? buildSlotRows({ member, nodes, vetoes }) : []), [member, nodes, vetoes]);
    const repairRows = useMemo(() => buildRepairTimeline(member?.repair), [member?.repair]);
    const [showPass, setShowPass] = useState(false);
    /** 自检真源：成员 `repair.finalSelfcheck`（生产者原样）优先；成员没投影时才回落 hub 透出的「自检」。 */
    const memberSelfcheck = useMemo(() => selfcheckItemsOf(member?.repair), [member?.repair]);
    const taskSelfcheck = useMemo(() => (Array.isArray(task?.自检) ? (task!.自检 as unknown as Record<string, unknown>[]) : []), [task]);
    const selfcheckSource = memberSelfcheck.length ? memberSelfcheck : taskSelfcheck;
    const selfcheckFail = selfcheckSource.filter((item) => selfcheckVerdictOf(item).trim().toUpperCase() !== "PASS").length;
    const passCount = selfcheckSource.length - selfcheckFail;
    const replace = useMemo(() => buildReplaceSummary({ pairLines: task?.pair_lines, assetCards: task?.asset_cards, mustReplaceOrders: [] }), [task]);

    const promptNodeId = member?.nodes.product || member?.nodes.lapian || "";
    const promptText = useMemo(() => {
        const snapshot: CanvasAgentSnapshot = { projectId: projectId || "", title: project?.title || "", nodes, connections: project?.connections || [], selectedNodeIds: [], viewport: project?.viewport || { x: 0, y: 0, k: 1 } };
        const full = readNodeFullText({ nodeId: promptNodeId, canvasSnapshot: snapshot });
        return String(full?.content || full?.prompt || full?.composerContent || "");
    }, [nodes, project?.connections, project?.title, project?.viewport, projectId, promptNodeId]);

    const videoNode = useMemo(() => (member ? nodes.find((node) => node.id === member.nodes.videoTarget && node.type === CanvasNodeType.Video) : undefined), [member, nodes]);
    const videoSrc = String(videoNode?.metadata?.storageKey ? previewUrlFor(videoNode.metadata.storageKey) || "" : videoNode?.metadata?.content || "");

    const channels = useMemo(() => hubQuickChannels(config), [config]);
    const quick = useMemo(
        () =>
            hubQuickAction({
                localId,
                state: String(task?.state || ""),
                submit: task?.submit,
                cloudTaskId: String(task?.cloud_task_id || ""),
                channels,
                approvedSignature: localId ? readApprovedSignature(localId) : "",
                draft: localId ? readGenDraft(localId) : null,
                busy: working === "submit" || Boolean(phase && phase.phase === "accepted"),
            }),
        [channels, localId, phase, task, working],
    );

    const approve = async () => {
        const draft = localId ? readGenDraft(localId) : null;
        const channel = draft ? channels.find((item) => item.id === draft.channelId) : undefined;
        if (!draft || !channel) {
            message.warning(t("canvas.review.action.approveNeedConfig"));
            return;
        }
        setWorking("approve");
        try {
            await approveHubTask(localId, { 渠道身份: { baseUrl: channel.baseUrl, apiFormat: channel.apiFormat, 渠道名: channel.name }, model: draft.model, mode: draft.mode, params: { duration: draft.duration, size: draft.size, vquality: draft.vquality, audio: draft.audio } });
            message.success(t("canvas.review.toast.approved"));
            await reloadTask();
            onChanged();
        } catch (failure) {
            message.error(errorText(failure));
        } finally {
            setWorking("");
        }
    };

    const rollback = async () => {
        setWorking("rollback");
        try {
            await rollbackHubTask(localId);
            message.success(t("canvas.review.toast.rolledBack"));
            await reloadTask();
            onChanged();
        } catch (failure) {
            message.error(errorText(failure));
        } finally {
            setWorking("");
        }
    };

    const submit = async () => {
        if (!confirming) {
            setConfirming(true);
            return;
        }
        setWorking("submit");
        try {
            const out = await runHubQuickAction(localId, quick, { onPhase: (next, state) => setPhase({ phase: next, state }) });
            const text = hubQuickPhaseText(out.phase, out.state).text;
            if (out.phase === "rejected") message.warning(text);
            else message.success(text);
        } catch (failure) {
            message.error(errorText(failure));
        } finally {
            setWorking("");
            setConfirming(false);
            await reloadTask();
            onChanged();
        }
    };

    const veto = async (slot: ReviewSlotRow) => {
        if (!project || !member || !slot.currentImageId) return;
        const record = vetoRecordForSlot({ member, row: slot, at: new Date().toISOString() });
        updateProject(project.id, { workflowAssetVetoes: upsertAssetVeto(project.workflowAssetVetoes, record) });
        const flushed = await flushProjects("p8-veto");
        if (flushed && flushed.ok === false) message.error(flushed.error || t("canvas.review.toast.saveFailed"));
        else message.success(t("canvas.review.toast.vetoed"));
    };

    if (!member) {
        return createPortal(
            <div className="fixed inset-0 z-[120] flex items-center justify-center bg-black/30" onClick={onClose} data-review-drawer-missing>
                <span className="rounded-lg border px-4 py-3 text-sm" style={{ background: theme.node.panel, borderColor: theme.toolbar.border, color: theme.node.text }} data-review-drawer-missing-text>
                    {t("canvas.review.drawer.instanceMissing")}
                </span>
            </div>,
            document.body,
        );
    }

    const state = String(task?.state || "");

    return createPortal(
        <div className="fixed inset-y-0 right-0 z-[70] flex w-[min(760px,92vw)] flex-col border-l shadow-2xl" style={{ background: theme.node.panel, borderColor: theme.toolbar.border, color: theme.node.text }} data-review-drawer data-review-drawer-panel>
                <header className="flex items-start gap-2 border-b px-4 py-3" style={{ borderColor: theme.toolbar.border }}>
                    <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5">
                            <span className="text-[11px] opacity-40">#{member.instanceIndex}</span>
                            <h3 className="min-w-0 flex-1 truncate text-sm font-semibold" data-review-drawer-title>
                                {row?.materialTitle || instanceId}
                            </h3>
                            {row ? (
                                <span className="shrink-0 rounded px-1.5 py-0.5 text-[11px] font-semibold" style={{ background: `${TONE_COLOR[row.statusTone]}1f`, color: TONE_COLOR[row.statusTone] }} data-review-drawer-status={row.status}>
                                    {t(row.statusI18nKey, { defaultValue: row.statusLabel })}
                                </span>
                            ) : null}
                        </div>
                        <div className="mt-0.5 flex items-center gap-1.5 text-[11px] opacity-45">
                            <span>{row?.templateName || member.templateId}</span>
                            <span>·</span>
                            <span>{localId || t("canvas.review.drawer.noHubShort")}</span>
                            <button type="button" onClick={() => row?.locateNodeId && onFocusNode(row.locateNodeId)} disabled={!row?.locateNodeId} className="ml-auto flex items-center gap-1 rounded px-1 py-0.5 transition hover:opacity-100 disabled:opacity-30" data-review-drawer-locate>
                                <MapPin className="size-3" />
                                {t("canvas.review.drawer.locate")}
                            </button>
                        </div>
                    </div>
                    <button type="button" onClick={onClose} className="grid size-7 shrink-0 place-items-center rounded-md opacity-60 transition hover:bg-black/10 hover:opacity-100 dark:hover:bg-white/10" aria-label={t("canvas.review.drawer.close")} data-review-drawer-close>
                        <X className="size-4" />
                    </button>
                </header>

                <div className="min-h-0 flex-1 overflow-y-auto">
                    {/* ① 两态条：提示词包 ↔ 成片（批准后自动补进） */}
                    <div className="flex gap-2 px-4 py-3" data-review-two-state>
                        <span className="flex-1 rounded-lg border px-2.5 py-1.5 text-[11px] font-medium" style={{ borderColor: theme.toolbar.activeText, background: theme.toolbar.activeBg }} data-review-state="prompt">
                            {t("canvas.review.twoState.prompt")}
                        </span>
                        <span className="flex-1 rounded-lg border px-2.5 py-1.5 text-[11px] font-medium opacity-60" style={{ borderColor: theme.toolbar.border }} data-review-state="video">
                            {videoSrc ? t("canvas.review.twoState.videoReady") : t("canvas.review.twoState.videoPending")}
                        </span>
                    </div>

                    {/* ② 提示词全文（只读；复用 readNodeFullText，不二次实现取文） */}
                    <Section title={t("canvas.review.prompt.title")} theme={theme} extra={<span className="text-[11px] opacity-40">{t("canvas.review.prompt.readonly")}</span>}>
                        <div className="mb-1 flex items-center gap-2 text-[11px] opacity-45">
                            <button type="button" onClick={() => promptNodeId && onFocusNode(promptNodeId)} className="underline decoration-dotted" data-review-prompt-locate>
                                {promptNodeId || t("canvas.review.prompt.noNode")}
                            </button>
                            <span>·</span>
                            <span>{t("canvas.review.prompt.length", { count: promptText.length })}</span>
                        </div>
                        {promptText ? (
                            <div className="max-h-[42vh] overflow-y-auto rounded-lg border px-3 py-2 text-[12px] leading-relaxed" style={{ borderColor: theme.toolbar.border }} data-review-prompt-body>
                                <Streamdown>{promptText}</Streamdown>
                            </div>
                        ) : (
                            <div className="rounded-lg border px-3 py-2 text-xs opacity-50" style={{ borderColor: theme.toolbar.border }} data-review-prompt-body>
                                {t("canvas.review.prompt.empty")}
                            </div>
                        )}
                        {videoSrc ? (
                            <div className="mt-2 flex items-center gap-2">
                                <video src={videoSrc} muted playsInline preload="metadata" className="h-12 w-20 rounded-md object-cover" />
                                <a href={videoSrc} download className="flex items-center gap-1 text-[11px] underline decoration-dotted opacity-70">
                                    <Download className="size-3" />
                                    {t("canvas.review.twoState.download")}
                                </a>
                            </div>
                        ) : null}
                    </Section>

                    {/* ③ 挂图位：当前真值 / 自动复算 / 否决 */}
                    <Section title={t("canvas.review.slots.title")} theme={theme} extra={<span className="text-[11px] opacity-40">{t("canvas.review.slots.count", { count: slotRows.length })}</span>}>
                        {slotRows.length ? (
                            <div className="space-y-2">
                                {slotRows.map((slot) => (
                                    <div key={slot.slotNodeId} className="rounded-lg border px-2.5 py-2" style={{ borderColor: slot.vetoed ? TONE_COLOR.fail : theme.toolbar.border }} data-review-slot={slot.slotNodeId} data-review-slot-vetoed={slot.vetoed ? "true" : "false"}>
                                        <div className="flex items-start gap-2">
                                            <span className="grid size-12 shrink-0 place-items-center overflow-hidden rounded-md border" style={{ borderColor: theme.toolbar.border }}>
                                                {slot.thumbUrl ? <img src={slot.thumbUrl} alt={slot.currentTitle} className="size-full object-cover" /> : <span className="text-[10px] opacity-40">#{slot.order}</span>}
                                            </span>
                                            <div className="min-w-0 flex-1">
                                                <div className="flex items-center gap-1.5">
                                                    <span className="shrink-0 text-[11px] opacity-40">#{slot.order}</span>
                                                    <span className="min-w-0 flex-1 truncate text-xs font-medium" title={slot.name}>
                                                        {slot.name}
                                                    </span>
                                                    <span className="shrink-0 rounded px-1 text-[10px] opacity-50" style={{ background: theme.toolbar.activeBg }}>
                                                        {slot.type}
                                                    </span>
                                                </div>
                                                <div className="mt-0.5 truncate text-[11px] opacity-55" data-review-slot-current>
                                                    {t("canvas.review.slots.current", { title: slot.currentTitle || t("canvas.review.slots.none") })}
                                                </div>
                                                {slot.mustReplace ? (
                                                    <div className="mt-0.5 text-[11px] font-semibold" style={{ color: TONE_COLOR.wait }} data-review-slot-must>
                                                        {t("canvas.review.slots.must")}
                                                    </div>
                                                ) : null}
                                                {slot.reason?.matchedTerms?.length ? (
                                                    <div className="mt-0.5 text-[11px] leading-snug opacity-45">{t("canvas.review.slots.matchedTerms", { terms: slot.reason.matchedTerms.join("、") })}</div>
                                                ) : null}
                                                {slot.reason?.note ? (
                                                    <div className="mt-0.5 break-words text-[11px] leading-snug opacity-50" data-review-slot-note>
                                                        {slot.reason.note}
                                                    </div>
                                                ) : null}
                                                {slot.reason?.alternatives?.length ? (
                                                    <div className="mt-0.5 break-words text-[11px] leading-snug opacity-35" data-review-slot-alternatives>
                                                        {t("canvas.review.slots.alternatives", { list: slot.reason.alternatives.slice(0, 2).map((item) => item.title || item.imageNodeId).join("、") })}
                                                    </div>
                                                ) : null}
                                                {slot.mismatchNote ? (
                                                    <div className="mt-1 flex items-start gap-1 text-[11px] leading-snug" style={{ color: TONE_COLOR.wait }} data-review-slot-mismatch>
                                                        <AlertTriangle className="mt-0.5 size-3 shrink-0" />
                                                        <span>{slot.mismatchNote}</span>
                                                    </div>
                                                ) : null}
                                            </div>
                                            <div className="flex shrink-0 flex-col items-end gap-1">
                                                <button type="button" onClick={() => onFocusNode(slot.slotNodeId)} className="grid size-6 place-items-center rounded-md opacity-55 transition hover:opacity-100" aria-label={t("canvas.review.drawer.locate")}>
                                                    <MapPin className="size-3.5" />
                                                </button>
                                                {slot.vetoed ? (
                                                    <span className="flex items-center gap-0.5 text-[10px]" style={{ color: TONE_COLOR.fail }} data-review-slot-vetoed-label>
                                                        <Ban className="size-3" />
                                                        {t("canvas.review.slots.vetoed")}
                                                    </span>
                                                ) : slot.currentImageId ? (
                                                    <Popconfirm title={t("canvas.review.slots.vetoConfirm")} okText={t("common.confirm")} cancelText={t("common.cancel")} onConfirm={() => void veto(slot)}>
                                                        <button type="button" className="rounded px-1 py-0.5 text-[10px] opacity-55 transition hover:opacity-100" data-review-slot-veto>
                                                            {t("canvas.review.slots.veto")}
                                                        </button>
                                                    </Popconfirm>
                                                ) : null}
                                            </div>
                                        </div>
                                    </div>
                                ))}
                            </div>
                        ) : (
                            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("canvas.review.slots.empty")} className="!my-2" />
                        )}
                    </Section>

                    {/* ④ 置换摘要（只读；未绑 hub 显式空态） */}
                    <Section title={t("canvas.review.replace.title")} theme={theme}>
                        {taskLoading ? (
                            <div className="py-2 text-xs opacity-50">
                                <Spin size="small" /> {t("canvas.review.replace.loading")}
                            </div>
                        ) : replace.available ? (
                            <div className="space-y-1.5" data-review-replace>
                                <div className="flex flex-wrap gap-1.5 text-[11px]">
                                    <span className="rounded px-1.5 py-0.5" style={{ background: `${TONE_COLOR.wait}1f`, color: TONE_COLOR.wait }}>
                                        {t("canvas.review.replace.must", { count: replace.must })}
                                    </span>
                                    <span className="rounded px-1.5 py-0.5" style={{ background: `${TONE_COLOR.done}1f`, color: TONE_COLOR.done }}>
                                        {t("canvas.review.replace.replaced", { count: replace.replaced })}
                                    </span>
                                    <span className="rounded px-1.5 py-0.5 opacity-60" style={{ background: theme.toolbar.activeBg }}>
                                        {t("canvas.review.replace.keep", { count: replace.keepOrigin })}
                                    </span>
                                </div>
                                <div className="max-h-32 overflow-y-auto">
                                    {replace.rows.map((item) => (
                                        <div key={`${item.no}-${item.name}`} className="flex items-center gap-1.5 py-0.5 text-[11px]" data-review-replace-row={item.no}>
                                            <span className="w-6 shrink-0 opacity-40">{item.no}</span>
                                            <span className="min-w-0 flex-1 truncate">{item.name || item.category}</span>
                                            <span className="shrink-0 opacity-60">{item.keepOrigin ? t("canvas.review.replace.keepTag") : item.replaced ? t("canvas.review.replace.replacedTag") : t("canvas.review.replace.mustTag")}</span>
                                        </div>
                                    ))}
                                </div>
                            </div>
                        ) : (
                            <div className="text-xs opacity-50" data-review-replace-empty>
                                {taskError || t("canvas.review.replace.noHub")}
                            </div>
                        )}
                    </Section>

                    {/* ⑤ 自检（生产者 finalSelfcheck 原样：不重判、不拆证据；PASS 折叠） */}
                    <Section
                        title={t("canvas.review.selfcheck.title")}
                        theme={theme}
                        extra={
                            selfcheckSource.length ? (
                                <span className="text-[11px] font-medium" style={{ color: selfcheckFail ? TONE_COLOR.fail : TONE_COLOR.done }} data-review-selfcheck-badge={selfcheckFail}>
                                    {selfcheckFail ? t("canvas.review.selfcheck.failBadge", { count: selfcheckFail, total: selfcheckSource.length }) : t("canvas.review.selfcheck.passBadge", { count: selfcheckSource.length })}
                                </span>
                            ) : null
                        }
                    >
                        {selfcheckSource.length ? (
                            <div className="space-y-1" data-review-selfcheck-items>
                                {selfcheckSource.map((item, index) => {
                                    const verdict = selfcheckVerdictOf(item);
                                    const pass = verdict.trim().toUpperCase() === "PASS";
                                    if (pass && !showPass) return null;
                                    return (
                                        <div key={index} className="rounded-lg border px-2 py-1.5" style={{ borderColor: pass ? theme.toolbar.border : TONE_COLOR.fail }} data-review-selfcheck-item={pass ? "pass" : "fail"}>
                                            <div className="flex items-center gap-1.5 text-[11px] font-medium">
                                                <ShieldCheck className="size-3" style={{ color: pass ? TONE_COLOR.done : TONE_COLOR.fail }} />
                                                <span className="min-w-0 flex-1 truncate">{selfcheckNameOf(item, index)}</span>
                                                <span style={{ color: pass ? TONE_COLOR.done : TONE_COLOR.fail }}>{verdict || "—"}</span>
                                            </div>
                                            {item.证据 ? <div className="mt-0.5 break-words text-[11px] leading-snug opacity-55">{String(item.证据)}</div> : null}
                                            {item.说明 ? <div className="mt-0.5 break-words text-[11px] leading-snug" style={{ color: pass ? undefined : TONE_COLOR.human }}>{String(item.说明)}</div> : null}
                                        </div>
                                    );
                                })}
                                {passCount ? (
                                    <button type="button" onClick={() => setShowPass((value) => !value)} className="w-full rounded px-1 py-1 text-[11px] font-medium opacity-55 transition hover:opacity-100" data-review-selfcheck-toggle>
                                        {showPass ? t("canvas.review.selfcheck.hidePass", { count: passCount }) : t("canvas.review.selfcheck.showPass", { count: passCount })}
                                    </button>
                                ) : null}
                            </div>
                        ) : (
                            <div className="text-xs opacity-50" data-review-selfcheck-empty>
                                {t("canvas.review.selfcheck.none")}
                            </div>
                        )}
                    </Section>

                    {/* ⑥ 自修痕迹（逐轮：改了什么 / 为什么 / 结果；升级码原样） */}
                    <Section title={t("canvas.review.repair.title")} theme={theme} extra={row?.repairBadge ? <span className="text-[11px] opacity-45">{row.repairBadge.text}</span> : null}>
                        {repairRows ? (
                            <div className="space-y-1.5" data-review-repair-timeline>
                                {repairRows.map((item, index) => (
                                    <div key={index} className="rounded-lg border px-2 py-1.5" style={{ borderColor: item.tone === "fail" ? TONE_COLOR.fail : theme.toolbar.border }} data-review-repair-row={item.kind}>
                                        <div className="text-[11px] font-semibold" style={{ color: item.tone === "fail" ? TONE_COLOR.fail : undefined }}>
                                            {item.headline}
                                        </div>
                                        {item.details.map((detail, detailIndex) => (
                                            <div key={detailIndex} className="mt-0.5 break-words text-[11px] leading-snug opacity-55">
                                                {detail}
                                            </div>
                                        ))}
                                    </div>
                                ))}
                            </div>
                        ) : (
                            <div className="text-xs opacity-50" data-review-repair-empty>
                                {t("canvas.review.repair.none")}
                            </div>
                        )}
                    </Section>
                </div>

                {/* 动作位（抽屉底部固定；长正文滚动不推走） */}
                <footer className="border-t px-4 pt-2.5 pb-3" style={{ borderColor: theme.toolbar.border }} data-review-actions>
                    <div className="mb-1.5 flex items-center gap-1.5 text-[11px] opacity-50">
                        <ShieldCheck className="size-3" />
                        <span>{t("canvas.review.action.paidNote")}</span>
                    </div>
                    {confirming ? (
                        <div className="mb-1.5 rounded-lg border px-2.5 py-1.5 text-xs" style={{ borderColor: TONE_COLOR.wait }} data-review-submit-confirm>
                            <div className="font-medium" style={{ color: TONE_COLOR.wait }}>
                                {t("canvas.review.action.submitConfirmTitle", { count: 1, channel: quick.channelName || quick.channelId || t("canvas.review.action.noChannel") })}
                            </div>
                            <div className="mt-0.5 opacity-55">{quick.hint || t("canvas.review.action.submitConfirmHint")}</div>
                            <div className="mt-1 flex justify-end gap-1.5">
                                <button type="button" onClick={() => setConfirming(false)} className="rounded px-2 py-1 text-[11px] font-medium opacity-70">
                                    {t("common.cancel")}
                                </button>
                                <button type="button" onClick={() => void submit()} disabled={working === "submit"} className="rounded px-2 py-1 text-[11px] font-semibold text-white disabled:opacity-40" style={{ background: TONE_COLOR.wait }} data-review-submit-ok>
                                    {t("canvas.review.action.submitConfirmOk")}
                                </button>
                            </div>
                        </div>
                    ) : null}
                    <div className="flex items-center gap-1.5">
                        {state === "awaiting_approval" ? (
                            <button type="button" onClick={() => void approve()} disabled={working === "approve"} className="flex items-center gap-1 rounded-lg px-2.5 py-1.5 text-xs font-semibold text-white disabled:opacity-40" style={{ background: TONE_COLOR.run }} data-review-approve>
                                <Play className="size-3.5" />
                                {t("canvas.review.action.approve")}
                            </button>
                        ) : null}
                        {canShowRollback(state) ? (
                            <Popconfirm title={t("canvas.review.action.rollbackConfirm")} description={t("canvas.review.action.rollbackNote")} okText={t("common.confirm")} cancelText={t("common.cancel")} okButtonProps={{ danger: true }} onConfirm={() => void rollback()}>
                                <button type="button" disabled={working === "rollback"} className="flex items-center gap-1 rounded-lg px-2.5 py-1.5 text-xs font-semibold opacity-80 disabled:opacity-40" style={{ border: `1px solid ${TONE_COLOR.fail}`, color: TONE_COLOR.fail }} data-review-rollback>
                                    <RotateCcw className="size-3.5" />
                                    {t("canvas.review.action.rollback")}
                                </button>
                            </Popconfirm>
                        ) : null}
                        <button
                            type="button"
                            onClick={() => void submit()}
                            disabled={!quick.enabled || working === "submit"}
                            title={quick.enabled ? quick.hint : quick.reason}
                            className="ml-auto flex items-center gap-1 rounded-lg px-3 py-1.5 text-xs font-semibold text-white transition disabled:cursor-not-allowed disabled:opacity-40"
                            style={{ background: TONE_COLOR.fail }}
                            data-review-submit
                        >
                            {working === "submit" ? t("canvas.review.action.submitting") : t("canvas.review.action.submit")}
                        </button>
                    </div>
                    {canShowRollback(state) ? (
                        <div className="mt-1 break-words text-[11px] leading-snug opacity-45" data-review-rollback-note>
                            {t("canvas.review.action.rollbackNote")}
                        </div>
                    ) : null}
                    {!quick.enabled ? (
                        <div className="mt-1 break-words text-[11px] leading-snug opacity-45" data-review-submit-reason>
                            {quick.reason || (state === "awaiting_approval" ? t("canvas.review.action.needApprove") : t("canvas.review.action.unavailable"))}
                        </div>
                    ) : null}
                    {phase && phase.phase !== "rejected" ? (
                        <div className="mt-1 text-[11px] leading-snug" style={{ color: phase.phase === "concluded" || phase.phase === "confirmed" ? TONE_COLOR.done : TONE_COLOR.wait }} data-review-phase={phase.phase}>
                            {hubQuickPhaseText(phase.phase, phase.state).text}
                        </div>
                    ) : null}
                    <div className="mt-1 text-[11px] leading-snug opacity-35" data-review-approve-note>
                        {t("canvas.review.action.approveHint")}
                    </div>
                </footer>
        </div>,
        document.body,
    );
}

export default CanvasPendingReviewTab;
