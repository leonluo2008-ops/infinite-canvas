/**
 * P4 · 单实例端到端编排器（拉片完成 → 五槽代入校验 → 提示词组装 → 自检 → 停在待批）。
 *
 * 依据：
 * - `wanrepro/docs/方案A探索/设计-P0a批量代办设计裁决-20261113.md`
 *   §四（hub 只消费落地：入口=桥侧 ingest；「建到任务」≠「可审批」；审批对象=四件套同一实例同一修订）、
 *   §5.3（身份只回答「谁是谁」，状态一律现查——本文件不落任何「现在怎么样」）。
 * - 审计 G18（输出身份陷阱）：Config 每次生成都新建输出节点（`project.tsx:3560` `rootId = nanoid()`），
 *   模板里那条占位产物与下游 `@[node:…]` token/连线**不会**自动迁移 —— 本文件用「执行期回填的产物 id」
 *   做显式重绑（`buildProductBindingOps`），禁止「取最新节点」式猜测。
 * - 审计 B2/G16（审批与自检真源）：自检**不另写评分器**，一律走 hub 既有 `POST /api/hub/task/{id}/prompt`
 *   （写正文 → 重算审批快照 → 重跑自检 → 返回 items，`hub_api.save_prompt_text`）；审批只用既有
 *   approve / rollback / revoke，本文件不建第二套状态机。
 * - 红线 25：组装正文用既有 `buildNodeGenerationContext`（与页面生成链同一个函数）；可选段投影用既有
 *   `/api/render/<taskId>/<template>?optional=`（`lapian-projection.ts`，与手工「拉片→生成」同源）。
 *
 * 边界：本文件只做**单实例**编排（P5 批次/队列、P8 侧栏 UI 不在此）；不发起任何付费视频创建
 * （`runGeneration` 只会以 `mode:"text"` 被调用一次，结果里 `paidVideoCreations` 恒为 0）。
 */

import { buildNodeGenerationContext } from "@/components/canvas/canvas-node-generation";
import { readNodeFullText } from "@/lib/agent/agent-node-text";
import { CanvasNodeType, type CanvasConnection, type CanvasNodeData } from "@/types/canvas";

import type { CanvasAgentOp } from "@/lib/canvas/canvas-agent-ops";
import { lapianOptionalLetters, renderLapianProjection, type LapianProjectionFetch } from "@/lib/canvas/lapian-projection";
import { buildBatchMember, type WorkflowBatchMember } from "@/lib/canvas/workflow-batch";
import { classifyChannelFailure, type ChannelFailureClass } from "@/lib/canvas/workflow-channel-failure";
import { frozenSnapshotOf, repairIdentityOf, repairNeeded, runInstanceSelfRepair, type FrozenSnapshot, type InstanceRepairRecord, type InstanceRepairer } from "@/lib/canvas/workflow-instance-repair";
import { stableDigest } from "@/lib/canvas/workflow-template";
import { templateInstanceConnectionId } from "@/lib/canvas/workflow-template-ids";
import type { WorkflowInstancePlan, WorkflowInstanceMaterials } from "@/lib/canvas/workflow-instance";
import type { WorkflowAssetVetoRecord } from "@/lib/canvas/workflow-asset-match";

/** 实例运行错误（预期内失败：一律带 stage，不靠解析文案）。 */
export class WorkflowInstanceRunError extends Error {
    readonly stage: "assembly_plan" | "slot_verify" | "generation" | "product_binding" | "hub_bind";
    readonly details: Record<string, unknown>;
    constructor(stage: WorkflowInstanceRunError["stage"], message: string, details: Record<string, unknown> = {}) {
        super(message);
        this.name = "WorkflowInstanceRunError";
        this.stage = stage;
        this.details = details;
    }
}

// ─────────────────────────────────────────────── 组装计划（纯函数）

/** 一个下游消费者是怎么消费占位产物的（token / 连线；探针按此断言重绑覆盖面）。 */
export type AssemblyDownstreamConsumer = {
    nodeId: string;
    via: ("token" | "connection")[];
};

export type InstanceAssemblyPlan = {
    lapianNodeId: string;
    /** 文本组装 Config（模板冻结件；实例里由 `lapianSourceId` 定位，全实例唯一）。 */
    assemblyConfigId: string;
    /** 组装模板串（composerContent，含五槽 token）——发给生成链的 prompt 入参就是它。 */
    composerContent: string;
    /** **本次实际发送正文**（既有 buildNodeGenerationContext 展开 token 后的口径）。 */
    assemblyPrompt: string;
    assemblyPromptDigest: string;
    /** 模板占位产物（G18：执行期必须被本次产物替换）。 */
    staleOutputNodeIds: string[];
    /** 消费占位产物的下游节点。 */
    downstream: AssemblyDownstreamConsumer[];
};

/** 组装 Config 定位：全实例唯一（0 → 缺组装节点；>1 → 有歧义，拒绝猜）。 */
function resolveAssemblyConfig(plan: WorkflowInstancePlan, nodes: CanvasNodeData[]): CanvasNodeData {
    const candidates = nodes.filter((node) => node.type === CanvasNodeType.Config && String(node.metadata?.generationMode || "") === "text" && String(node.metadata?.lapianSourceId || "") === plan.lapianNodeId);
    if (candidates.length !== 1) {
        throw new WorkflowInstanceRunError("assembly_plan", candidates.length ? `本实例有 ${candidates.length} 个文本组装 Config，无法确定唯一组装节点` : "本实例没有文本组装 Config（generationMode=text 且 lapianSourceId=拉片节点）", { lapianNodeId: plan.lapianNodeId, candidates: candidates.map((node) => node.id) });
    }
    return candidates[0];
}

/** 下游引用扫描：token（composerContent/prompt/content）与连线两种方式都算消费。 */
function collectDownstream(nodes: CanvasNodeData[], connections: CanvasConnection[], staleIds: string[], excludeIds: string[]): AssemblyDownstreamConsumer[] {
    const stale = new Set(staleIds);
    const exclude = new Set(excludeIds);
    const found = new Map<string, Set<"token" | "connection">>();
    for (const node of nodes) {
        if (exclude.has(node.id)) continue;
        const texts = [String(node.metadata?.composerContent || ""), String(node.metadata?.prompt || ""), String(node.metadata?.content || "")];
        if (texts.some((text) => [...stale].some((id) => text.includes(`@[node:${id}]`)))) {
            if (!found.has(node.id)) found.set(node.id, new Set());
            found.get(node.id)!.add("token");
        }
    }
    for (const connection of connections) {
        if (!stale.has(connection.fromNodeId) || exclude.has(connection.toNodeId)) continue;
        if (!found.has(connection.toNodeId)) found.set(connection.toNodeId, new Set());
        found.get(connection.toNodeId)!.add("connection");
    }
    return [...found.entries()].map(([nodeId, via]) => ({ nodeId, via: (["token", "connection"] as const).filter((kind) => via.has(kind)) }));
}

