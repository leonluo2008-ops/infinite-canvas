/**
 * 批次状态派生（P5；C5「状态真源=派生」+ B5 判据落地）。
 *
 * 输入只有三类**现查证据**，本文件不读也不写任何「状态」字段：
 * ① 画布节点 `metadata.status`（生成态：idle/loading/success/error，见 `types/canvas.ts`）；
 * ② 后端任务状态（拉片 job / 视频任务；经既有 `generation_get_status` 与任务查询取得，红线 25 禁新写轮询器）；
 * ③ hub 任务当前 state（`GET /api/hub/task/{id}`，审批真源，本文件只消费）。
 *
 * 派生规则表（顺序即优先级，命中即返回；`rule` 字段把命中行带出去，可复核）：
 *
 * | 规则 | 判据 | 结论 | 为什么 |
 * |---|---|---|---|
 * | R1 | 任一后端任务 failed / 任一成员节点 status=error / hub state=failed | failed | 失败证据优先，不被上游 success 掩盖 |
 * | R2 | 成员必需节点（骨架，去掉已退役占位产物）有缺失 | unknown | 快照缺节点=证据不足；B5「缺失/未知≠成功」 |
 * | R3 | 产物节点存在 + 终态后端成功（视频链要求视频任务 succeeded；非视频链要求产物 success） | done | 只有终态产物成功才算完成 |
 * | R4 | hub state=awaiting_approval | awaiting_approval | 停待批（P4 正常终点）；组装 Config success 也不改判完成 |
 * | R5 | hub state=approved | approved | 批了还没开始生成 |
 * | R6 | hub state=generating 或视频任务 running/queued | generating | 已批准后在出片 |
 * | R7 | 任一成员节点 status=loading 或任一后端任务 running/queued | running | 正在跑（含「Config success 但视频仍 running」） |
 * | R8 | 产物存在且成功 + 无 hub + 无视频任务 | assembled | 画布侧完成、未绑 hub（P4 canvas_ready） |
 * | R9 | 骨架齐、无产物、无任务、无 hub | pending | 入册未起跑 |
 * | R10 | 以上都不成立 | unknown | 兜底：证据不足一律不判成功 |
 *
 * 关于 Lapian（B5）：拉片节点**没有通用 status**（`metadata.status` 不参与拉片进度判定），
 * 拉片进度/成败只看其后端任务（`backend.lapianTaskId/jobId`）。拉片任务状态查不到时记为
 * 缺失证据（进 `missing`、拉低覆盖），但**不把已有下游产物的实例打回 unknown**：
 * 下游产物存在本身就是「拉片跑完了」的硬证据；只有在下游也无证据时才落到 R10 unknown。
 */

import type { CanvasNodeData } from "@/types/canvas";
import { batchMemberRequiredNodeIds, type WorkflowBatchMember, type WorkflowBatchRecord } from "@/lib/canvas/workflow-batch";

/** 实例派生状态（全部是算出来的，项目里没有任何字段存它）。 */
export type InstanceStatus = "pending" | "running" | "assembled" | "awaiting_approval" | "needs_human" | "approved" | "generating" | "done" | "failed" | "unknown";

export const INSTANCE_STATUSES: InstanceStatus[] = ["pending", "running", "assembled", "awaiting_approval", "needs_human", "approved", "generating", "done", "failed", "unknown"];

export type BackendTaskStatus = "queued" | "running" | "succeeded" | "failed" | "unknown";

/** 派生输入里的后端任务证据（来自 `workflow-batch-query.ts`，键都是身份 id）。 */
export type MemberTaskEvidence = {
    /** 后端任务 id → 状态（拉片 taskId / 视频 videoTaskId）。 */
    byTaskId?: Map<string, BackendTaskStatus>;
    /** 画布节点 id → 状态（`generation_get_status` 的 nodeIds 口径）。 */
    byNodeId?: Map<string, BackendTaskStatus>;
    /** 查询覆盖率（不足时派生结论必须显式部分未知）。 */
    coverage?: { complete: boolean; missingIds: string[]; truncated?: boolean };
};

