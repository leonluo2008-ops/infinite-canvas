/**
 * P8 · 待审批侧栏派生视图（纯逻辑：**无 React / 无 i18n / 不碰 store**）。
 *
 * 依据：
 * - `wanrepro/docs/方案A探索/设计-P0c待审批侧栏原型-20261113.md`（唯一 UI 合同：候选 A = 左栏 280 列表 + 右侧 760 抽屉）。
 * - 卡面红线 25：文本查看复用 `readNodeFullText`、节点定位复用 `onFocusNode`、付费确认复用
 *   `runHubQuickAction`/`hubQuickAction`、队列复用 P6 `runBatchQueue` —— 本文件**不复制**其中任何一份；
 *   只做「把既有实现的输出摆成列表/抽屉要的形状」。
 * - C5 / §5.3：状态一律派生（`workflow-batch-status.ts` 是唯一派生器），身份一律读批次记录
 *   （`workflow-batch.ts`）。本文件既不存状态，也不新增状态源；`REVIEW_STATUS_VIEW` 是
 *   `InstanceStatus` 的 **1:1 文案映射**（10 态全覆盖，禁多禁少）。
 *
 * 为什么单列一层：页面组件只消费这里的行数组，派生逻辑可以 esbuild 打包到 node 侧真跑探针
 * （`scripts/probe-p8-pending-review.mjs` A/B/C/E 段直测本文件）。
 */

import { attachedImageIdOf } from "@/lib/canvas/lapian-assets";
import { CanvasNodeType, type CanvasNodeData } from "@/types/canvas";
import {
    collectAssetLibraryCandidates,
    listInstanceAssetSlots,
    matchInstanceAssetSlots,
    vetoAssetMatch,
    vetoedImagesOfSlot,
    type AssetSlotMatch,
    type WorkflowAssetVetoRecord,
} from "@/lib/canvas/workflow-asset-match";
import type { WorkflowBatchMember, WorkflowBatchRecord } from "@/lib/canvas/workflow-batch";
import { deriveBatchStatus, type BatchStatusView, type InstanceStatus, type InstanceStatusView, type MemberTaskEvidence } from "@/lib/canvas/workflow-batch-status";
import type { BatchQueuePhase } from "@/lib/canvas/workflow-batch-queue";
import { REPAIR_STOP_TEXT, type InstanceRepairIdentity, type RepairStopCode } from "@/lib/canvas/workflow-instance-repair";
import type { WorkflowInstanceMaterials } from "@/lib/canvas/workflow-instance";

/** 徽标语气（UI 按此上色；色值仍走 `canvasThemes`，本层不产出色值）。 */
export type ReviewTone = "wait" | "run" | "human" | "fail" | "unknown" | "done";

/**
 * 派生状态 → 侧栏徽标（**1:1**，卡面 §2.2 逐条给定）。
 *
 * 注意：这里的中文 label 是**合同常量**（任务书逐条写死的措辞），组件层仍必须经 i18n key 渲染
 * （`i18nKey` 字段），所以界面文案不会散落字面量，而纯逻辑层也不必依赖 i18n 运行时。
 */
export const REVIEW_STATUS_VIEW: Record<InstanceStatus, { label: string; tone: ReviewTone; i18nKey: string }> = {
    awaiting_approval: { label: "待批", tone: "wait", i18nKey: "canvas.review.status.awaitingApproval" },
    needs_human: { label: "需人工", tone: "human", i18nKey: "canvas.review.status.needsHuman" },
    failed: { label: "失败", tone: "fail", i18nKey: "canvas.review.status.failed" },
    pending: { label: "待跑", tone: "run", i18nKey: "canvas.review.status.pending" },
    running: { label: "进行中", tone: "run", i18nKey: "canvas.review.status.running" },
    assembled: { label: "已组装", tone: "run", i18nKey: "canvas.review.status.assembled" },
    approved: { label: "已批准", tone: "run", i18nKey: "canvas.review.status.approved" },
    generating: { label: "生成中", tone: "run", i18nKey: "canvas.review.status.generating" },
    done: { label: "已完成", tone: "done", i18nKey: "canvas.review.status.done" },
    unknown: { label: "未知", tone: "unknown", i18nKey: "canvas.review.status.unknown" },
};

/** 单状态 → 徽标视图（未知取值兜底 unknown，绝不抛错）。 */
export function reviewStatusOf(status: InstanceStatus | string): { status: InstanceStatus; label: string; tone: ReviewTone; i18nKey: string } {
    const key = (status in REVIEW_STATUS_VIEW ? status : "unknown") as InstanceStatus;
    return { status: key, ...REVIEW_STATUS_VIEW[key] };
}