/**
 * 组装计划：定位组装 Config → 算本次正文 → 找出模板占位产物与其下游。
 * 纯函数（不写盘）：任何缺失都在此抛出，调用方零写入。
 */
export function planInstanceAssembly(input: { plan: WorkflowInstancePlan; nodes: CanvasNodeData[]; connections: CanvasConnection[] }): InstanceAssemblyPlan {
    const configNode = resolveAssemblyConfig(input.plan, input.nodes);
    const composerContent = String(configNode.metadata?.composerContent || "");
    if (!composerContent.trim()) throw new WorkflowInstanceRunError("assembly_plan", `组装 Config ${configNode.id} 没有 composerContent（模板冻结件缺失）`, { configNodeId: configNode.id });
    const context = buildNodeGenerationContext(configNode.id, input.nodes, input.connections, composerContent);
    const assemblyPrompt = context.prompt.trim();
    if (!assemblyPrompt) throw new WorkflowInstanceRunError("assembly_plan", "组装正文为空（token 展开后没有任何正文）", { configNodeId: configNode.id });

    // 模板占位产物：组装 Config 的出边指向的 Text 节点（模板自足性要求它存在，G18 陷阱正源于此）
    const staleOutputNodeIds = input.connections
        .filter((connection) => connection.fromNodeId === configNode.id)
        .map((connection) => input.nodes.find((node) => node.id === connection.toNodeId))
        .filter((node): node is CanvasNodeData => Boolean(node && node.type === CanvasNodeType.Text && node.id !== configNode.id))
        .map((node) => node.id);
    const downstream = collectDownstream(input.nodes, input.connections, staleOutputNodeIds, [configNode.id]);
    return {
        lapianNodeId: input.plan.lapianNodeId,
        assemblyConfigId: configNode.id,
        composerContent,
        assemblyPrompt,
        assemblyPromptDigest: stableDigest(assemblyPrompt),
        staleOutputNodeIds,
        downstream,
    };
}

// ─────────────────────────────────────────────── G18：产物绑定（纯函数 → ops）

/** 把 stale 产物引用整体替换成本次产物 id（token 与连线两条路都换）。 */
function rebindText(text: string, staleIds: string[], productNodeId: string): string {
    return staleIds.reduce((result, staleId) => result.split(`@[node:${staleId}]`).join(`@[node:${productNodeId}]`), String(text || ""));
}

/**
 * 产物绑定 ops（G18 判据落地）：
 * 1. 每个下游节点的 token 引用 → 本次产物 id（composerContent / prompt / content 三处）；
 * 2. 原来「占位产物 → 下游」的连线改成「本次产物 → 下游」（确定性 id，重放幂等）；
 * 3. 删除占位产物节点（连同它自己的边）——旧产物零残留，下游只可能消费本次新产物。
 */
export function buildProductBindingOps(input: { plan: WorkflowInstancePlan; assembly: InstanceAssemblyPlan; productNodeId: string; nodes: CanvasNodeData[]; connections: CanvasConnection[] }): CanvasAgentOp[] {
    const { plan, assembly, productNodeId } = input;
    const staleIds = assembly.staleOutputNodeIds;
    if (!staleIds.length) return [];
    const ops: CanvasAgentOp[] = [];
    for (const consumer of assembly.downstream) {
        const node = input.nodes.find((item) => item.id === consumer.nodeId);
        if (!node) continue;
        const metadata: Record<string, unknown> = {};
        for (const field of ["composerContent", "prompt", "content"] as const) {
            const current = String(node.metadata?.[field] || "");
            if (current.includes("@[node:")) metadata[field] = rebindText(current, staleIds, productNodeId);
        }
        if (consumer.via.includes("connection")) {
            for (const staleId of staleIds) {
                if (!input.connections.some((connection) => connection.fromNodeId === staleId && connection.toNodeId === consumer.nodeId)) continue;
                ops.push({
                    type: "connect_nodes",
                    id: templateInstanceConnectionId(plan.templateId, plan.instanceIndex, `product:${productNodeId}->${consumer.nodeId}`),
                    fromNodeId: productNodeId,
                    toNodeId: consumer.nodeId,
                });
            }
        }
        if (Object.keys(metadata).length) ops.push({ type: "update_node", id: consumer.nodeId, metadata });
    }
    ops.push({ type: "delete_node", ids: staleIds });
    return ops;
}

// ─────────────────────────────────────────────── 五槽代入校验（纯函数）

export type InstanceSlotCheck = {
    slotKind: "sourceVideo" | "intentText" | "assetAttach" | "generationParams" | "optionalSegments";
    ok: boolean;
    detail: string;
    evidence: Record<string, unknown>;
};

function nodeById(nodes: CanvasNodeData[], id: string): CanvasNodeData | undefined {
    return nodes.find((node) => node.id === id);
}

/**
 * 五槽代入校验：槽值必须真的落进节点/正文，而不是只改了实例计划。
 * 判据全部可用字符串/节点字段复核（探针逐条打印 evidence），失败即整轮失败（不降级）。
 */
