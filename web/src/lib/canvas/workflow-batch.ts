/**
 * 批次身份记录（P5；P0a §5.1/§5.3 落地）：batchId ↔ 实例 ↔ 节点 ↔ 后端任务的持久关联。
 *
 * 界线（§5.3 判据，本文件据此设计字段）：
 * - **身份（本文件可存）**：谁属于哪个批次、哪个实例用哪套模板/哪组素材、实例的节点骨架 id、
 *   执行期回填的产物 id、hub local_id/bridge_id/修订 digest、拉片与视频的后端任务号。
 * - **状态（本文件禁存）**：跑到哪步/成功失败/是否已批——一律由 `workflow-batch-status.ts`
 *   从节点 `metadata.status` + 后端任务现查 + hub 任务态派生。成员记录里**任何字段都不回答
 *   「现在怎么样」**，所以也没有 `status` 字段可写错。
 * - 落点 = `CanvasProject.workflowBatches`（与 P2 `workflowTemplate`、P4 `workflowInstances`
 *   同一种显式扩展字段）：随项目持久化 + 随 v3 导出同路；不进节点 metadata（C5：metadata 被
 *   通用 ops 浅合并且无白名单），不建第二份 JSON。
 */

import type { CanvasConnection, CanvasNodeData } from "@/types/canvas";
import type { WorkflowInstanceMaterials, WorkflowInstancePlan } from "@/lib/canvas/workflow-instance";
import type { WorkflowInstanceIdentityRecord } from "@/lib/canvas/workflow-instance-run";
import type { WorkflowBatchQueueRecord } from "@/lib/canvas/workflow-batch-queue";
import { stableDigest } from "@/lib/canvas/workflow-template";

export const WORKFLOW_BATCH_SCHEMA_VERSION = 1;

/** 素材组摘要（身份：这批/这个实例用的是哪组素材；改一个字段就换摘要）。 */
export type WorkflowBatchMaterialSummary = {
    sourceVideoStorageKey: string;
    sourceVideoTitle?: string;
    intentChars: number;
    assetCount: number;
    optionalSegments?: string;
    /** 素材组摘要 digest（同一组素材可复算同一值；不含任何运行态）。 */
    digest: string;
};

/** 实例的节点骨架 id 表（身份：哪些节点属于本实例）。 */
export type WorkflowBatchMemberNodes = {
    /** 本实例全部节点 id（P3 确定性重映射产物；含执行期新建的产物节点）。 */
    all: string[];
    sourceVideo: string;
    lapian: string;
    /** 文本组装 Config（正文组装的那个；模板冻结件）。 */
    assemblyConfig?: string;
    /** 执行期回填的本次组装产物（G18 闭合后才有）。 */
    product?: string;
    /** 模板占位产物（已退役；留档供重跑/对账复核）。 */
    retiredPlaceholders: string[];
    /** 视频生成 Config + 视频结果节点（终态产物落点）。 */
    videoConfig?: string;
    videoTarget?: string;
    /** 消费本次产物的下游节点（token/连线两种消费方式）。 */
    downstream: string[];
};

/** 实例 → hub 身份外键（只回答「哪个 hub 任务」，不回答「批没批」）。 */
export type WorkflowBatchMemberHub = {
    localId: string;
    bridgeId: string;
    revisionDigest: string;
    approvalSnapshot?: string;
    boundAt?: string;
};

/** 实例 → 后端任务号（拉片 job / 视频任务；状态一律现查）。 */
export type WorkflowBatchBackendTaskRef = {
    lapianTaskId: string;
    lapianJobId: string;
    videoTaskId?: string;
    videoProvider?: string;
    /**
     * Wan 成片文件任务号（S2，P6）：job（= videoTaskId）→ 成片 taskId 的持久归属点之一。
     * 旧行为只落 localStorage 且仅留 200 条、导出不带（`services/api/video.ts:302-310` 亲读）；
     * P6 起项目级登记表 `CanvasProject.workflowWanFileTasks` 是真源，这里是批次成员侧的**外键**，
     * 让「哪个成片属于哪个实例」随批次记录一起导出。
     */
    wanFileTaskId?: string;
};