export type InstanceStatusView = {
    instanceId: string;
    templateId: string;
    instanceIndex: number;
    status: InstanceStatus;
    /** 命中的规则代号（R1–R10）。 */
    rule: string;
    reason: string;
    /** 缺失证据（空 = 证据齐）。 */
    missing: string[];
    evidence: Record<string, unknown>;
};

const HUB_RUNNING_STATES = new Set(["generating", "generated", "submitting", "submitted", "running", "in_progress"]);
const HUB_DONE_STATES = new Set(["done", "finished", "completed", "success", "succeeded"]);

function normalizeNodeStatus(value: unknown): "idle" | "loading" | "success" | "error" | "unknown" {
    const text = String(value ?? "");
    if (text === "idle") return "idle";
    if (text === "loading") return "loading";
    if (text === "success") return "success";
    if (text === "error") return "error";
    return "unknown";
}

/** 单个实例状态派生（纯函数）。 */
export function deriveInstanceStatus(input: {
    member: WorkflowBatchMember;
    /** 当前画布节点快照（缺省/部分 = 证据不足）。 */
    nodes?: CanvasNodeData[];
    /** hub 任务当前 state：字符串=查到了；null=查了拿不到；undefined=没查。 */
    hubState?: string | null;
    evidence?: MemberTaskEvidence;
}): InstanceStatusView {
    const { member } = input;
    const nodeById = new Map((input.nodes || []).map((node) => [node.id, node]));
    const required = batchMemberRequiredNodeIds(member);
    const missingNodeIds = required.filter((id) => !nodeById.has(id));
    const hubState = input.hubState === undefined || input.hubState === null ? input.hubState : String(input.hubState).trim().toLowerCase();
    const taskStatus = (taskId: string | undefined): BackendTaskStatus => (taskId ? input.evidence?.byTaskId?.get(taskId) || "unknown" : "unknown");

    const nodesOfMember = member.nodes.all.map((id) => nodeById.get(id)).filter((node): node is CanvasNodeData => Boolean(node));
    const nodeStatuses = nodesOfMember.map((node) => ({ id: node.id, status: normalizeNodeStatus(node.metadata?.status) }));
    const product = member.nodes.product ? nodeById.get(member.nodes.product) : undefined;
    const videoTarget = member.nodes.videoTarget ? nodeById.get(member.nodes.videoTarget) : undefined;
    const videoConfigNode = member.nodes.videoConfig ? nodeById.get(member.nodes.videoConfig) : undefined;

    // 视频任务状态：优先按 videoTaskId 查后端；退化到节点自身的 status（生成态）。
    const videoTaskStatus: BackendTaskStatus = member.backend.videoTaskId
        ? taskStatus(member.backend.videoTaskId)
        : videoTarget || videoConfigNode
          ? (() => {
                const status = normalizeNodeStatus(videoTarget?.metadata?.status ?? videoConfigNode?.metadata?.status);
                return status === "error" ? "failed" : status === "loading" ? "running" : status === "success" ? "succeeded" : "unknown";
            })()
          : "unknown";
    const lapianTaskStatus = taskStatus(member.backend.lapianTaskId);

    const missing: string[] = [];
    if (missingNodeIds.length) missing.push(`节点快照缺 ${missingNodeIds.length} 个：${missingNodeIds.slice(0, 5).join(",")}`);
    if (member.backend.lapianTaskId && lapianTaskStatus === "unknown") missing.push("拉片任务状态未知（Lapian 无通用 status，按后端任务判定）");
    /**
     * 视频任务「缺」有两种：① 声明了 videoTaskId 但查不到（B5 判据：缺失≠成功，判未知）；
     * ② 还没起生成（节点 idle/无任务号）——那是「待批/已批未生成」，不是未知。
     */
    const videoTaskDeclaredButMissing = Boolean(member.backend.videoTaskId) && videoTaskStatus === "unknown";
    if (videoTaskDeclaredButMissing) missing.push("视频后端任务状态未知");
    if (input.evidence?.coverage && !input.evidence.coverage.complete) missing.push(`任务查询未全覆盖（缺 ${input.evidence.coverage.missingIds.length} 个节点）`);

    const baseEvidence = {
        hubState: hubState ?? (input.hubState === undefined ? "未查询" : "查不到"),
        nodeStatuses,
        lapianTaskStatus,
        videoTaskStatus,
        missingNodeIds,
    };
    const view = (status: InstanceStatus, rule: string, reason: string, extra: Record<string, unknown> = {}): InstanceStatusView => ({
        instanceId: member.instanceId,
        templateId: member.templateId,
        instanceIndex: member.instanceIndex,
        status,
        rule,
        reason,
        missing,
        evidence: { ...baseEvidence, ...extra },
    });

    // R1 失败优先
    const failedNodes = nodeStatuses.filter((item) => item.status === "error");
    if (failedNodes.length || videoTaskStatus === "failed" || hubState === "failed") {
        const why = failedNodes.length ? `节点生成失败：${failedNodes.map((item) => item.id).join(",")}` : videoTaskStatus === "failed" ? "视频后端任务 failed" : "hub 任务 failed";
        return view("failed", "R1", why, { failedNodeIds: failedNodes.map((item) => item.id) });
    }

    // R2 必需节点缺失 → 证据不足（不判成功）
    if (missingNodeIds.length) return view("unknown", "R2", `成员必需节点在快照里缺 ${missingNodeIds.length} 个：无法确认状态（缺失≠成功）`);

    // R3 完成：只有终态成功才算
    const productOk = Boolean(product) && (normalizeNodeStatus(product?.metadata?.status) === "success" || Boolean(String(product?.metadata?.content || "").trim()));
    const isVideoChain = Boolean(member.nodes.videoConfig || member.backend.videoTaskId);
    if (productOk && isVideoChain && videoTaskStatus === "succeeded") {
        return view("done", "R3", "产物就位 + 视频后端任务 succeeded：实例完成", { productNodeId: member.nodes.product, videoTaskId: member.backend.videoTaskId });
    }
    if (productOk && !isVideoChain) return view("done", "R3", "产物就位 + 非视频链：实例完成", { productNodeId: member.nodes.product });
    if (productOk && videoTaskDeclaredButMissing) {
        // 组装/Config success 但视频任务查不到 → 明确未知，绝不当完成
        return view("unknown", "R3", "组装产物已就位但视频后端任务状态查不到：不判完成（B5：缺失≠成功）", { productNodeId: member.nodes.product, videoTaskId: member.backend.videoTaskId });
    }

    // R4a 自修升级给人（P7b/D11）：自修 2 轮仍 FAIL → 需人工，**不是**「待批可批准」
    // 判据=hub 停在待审批 + 成员上带自修升级痕迹（痕迹是事件记录，状态仍是这里算出来的）。
    const repairEscalated = member.repair?.outcome === "escalated";
    if (repairEscalated && hubState === "awaiting_approval") {
        return view("needs_human", "R4a", `自修 ${member.repair?.rounds.length ?? 0} 轮仍 FAIL（${member.repair?.escalation?.code || "U5"}：${member.repair?.escalation?.detail || ""}）：需人工处理，不自动豁免、不进下一步`, { repairEscalation: member.repair?.escalation });
    }

    // R4 待批（P4 正常终点；Config success 不改判完成）
    if (hubState === "awaiting_approval") return view("awaiting_approval", "R4", "hub 任务在待审批：等人审，不是完成", { configSucceeded: nodeStatuses.some((item) => item.status === "success") });

    // R5 已批准
    if (hubState === "approved") {
        if (videoTaskStatus === "running" || videoTaskStatus === "queued") return view("generating", "R6", `已批准且视频任务在跑（${videoTaskStatus}）`);
        return view("approved", "R5", "hub 已批准，尚未开始生成");
    }

    // R6 生成中（hub 侧的生成态）
    if (HUB_RUNNING_STATES.has(String(hubState))) return view("generating", "R6", `hub 侧生成中（hub=${hubState}，视频任务=${videoTaskStatus}）`);

    // R7 运行中（含「组装 Config success 但视频仍 running/loading」）
    const loadingNodes = nodeStatuses.filter((item) => item.status === "loading");
    const runningTask = input.evidence?.byNodeId ? [...input.evidence.byNodeId.values()].some((status) => status === "running" || status === "queued") : false;
    if (loadingNodes.length || runningTask) return view("running", "R7", loadingNodes.length ? `节点生成中：${loadingNodes.map((item) => item.id).join(",")}` : "后端任务 running/queued");

    // R8 画布侧完成、未绑 hub
    if (productOk && !member.hub) return view("assembled", "R8", "组装产物就位但未绑 hub（P4 canvas_ready）", { productNodeId: member.nodes.product });

    // R9 入册未起跑
    if (!product && !member.hub && !member.backend.videoTaskId && videoTaskStatus === "unknown") {
        return view("pending", "R9", "骨架齐但没有任何运行态证据：入册未起跑");
    }

    // R10 兜底
    return view("unknown", "R10", `证据不足，不判成功（hub=${hubState ?? "未查询"}，视频任务=${videoTaskStatus}）`);
}