export function verifySlotSubstitution(input: {
    plan: WorkflowInstancePlan;
    materials: WorkflowInstanceMaterials;
    nodes: CanvasNodeData[];
    connections: CanvasConnection[];
    assembly: InstanceAssemblyPlan;
    /** 本次拉片正文（投影后；缺省用拉片节点现正文）。 */
    lapianText: string;
    /**
     * 所有者否决记录（P7a）：否决过的匹配**不得再进请求**。这里做请求前的最后一道核对
     * （挂图材料 / 槽上已挂图 / 本次计划三处任一命中否决 → 槽③判 FAIL，不静默放行）。
     */
    vetoes?: WorkflowAssetVetoRecord[];
}): InstanceSlotCheck[] {
    const { plan, materials, nodes, connections, assembly } = input;
    const checks: InstanceSlotCheck[] = [];
    const push = (slotKind: InstanceSlotCheck["slotKind"], ok: boolean, detail: string, evidence: Record<string, unknown>) => checks.push({ slotKind, ok, detail, evidence });

    // ① 源视频：本次 storageKey 落进源视频节点，旧运行态零残留
    const sourceNode = nodeById(nodes, plan.sourceVideoNodeId);
    const expectedKey = String(materials.sourceVideo?.storageKey || "");
    const sourceOk = Boolean(sourceNode) && String(sourceNode?.metadata?.storageKey || "") === expectedKey && !String(sourceNode?.metadata?.taskId || "");
    push("sourceVideo", sourceOk, sourceOk ? "源视频节点写的是本次 storageKey，旧运行态已清" : "源视频节点 storageKey 不是本次素材（或残留旧 taskId）", { nodeId: plan.sourceVideoNodeId, storageKey: sourceNode?.metadata?.storageKey, expectedKey, taskId: sourceNode?.metadata?.taskId });

    // ② 意图正文：意图节点 content 与本次意图一致（缺省保留占位符 = 允许）
    const intentText = String(materials.intentText ?? "").trim();
    const intentIds = plan.slotWrites.filter((write) => write.slotKind === "intentText").map((write) => write.instanceNodeId);
    const intentOk = !intentText || intentIds.every((id) => String(nodeById(nodes, id)?.metadata?.content || "") === intentText);
    push("intentText", intentOk, intentOk ? (intentText ? "意图正文已写入意图节点" : "本次未给意图正文（保留模板占位符）") : "意图节点正文与本次意图不一致", { intentIds, intentText, actual: intentIds.map((id) => String(nodeById(nodes, id)?.metadata?.content || "")) });

    // ③ 挂图：扁平真源 + assetSlot 镜像 + 对照图 + 清单 token + 请求参考图序
    const attaches = plan.assetAttaches;
    const context = buildNodeGenerationContext(assembly.assemblyConfigId, nodes, connections, assembly.composerContent);
    const referenceImageIds = context.referenceImages.map((image) => String(image.id));
    const attachProblems: string[] = [];
    for (const attach of attaches) {
        const slotNode = nodeById(nodes, attach.slotNodeId);
        const graftNode = nodeById(nodes, attach.graftNodeId);
        if (!slotNode || String(slotNode.metadata?.attachedImageId || "") !== attach.imageNodeId) attachProblems.push(`资产位 ${attach.slotNodeId} 扁平真源不是本次挂图`);
        if (slotNode && String(slotNode.metadata?.assetSlot?.attachedImageId || "") !== attach.imageNodeId) attachProblems.push(`资产位 ${attach.slotNodeId} assetSlot 镜像不是本次挂图`);
        if (!graftNode || String(graftNode.metadata?.assetGraftForSlotId || "") !== attach.slotNodeId) attachProblems.push(`资产位 ${attach.slotNodeId} 缺挂图对照节点`);
        if (!String(assembly.composerContent).includes(`@[node:${attach.imageNodeId}]`)) attachProblems.push(`组装串里没有本次挂图 token ${attach.imageNodeId}`);
    }
    const vetoedInRequest: string[] = [];
    {
        const vetoes = input.vetoes || [];
        const vetoHit = (slotNodeId: string, imageNodeId: string) => {
            if (!imageNodeId) return false;
            const ids = new Set([imageNodeId]);
            const librarySource = String(nodes.find((node) => node.id === imageNodeId)?.metadata?.assetLibrarySourceId || "");
            if (librarySource) ids.add(librarySource);
            return vetoes.some((veto) => veto.instanceId === plan.instanceId && veto.slotNodeId === slotNodeId && ids.has(veto.rejectedImageId));
        };
        for (const attach of attaches) if (vetoHit(attach.slotNodeId, attach.imageNodeId)) vetoedInRequest.push(`计划挂图 ${attach.slotNodeId}←${attach.imageNodeId}`);
        for (const attach of materials.assetAttach || []) {
            const slotNodeId = plan.nodeIdMap[String(attach.slotNodeId || "")] || String(attach.slotNodeId || "");
            const imageId = String(attach.imageNodeId || attach.image?.id || "");
            if (slotNodeId && imageId && vetoHit(slotNodeId, imageId)) vetoedInRequest.push(`挂图材料 ${slotNodeId}←${imageId}`);
        }
        for (const slotNode of nodes.filter((node) => String(node.metadata?.assetSlotSourceId || "") === plan.lapianNodeId)) {
            const attached = String(slotNode.metadata?.attachedImageId || "");
            if (attached && vetoHit(slotNode.id, attached)) vetoedInRequest.push(`槽上已挂 ${slotNode.id}←${attached}`);
        }
        if (vetoedInRequest.length) attachProblems.push(`否决过的匹配又出现在请求里（否决不复用被破坏）：${vetoedInRequest.slice(0, 4).join("；")}`);
    }
    if (plan.assetAttachPendingDerivation && (materials.assetAttach || []).length) attachProblems.push("模板槽③为派生槽：挂图未派生即视为未生效（P4 派生范围外）");
    // 图序的真源 = 组装串里 token 的**出现序**（buildComposerGenerationContext 就是按 token 序取用），
    // 不是 materials.assetAttach 的传参序——传参序变了但槽序没变时，请求图序必须不变。
    const tokenOrder = [...String(assembly.composerContent).matchAll(/@\[node:([^\]]+)\]/g)].map((match) => match[1]);
    const attachImageIds = attaches.map((attach) => String(attach.imageNodeId));
    const slotImageOrder = tokenOrder.filter((id) => attachImageIds.includes(id));
    const actualAttachedOrder = referenceImageIds.filter((id) => attachImageIds.includes(id));
    const orderOk = attaches.length === 0 || JSON.stringify(actualAttachedOrder) === JSON.stringify(slotImageOrder);
    if (!orderOk) attachProblems.push(`实际请求图序 ${actualAttachedOrder.join(",") || "（空）"} 与槽序 ${slotImageOrder.join(",")} 不一致`);
    push("assetAttach", attachProblems.length === 0, attachProblems.length ? attachProblems.slice(0, 4).join("；") : `挂图 ${attaches.length} 个：扁平/镜像/对照/token 全一致，请求参考图序与槽序一致（${actualAttachedOrder.length} 张）`, { attaches: attaches.map((attach) => ({ slotNodeId: attach.slotNodeId, imageNodeId: attach.imageNodeId })), referenceImageIds, actualAttachedOrder, problems: attachProblems });

    // ④ 生成参数：槽④写值必须真的在目标节点 metadata 上
    const paramProblems: string[] = [];
    for (const override of materials.generationParams || []) {
        const node = nodeById(nodes, plan.nodeIdMap[override.nodeId] || override.nodeId);
        for (const [field, value] of Object.entries(override.values || {})) {
            if (!node) paramProblems.push(`参数目标 ${override.nodeId} 不在实例里`);
            else if (String((node.metadata as Record<string, unknown>)?.[field] ?? "") !== String(value)) paramProblems.push(`${override.nodeId}.${field} 期望 ${value} 实际 ${(node.metadata as Record<string, unknown>)?.[field]}`);
        }
    }
    push("generationParams", paramProblems.length === 0, paramProblems.length ? paramProblems.slice(0, 4).join("；") : "槽④覆盖值已落在目标节点", { overrides: materials.generationParams || [] });

    // ⑤ 可选段：节点元数据 + 本次正文两处都要变（只改 metadata = 未生效）
    const expectedOptional = lapianOptionalLetters(materials.optionalSegments);
    const lapianNode = nodeById(nodes, plan.lapianNodeId);
    const actualOptional = lapianOptionalLetters(String(lapianNode?.metadata?.lapianOptional || ""));
    const lapianTextOk = Boolean(String(input.lapianText || "").trim()) && String(assembly.assemblyPrompt || "").includes(String(input.lapianText).trim());
    const optionalProblems: string[] = [];
    if (actualOptional !== expectedOptional) optionalProblems.push(`节点 lapianOptional=${actualOptional || "（空）"} 期望 ${expectedOptional || "（空）"}`);
    if (!lapianTextOk) optionalProblems.push("本次正文里没有本次拉片正文（可选段不会生效）");
    push("optionalSegments", optionalProblems.length === 0, optionalProblems.length ? optionalProblems.join("；") : `可选段 ${expectedOptional || "（全关）"} 已同时体现在节点元数据与本次正文`, { expectedOptional, actualOptional, lapianTextChars: String(input.lapianText || "").length });

    return checks;
}