export type WorkflowBatchMember = {
    instanceId: string;
    templateId: string;
    /** 1-based 实例序号（与 P3 确定性 id 同源）。 */
    instanceIndex: number;
    materials: WorkflowBatchMaterialSummary;
    nodes: WorkflowBatchMemberNodes;
    hub: WorkflowBatchMemberHub | null;
    backend: WorkflowBatchBackendTaskRef;
    /** 入册时刻（= 画布侧落库那一刻，不是状态时间戳）。 */
    createdAt: string;
};

export type WorkflowBatchRecord = {
    schemaVersion: typeof WORKFLOW_BATCH_SCHEMA_VERSION;
    batchId: string;
    templateId: string;
    title?: string;
    /** 批次创建参数摘要（模板 id + 成员模板 + 素材组摘要集合；批次身份的一部分）。 */
    signature: string;
    createdAt: string;
    updatedAt: string;
    members: WorkflowBatchMember[];
    /**
     * 排队簿记（P6）：并发上限、队列相位、每实例尝试/退避/提交守卫。
     * 与成员同族落在批次记录里（随项目持久化 + 导出同路），但它**只记排队过程**，
     * 实例当前状态仍由 `workflow-batch-status.ts` 派生（C5/§5.3）。
     */
    queue?: WorkflowBatchQueueRecord;
};

/** 素材组摘要：只收「这批用的什么素材」的身份字段。 */
export function summarizeMaterials(materials: WorkflowInstanceMaterials): WorkflowBatchMaterialSummary {
    const intentText = String(materials.intentText ?? "").trim();
    const attaches = Array.isArray(materials.assetAttach) ? materials.assetAttach : [];
    const attachKeys = attaches.map((attach) => `${attach.slotNodeId ?? `order:${attach.order ?? ""}`}=${attach.imageNodeId ?? attach.image?.id ?? ""}`);
    const optionalSegments = String(materials.optionalSegments ?? "").trim();
    const summary = {
        sourceVideoStorageKey: String(materials.sourceVideo?.storageKey || ""),
        sourceVideoTitle: materials.sourceVideo?.title,
        intentChars: intentText.length,
        assetCount: attaches.length,
        optionalSegments: optionalSegments || undefined,
    };
    return { ...summary, digest: stableDigest([summary.sourceVideoStorageKey, summary.sourceVideoTitle || "", summary.intentChars, intentText, summary.assetCount, attachKeys.join(","), optionalSegments].join("|")) };
}

/** 批次短 id（同模板 + 同成员素材 + 同创建时刻 → 同一 id，重放幂等）。 */
export function deriveBatchId(input: { templateId: string; materialDigests: string[]; createdAt: string }): string {
    return `batch-${stableDigest([input.templateId, input.materialDigests.join(","), input.createdAt].join("|")).slice(0, 8)}`;
}

/**
 * 成员入册（纯函数）：从 P3 计划 + 素材 + （可选）P4 身份记录算出身份字段。
 * 未绑 hub 时 `hub` 恒为 null——绝不写「待批/已批」这类状态。
 */