/**
 * 抽屉「退回置换」按钮可用性（**与后端守卫逐条对齐**，唯一真源
 * `wanrepro/automation/state.py:245-254` 的 `rollback_to_assets`）：
 *
 * - `awaiting_approval` → 可退。后端只放行这一态（`if task["state"] != "awaiting_approval":
 *   raise StateError(f"退回置换需 awaiting_approval，当前 {task['state']}")`），且退回是唯一
 *   非 forward 的合法迁移（awaiting_approval → awaiting_assets，丢弃本次提示词）；
 * - 其余一切（`approved` / `awaiting_assets` / `generating` / `done` / 空串 / 查不到）→ 不可退：
 *   后端必然抛 StateError，UI 若照旧渲染按钮就是死链。
 *
 * 入参是 **hub 任务 state**（`getHubTask().state`，与 `state.py` 同一取值空间），不是派生
 * `InstanceStatus`。后端用全等比较、不做大小写/空白归一，这里同样不做，逐条对齐。
 * UI 只认这一个判定，**禁自造第二条退回路径**。
 */
export function canShowRollback(state: string | null | undefined): boolean {
    return String(state ?? "") === "awaiting_approval";
}

/**
 * 页签角标 N 口径（本卡裁定，写进注释备查）：
 * `N = 待批(awaiting_approval) + 需人工(needs_human) + 失败(failed)` —— 三者都等人动作；
 * `已批准 / 生成中` **不算**（那是付费停点的事，批次头另给计数）。
 * 派生模型里没有「已退回」独立状态（退回 = 受控回退到 awaiting_assets，批次记录里没有这个字段），
 * 所以口径里也不虚构它。
 */
export function reviewBadgeCount(view: Pick<BatchStatusView, "counts"> | null | undefined): number {
    const counts = view?.counts;
    if (!counts) return 0;
    return Number(counts.awaiting_approval || 0) + Number(counts.needs_human || 0) + Number(counts.failed || 0);
}

/** 自修徽标：`自修 n/2` / `n/2·升级 U<码>` / `无自修`（**不写「0 轮」**）。 */
export type ReviewRepairBadge = { text: string; tone: "run" | "fail" | "none"; rounds: number; max: number; code?: RepairStopCode };

export function repairBadge(repair?: InstanceRepairIdentity | null): ReviewRepairBadge {
    const max = Math.max(1, Number(repair?.maxRounds || 2));
    const rounds = Array.isArray(repair?.rounds) ? repair!.rounds.length : 0;
    const code = repair?.escalation?.code;
    if (code) return { text: rounds > 0 ? `${rounds}/${max}·升级 ${code}` : `升级 ${code}`, tone: "fail", rounds, max, code };
    if (rounds > 0) return { text: `自修 ${rounds}/${max}`, tone: "run", rounds, max };
    return { text: "无自修", tone: "none", rounds: 0, max };
}

/**
 * 自检异常数（生产者结论口径，**不重判**）：
 * ① 成员里带 `finalSelfcheck` 原始条目（完整 `InstanceRepairRecord` 投影时才有）→ 数 `结论 !== "PASS"`；
 * ② 只有投影（`repairIdentityOf` 会把 finalSelfcheck 剥掉）→ 用生产者自己写的 `finalFindings`；
 * ③ 都没有 → `count=null`（**未知就不写 0**，界面显示「自检 —」）。
 */
export function selfcheckFailureCountOf(repair?: InstanceRepairIdentity | null): { count: number | null; total: number | null; source: "findings" | "identity" | "none" } {
    const raw = (repair as (InstanceRepairIdentity & { finalSelfcheck?: unknown[] }) | null | undefined)?.finalSelfcheck;
    if (Array.isArray(raw) && raw.length) {
        const items = raw.map((item) => (item && typeof item === "object" ? (item as Record<string, unknown>) : {}));
        const failing = items.filter((item) => String(item.结论 ?? "").trim().toUpperCase() !== "PASS").length;
        return { count: failing, total: items.length, source: "findings" };
    }
    if (repair && Number.isFinite(Number(repair.finalFindings))) return { count: Math.max(0, Number(repair.finalFindings)), total: null, source: "identity" };
    return { count: null, total: null, source: "none" };
}

/** 自检原始条目（抽屉里逐条渲染用；只读生产者字段，不拆证据字符串、不重判）。 */
export function selfcheckItemsOf(repair?: InstanceRepairIdentity | null): Record<string, unknown>[] {
    const raw = (repair as (InstanceRepairIdentity & { finalSelfcheck?: unknown[] }) | null | undefined)?.finalSelfcheck;
    if (!Array.isArray(raw)) return [];
    return raw.map((item) => (item && typeof item === "object" ? (item as Record<string, unknown>) : {}));
}