// ─────────────────────────────────────────────── hub 身份（B2/P1c 并入）

export type WorkflowInstanceHubClient = {
    /** 桥侧 ingest（POST /api/hub/ingest，幂等键=视频 sha）。 */
    ingest: (input: { blob: Blob; filename: string; intent: string }) => Promise<{ localId: string; bridgeId: string; result?: string; hitExisting?: boolean; videoName?: string }>;
    /** 资产上传（POST /api/asset/upload + `X-Task-Id` → 本地引用，不碰图床）。 */
    uploadAsset: (input: { localId: string; blob: Blob; filename: string }) => Promise<{ ref: string; source?: string }>;
    getTask: (localId: string) => Promise<{ state: string; bridgeId?: string; approvalSnapshot?: string }>;
    /** 正文保存（POST /api/hub/task/{id}/prompt：写正文 → 重算快照 → 重跑自检，G16 唯一真源）。 */
    saveRevision: (input: { localId: string; text: string; expectText?: string | null }) => Promise<{ state?: string; unchanged?: boolean; promptEditedAt?: string; approvalSnapshot?: string; selfcheck?: unknown[]; note?: string }>;
    approve: (localId: string) => Promise<unknown>;
    rollback: (localId: string) => Promise<unknown>;
    revoke: (localId: string) => Promise<unknown>;
    /** 生产：等 hub 自己那套拆解/置换跑到 awaiting_approval；缺省 = 要求当前已在待审批。 */
    waitForApproval?: (localId: string, opts?: { timeoutMs?: number; intervalMs?: number }) => Promise<string>;
};

/** 实例→hub 身份记录（§5.1：随项目持久化；只回答谁是谁，不回答现在怎么样）。 */
export type WorkflowInstanceIdentityRecord = {
    schemaVersion: 1;
    instanceId: string;
    templateId: string;
    instanceIndex: number;
    createdAt: string;
    canvas: {
        /** 本实例全部节点 id（确定性重映射产物）。 */
        nodeIds: string[];
        /** 本次组装产物（执行期新建的 Text 节点，替换模板占位产物）。 */
        outputNodeId: string;
        retiredPlaceholderNodeIds: string[];
        lapianTaskId: string;
        lapianJobId: string;
        videoStorageKey: string;
        runToken: string;
        assemblyPromptDigest: string;
        assemblyPromptChars: number;
    };
    hub: {
        localId: string;
        bridgeId: string;
        boundAt: string;
        revision: { digest: string; chars: number; approvalSnapshot: string; promptEditedAt?: string };
        assets: { slotNodeId: string; imageNodeId: string; ref: string; source?: string }[];
    } | null;
};

/** 修订变更决策（「编辑/换图/参数变化须撤销旧批准」的挂钩点；不新造审批状态机）。 */
export function planRevisionChange(input: { record: WorkflowInstanceIdentityRecord | null; nextTextDigest: string; hubState: string }): { action: "none" | "rewrite" | "revoke_required"; reason: string } {
    const record = input.record;
    if (!record || !record.hub) return { action: "none", reason: "实例还没绑定 hub 任务：直接保存正文" };
    if (record.hub.revision.digest === input.nextTextDigest) return { action: "none", reason: "正文与已绑定修订逐字节一致：不重写、不重批" };
    const state = String(input.hubState || "");
    if (state === "approved" || state === "generating" || state === "done") {
        return { action: "revoke_required", reason: `hub 任务已批准/在生成/已完成（${state}）：改正文前必须先撤销批准（revoke），否则快照 stale 拿旧批准发新正文` };
    }
    return { action: "rewrite", reason: `hub 任务处于 ${state || "未知"}：直接保存正文并重算快照` };
}

/** 读图片节点字节（槽③资产上传用）：storageKey 优先，其次 data:/blob: content。 */
async function readAssetBlob(node: CanvasNodeData, deps: { getBlob?: (storageKey: string) => Promise<Blob | null>; fetch?: typeof fetch }): Promise<Blob | null> {
    const storageKey = String(node.metadata?.storageKey || "");
    if (storageKey && deps.getBlob) {
        const blob = await deps.getBlob(storageKey);
        if (blob) return blob;
    }
    const content = String(node.metadata?.content || "");
    if (content.startsWith("data:") || content.startsWith("blob:")) {
        const fetchImpl = deps.fetch || globalThis.fetch;
        if (!fetchImpl) return null;
        return fetchImpl(content).then((response) => response.blob());
    }
    return null;
}

export type InstanceHubBinding = {
    localId: string;
    bridgeId: string;
    boundAt: string;
    state: string;
    revision: { digest: string; chars: number; approvalSnapshot: string; promptEditedAt?: string };
    assets: { slotNodeId: string; imageNodeId: string; ref: string; source?: string }[];
    selfcheck: unknown[];
    /** 自检未通过项数（如实记录：不阻断批准，人工终审在所有者）。 */
    selfcheckFailures: number;
};