export function buildBatchMember(input: {
    plan: WorkflowInstancePlan;
    materials: WorkflowInstanceMaterials;
    /** P4 身份记录（hub 绑定后才存在；缺省 = 画布侧刚落库）。 */
    identity?: WorkflowInstanceIdentityRecord | null;
    /** 当前画布节点（用于定位视频结果节点与其后端任务号）；缺省只用计划内节点。 */
    nodes?: CanvasNodeData[];
    connections?: CanvasConnection[];
    /** 拉片后端任务号（P3 落库时即可知；缺省取身份记录）。 */
    lapian?: { taskId: string; jobId: string };
    /** Wan 成片映射（S2）：job → 成片 taskId（项目级登记表现查；缺省=不写外键）。 */
    wanFileTasks?: Map<string, string> | Record<string, string>;
    createdAt?: string;
}): WorkflowBatchMember {
    const { plan } = input;
    const identity = input.identity || null;
    const canvasNodes = input.nodes || [];
    const lapianNodeId = plan.lapianNodeId;
    const planNodes = plan.nodes || [];

    const planNode = (id: string | undefined) => (id ? planNodes.find((node) => node.id === id) : undefined);
    const assemblyConfig = planNodes.find((node) => node.type === "config" && String(node.metadata?.generationMode || "") === "text" && String(node.metadata?.lapianSourceId || "") === lapianNodeId);
    const videoConfig = planNodes.find((node) => node.type === "config" && String(node.metadata?.generationMode || "") === "video");
    // 视频结果节点是运行期新建的（不在计划内）：按「视频 Config → 视频节点」的连线定位。
    const videoTarget = videoConfig
        ? (input.connections || [])
              .filter((connection) => connection.fromNodeId === videoConfig.id)
              .map((connection) => canvasNodes.find((node) => node.id === connection.toNodeId))
              .find((node): node is CanvasNodeData => Boolean(node && node.type === "video"))
        : undefined;

    const product = identity?.canvas.outputNodeId || "";
    const retired = identity?.canvas.retiredPlaceholderNodeIds || [];
    const downstream = (input.connections || [])
        .filter((connection) => [product, ...retired].filter(Boolean).includes(connection.fromNodeId))
        .map((connection) => connection.toNodeId)
        .filter((id, index, list) => list.indexOf(id) === index);
    const videoTaskId = String(videoTarget?.metadata?.videoTaskId || "");
    const wanLookup = (job: string): string => {
        if (!job) return "";
        const table = input.wanFileTasks;
        if (!table) return "";
        return table instanceof Map ? String(table.get(job) || "") : String((table as Record<string, string>)[job] || "");
    };

    // all = **现存**节点 id 全集（已退役的模板占位产物在 G18 绑定后被删，单独记在 retiredPlaceholders 里，
    // 不能混进 all：否则每个完成实例都会被判成「节点缺失」）。
    const retiredSet = new Set(retired);
    const all = planNodes.map((node) => node.id).filter((id) => !retiredSet.has(id));
    for (const extra of [product, videoTarget?.id]) if (extra && !all.includes(extra)) all.push(extra);

    const hub: WorkflowBatchMemberHub | null = identity?.hub
        ? {
              localId: identity.hub.localId,
              bridgeId: identity.hub.bridgeId,
              revisionDigest: identity.hub.revision.digest,
              approvalSnapshot: identity.hub.revision.approvalSnapshot,
              boundAt: identity.hub.boundAt,
          }
        : null;

    return {
        instanceId: plan.instanceId,
        templateId: plan.templateId,
        instanceIndex: plan.instanceIndex,
        materials: summarizeMaterials(input.materials),
        nodes: {
            all,
            sourceVideo: plan.sourceVideoNodeId,
            lapian: lapianNodeId,
            assemblyConfig: assemblyConfig?.id,
            product: product || undefined,
            retiredPlaceholders: retired,
            videoConfig: videoConfig?.id,
            videoTarget: videoTarget?.id,
            downstream,
        },
        hub,
        backend: {
            lapianTaskId: String(identity?.canvas.lapianTaskId || input.lapian?.taskId || ""),
            lapianJobId: String(identity?.canvas.lapianJobId || input.lapian?.jobId || ""),
            videoTaskId: videoTaskId || undefined,
            videoProvider: String(videoTarget?.metadata?.videoTaskProvider || "") || undefined,
            wanFileTaskId: wanLookup(videoTaskId) || undefined,
        },
        createdAt: input.createdAt || identity?.createdAt || new Date().toISOString(),
    };
}

/** 批次签名（模板 + 成员素材组摘要；批次身份的一部分，不含运行态）。 */
export function batchSignature(input: { templateId: string; members: WorkflowBatchMember[] }): string {
    const digests = input.members.map((member) => `${member.templateId}#${member.instanceIndex}#${member.materials.digest}`).sort();
    return stableDigest([input.templateId, digests.join(",")].join("|"));
}

export function createWorkflowBatch(input: {
    templateId: string;
    batchId?: string;
    title?: string;
    createdAt?: string;
    members?: WorkflowBatchMember[];
}): WorkflowBatchRecord {
    const createdAt = input.createdAt || new Date().toISOString();
    const members = input.members || [];
    return {
        schemaVersion: WORKFLOW_BATCH_SCHEMA_VERSION,
        batchId: input.batchId || deriveBatchId({ templateId: input.templateId, materialDigests: members.map((member) => member.materials.digest), createdAt }),
        templateId: input.templateId,
        title: input.title,
        signature: batchSignature({ templateId: input.templateId, members }),
        createdAt,
        updatedAt: createdAt,
        members,
    };
}