/** 自修时间线一行（第 n 轮 / 停止码）。 */
export type RepairTimelineRow = {
    kind: "round" | "stop";
    round: number | null;
    headline: string;
    details: string[];
    tone: "run" | "fail";
};

/**
 * 自修时间线：`null` = 无痕迹（界面写「无自修」）。有轮次就逐轮给「改了什么 / 为什么 / 结果」，
 * 升级给人就给停止码那一行（**失败轮次不隐藏**）。
 */
export function buildRepairTimeline(repair?: InstanceRepairIdentity | null): RepairTimelineRow[] | null {
    if (!repair) return null;
    const rows: RepairTimelineRow[] = [];
    for (const trace of repair.rounds || []) {
        const lines: string[] = [];
        lines.push(trace.changedLines?.length ? `改了第 ${trace.changedLines.join("、")} 行` : "无改动行号记录");
        lines.push(trace.removedTerms?.length ? `删掉违规词「${trace.removedTerms.join("/")}」` : "没有删词记录");
        lines.push(`为什么：${trace.reason || "（执行体没给原因）"}`);
        lines.push(`结果：${trace.findingsBefore} → ${trace.findingsAfter} 项未通过（${trace.findingsAfter === 0 ? "修好" : "仍不过"}）`);
        rows.push({ kind: "round", round: trace.round, headline: `第 ${trace.round} 轮`, details: lines, tone: trace.findingsAfter === 0 ? "run" : "fail" });
    }
    const escalation = repair.escalation;
    if (escalation) {
        rows.push({
            kind: "stop",
            round: escalation.round ?? null,
            headline: `升级 ${escalation.code}（停在第 ${escalation.round ?? 0} 轮）`,
            details: [REPAIR_STOP_TEXT[escalation.code] || "", escalation.detail || ""].filter(Boolean),
            tone: "fail",
        });
    }
    return rows.length ? rows : null;
}

// ─────────────────────────────────────────────── 列表（批次头 + 实例行）

export type ReviewInstanceRow = {
    batchId: string;
    instanceId: string;
    templateId: string;
    instanceIndex: number;
    templateName: string;
    /** 素材标题（源视频名；没有就如实给成员素材键）。 */
    materialTitle: string;
    /**
     * 素材缩略的数据源（只读派生，不新写存储读取）：
     * - `materialStorageKey`：源视频节点的媒体存储键（`resolveMediaUrl` 直接消费）；
     * - `materialUrl`：节点 `metadata.content` 里现成的 dataURL/blob/http URL（没有就空串，绝不造）；
     * - `materialKind`：video → `<video>`，image → `<img>`，none → 图标占位（不渲染空白）。
     */
    materialStorageKey: string;
    materialUrl: string;
    materialKind: "video" | "image" | "none";
    status: InstanceStatus;
    statusLabel: string;
    statusTone: ReviewTone;
    statusI18nKey: string;
    /** 命中的派生规则代号（R1–R10，可复核）。 */
    rule: string;
    reason: string;
    /**
     * hub 现查 state 原文（`getHubTask().state`；未查/查不到 = 空串）。
     * 只用于诚实标注「回置换区」（hub=awaiting_assets），**不当派生状态源**（派生仍走 R1–R10）。
     */
    hubState: string;
    /** 自检异常数（null = 无痕迹/未知，界面不写 0）。 */
    selfcheckFailCount: number | null;
    selfcheckTotal: number | null;
    selfcheckSource: "findings" | "identity" | "none";
    repairBadge: ReviewRepairBadge;
    hubLocalId: string;
    /** 定位节点（null = 成员节点全被删，按钮置灰并写「节点已被删除」）。 */
    locateNodeId: string | null;
    /** 成员骨架里仍在画布上的节点数 / 应有节点数（证据完整度，人话依据）。 */
    nodesPresent: number;
    nodesExpected: number;
    missingEvidence: string[];
};

export type ReviewBatchRow = {
    batchId: string;
    title: string;
    templateId: string;
    templateName: string;
    memberCount: number;
    counts: Record<InstanceStatus, number>;
    /** 计数 chips（只列非零项；口径 = 派生器计数，不另算）。 */
    chips: { status: InstanceStatus; label: string; count: number; tone: ReviewTone; i18nKey: string }[];
    badgeCount: number;
    outcome: BatchStatusView["outcome"];
    queuePhase: BatchQueuePhase | "none";
    queue: { concurrency: number; phase: BatchQueuePhase | "none"; entries: number; attempted: number; awaitingApproval: number; failed: number } | null;
    coverage: BatchStatusView["coverage"];
    instances: ReviewInstanceRow[];
};