/**
 * 实例 → hub 身份绑定：ingest 建任务（同一 video sha 幂等）→ 资产带 `X-Task-Id` 上传（本地引用）
 * → 正文保存（**自带**快照重算 + 自检重跑，G16 复用）→ 记录「实例↔local_id↔bridge_id↔修订」。
 * 前置：hub 任务必须已到 awaiting_approval（保存正文的唯一安全区）；生产由 `waitForApproval` 等到，
 * 探针在隔离实例里用夹具/推进到该状态，绝不改 hub 合同。
 */
export async function bindInstanceToHub(input: {
    plan: WorkflowInstancePlan;
    materials: WorkflowInstanceMaterials;
    assembly: InstanceAssemblyPlan;
    assemblyText: string;
    productNodeId: string;
    retiredPlaceholderNodeIds: string[];
    lapian: { taskId: string; jobId: string };
    runToken: string;
    nodes: CanvasNodeData[];
}, deps: { hub: WorkflowInstanceHubClient; getBlob?: (storageKey: string) => Promise<Blob | null>; fetch?: typeof fetch; now?: () => string }): Promise<InstanceHubBinding> {
    const { hub } = deps;
    const now = deps.now || (() => new Date().toISOString());
    const storageKey = String(input.materials.sourceVideo?.storageKey || "");
    const videoBlob = deps.getBlob ? await deps.getBlob(storageKey) : null;
    if (!videoBlob) throw new WorkflowInstanceRunError("hub_bind", `源视频字节取不回（storageKey=${storageKey}）：不建 hub 影子任务`, { storageKey });

    const ingested = await hub.ingest({ blob: videoBlob, filename: String(input.materials.sourceVideo?.title || "canvas-video.mp4"), intent: String(input.materials.intentText ?? "").trim() });
    const localId = String(ingested.localId || "").trim();
    const bridgeId = String(ingested.bridgeId || "").trim();
    if (!localId || !bridgeId) throw new WorkflowInstanceRunError("hub_bind", `ingest 没给出完整身份（local_id=${localId || "空"} bridge_id=${bridgeId || "空"}）：拒绝只造影子壳`, ingested);

    // 资产：按槽③顺序上传（X-Task-Id → 本地引用），顺序进身份记录（挂图改序可复核）
    const assets: InstanceHubBinding["assets"] = [];
    for (const attach of input.plan.assetAttaches) {
        const imageNode = input.nodes.find((node) => node.id === attach.imageNodeId);
        if (!imageNode) continue;
        const blob = await readAssetBlob(imageNode, { getBlob: deps.getBlob, fetch: deps.fetch });
        if (!blob) throw new WorkflowInstanceRunError("hub_bind", `槽③挂图 ${attach.imageNodeId} 字节取不回：资产不入 hub，拒绝半份身份`, { imageNodeId: attach.imageNodeId });
        const uploaded = await hub.uploadAsset({ localId, blob, filename: `${attach.slotNodeId}.png` });
        if (!String(uploaded.ref || "").trim()) throw new WorkflowInstanceRunError("hub_bind", `挂图 ${attach.imageNodeId} 上传没回引用`, { imageNodeId: attach.imageNodeId });
        assets.push({ slotNodeId: attach.slotNodeId, imageNodeId: attach.imageNodeId, ref: uploaded.ref, source: uploaded.source });
    }

    // 正文保存的唯一安全区：awaiting_approval（不在该状态 → 等；等不到 → 如实失败，不硬写）
    let state = String((await hub.getTask(localId)).state || "");
    if (state !== "awaiting_approval") {
        if (!hub.waitForApproval) throw new WorkflowInstanceRunError("hub_bind", `hub 任务 ${localId} 当前 ${state}，未到待审批：正文/自检/快照必须在 awaiting_approval 落（未提供等待器）`, { localId, state });
        state = await hub.waitForApproval(localId);
        if (state !== "awaiting_approval") throw new WorkflowInstanceRunError("hub_bind", `等待后 hub 任务 ${localId} 仍是 ${state}，未到待审批`, { localId, state });
    }

    const saved = await hub.saveRevision({ localId, text: input.assemblyText });
    const approvalSnapshot = String(saved.approvalSnapshot || "");
    if (!approvalSnapshot) throw new WorkflowInstanceRunError("hub_bind", "保存正文后 hub 没回审批快照（快照是批准的唯一绑定物）", { localId, saved });
    const selfcheck = Array.isArray(saved.selfcheck) ? saved.selfcheck : [];
    const selfcheckFailures = selfcheck.filter((item) => String((item as Record<string, unknown>)?.结论 ?? "") !== "PASS").length;
    return {
        localId,
        bridgeId,
        boundAt: now(),
        state: String(saved.state || state),
        revision: { digest: stableDigest(input.assemblyText), chars: input.assemblyText.length, approvalSnapshot, promptEditedAt: saved.promptEditedAt },
        assets,
        selfcheck,
        selfcheckFailures,
    };
}

export function buildIdentityRecord(input: {
    plan: WorkflowInstancePlan;
    assembly: InstanceAssemblyPlan;
    assemblyText: string;
    productNodeId: string;
    retiredPlaceholderNodeIds: string[];
    lapian: { taskId: string; jobId: string };
    runToken: string;
    binding: InstanceHubBinding | null;
    now?: () => string;
}): WorkflowInstanceIdentityRecord {
    const now = input.now || (() => new Date().toISOString());
    return {
        schemaVersion: 1,
        instanceId: input.plan.instanceId,
        templateId: input.plan.templateId,
        instanceIndex: input.plan.instanceIndex,
        createdAt: now(),
        canvas: {
            nodeIds: input.plan.nodes.map((node) => node.id),
            outputNodeId: input.productNodeId,
            retiredPlaceholderNodeIds: input.retiredPlaceholderNodeIds,
            lapianTaskId: input.lapian.taskId,
            lapianJobId: input.lapian.jobId,
            videoStorageKey: String(input.plan.nodes.find((node) => node.id === input.plan.sourceVideoNodeId)?.metadata?.storageKey || ""),
            runToken: input.runToken,
            assemblyPromptDigest: input.assembly.assemblyPromptDigest,
            assemblyPromptChars: input.assembly.assemblyPrompt.length,
        },
        hub: input.binding
            ? { localId: input.binding.localId, bridgeId: input.binding.bridgeId, boundAt: input.binding.boundAt, revision: input.binding.revision, assets: input.binding.assets }
            : null,
    };
}

// ─────────────────────────────────────────────── 编排器

/** 落盘关键节点（P5 flush）：实例入册（画布侧落库）、停在待批、自修升级给人。 */
export type InstanceFlushStage = "instance_persisted" | "awaiting_approval" | "needs_human";

/** flush 结果（失败如实记，绝不静默）。 */
export type InstanceFlushOutcome = { stage: InstanceFlushStage; ok: boolean; error?: string };