/** 同批次同实例只留最新一条（身份更新，不是历史日志）；没有该批次则新建。 */
export function upsertBatchMember(records: WorkflowBatchRecord[] | undefined, batch: { templateId: string; batchId: string; title?: string }, member: WorkflowBatchMember, now?: string): WorkflowBatchRecord[] {
    const stamp = now || new Date().toISOString();
    const list = Array.isArray(records) ? records : [];
    const index = list.findIndex((item) => item.batchId === batch.batchId);
    if (index < 0) {
        const created = createWorkflowBatch({ templateId: batch.templateId, batchId: batch.batchId, title: batch.title, createdAt: stamp, members: [member] });
        return [...list, created];
    }
    const current = list[index];
    const members = [...current.members.filter((item) => !(item.instanceId === member.instanceId && item.templateId === member.templateId)), member];
    const next: WorkflowBatchRecord = { ...current, members, signature: batchSignature({ templateId: current.templateId, members }), updatedAt: stamp };
    return [...list.slice(0, index), next, ...list.slice(index + 1)];
}

export function findWorkflowBatch(records: WorkflowBatchRecord[] | undefined, batchId: string): WorkflowBatchRecord | undefined {
    return (records || []).find((item) => item.batchId === batchId);
}

export function findBatchMember(batch: WorkflowBatchRecord | null | undefined, instanceId: string): WorkflowBatchMember | undefined {
    return (batch?.members || []).find((item) => item.instanceId === instanceId);
}

/** 导入侧校验：形如批次记录就透传（未知新版本不静默丢：schemaVersion 原样保留）。 */
export function acceptWorkflowBatches(value: unknown): WorkflowBatchRecord[] | undefined {
    if (!Array.isArray(value)) return undefined;
    const records = value.filter((item): item is WorkflowBatchRecord => {
        if (!item || typeof item !== "object") return false;
        const candidate = item as Partial<WorkflowBatchRecord>;
        return Boolean(candidate.batchId) && Boolean(candidate.templateId) && Array.isArray(candidate.members);
    });
    return records.length ? records : undefined;
}

/**
 * 四层关联完整性（批次 ↔ 实例 ↔ 节点 ↔ hub）：缺哪层就直说是哪层，不返回一个总布尔值。
 * 注意：这里只查**身份**齐不齐，不查状态（能不能批/跑没跑完是 hub/节点现查的事）。
 */
export function batchMemberGaps(member: WorkflowBatchMember | null | undefined): string[] {
    if (!member) return ["没有批次成员记录"];
    const gaps: string[] = [];
    if (!member.instanceId) gaps.push("缺实例 id");
    if (!member.templateId) gaps.push("缺模板 id");
    if (!member.nodes.sourceVideo) gaps.push("缺源视频节点 id");
    if (!member.nodes.lapian) gaps.push("缺拉片节点 id");
    if (!member.nodes.all.length) gaps.push("缺节点骨架 id 表");
    if (!member.backend.lapianTaskId) gaps.push("缺拉片 taskId（后端任务不可查）");
    if (!member.hub?.localId) gaps.push("未绑 hub local_id");
    if (member.hub && !member.hub.bridgeId) gaps.push("缺 bridge_id");
    if (member.hub && !member.hub.revisionDigest) gaps.push("缺修订 digest");
    return gaps;
}

/** 成员可查询的节点 id 全集（批次状态派生的查询输入；去重保序）。 */
export function batchMemberNodeIds(member: WorkflowBatchMember): string[] {
    const ids = [member.nodes.sourceVideo, member.nodes.lapian, member.nodes.assemblyConfig || "", member.nodes.product || "", member.nodes.videoConfig || "", member.nodes.videoTarget || "", ...member.nodes.all, ...member.nodes.downstream];
    return ids.filter(Boolean).filter((id, index, list) => list.indexOf(id) === index);
}

/**
 * 成员**必需**节点 id（骨架，扣掉已退役的模板占位产物）：
 * 占位产物在 G18 绑定后被删除是正常终态，把它算进「必需」会让每个完成实例都变成未知。
 */
export function batchMemberRequiredNodeIds(member: WorkflowBatchMember): string[] {
    const retired = new Set(member.nodes.retiredPlaceholders);
    const ids = [member.nodes.sourceVideo, member.nodes.lapian, member.nodes.assemblyConfig || "", ...member.nodes.all];
    return ids.filter(Boolean).filter((id, index, list) => !retired.has(id) && list.indexOf(id) === index);
}