export type ReviewListInput = {
    batches?: WorkflowBatchRecord[];
    nodes?: CanvasNodeData[];
    /** 实例 id → hub 当前 state（现查；缺省 = 未查）。 */
    hubStates?: Record<string, string | null>;
    evidenceOf?: (member: WorkflowBatchMember) => MemberTaskEvidence | undefined;
    /** 模板名（缺省回落 templateId，不编名字）。 */
    templateNameOf?: (templateId: string) => string | undefined;
};

function materialTitleOf(member: WorkflowBatchMember, nodeById: Map<string, CanvasNodeData>): string {
    const fromSummary = String(member.materials?.sourceVideoTitle || "").trim();
    if (fromSummary) return fromSummary;
    const node = nodeById.get(member.nodes.sourceVideo);
    const fromNode = String(node?.title || "").trim();
    if (fromNode) return fromNode;
    return member.materials?.sourceVideoStorageKey || member.nodes.sourceVideo || "";
}

/**
 * 素材缩略数据源（只派生、不读取存储）：存储键优先取成员素材摘要，回落源视频节点；
 * URL 只认节点里已经存在的 dataURL/blob/http（`metadata.content` 可能是纯文本，这里不当图用）。
 */
function materialThumbOf(member: WorkflowBatchMember, sourceNode?: CanvasNodeData): { storageKey: string; url: string; kind: "video" | "image" | "none" } {
    const storageKey = String(member.materials?.sourceVideoStorageKey || sourceNode?.metadata?.storageKey || "");
    const rawContent = String(sourceNode?.metadata?.content || "");
    const url = /^(data:(image|video)\/|blob:|https?:\/\/)/i.test(rawContent) ? rawContent : "";
    const mime = String(sourceNode?.metadata?.mimeType || "");
    const kind: "video" | "image" | "none" =
        sourceNode?.type === CanvasNodeType.Video || /^video\//i.test(mime) || storageKey.startsWith("video:")
            ? "video"
            : sourceNode?.type === CanvasNodeType.Image || /^image\//i.test(mime) || storageKey.startsWith("image:")
              ? "image"
              : "none";
    return { storageKey, url, kind };
}

/**
 * 批次列表（只读派生）：每批一行头 + N 个实例行。
 * 实例行数 = 批次成员数（卡面判据：不合并、不省略、不因状态过滤而少给行）。
 */
export function buildReviewList(input: ReviewListInput): ReviewBatchRow[] {
    const batches = Array.isArray(input.batches) ? input.batches : [];
    const nodes = input.nodes || [];
    const nodeById = new Map(nodes.map((node) => [node.id, node]));
    return batches.map((batch) => {
        const view = deriveBatchStatus({ batch, nodes, hubStates: input.hubStates, evidenceOf: input.evidenceOf });
        const instances = batch.members.map((member) => instanceRowOf({ batch, member, view, nodeById, nodes, hubState: String(input.hubStates?.[member.instanceId] ?? ""), templateName: templateNameOf(input, batch.templateId) }));
        const queue = batch.queue;
        return {
            batchId: batch.batchId,
            title: String(batch.title || "").trim() || batch.batchId,
            templateId: batch.templateId,
            templateName: templateNameOf(input, batch.templateId),
            memberCount: batch.members.length,
            counts: view.counts,
            chips: (Object.keys(REVIEW_STATUS_VIEW) as InstanceStatus[])
                .filter((status) => Number(view.counts[status] || 0) > 0)
                .map((status) => ({ status, label: REVIEW_STATUS_VIEW[status].label, count: Number(view.counts[status] || 0), tone: REVIEW_STATUS_VIEW[status].tone, i18nKey: REVIEW_STATUS_VIEW[status].i18nKey })),
            badgeCount: reviewBadgeCount(view),
            outcome: view.outcome,
            queuePhase: queue?.phase || "none",
            queue: queue
                ? {
                      concurrency: Number(queue.concurrency || 0),
                      phase: queue.phase,
                      entries: (queue.entries || []).length,
                      attempted: Number(queue.summary?.attempted || 0),
                      awaitingApproval: Number(queue.summary?.awaitingApproval || 0),
                      failed: (queue.summary?.failed || []).length,
                  }
                : null,
            coverage: view.coverage,
            instances,
        };
    });
}

function templateNameOf(input: ReviewListInput, templateId: string): string {
    const named = String(input.templateNameOf?.(templateId) || "").trim();
    return named || templateId;
}