export type BatchStatusView = {
    batchId: string;
    templateId: string;
    title?: string;
    total: number;
    members: InstanceStatusView[];
    counts: Record<InstanceStatus, number>;
    /** 覆盖率：不足即「部分未知」，不算成功。 */
    coverage: { complete: boolean; partialUnknownMembers: string[]; missingEvidence: string[] };
    /** 批次结论：任何失败→failed；有人待处理→needs_human；全完成→done；有未知→partial_unknown；否则 in_progress。 */
    outcome: "done" | "in_progress" | "failed" | "needs_human" | "partial_unknown";
};

/** 批次聚合视图（纯函数）：N 个实例各自状态 + 计数汇总 + 覆盖率。 */
export function deriveBatchStatus(input: {
    batch: WorkflowBatchRecord;
    nodes?: CanvasNodeData[];
    /** 实例 id → hub 当前 state（现查；缺省 = 未查）。 */
    hubStates?: Record<string, string | null>;
    /** 实例 id → 任务证据（来自批次成员分组查询）。 */
    evidenceOf?: (member: WorkflowBatchMember) => MemberTaskEvidence | undefined;
}): BatchStatusView {
    const members = input.batch.members.map((member) =>
        deriveInstanceStatus({ member, nodes: input.nodes, hubState: input.hubStates ? (input.hubStates[member.instanceId] ?? undefined) : undefined, evidence: input.evidenceOf?.(member) }),
    );
    const counts = INSTANCE_STATUSES.reduce<Record<InstanceStatus, number>>((acc, status) => ({ ...acc, [status]: members.filter((member) => member.status === status).length }), {} as Record<InstanceStatus, number>);
    const partialUnknownMembers = members.filter((member) => member.status === "unknown" || member.missing.length > 0).map((member) => member.instanceId);
    const missingEvidence = members.flatMap((member) => member.missing.map((item) => `${member.instanceId}: ${item}`));
    const complete = members.length > 0 && partialUnknownMembers.length === 0;
    const outcome: BatchStatusView["outcome"] = counts.failed > 0 ? "failed" : counts.needs_human > 0 ? "needs_human" : !complete ? "partial_unknown" : counts.done === members.length ? "done" : "in_progress";
    return {
        batchId: input.batch.batchId,
        templateId: input.batch.templateId,
        title: input.batch.title,
        total: members.length,
        members,
        counts,
        coverage: { complete, partialUnknownMembers, missingEvidence },
        outcome,
    };
}