export type InstanceRunDeps = {
    nodes: () => CanvasNodeData[];
    connections: () => CanvasConnection[];
    applyOps: (ops: CanvasAgentOp[]) => void | Promise<void>;
    /**
     * 复用现有生成链的单次执行（页面 = `generateNodeRef`/`handleGenerateNode`；探针 = 按同一
     * `buildNodeGenerationContext` 建子节点的 stub）。**必须回报本次新建的子节点 id**——
     * 拿不到就是 G18 未闭合，编排器拒绝猜（失败 `assembly_output_unbound`）。
     */
    runGeneration: (input: { nodeId: string; mode: "text" | "image" | "video" | "audio"; prompt: string }) => Promise<{ childNodeId?: string }>;
    /** 拉片投影网络（页面 = 全局 fetch；缺省 = 不刷新投影，槽⑤只能靠节点现正文，记 warning）。 */
    fetchProjection?: LapianProjectionFetch;
    /** hub 身份客户端（缺省 = 只做画布侧，不绑 hub）。 */
    hub?: WorkflowInstanceHubClient;
    getBlob?: (storageKey: string) => Promise<Blob | null>;
    saveIdentity?: (record: WorkflowInstanceIdentityRecord) => void | Promise<void>;
    /** 批次成员入册（P5；只写身份，状态派生层现查）。缺省 = 不记批次（P4 行为不变）。 */
    saveBatchMember?: (member: WorkflowBatchMember) => void | Promise<void>;
    /** Wan 成片映射现查（S2：job→成片 taskId；入册时写成批次成员外键）。缺省 = 不写该外键。 */
    wanFileTasks?: () => Map<string, string> | Record<string, string> | undefined;
    /**
     * 自修闭环（P7b）：给了执行体才自修，缺省 = 不自修（P4/P5/P6 行为不变）。
     * 自修只走文本链（既有保存+重检入口），不发起任何付费生成。
     */
    repair?: {
        /** 自修执行体（生产 = 画布对话 Agent；探针 = `createTermStrippingRepairer`）。 */
        repairer: InstanceRepairer;
        /** 上限（D11 固定 2；传更大值会被夹回 2）。 */
        maxRounds?: number;
        /** 读正文全文（缺省 = `readNodeFullText`（read_node_text 同一实现））。 */
        readText?: (nodeId: string) => string | null;
        /** 冻结项快照（缺省 = `frozenSnapshotOf(画布, 实例拓扑 digest)`）。 */
        frozen?: () => FrozenSnapshot;
    };
    /** 所有者否决记录（P7a）：槽③请求前核对「否决过的匹配不得再进请求」。 */
    vetoes?: () => WorkflowAssetVetoRecord[] | undefined;
    /** 立即落盘（页面 = `useCanvasStore.flushProjects`）。缺省 = 不 flush（只走防抖）。 */
    flush?: (stage: InstanceFlushStage) => void | Promise<void | { ok?: boolean; error?: string }>;
    newRunToken?: () => string;
    now?: () => string;
    log?: (message: string) => void;
};

export type AssemblyProjection = { url: string; optional: string; changed: boolean; chars: number };

export type InstanceRunResult = {
    /**
     * awaiting_approval = 已绑 hub 且停在待批；needs_human = 自修 2 轮仍 FAIL（升级给人，不自动豁免、
     * 不自动通过、不进下一步）；canvas_ready = 画布侧完成但未绑 hub；failed = 中止。
     */
    status: "awaiting_approval" | "needs_human" | "canvas_ready" | "failed";
    instanceId: string;
    templateId: string;
    instanceIndex: number;
    slotChecks: InstanceSlotCheck[];
    assembly?: {
        configNodeId: string;
        composerContent: string;
        prompt: string;
        promptDigest: string;
        productNodeId: string;
        retiredPlaceholderNodeIds: string[];
        downstreamNodeIds: string[];
        /** 下游当前正文（重绑后按既有 buildNodeGenerationContext 复核）= 本次产物内容。 */
        downstreamPrompt: string;
        runToken: string;
        projection?: AssemblyProjection;
    };
    identity?: WorkflowInstanceIdentityRecord;
    hub?: Omit<InstanceHubBinding, "assets"> & { assets: InstanceHubBinding["assets"] };
    /** 自修闭环痕迹（P7b；未启用或首检就干净时缺省）。 */
    repair?: InstanceRepairRecord;
    /** 批次成员（P5；入册后的身份记录，随项目持久化）。 */
    batchMember?: WorkflowBatchMember;
    /** 关键节点 flush 结果（失败如实回传，见 warnings）。 */
    flushes: InstanceFlushOutcome[];
    /** 付费视频创建调用次数（本编排器恒 0；探针据此断言 P0b 闸前置）。 */
    paidVideoCreations: number;
    generationCalls: { mode: string; nodeId: string }[];
    appliedOps: CanvasAgentOp[];
    warnings: string[];
    failure?: { stage: WorkflowInstanceRunError["stage"] | "projection"; reason: string; details?: Record<string, unknown>; channel?: ChannelFailureClass };
};

/**
 * 单实例编排：拉片已完成 → （可选段投影刷新）→ 五槽代入校验 → 组装节点执行（非付费文本链）
 * → G18 产物绑定 → 自检/正文进 hub → 停在待审批。
 *
 * 全程零付费视频创建：`runGeneration` 只会以 `mode:"text"` 调用一次；任何失败都如实返回 failed。
 */