function instanceRowOf(payload: { batch: WorkflowBatchRecord; member: WorkflowBatchMember; view: BatchStatusView; nodeById: Map<string, CanvasNodeData>; nodes: CanvasNodeData[]; hubState: string; templateName: string }): ReviewInstanceRow {
    const { member } = payload;
    const status = payload.view.members.find((item) => item.instanceId === member.instanceId) || fallbackStatusView(member);
    const statusView = reviewStatusOf(status.status);
    const selfcheck = selfcheckFailureCountOf(member.repair);
    const present = member.nodes.all.filter((id) => payload.nodeById.has(id)).length;
    const material = materialThumbOf(member, payload.nodeById.get(member.nodes.sourceVideo));
    return {
        batchId: payload.batch.batchId,
        instanceId: member.instanceId,
        templateId: member.templateId,
        instanceIndex: member.instanceIndex,
        templateName: payload.templateName,
        materialTitle: materialTitleOf(member, payload.nodeById),
        materialStorageKey: material.storageKey,
        materialUrl: material.url,
        materialKind: material.kind,
        status: statusView.status,
        statusLabel: statusView.label,
        statusTone: statusView.tone,
        statusI18nKey: statusView.i18nKey,
        rule: status.rule,
        reason: status.reason,
        hubState: payload.hubState,
        selfcheckFailCount: selfcheck.count,
        selfcheckTotal: selfcheck.total,
        selfcheckSource: selfcheck.source,
        repairBadge: repairBadge(member.repair),
        hubLocalId: String(member.hub?.localId || ""),
        locateNodeId: pickLocateNodeId(member, payload.nodes),
        nodesPresent: present,
        nodesExpected: member.nodes.all.length,
        missingEvidence: status.missing,
    };
}

/** 派生器没给该成员视图时（不应发生）不抛错：如实标 unknown，绝不冒充成功。 */
function fallbackStatusView(member: WorkflowBatchMember): InstanceStatusView {
    return { instanceId: member.instanceId, templateId: member.templateId, instanceIndex: member.instanceIndex, status: "unknown", rule: "R10", reason: "派生器没有给出该成员的状态视图（证据缺失，不判成功）", missing: [], evidence: {} };
}

/** 本画布没有任何批次（或批次都是空壳）→ 页签空态。 */
export function isReviewTabEmpty(batches?: WorkflowBatchRecord[] | null): boolean {
    const list = Array.isArray(batches) ? batches : [];
    return list.length === 0 || list.every((batch) => !(batch.members || []).length);
}

/** 列表窗口化：每块 50 行（百级实例先渲一块 + 「显示更多」；禁一次性渲染千行）。 */
export function chunkRows<T>(rows: T[] | null | undefined, size = 50): T[][] {
    const list = Array.isArray(rows) ? rows : [];
    const step = Math.max(1, Math.floor(Number(size) || 50));
    const chunks: T[][] = [];
    for (let cursor = 0; cursor < list.length; cursor += step) chunks.push(list.slice(cursor, cursor + step));
    return chunks;
}

// ─────────────────────────────────────────────── 批次头状态筛选（P0c 图2 ②）

/**
 * 批次头状态筛选（P0c 图2 ②「批次名 + 实例数 + 状态筛选」）。
 *
 * 键只是**点击手势**，值仍来自派生器（`InstanceStatus`），除 `replaced` 外一律 1:1 映射回状态，
 * 禁新状态源。
 */
export const REVIEW_FILTER_ALL = "all";

/** 派生状态 → 筛选键（`awaiting_approval → awaiting` 只为读得顺，其余沿用状态原名）。 */
export const REVIEW_STATUS_FILTER: Record<InstanceStatus, string> = {
    awaiting_approval: "awaiting",
    needs_human: "human",
    failed: "fail",
    approved: "approved",
    done: "done",
    pending: "pending",
    running: "running",
    assembled: "assembled",
    generating: "generating",
    unknown: "unknown",
};

export function reviewFilterIdOf(status: InstanceStatus | string): string {
    const key = (status in REVIEW_STATUS_FILTER ? status : "unknown") as InstanceStatus;
    return REVIEW_STATUS_FILTER[key];
}

/**
 * 「回置换区」筛选项（P0c 图2 ② 的「已退回」）。
 *
 * 派生模型里**没有「已退回」这个状态**（退回 = 后端受控回退，落点是 hub 的 `awaiting_assets`，
 * 批次记录里没有该字段）。列表层手里只有「hub 现查 state」，没有 history，所以这里**不虚构
 * 「已退回」状态**，只按 hub 当前 `awaiting_assets` 诚实标注为「回置换区」，并在 UI 上写清
 * 「是否已退回要看实例详情的 hub history」。不新增 GET、不加轮询。
 */
export const REVIEW_FILTER_REPLACED = "replaced";
export const REVIEW_FILTER_REPLACED_HUB_STATE = "awaiting_assets";

/** 按筛选键过滤实例行；未知键 → 空（不静默降级成「全部」，否则筛选失效还看不出来）。 */
export function filterReviewInstances(instances: ReviewInstanceRow[] | null | undefined, filter?: string | null): ReviewInstanceRow[] {
    const list = Array.isArray(instances) ? instances : [];
    const key = String(filter || REVIEW_FILTER_ALL);
    if (key === REVIEW_FILTER_ALL) return list;
    if (key === REVIEW_FILTER_REPLACED) return list.filter((row) => row.hubState === REVIEW_FILTER_REPLACED_HUB_STATE);
    return list.filter((row) => reviewFilterIdOf(row.status) === key);
}

/** 「回置换区」计数（hub 现查 = awaiting_assets 的实例数；0 → UI 不列该项）。 */
export function reviewReplacedCount(instances: ReviewInstanceRow[] | null | undefined): number {
    return filterReviewInstances(instances, REVIEW_FILTER_REPLACED).length;
}

// ─────────────────────────────────────────────── 节点定位 / 素材重建

/**
 * 定位节点取序（P0c §7.2 建议）：产物 → 视频结果 → 组装 Config → 拉片 → 源视频，取第一个仍在画布的。
 * 都没有 → null（界面置灰 + 「节点已被删除」，不静默失败）。
 */
export function pickLocateNodeId(member: WorkflowBatchMember | null | undefined, nodes: CanvasNodeData[]): string | null {
    if (!member) return null;
    const present = new Set((nodes || []).map((node) => node.id));
    // 卡面取序：videoTarget → product → assemblyConfig → lapian → sourceVideo
    const order = [member.nodes.videoTarget, member.nodes.product, member.nodes.assemblyConfig, member.nodes.lapian, member.nodes.sourceVideo, ...(member.nodes.all || [])];
    for (const id of order) {
        if (id && present.has(id)) return id;
    }
    return null;
}

/**
 * 实例内节点 id → 模板节点 id 的反向表。
 *
 * 为什么能反推：P3 的实例 id 是确定性的 `t<模板短id>-<实例序>-<旧id>`（`workflow-template-ids.ts`），
 * 这里只是**读回来**，不新造映射规则。槽表 / 挂图材料都要「模板槽 id」，没有它 replan 会判越槽。
 */
export function memberNodeIdMap(member: WorkflowBatchMember): Record<string, string> {
    const prefix = `t${member.templateId}-${member.instanceIndex}-`;
    const map: Record<string, string> = {};
    for (const id of member.nodes.all || []) {
        if (!id.startsWith(prefix)) continue;
        const templateId = id.slice(prefix.length);
        if (templateId) map[templateId] = id;
    }
    return map;
}

/** 成员素材（重跑用）：源视频 / 意图正文 / 挂图组 / 可选段，全部从成员身份 + 现画布重新拼。 */
export function buildMaterialsFromMember(member: WorkflowBatchMember, nodes: CanvasNodeData[]): WorkflowInstanceMaterials {
    const nodeById = new Map((nodes || []).map((node) => [node.id, node]));
    const sourceNode = nodeById.get(member.nodes.sourceVideo);
    const intentNode = (nodes || []).find((node) => member.nodes.all.includes(node.id) && node.metadata?.lapianIntent === true);
    const intentText = String(intentNode?.metadata?.content ?? intentNode?.metadata?.prompt ?? "").trim();
    const slots = listInstanceAssetSlots({ plan: { lapianNodeId: member.nodes.lapian, nodeIdMap: memberNodeIdMap(member) }, nodes });
    const assetAttach = slots
        .map((slot) => {
            const slotNode = nodeById.get(slot.slotNodeId);
            const imageNodeId = slot.currentImageId || (slotNode ? attachedImageIdOf(slotNode) : "");
            if (!imageNodeId) return null;
            const copy = nodeById.get(imageNodeId);
            // 资产库原图优先：plan 会按确定性 id 重建同一个副本，重放/续跑因此逐字节一致。
            const libraryId = String(copy?.metadata?.assetLibrarySourceId || "");
            const image = (libraryId ? nodeById.get(libraryId) : undefined) || copy;
            if (!image) return null;
            return { slotNodeId: slot.slotTemplateNodeId, order: slot.slotOrder, image, imageNodeId: libraryId || imageNodeId, caption: String(copy?.metadata?.attachedImageDesc || "") };
        })
        .filter((item): item is { slotNodeId: string; order: number; image: CanvasNodeData; imageNodeId: string; caption: string } => Boolean(item));
    return {
        sourceVideo: {
            storageKey: String(member.materials?.sourceVideoStorageKey || sourceNode?.metadata?.storageKey || ""),
            title: String(member.materials?.sourceVideoTitle || sourceNode?.title || ""),
            content: String(sourceNode?.metadata?.content || ""),
            mimeType: String(sourceNode?.metadata?.mimeType || ""),
            bytes: Number(sourceNode?.metadata?.bytes || 0) || undefined,
            durationMs: Number(sourceNode?.metadata?.durationMs || 0) || undefined,
            naturalWidth: Number(sourceNode?.metadata?.naturalWidth || 0) || undefined,
            naturalHeight: Number(sourceNode?.metadata?.naturalHeight || 0) || undefined,
        },
        ...(intentText ? { intentText } : {}),
        ...(assetAttach.length ? { assetAttach } : {}),
        ...(String(member.materials?.optionalSegments || "").trim() ? { optionalSegments: String(member.materials.optionalSegments) } : {}),
    };
}