export async function runWorkflowInstance(input: { plan: WorkflowInstancePlan; materials: WorkflowInstanceMaterials; lapian: { taskId: string; jobId: string }; batchId?: string }, deps: InstanceRunDeps): Promise<InstanceRunResult> {
    const now = deps.now || (() => new Date().toISOString());
    const newRunToken = deps.newRunToken || (() => `run-${Math.random().toString(36).slice(2, 10)}`);
    const result: InstanceRunResult = {
        status: "failed",
        instanceId: input.plan.instanceId,
        templateId: input.plan.templateId,
        instanceIndex: input.plan.instanceIndex,
        slotChecks: [],
        paidVideoCreations: 0,
        generationCalls: [],
        appliedOps: [],
        warnings: [],
        flushes: [],
    };
    const apply = async (ops: CanvasAgentOp[]) => {
        if (!ops.length) return;
        await deps.applyOps(ops);
        result.appliedOps.push(...ops);
    };

    /**
     * 渠道失败分类（P6）：编排器自己**不判断**「能不能自动重试」——队列才是唯一裁决点。
     * 这里只把结构化分类（429/409 拥塞 vs 未知提交）带进 failure，供 P6 队列分派（审计 §5.2）。
     */
    const channelOf = (error: unknown): ChannelFailureClass | undefined => classifyChannelFailure(error) || undefined;

    // 关键节点立即落盘（P5 / A8-G14）：失败一律可见（warnings + flushes），绝不静默。
    const flushAt = async (stage: InstanceFlushStage) => {
        if (!deps.flush) return;
        try {
            const outcome = await deps.flush(stage);
            // flush 依赖可以回 {ok:false,error}（页面 = store.flushProjects 的返回值）：不静默吞掉。
            if (outcome && typeof outcome === "object" && outcome.ok === false) throw new Error(outcome.error || "flush 返回失败（存储写入未成功）");
            result.flushes.push({ stage, ok: true });
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error ?? "未知错误");
            result.flushes.push({ stage, ok: false, error: message });
            result.warnings.push(`落盘 flush 失败（${stage}）：${message}——最新状态可能停在 400ms 防抖窗口里，需重试 flush`);
        }
    };
    // 批次成员入册（只写身份；状态派生层现查 —— C5）。identity 缺省 = 画布侧刚落库。
    const batchId = String(input.batchId || "").trim();
    const saveMember = async (identity: WorkflowInstanceIdentityRecord | null) => {
        if (!batchId) return null;
        const member = buildBatchMember({
            plan: input.plan,
            materials: input.materials,
            identity,
            nodes: deps.nodes(),
            connections: deps.connections(),
            lapian: { taskId: String(input.lapian.taskId || ""), jobId: String(input.lapian.jobId || "") },
            wanFileTasks: deps.wanFileTasks?.(),
            repair: result.repair,
            createdAt: now(),
        });
        result.batchMember = member;
        if (deps.saveBatchMember) await deps.saveBatchMember(member);
        return member;
    };

    // ① 可选段投影刷新（槽⑤真实生效的唯一入口：正文必须换成「按本次可选段渲染的投影」）
    let lapianText = "";
    let projection: AssemblyProjection | undefined = undefined;
    const lapianNode = deps.nodes().find((node) => node.id === input.plan.lapianNodeId);
    const optionalLetters = lapianOptionalLetters(String(lapianNode?.metadata?.lapianOptional || input.materials.optionalSegments || ""));
    if (deps.fetchProjection && String(lapianNode?.metadata?.taskId || input.lapian.taskId)) {
        try {
            const rendered = await renderLapianProjection({ taskId: String(lapianNode?.metadata?.taskId || input.lapian.taskId), optionalSegments: optionalLetters }, { fetch: deps.fetchProjection });
            lapianText = rendered.text;
            const previous = String(lapianNode?.metadata?.content || "");
            projection = { url: rendered.url, optional: rendered.optional, changed: previous.trim() !== rendered.text.trim(), chars: rendered.text.length };
            if (!rendered.text.trim()) throw new WorkflowInstanceRunError("assembly_plan", "拉片投影正文为空（可选段渲染没出正文）", { url: rendered.url });
            if (projection.changed) await apply([{ type: "update_node", id: input.plan.lapianNodeId, metadata: { content: rendered.text } }]);
        } catch (error) {
            result.failure = { stage: "projection", reason: (error as Error).message, details: (error as WorkflowInstanceRunError).details, channel: channelOf(error) };
            return result;
        }
    } else {
        lapianText = String(lapianNode?.metadata?.content || "");
        result.warnings.push("未提供投影渲染依赖：槽⑤只能按拉片节点现正文组装（可选段不会改变正文）");
    }

    // ② 组装计划 + 五槽代入校验（校验失败=零生成调用）
    let assembly: InstanceAssemblyPlan;
    let slotChecks: InstanceSlotCheck[];
    try {
        assembly = planInstanceAssembly({ plan: input.plan, nodes: deps.nodes(), connections: deps.connections() });
        slotChecks = verifySlotSubstitution({ plan: input.plan, materials: input.materials, nodes: deps.nodes(), connections: deps.connections(), assembly, lapianText, vetoes: deps.vetoes?.() });
    } catch (error) {
        result.failure = { stage: (error as WorkflowInstanceRunError).stage || "assembly_plan", reason: (error as Error).message, details: (error as WorkflowInstanceRunError).details, channel: channelOf(error) };
        return result;
    }
    result.slotChecks = slotChecks;
    if (slotChecks.some((check) => !check.ok)) {
        result.failure = { stage: "slot_verify", reason: slotChecks.filter((check) => !check.ok).map((check) => `${check.slotKind}: ${check.detail}`).join("；"), details: { slotChecks } };
        return result;
    }

    // ③ 组装执行（既有文本生成链；非付费）
    const runToken = newRunToken();
    let childNodeId = "";
    try {
        const generated = await deps.runGeneration({ nodeId: assembly.assemblyConfigId, mode: "text", prompt: assembly.composerContent });
        result.generationCalls.push({ mode: "text", nodeId: assembly.assemblyConfigId });
        childNodeId = String(generated?.childNodeId || "").trim();
    } catch (error) {
        result.failure = { stage: "generation", reason: (error as Error).message, channel: channelOf(error) };
        return result;
    }
    if (!childNodeId) {
        result.failure = { stage: "product_binding", reason: "生成链没有回报本次新建的输出节点 id：G18 输出身份未闭合（拒绝按「最新节点」猜）", details: { assemblyConfigId: assembly.assemblyConfigId } };
        return result;
    }

    // ④ G18 产物绑定 + 复核（下游必须消费本次产物）
    const bindingOps = buildProductBindingOps({ plan: input.plan, assembly, productNodeId: childNodeId, nodes: deps.nodes(), connections: deps.connections() });
    await apply(bindingOps);
    const nodesAfter = deps.nodes();
    const connectionsAfter = deps.connections();
    const productNode = nodesAfter.find((node) => node.id === childNodeId);
    const downstreamPrompts = assembly.downstream.map((consumer) => {
        const node = nodesAfter.find((item) => item.id === consumer.nodeId);
        if (!node) return "";
        const composer = String(node.metadata?.composerContent || "");
        return composer.includes("@[node:") ? buildNodeGenerationContext(node.id, nodesAfter, connectionsAfter, composer).prompt.trim() : String(node.metadata?.content || "").trim();
    });
    const productText = String(productNode?.metadata?.content || "");
    const downstreamOk = assembly.downstream.every((consumer, index) => {
        const node = nodesAfter.find((item) => item.id === consumer.nodeId);
        const composer = String(node?.metadata?.composerContent || "");
        const tokenBound = !consumer.via.includes("token") || composer.includes(`@[node:${childNodeId}]`);
        const consumed = !productText.trim() || downstreamPrompts[index].includes(productText.trim());
        return tokenBound && consumed;
    });
    if (!productNode || !productText.trim() || !downstreamOk) {
        result.failure = {
            stage: "product_binding",
            reason: !productNode ? "本次产物节点不在画布上" : !productText.trim() ? "本次产物没有正文（生成失败或空）" : "下游没有消费本次产物（token 未换到本次 id 或正文未含本次产物）",
            details: { childNodeId, downstream: assembly.downstream, downstreamPrompts },
        };
        return result;
    }

    result.assembly = {
        configNodeId: assembly.assemblyConfigId,
        composerContent: assembly.composerContent,
        prompt: assembly.assemblyPrompt,
        promptDigest: assembly.assemblyPromptDigest,
        productNodeId: childNodeId,
        retiredPlaceholderNodeIds: assembly.staleOutputNodeIds,
        downstreamNodeIds: assembly.downstream.map((consumer) => consumer.nodeId),
        downstreamPrompt: downstreamPrompts[0] || "",
        runToken,
        projection,
    };

    // ④′ 批次成员入册（画布侧已落库）+ **立即落盘**：防抖窗口崩溃也不丢成员关系（P5 / A8-G14）。
    await saveMember(null);
    await flushAt("instance_persisted");

    // ⑤ hub 身份绑定（ingest → 资产 → 正文保存（自带自检）→ 身份记录）
    let binding: InstanceHubBinding | null = null;
    if (deps.hub) {
        try {
            binding = await bindInstanceToHub(
                {
                    plan: input.plan,
                    materials: input.materials,
                    assembly,
                    assemblyText: String(productText || "").trim(),
                    productNodeId: childNodeId,
                    retiredPlaceholderNodeIds: assembly.staleOutputNodeIds,
                    lapian: { taskId: String(lapianNode?.metadata?.taskId || input.lapian.taskId), jobId: String(lapianNode?.metadata?.jobId || input.lapian.jobId) },
                    runToken,
                    nodes: nodesAfter,
                },
                { hub: deps.hub, getBlob: deps.getBlob, fetch: globalThis.fetch, now },
            );
            result.hub = binding;
        } catch (error) {
            result.failure = { stage: "hub_bind", reason: (error as Error).message, details: (error as WorkflowInstanceRunError).details, channel: channelOf(error) };
            return result;
        }
        if (binding.selfcheckFailures) result.warnings.push(`hub 自检有 ${binding.selfcheckFailures} 项未通过（如实回传，不阻断批准）`);
    } else {
        result.warnings.push("未提供 hub 客户端：本实例未绑 hub 身份（画布侧已完成）");
    }

    // ⑤′ 自修闭环（P7b）：首检有未通过项 → 读全文 → 限定范围改正文 → 既有保存+重检（同一修订），
    // 最多 2 轮（D11）；2 轮仍 FAIL → 升级给人（needs_human），不豁免、不通过、不进下一步。
    let finalText = String(productText || "").trim();
    if (binding && deps.repair && repairNeeded(binding.selfcheck)) {
        const repairDeps = deps.repair;
        const readText = repairDeps.readText || ((nodeId: string) => {
            const snapshot = { projectId: "", title: "", nodes: deps.nodes(), connections: deps.connections(), selectedNodeIds: [] as string[], viewport: { x: 0, y: 0, k: 1 } };
            return readNodeFullText({ nodeId, canvasSnapshot: snapshot })?.content ?? null;
        });
        const record = await runInstanceSelfRepair(
            {
                plan: { instanceId: input.plan.instanceId, templateId: input.plan.templateId, instanceIndex: input.plan.instanceIndex },
                productNodeId: childNodeId,
                localId: binding.localId,
                findings: binding.selfcheck,
                maxRounds: repairDeps.maxRounds,
            },
            {
                readText,
                // 画布=唯一操作面：改好的正文先落回产物节点，再走 hub 保存（两处同一份字节 = 同一修订）
                writeCanvasText: async ({ nodeId, text }) => {
                    await apply([{ type: "update_node", id: nodeId, metadata: { content: text, prompt: text } }]);
                },
                // **既有**保存+重检入口（写正文 → 重算审批快照 → 重跑自检）；不另写评分器（G16）
                saveRevision: (payload) => deps.hub!.saveRevision(payload),
                repairer: repairDeps.repairer,
                frozen: repairDeps.frozen || (() => frozenSnapshotOf(deps.nodes(), input.plan.topologyDigest)),
                now,
                log: deps.log,
            },
        );
        result.repair = record;
        // 自检结论回到「最后一版修订」上呈现（同修订可见）：不再用首检结果
        binding.selfcheck = record.finalSelfcheck;
        binding.selfcheckFailures = record.finalFindings;
        const lastRound = record.rounds[record.rounds.length - 1];
        if (lastRound) {
            finalText = String(readText(childNodeId) || "").trim() || finalText;
            binding.revision = { digest: lastRound.digestAfter, chars: finalText.length, approvalSnapshot: lastRound.approvalSnapshot, promptEditedAt: lastRound.at };
        }
        if (record.outcome === "passed") {
            result.warnings.push(`自修 ${record.rounds.length} 轮修好（${record.rounds.map((round) => `${round.findingsBefore}→${round.findingsAfter}`).join("，")}）：同一修订上重检通过，可正常待批`);
        } else if (record.outcome === "escalated") {
            const code = record.escalation?.code || "U5";
            result.warnings.push(`自修未修好：${code} ${record.escalation?.detail || ""}——实例交人处理（不自动豁免、不自动通过）`);
        }
        result.hub = binding;
    }

    // ⑥ 身份记录（随项目持久化；状态不落这里——待批/已批一律现查 hub）
    const identity = buildIdentityRecord({
        plan: input.plan,
        assembly,
        assemblyText: finalText,
        productNodeId: childNodeId,
        retiredPlaceholderNodeIds: assembly.staleOutputNodeIds,
        lapian: { taskId: String(lapianNode?.metadata?.taskId || input.lapian.taskId), jobId: String(lapianNode?.metadata?.jobId || input.lapian.jobId) },
        runToken,
        binding,
        now,
    });
    result.identity = identity;
    if (deps.saveIdentity) await deps.saveIdentity(identity);
    // ⑤⑥ 身份 + 批次成员（含 hub 外键）落库后**立即落盘**：停在待批那一刻磁盘上就是最新身份。
    await saveMember(identity);
    const escalated = result.repair?.outcome === "escalated";
    await flushAt(binding ? (escalated ? "needs_human" : "awaiting_approval") : "instance_persisted");

    result.status = binding ? (escalated ? "needs_human" : "awaiting_approval") : "canvas_ready";
    return result;
}