// ─────────────────────────────────────────────── 挂图位（理由 / 否决 / 复算）

export type ReviewSlotRow = {
    slotNodeId: string;
    slotTemplateNodeId: string;
    slotKey: string;
    order: number;
    name: string;
    type: string;
    desc: string;
    /** 缩略图数据源（storageKey 预览优先，其次节点 content；都没有 → 空）。 */
    thumbNodeId: string;
    thumbUrl?: string;
    /** 当前挂图（**真值**）。 */
    currentImageId: string;
    currentTitle: string;
    /** 当前挂图背后的资产库原图 id（否决判据用它，副本 id 也认）。 */
    currentLibraryImageId: string;
    /** 自动匹配复算首选（无候选 → 空）。 */
    automaticImageId: string;
    automaticTitle: string;
    /** 复算首选 === 当前挂图（true 才敢说「就是自动选的」）。 */
    pickedMatchesAttached: boolean;
    /** 当前挂图被所有者否决过。 */
    vetoed: boolean;
    /** hub「必换」标记（取不到 → undefined，不猜）。 */
    mustReplace?: boolean;
    /** 匹配理由（`matchInstanceAssetSlots` 原样；当前挂图与复算不符时 UI 必须如实并列）。 */
    reason?: AssetSlotMatch["reason"];
    /** 人话对照句（不符时给；相符时为空）。 */
    mismatchNote: string;
};

export function buildSlotRows(input: { member: WorkflowBatchMember; nodes: CanvasNodeData[]; vetoes?: WorkflowAssetVetoRecord[]; mustReplaceOrders?: number[] }): ReviewSlotRow[] {
    const nodes = input.nodes || [];
    const nodeById = new Map(nodes.map((node) => [node.id, node]));
    const planRef = { lapianNodeId: input.member.nodes.lapian, nodeIdMap: memberNodeIdMap(input.member) };
    const slots = listInstanceAssetSlots({ plan: planRef, nodes });
    if (!slots.length) return [];
    const library = collectAssetLibraryCandidates({ nodes, lapianNodeId: input.member.nodes.lapian });
    const match = matchInstanceAssetSlots({
        slots,
        candidates: library.candidates,
        vetoes: input.vetoes,
        instanceId: input.member.instanceId,
        templateId: input.member.templateId,
        instanceIndex: input.member.instanceIndex,
    });
    const mustReplace = new Set(input.mustReplaceOrders || []);
    return slots.map((slot) => {
        const slotMatch = match.slots.find((item) => item.slotNodeId === slot.slotNodeId);
        const current = slot.currentImageId ? nodeById.get(slot.currentImageId) : undefined;
        const libraryId = String(current?.metadata?.assetLibrarySourceId || slot.currentImageId || "");
        const vetoedImages = vetoedImagesOfSlot(input.vetoes, { instanceId: input.member.instanceId, slotNodeId: slot.slotNodeId });
        const vetoed = Boolean(slot.currentImageId) && (vetoedImages.includes(slot.currentImageId) || (Boolean(libraryId) && vetoedImages.includes(libraryId)));
        const automaticImageId = String(slotMatch?.imageNodeId || "");
        const pickedMatchesAttached = Boolean(automaticImageId) && Boolean(slot.currentImageId) && (automaticImageId === libraryId || automaticImageId === slot.currentImageId);
        const currentTitle = String(current?.title || slot.currentImageId || "");
        const automaticTitle = String(slotMatch?.reason?.candidateTitle || "");
        const mismatchNote =
            slot.currentImageId && automaticImageId && !pickedMatchesAttached
                ? `当前挂「${currentTitle}」（人工/历史），自动首选「${automaticTitle || automaticImageId}」`
                : "";
        return {
            slotNodeId: slot.slotNodeId,
            slotTemplateNodeId: slot.slotTemplateNodeId,
            slotKey: slot.slotKey,
            order: Number(slot.slotOrder || 0),
            name: slot.name || slot.slotNodeId,
            type: slot.type,
            desc: slot.desc,
            thumbNodeId: slot.currentImageId || slot.slotNodeId,
            thumbUrl: String(current?.metadata?.storageKey || current?.metadata?.content || ""),
            currentImageId: slot.currentImageId,
            currentTitle,
            currentLibraryImageId: libraryId,
            automaticImageId,
            automaticTitle,
            pickedMatchesAttached,
            vetoed,
            ...(mustReplace.size ? { mustReplace: mustReplace.has(Number(slot.slotOrder || 0)) } : {}),
            ...(slotMatch ? { reason: slotMatch.reason } : {}),
            mismatchNote,
        };
    });
}

/** 置换摘要（只读）：从 hub 详情 `pair_lines` / `asset_cards` 取「必换/已换/按原样」计数。 */
export type ReviewReplaceSummary = {
    available: boolean;
    /** true = 未绑定 hub 任务（无置换摘要，显式空态）。 */
    reason?: string;
    must: number;
    replaced: number;
    keepOrigin: number;
    rows: { no: string; name: string; category: string; replaced: boolean; keepOrigin: boolean; must: boolean }[];
};

export function buildReplaceSummary(input: { pairLines?: { no: number | string; desc?: string; asset?: string; img?: string; name?: string }[]; assetCards?: { n: number; 短名?: string; 类别?: string; 已传图?: string[]; 决策?: string }[]; mustReplaceOrders?: number[] }): ReviewReplaceSummary {
    const cards = Array.isArray(input.assetCards) ? input.assetCards : [];
    const lines = Array.isArray(input.pairLines) ? input.pairLines : [];
    if (!cards.length && !lines.length) return { available: false, must: 0, replaced: 0, keepOrigin: 0, rows: [] };
    const mustReplace = new Set(input.mustReplaceOrders || []);
    const rows = (cards.length
        ? cards.map((card) => ({ no: String(card.n), name: String(card.短名 || ""), category: String(card.类别 || ""), attached: Boolean((card.已传图 || []).length), keep: String(card.决策 || "") === "keep_origin" || String(card.决策 || "") === "dismissed" }))
        : lines.map((line) => ({ no: String(line.no), name: String(line.name || line.asset || line.desc || ""), category: "", attached: Boolean(line.img), keep: false }))
    ).map((row) => ({
        no: row.no,
        name: row.name,
        category: row.category,
        keepOrigin: row.keep,
        replaced: !row.keep && row.attached,
        must: mustReplace.has(Number(row.no)) || (!row.keep && !row.attached),
    }));
    return {
        available: true,
        must: rows.filter((row) => row.must).length,
        replaced: rows.filter((row) => row.replaced).length,
        keepOrigin: rows.filter((row) => row.keepOrigin).length,
        rows,
    };
}

/** 成员的可挂图节点（抽屉缩略图用；节点不存在 → undefined，界面不画假图）。 */
export function imageNodeOf(nodes: CanvasNodeData[], nodeId: string | undefined): CanvasNodeData | undefined {
    if (!nodeId) return undefined;
    return (nodes || []).find((node) => node.id === nodeId && node.type === CanvasNodeType.Image);
}

/**
 * 「否决这张图」→ 否决记录（复用 `vetoAssetMatch` 单实现；本函数只是把侧栏行翻译成它要的形状）。
 *
 * 拒的是**当前挂的那张图**（界面语义：你看到的就是你拒的），id 取资产库原图 id（副本也认，
 * `vetoedImagesOfSlot` 用的是同一口径）。
 */
export function vetoRecordForSlot(input: { member: WorkflowBatchMember; row: ReviewSlotRow; at: string; note?: string }): WorkflowAssetVetoRecord {
    const imageId = String(input.row.currentLibraryImageId || input.row.currentImageId || "").trim();
    const match: AssetSlotMatch = {
        slotNodeId: input.row.slotNodeId,
        slotTemplateNodeId: input.row.slotTemplateNodeId,
        slotKey: input.row.slotKey,
        slotOrder: input.row.order,
        slotName: input.row.name,
        slotType: input.row.type,
        status: "matched",
        imageNodeId: imageId,
        reason:
            input.row.reason || {
                rule: "owner_pinned",
                source: "侧栏逐槽否决",
                matchedTerms: [],
                score: 0,
                candidateTitle: input.row.currentTitle,
                alternatives: [],
                vetoedImageIds: [],
                note: "所有者逐槽否决当前挂图",
            },
    };
    return vetoAssetMatch({
        match,
        templateId: input.member.templateId,
        instanceId: input.member.instanceId,
        instanceIndex: input.member.instanceIndex,
        vetoedAt: input.at,
        note: input.note,
        imageTitleById: imageId ? { [imageId]: input.row.currentTitle } : undefined,
    });
}
