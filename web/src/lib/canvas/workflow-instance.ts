/**
 * 工作流实例化器（P3，方案A 批量代办）：模板声明 + 一组素材 → 画布上一条**可执行的新链路实例**。
 *
 * 依据（条款冻结）：
 * - `wanrepro/docs/方案A探索/设计-P0a批量代办设计裁决-20261113.md`
 *   §2.1（确定性 id 即幂等锚、跨实例 id 零交集）、§2.2（七类引用重写）、§2.3（先映射后出 ops 的顺序 + 中途失败语义）、
 *   §3.3（run_generation 不覆盖拉片链：槽①必须走受控执行入口）、§1.2/§1.3（机械替换 vs 冻结项 F1-F5）。
 * - P2 实现直接复用（禁复制）：`workflow-template.ts`（角色/剥离/槽声明/冻结项）、`workflow-template-ids.ts`（七类 id 重写）、
 *   `lapian-assets.ts`（清单行 collect/build/upsert 同源 + 扁平真源 `attachedImageIdOf`）。
 *
 * 本文件的两个入口：
 * 1. `planWorkflowInstance` —— **纯函数**：校验 → 重映射 → 槽值代入 → 挂图/清单重算 → 产出 ops 与诊断。
 *    任何校验失败都在此抛出（此时零写入，链路原子）。
 * 2. `materializeWorkflowInstance` —— 受控执行 + 落库：先跑槽①受控入口（`lapian-execute.ts`：上传→拉片→**本次正文**），
 *    拿到本次正文后一次性提交整批 ops（一次事务，见 §2.3 ⑤ 通过才入册）。中途失败按「回滚 + `instance_incomplete`」处置。
 *
 * 付费边界：本文件**不生成任何 `run_generation` op**——付费生成属 P0b 闸 + P4 执行链；槽①拉片属前置自动段（B0-D12 记账）。
 * 纯函数层：不读 i18n、不摸 React、不碰 store（探针可 esbuild 打包真跑）。
 */

import type { CanvasAgentOp } from "@/lib/canvas/canvas-agent-ops";
import { canvasNodeShapeKey } from "@/lib/canvas/canvas-agent-ops";
import { executeLapianChain, waitLapianContent, type LapianExecuteConfig, type LapianExecuteDeps, type LapianExecuteResult } from "@/lib/canvas/lapian-execute";
import { buildLapianAssetManifest, collectLapianAssetReferences, truncateAssetCaption, upsertLapianAssetManifest } from "@/lib/canvas/lapian-assets";
import {
    DEFAULT_MANIFEST_PREFIXES,
    stableDigest,
    validateTemplateProject,
    WORKFLOW_TEMPLATE_SCHEMA_VERSION,
    type CanvasTemplateSlot,
    type CanvasWorkflowTemplate,
} from "@/lib/canvas/workflow-template";
import { rewriteTemplateIds, templateInstanceConnectionId, templateInstanceNodeId } from "@/lib/canvas/workflow-template-ids";
import { CanvasNodeType, type CanvasConnection, type CanvasNodeData, type CanvasNodeMetadata } from "@/types/canvas";

/** 实例化错误码：调用方/探针据此判定「哪一类素材或模板问题」，不靠解析文案。 */
export type WorkflowInstanceErrorCode =
    | "template_incomplete"
    | "template_version_unsupported"
    | "source_video_slot_missing"
    | "material_missing"
    | "slot_violation"
    | "slot_target_missing"
    | "asset_slot_derivation_required"
    | "asset_image_missing"
    | "frozen_violation";

export class WorkflowInstanceError extends Error {
    readonly code: WorkflowInstanceErrorCode;
    readonly details: Record<string, unknown>;
    constructor(code: WorkflowInstanceErrorCode, message: string, details: Record<string, unknown> = {}) {
        super(message);
        this.name = "WorkflowInstanceError";
        this.code = code;
        this.details = details;
    }
}

/** 槽①素材：新源视频（唯一必填素材）。 */
export type WorkflowInstanceSourceVideo = {
    /** 画布本地媒体键（`services/file-storage` 的 storageKey）——上传/拉片的唯一输入。 */
    storageKey: string;
    /** 上传文件名（画布节点标题口径；缺省 `canvas-video.mp4`）。 */
    title?: string;
    /** 展示用地址（blob:/https:）；缺省时节点只有 storageKey（画布按 key 取字节）。 */
    content?: string;
    mimeType?: string;
    bytes?: number;
    durationMs?: number;
    naturalWidth?: number;
    naturalHeight?: number;
};

/** 槽③一条挂图：资产位 + 要挂的图（材料新图可随实例一起建）。 */
export type WorkflowInstanceAssetAttach = {
    /** 模板内资产位节点 id（旧 id）；模板槽③为 derived 时留空。 */
    slotNodeId?: string;
    /** 派生资产位序号（assetSlotOrder）；derived 模板用（P4 派生出槽后传入）。 */
    order?: number;
    /** 已在实例/模板内的图片节点 id（模板旧 id 会被重映射）。 */
    imageNodeId?: string;
    /** 材料新图节点：随实例一起建（id 走确定性重映射），便于「挂图组」直接来自素材。 */
    image?: CanvasNodeData;
    /** 清单短说明（模板期字段：attachedImageDesc / assetSlot.caption）。 */
    caption?: string;
};

/** 槽④一条参数覆盖：目标节点（模板旧 id）+ 要写的值（键必须在槽④字段白名单内）。 */
export type WorkflowInstanceParamOverride = {
    nodeId: string;
    values: Record<string, string | number>;
};

/** 素材组（五槽代入的输入）。 */
export type WorkflowInstanceMaterials = {
    sourceVideo: WorkflowInstanceSourceVideo;
    /** 槽②意图正文（缺省 = 保留模板占位符）。 */
    intentText?: string;
    /** 槽③挂图组（缺省 = 不挂图，清单行由模板剥离状态保持为空）。 */
    assetAttach?: WorkflowInstanceAssetAttach[];
    /** 槽④生成参数覆盖。 */
    generationParams?: WorkflowInstanceParamOverride[];
    /** 槽⑤可选段字母（空/缺省 = 全关）。 */
    optionalSegments?: string;
};

export type PlanWorkflowInstanceInput = {
    template: CanvasWorkflowTemplate;
    /** 模板项目节点/连线（P2 `extractWorkflowTemplate().project`）。 */
    templateNodes: CanvasNodeData[];
    templateConnections: CanvasConnection[];
    materials: WorkflowInstanceMaterials;
    /** 实例序号（1-based，确定性 id 的一部分；同序号重放 = 幂等）。 */
    instanceIndex: number;
    /** 实例整体平移（新链落在画布何处）；缺省 0,0。 */
    origin?: { x: number; y: number };
    /** 清单行前缀（与 i18n `canvas.lapianAsset.manifestPrefix` 同值；缺省 P2 默认）。 */
    manifestPrefixes?: string[];
    /** 清单短说明 fallback（i18n `canvas.lapianAsset.unnamed`；纯函数层不读 i18n）。 */
    unnamedCaption?: string;
};

export type WorkflowInstanceSlotWrite = {
    slotKind: CanvasTemplateSlot["kind"];
    templateNodeId: string;
    instanceNodeId: string;
    fields: string[];
};

export type WorkflowInstanceAssetAttachRecord = {
    slotNodeId: string;
    slotTemplateNodeId: string;
    imageNodeId: string;
    caption: string;
    graftNodeId: string;
};

/** 实例计划：纯函数产物（节点/连线/ops/诊断），不改模板、不落库。 */
export type WorkflowInstancePlan = {
    instanceId: string;
    templateId: string;
    instanceIndex: number;
    nodes: CanvasNodeData[];
    connections: CanvasConnection[];
    ops: CanvasAgentOp[];
    nodeIdMap: Record<string, string>;
    connectionIdMap: Record<string, string>;
    slotWrites: WorkflowInstanceSlotWrite[];
    assetAttaches: WorkflowInstanceAssetAttachRecord[];
    /** 槽③派生模式（模板不含资产位）：P4 按新拉片派生资产位后再挂图。 */
    assetAttachPendingDerivation: boolean;
    topologyDigest: string;
    /** 槽①落点（源视频节点新 id）与拉片节点新 id（受控入口的写入目标）。 */
    sourceVideoNodeId: string;
    lapianNodeId: string;
    /** 模板抽取时报告的图外引用（随实例带出，供 P4/P5 复核；不影响本实例自足性）。 */
    carriedExternalReferences: CanvasWorkflowTemplate["externalReferences"];
    warnings: string[];
};

/** 受控入口回填给实例的本次拉片结果。 */
export type WorkflowInstanceLapian = Pick<LapianExecuteResult, "taskId" | "jobId" | "intent" | "lapianModel"> & {
    /** **本次**拉片正文（旧正文零残留）。 */
    content: string;
};

/** 中途失败标记：不可冒充完整实例（P0a §2.3「失败前不入册」）。 */
export type WorkflowInstanceIncomplete = {
    instanceId: string;
    templateId: string;
    instanceIndex: number;
    stage: "lapian_chain" | "lapian_content" | "apply_ops";
    reason: string;
    /** 计划里已落库的节点/连线（回滚或后续对账用；零写入时为空数组）。 */
    createdNodeIds: string[];
    createdConnectionIds: string[];
    rolledBack: boolean;
};

export type MaterializeWorkflowInstanceInput = PlanWorkflowInstanceInput & {
    /** 拉片模型配置（页面 `effectiveConfig` 同口径）。 */
    lapianConfig: LapianExecuteConfig;
    /** 节点上显式选过的拉片模型（可选）。 */
    nodeLapianModel?: string;
    /** 正文轮询参数（探针注入 sleep/now 实现零等待）。 */
    contentWait?: { pollMs?: number; maxWaitMs?: number; sleep?: (ms: number) => Promise<void>; now?: () => number };
};

export type MaterializeWorkflowInstanceDeps = LapianExecuteDeps & {
    /** 落库：页面传 `applyAgentOps`；探针传 stub（把 ops 应用到内存快照）。 */
    applyOps: (ops: CanvasAgentOp[]) => void | Promise<void>;
    /** 现有画布节点（幂等/冲突判定：同实例已落库就不再花钱重跑拉片）。 */
    existingNodes?: () => CanvasNodeData[];
};

export type MaterializeWorkflowInstanceResult = {
    status: "created" | "already_present" | "incomplete";
    plan: WorkflowInstancePlan;
    lapian?: WorkflowInstanceLapian;
    /** 实际提交的 ops（created 时 = plan.ops 加上本次正文回填）。 */
    appliedOps?: CanvasAgentOp[];
    /** 已存在且形态一致时命中的节点 id（幂等重放）。 */
    existingNodeIds?: string[];
    incomplete?: WorkflowInstanceIncomplete;
};

/** 取某个槽；模板缺该槽即报模板不完整（P2 声明五槽固定齐备，缺一说明声明被改坏）。 */
function slotOf(template: CanvasWorkflowTemplate, kind: CanvasTemplateSlot["kind"]): CanvasTemplateSlot {
    const slot = template.slots?.find((item) => item.kind === kind);
    if (!slot) throw new WorkflowInstanceError("template_incomplete", `模板声明缺槽 ${kind}（schema v${template.schemaVersion}）：拒绝实例化`);
    return slot;
}

/** F5 越槽校验：写入字段必须在槽声明白名单内（冻结字段自然写不进）。 */
function assertSlotFields(slot: CanvasTemplateSlot, templateNodeId: string, fields: string[]) {
    const target = slot.targets.find((item) => item.nodeId === templateNodeId);
    if (!target) throw new WorkflowInstanceError("slot_target_missing", `槽 ${slot.kind} 没有可写目标 ${templateNodeId}；可写目标=${slot.targets.map((item) => item.nodeId).join(",") || "（空，派生槽）"}`, { slot: slot.kind, templateNodeId });
    const rejected = fields.filter((field) => !target.fields.includes(field) && !target.fields.some((allowed) => field.startsWith(`${allowed}.`)));
    if (rejected.length) {
        throw new WorkflowInstanceError("slot_violation", `越槽写入被拒：${templateNodeId} 的字段 ${rejected.join(",")} 不在槽 ${slot.kind} 白名单（${target.fields.join("/")}）`, { slot: slot.kind, templateNodeId, rejected });
    }
}

function cloneNodes(nodes: CanvasNodeData[]): CanvasNodeData[] {
    return nodes.map((node) => ({ ...node, position: { ...node.position }, metadata: JSON.parse(JSON.stringify(node.metadata || {})) as CanvasNodeMetadata }));
}

/** 取节点 metadata（克隆后必然存在；这里消除「可能 undefined」的类型噪音）。 */
function metaOf(node: CanvasNodeData): CanvasNodeMetadata {
    if (!node.metadata) node.metadata = {};
    return node.metadata;
}

/** 清单行之外的组装串骨架（F4 比对口径：剔除清单行后逐字节 digest）。 */
function composerSkeleton(composer: string, manifestPrefixes: string[]): string {
    return manifestPrefixes.reduce((text, prefix) => upsertLapianAssetManifest(text, "", prefix), String(composer || ""));
}

/** token id 段按「新 → 旧」映射归一（F4 比的是骨架结构，id 重绑定属 M1/M4 允许项）。 */
function normalizeComposerTokens(text: string, inverseIdOf: Map<string, string>): string {
    return String(text || "").replace(/@\[node:([^\]]+)\]/g, (whole: string, id: string) => `@[node:${inverseIdOf.get(id) || id}]`);
}

/**
 * 冻结项校验（F1-F4）：拓扑逐边同构 + 指令模板 id 未变 + generationMode 未变 + composerContent 骨架逐字节未变。
 * 骨架 digest 用与 P2 同一函数（`stableDigest`）与同一剔除口径（清单行），保证可比。
 */
function verifyFrozen(plan: { nodes: CanvasNodeData[]; topologyDigest: string; instanceIndex: number; inverseIdOf: Map<string, string> }, template: CanvasWorkflowTemplate, manifestPrefixes: string[]): string[] {
    const problems: string[] = [];
    const byId = new Map(plan.nodes.map((node) => [node.id, node]));
    const mapped = (oldId: string) => templateInstanceNodeId(template.templateId, plan.instanceIndex, oldId);

    if (plan.topologyDigest !== template.frozen.topology.digest) {
        problems.push(`F1 骨架拓扑不同构：实例 digest=${plan.topologyDigest} 模板 digest=${template.frozen.topology.digest}`);
    }
    for (const instruction of template.frozen.instructions) {
        const node = [...byId.values()].find((item) => item.id.endsWith(`-${instruction.nodeId}`) && item.metadata?.promptTemplate !== undefined);
        if (!node) continue;   // 组内指令等非实例节点（模板里存在但未被重映射到）由拓扑/自足性兜底
        const promptTemplate = String(node.metadata?.promptTemplate || "");
        const content = String(node.metadata?.content || node.metadata?.prompt || "");
        if (promptTemplate !== instruction.promptTemplate) problems.push(`F2 指令模板 id 被改：${instruction.nodeId} ${instruction.promptTemplate} → ${promptTemplate}`);
        if (stableDigest(content) !== instruction.contentDigest) problems.push(`F2 指令正文被改：${instruction.nodeId}（digest 不一致）`);
    }
    for (const mode of template.frozen.generationModes) {
        const node = byId.get(mapped(mode.nodeId)) ?? [...byId.values()].find((item) => item.id.endsWith(`-${mode.nodeId}`));
        if (!node) continue;
        const actual = String(node.metadata?.generationMode || "");
        if (actual !== mode.mode) problems.push(`F3 generationMode 被改：${mode.nodeId} ${mode.mode} → ${actual}`);
    }
    for (const skeleton of template.frozen.composerSkeletons) {
        const node = byId.get(mapped(skeleton.nodeId)) ?? [...byId.values()].find((item) => item.id.endsWith(`-${skeleton.nodeId}`));
        if (!node) continue;
        const actual = composerSkeleton(normalizeComposerTokens(String(node.metadata?.composerContent || ""), plan.inverseIdOf), manifestPrefixes);
        if (stableDigest(actual) !== skeleton.digest) problems.push(`F4 composerContent 骨架被改：${skeleton.nodeId}（剔除清单行、token 归一到旧 id 后 digest 不一致）`);
    }
    return problems;
}

/**
 * 生成实例计划（P0a §2.3 顺序：①映射 → ②建节点 → ③连线 → ④槽值 → ⑤校验）。
 * 全部校验在此完成；抛错时调用方没有任何写入。
 */
export function planWorkflowInstance(input: PlanWorkflowInstanceInput): WorkflowInstancePlan {
    const { template } = input;
    const manifestPrefixes = input.manifestPrefixes?.length ? input.manifestPrefixes : DEFAULT_MANIFEST_PREFIXES;
    const unnamedCaption = input.unnamedCaption || "未命名参考图";
    const instanceIndex = Number(input.instanceIndex);
    if (!Number.isInteger(instanceIndex) || instanceIndex < 1) throw new WorkflowInstanceError("template_incomplete", `实例序号必须是 ≥1 的整数（收到 ${input.instanceIndex}）`);
    if (template.schemaVersion !== WORKFLOW_TEMPLATE_SCHEMA_VERSION) {
        throw new WorkflowInstanceError("template_version_unsupported", `模板声明 schemaVersion=${template.schemaVersion}，本实例化器只支持 ${WORKFLOW_TEMPLATE_SCHEMA_VERSION}`);
    }

    // ① 模板自足性（图外引用/运行态残留/扁平-镜像不一致一律拦下，不带着坏模板建链）
    const problems = validateTemplateProject(input.templateNodes, input.templateConnections);
    if (problems.length) throw new WorkflowInstanceError("template_incomplete", `模板不自足（${problems.length} 项）：${problems.slice(0, 5).join("；")}`, { problems });
    const declaredIds = new Set(template.frozen.topology.nodeIds || []);
    const actualIds = new Set(input.templateNodes.map((node) => node.id));
    if (declaredIds.size !== actualIds.size || [...declaredIds].some((id) => !actualIds.has(id))) {
        throw new WorkflowInstanceError("template_incomplete", "模板节点与冻结拓扑 nodeIds 不一致（声明被改坏）", { declared: [...declaredIds], actual: [...actualIds] });
    }

    // ② 全量确定性映射（先映射后出 ops；未命中即图外引用 → 拒绝）
    const rewritten = rewriteTemplateIds({
        nodes: input.templateNodes,
        connections: input.templateConnections,
        templateId: template.templateId,
        instanceIndex,
    });
    if (rewritten.unresolved.length) {
        throw new WorkflowInstanceError("template_incomplete", `重映射后有 ${rewritten.unresolved.length} 条引用落到模板外：${rewritten.unresolved.slice(0, 5).map((item) => `${item.nodeId}.${item.field}→${item.targetId}`).join("；")}`, { unresolved: rewritten.unresolved });
    }

    const nodes = cloneNodes(rewritten.nodes);
    const connections = rewritten.connections.map((connection) => ({ ...connection }));
    const nodeIdMap = rewritten.nodeIdMap;
    const instanceIdOf = new Map(Object.entries(nodeIdMap).map(([oldId, newId]) => [newId, oldId]));
    const byTemplateId = new Map<string, CanvasNodeData>();
    for (const node of nodes) {
        const templateId = instanceIdOf.get(node.id);
        if (templateId) byTemplateId.set(templateId, node);
    }
    const slotWrites: WorkflowInstanceSlotWrite[] = [];
    const warnings: string[] = [];
    const setSlotWrite = (slot: CanvasTemplateSlot, templateNodeId: string, fields: string[]) => {
        assertSlotFields(slot, templateNodeId, fields);
        slotWrites.push({ slotKind: slot.kind, templateNodeId, instanceNodeId: nodeIdMap[templateNodeId], fields });
    };

    // ③ 槽①源视频（必填）：媒体字段按槽白名单写入；旧 taskId 等运行态在模板期已剥离
    const sourceSlot = slotOf(template, "sourceVideo");
    if (!sourceSlot.targets.length) throw new WorkflowInstanceError("source_video_slot_missing", "模板没有源视频节点：槽①无待注入位");
    const sourceVideo = input.materials?.sourceVideo;
    const storageKey = String(sourceVideo?.storageKey || "").trim();
    if (!storageKey) throw new WorkflowInstanceError("material_missing", "素材缺失：槽①源视频必须有 storageKey（新视频字节的媒体键）", { slot: "sourceVideo" });
    const sourceMedia: Record<string, unknown> = {
        storageKey,
        content: sourceVideo?.content,
        mimeType: sourceVideo?.mimeType,
        bytes: sourceVideo?.bytes,
        durationMs: sourceVideo?.durationMs,
        naturalWidth: sourceVideo?.naturalWidth,
        naturalHeight: sourceVideo?.naturalHeight,
    };
    for (const target of sourceSlot.targets) {
        const node = byTemplateId.get(target.nodeId);
        if (!node) throw new WorkflowInstanceError("slot_target_missing", `槽①目标 ${target.nodeId} 不在模板里`);
        const written = Object.keys(sourceMedia).filter((field) => sourceMedia[field] !== undefined && target.fields.includes(field));
        for (const field of written) (node.metadata as Record<string, unknown>)[field] = sourceMedia[field];
        setSlotWrite(sourceSlot, target.nodeId, written);
    }
    // 源视频节点标题（画布展示/上传文件名）：材料给了就用材料的，否则留模板标题
    const sourceVideoNode = byTemplateId.get(sourceSlot.targets[0].nodeId)!;
    const sourceVideoNodeId = sourceVideoNode.id;
    if (String(sourceVideo?.title || "").trim()) sourceVideoNode.title = String(sourceVideo!.title).trim();

    // ④ 槽②意图正文（缺省保留占位符 —— 与手工链路「可留空」一致）
    const intentSlot = slotOf(template, "intentText");
    const intentText = String(input.materials?.intentText ?? "").trim();
    if (intentText) {
        for (const target of intentSlot.targets) {
            const node = byTemplateId.get(target.nodeId);
            if (!node) continue;
            assertSlotFields(intentSlot, target.nodeId, ["content", "prompt"]);
            const metadata = metaOf(node);
            metadata.content = intentText;
            metadata.prompt = intentText;
            slotWrites.push({ slotKind: "intentText", templateNodeId: target.nodeId, instanceNodeId: node.id, fields: ["content", "prompt"] });
        }
    }

    if (intentText && !intentSlot.targets.length) warnings.push("模板没有意图节点：槽②正文无处写入（已忽略）");

    // ⑤ 槽④生成参数（只写值，不判模型/画幅/方言——红线 25）
    const paramSlot = slotOf(template, "generationParams");
    for (const override of input.materials?.generationParams || []) {
        const target = paramSlot.targets.find((item) => item.nodeId === override.nodeId);
        if (!target) throw new WorkflowInstanceError("slot_target_missing", `槽④没有可写目标 ${override.nodeId}`, { nodeId: override.nodeId });
        const fields = Object.keys(override.values || {});
        if (!fields.length) continue;
        assertSlotFields(paramSlot, override.nodeId, fields);
        const node = byTemplateId.get(override.nodeId);
        if (!node) continue;
        for (const field of fields) (node.metadata as Record<string, unknown>)[field] = override.values[field];
        slotWrites.push({ slotKind: "generationParams", templateNodeId: override.nodeId, instanceNodeId: node.id, fields });
    }

    // ⑥ 槽⑤可选段
    const optionalSlot = slotOf(template, "optionalSegments");
    const optionalSegments = input.materials?.optionalSegments;
    if (optionalSegments !== undefined) {
        for (const target of optionalSlot.targets) {
            const node = byTemplateId.get(target.nodeId);
            if (!node) continue;
            assertSlotFields(optionalSlot, target.nodeId, ["lapianOptional"]);
            const value = String(optionalSegments).trim();
            const metadata = metaOf(node);
            if (value) metadata.lapianOptional = value;
            else delete metadata.lapianOptional;
            slotWrites.push({ slotKind: "optionalSegments", templateNodeId: target.nodeId, instanceNodeId: node.id, fields: ["lapianOptional"] });
        }
    }

    // ⑦ 材料新图（挂图组自带新图）：随实例一起建，id 走确定性重映射
    for (const attach of input.materials?.assetAttach || []) {
        if (!attach.image) continue;
        const imageId = nodeIdMap[attach.image.id] || templateInstanceNodeId(template.templateId, instanceIndex, attach.image.id);
        if (nodes.some((node) => node.id === imageId)) continue;
        nodes.push({ ...attach.image, id: imageId, position: { ...attach.image.position }, metadata: JSON.parse(JSON.stringify(attach.image.metadata || {})) as CanvasNodeMetadata });
        nodeIdMap[attach.image.id] = imageId;
    }

    // ⑧ 槽③挂图（P0a §1.2 M3/M5）：扁平真源 + assetSlot 镜像同一次写入，清单行走同源 collect/build/upsert
    const assetSlot = slotOf(template, "assetAttach");
    const lapianSlot = template.slots.find((item) => item.kind === "optionalSegments");
    const lapianNodeId = lapianSlot?.targets[0] ? nodeIdMap[lapianSlot.targets[0].nodeId] : "";
    const assetAttaches: WorkflowInstanceAssetAttachRecord[] = [];
    const attachList = input.materials?.assetAttach || [];
    /** 模板不含资产位（P0a §1.1）：槽③必须由 P4 按新拉片派生资产位后再挂图，本卡不发明资产位。 */
    const assetAttachPendingDerivation = assetSlot.targets.length === 0;
    if (attachList.length && assetAttachPendingDerivation) {
        throw new WorkflowInstanceError("asset_slot_derivation_required", "模板槽③为派生槽（模板不含资产位）：须先按新拉片派生资产位（P4）再挂图；本卡不发明资产位", { derived: true, note: assetSlot.note });
    }
    for (const attach of attachList) {
        const templateSlotId = String(attach.slotNodeId || "").trim();
        if (!templateSlotId) throw new WorkflowInstanceError("slot_target_missing", "槽③挂图必须给 slotNodeId（模板内资产位 id）", { order: attach.order });
        if (!assetSlot.targets.some((item) => item.nodeId === templateSlotId)) throw new WorkflowInstanceError("slot_target_missing", `槽③没有可写目标 ${templateSlotId}；可写目标=${assetSlot.targets.map((item) => item.nodeId).join(",")}`, { templateSlotId });
        const slotNode = byTemplateId.get(templateSlotId);
        if (!slotNode) throw new WorkflowInstanceError("slot_target_missing", `资产位 ${templateSlotId} 不在模板里`, { templateSlotId });
        const imageId = String(attach.imageNodeId || (attach.image ? nodeIdMap[attach.image.id] : "") || "").trim();
        const resolvedImageId = nodeIdMap[imageId] || imageId;
        const image = nodes.find((node) => node.id === resolvedImageId && node.type === CanvasNodeType.Image && Boolean(node.metadata?.content));
        if (!image) throw new WorkflowInstanceError("asset_image_missing", `槽③挂图目标图不存在或无内容：${imageId}`, { slotNodeId: templateSlotId, imageId });
        const caption = truncateAssetCaption(attach.caption || "") || String(slotNode.metadata?.assetSlotName || "");
        assertSlotFields(assetSlot, templateSlotId, ["attachedImageId", "assetSlot.attachedImageId", "attachedImageDesc", "assetSlot.caption"]);
        // 扁平真源 + 嵌套镜像同一次写入（浅合并语义下两处必须同 patch）
        const slotMeta = metaOf(slotNode);
        slotMeta.attachedImageId = image.id;
        slotMeta.attachedImageDesc = caption || undefined;
        if (slotMeta.assetSlot) {
            slotMeta.assetSlot = { ...slotMeta.assetSlot, attachedImageId: image.id, caption: caption || undefined };
        }
        // 对照图（与手工挂图副作用同形：素材副本 + 双向引用字段）：模板若已带该资产位的对照图则原地换图，不新增第二个
        const existingGraftNode = nodes.find((node) => node.metadata?.assetGraftForSlotId === slotNode.id);
        const graftId = existingGraftNode?.id || templateInstanceNodeId(template.templateId, instanceIndex, `graft@${templateSlotId}`);
        const graftNode: CanvasNodeData = {
            id: graftId,
            type: CanvasNodeType.Image,
            title: `挂图 · ${caption || String(slotNode.metadata?.assetSlotName || "")}`,
            position: existingGraftNode?.position || { x: slotNode.position.x + (slotNode.width || 240) + 48, y: slotNode.position.y },
            width: existingGraftNode?.width || slotNode.width || 240,
            height: existingGraftNode?.height || slotNode.height || 240,
            metadata: {
                content: image.metadata?.content,
                storageKey: image.metadata?.storageKey,
                naturalWidth: image.metadata?.naturalWidth,
                naturalHeight: image.metadata?.naturalHeight,
                bytes: image.metadata?.bytes,
                mimeType: image.metadata?.mimeType,
                status: "success",
                assetGraftForSlotId: slotNode.id,
                assetGraftOfImageId: image.id,
            },
        };
        if (existingGraftNode) {
            existingGraftNode.title = graftNode.title;
            existingGraftNode.metadata = { ...existingGraftNode.metadata, ...graftNode.metadata };
        } else {
            nodes.push(graftNode);
        }
        const graftEdgeId = templateInstanceConnectionId(template.templateId, instanceIndex, `graft@${templateSlotId}`);
        if (!connections.some((connection) => connection.fromNodeId === slotNode.id && connection.toNodeId === graftId)) {
            connections.push({ id: graftEdgeId, fromNodeId: slotNode.id, toNodeId: graftId });
        }
        // M5 伴生边：图 → Config（无连线=图不进请求）；按 lapianSourceId 定位本实例的配置节点
        if (lapianNodeId) {
            for (const configNode of nodes.filter((node) => node.type === CanvasNodeType.Config && node.metadata?.lapianSourceId === lapianNodeId)) {
                const templateConfigId = Object.keys(nodeIdMap).find((oldId) => nodeIdMap[oldId] === configNode.id) || configNode.id;
                const edgeId = templateInstanceConnectionId(template.templateId, instanceIndex, `attach:${image.id}->${templateConfigId}`);
                if (!connections.some((connection) => connection.fromNodeId === image.id && connection.toNodeId === configNode.id)) {
                    connections.push({ id: edgeId, fromNodeId: image.id, toNodeId: configNode.id });
                }
            }
            // M3 清单行重算：与挂图副作用同源实现（collect → build → upsert）
            const manifest = buildLapianAssetManifest(collectLapianAssetReferences(lapianNodeId, nodes, unnamedCaption), manifestPrefixes[0]);
            for (const configNode of nodes.filter((node) => node.type === CanvasNodeType.Config && node.metadata?.lapianSourceId === lapianNodeId)) {
                const configMeta = metaOf(configNode);
                configMeta.composerContent = upsertLapianAssetManifest(String(configMeta.composerContent || ""), manifest, manifestPrefixes[0]);
            }
        }
        assetAttaches.push({ slotNodeId: slotNode.id, slotTemplateNodeId: templateSlotId, imageNodeId: image.id, caption, graftNodeId: graftId });
    }

    // ⑨ 布局平移（新链落在画布何处；确定性，重放一致）
    const origin = input.origin || { x: 0, y: 0 };
    if (origin.x || origin.y) {
        for (const node of nodes) node.position = { x: node.position.x + origin.x, y: node.position.y + origin.y };
    }

    // ⑩ 冻结项校验（F1-F4）+ 实例/模板 id 隔离
    const frozenProblems = verifyFrozen({ nodes, topologyDigest: rewritten.topologyDigest, instanceIndex, inverseIdOf: instanceIdOf }, template, manifestPrefixes);
    if (frozenProblems.length) throw new WorkflowInstanceError("frozen_violation", `实例触碰冻结项：${frozenProblems.join("；")}`, { problems: frozenProblems });
    const templateIdSet = new Set(input.templateNodes.map((node) => node.id));
    const collisions = nodes.filter((node) => templateIdSet.has(node.id)).map((node) => node.id);
    if (collisions.length) throw new WorkflowInstanceError("template_incomplete", `实例 id 与模板 id 交集非空：${collisions.join(",")}`, { collisions });

    // ⑪ 出 ops：add_node（终态 metadata，幂等）/ connect_nodes（确定性连线 id）/ select
    const ops: CanvasAgentOp[] = nodes.map((node) => ({
        type: "add_node" as const,
        id: node.id,
        nodeType: node.type,
        title: node.title,
        position: { ...node.position },
        width: node.width,
        height: node.height,
        metadata: { ...node.metadata },
    }));
    for (const connection of connections) ops.push({ type: "connect_nodes", id: connection.id, fromNodeId: connection.fromNodeId, toNodeId: connection.toNodeId });
    const focusNodeId = lapianNodeId || sourceVideoNodeId;
    if (focusNodeId) ops.push({ type: "select_nodes", ids: [focusNodeId] });

    if (assetAttachPendingDerivation) warnings.push("槽③挂图组非空但模板为派生槽：P4 派生资产位后再挂（本实例未挂图）");
    if (template.externalReferences?.length) warnings.push(`模板携带 ${template.externalReferences.length} 条图外引用（抽取期报告）：实例不自动补，交 P4/P5 复核`);

    return {
        instanceId: `${template.templateId}#${instanceIndex}`,
        templateId: template.templateId,
        instanceIndex,
        nodes,
        connections,
        ops,
        nodeIdMap,
        connectionIdMap: rewritten.connectionIdMap,
        slotWrites,
        assetAttaches,
        assetAttachPendingDerivation,
        topologyDigest: rewritten.topologyDigest,
        sourceVideoNodeId,
        lapianNodeId,
        carriedExternalReferences: template.externalReferences || [],
        warnings,
    };
}

/** 回滚计划：删本次实例建出的连线与节点（只删自己的，不碰画布既有内容）。 */
export function buildInstanceRollbackOps(plan: WorkflowInstancePlan): CanvasAgentOp[] {
    const connectionIds = plan.connections.map((connection) => connection.id);
    return [
        ...(connectionIds.length ? [{ type: "delete_connections" as const, ids: connectionIds }] : []),
        ...(plan.nodes.length ? [{ type: "delete_node" as const, ids: plan.nodes.map((node) => node.id) }] : []),
    ];
}

/**
 * 把本次拉片结果写进计划的拉片节点（正文=**本次**；旧正文在模板期已剥离，不会残留）。
 * 返回可直接落库的整批 ops：拉片节点的 add_node 用回填后的终态 metadata（重放仍幂等）。
 */
export function applyLapianToPlan(plan: WorkflowInstancePlan, lapian: WorkflowInstanceLapian): CanvasAgentOp[] {
    const lapianNode = plan.nodes.find((node) => node.id === plan.lapianNodeId);
    if (!lapianNode) throw new WorkflowInstanceError("template_incomplete", "计划里没有拉片节点：槽⑤目标缺失，无法回填本次正文");
    const metadata: Partial<CanvasNodeMetadata> = {
        taskId: lapian.taskId,
        jobId: lapian.jobId,
        intent: lapian.intent,
        content: lapian.content || undefined,
        ...(lapian.lapianModel ? { lapianModel: lapian.lapianModel } : {}),
    };
    Object.assign(lapianNode.metadata as Record<string, unknown>, metadata);
    return plan.ops.map((op) => (op.type === "add_node" && op.id === lapianNode.id ? { ...op, metadata: { ...(op.metadata || {}), ...metadata } } : op));
}

/**
 * 受控执行 + 落库：先跑槽①入口拿到**本次**正文，再一次提交整批 ops。
 * - 幂等：同实例（同确定性 id）已落库且拉片节点带 taskId → 直接返回 `already_present`，**不重跑拉片（不重复花钱）**。
 * - 失败：任何阶段失败都返回 `incomplete`（附已建 id 与回滚结果），绝不留「看起来完整的半条链」。
 */
export async function materializeWorkflowInstance(
    input: MaterializeWorkflowInstanceInput,
    deps: MaterializeWorkflowInstanceDeps,
): Promise<MaterializeWorkflowInstanceResult> {
    const plan = planWorkflowInstance(input);   // 校验失败=零写入（此处抛出，调用方拿到 WorkflowInstanceError）
    if (!plan.lapianNodeId) throw new WorkflowInstanceError("template_incomplete", "模板没有拉片节点：槽①受控入口无回填目标（拉片链模板必须含 Lapian 节点）");
    const existingNodes = deps.existingNodes?.() || [];
    const existingById = new Map(existingNodes.map((node) => [node.id, node]));
    if (existingById.size) {
        // 幂等优先：同实例已落库（拉片节点带 taskId）→ 直接返回，绝不重复上传/拉片（不重复花钱）
        const lapianExisting = plan.lapianNodeId ? existingById.get(plan.lapianNodeId) : undefined;
        if (lapianExisting && String(lapianExisting.metadata?.taskId || "")) {
            return { status: "already_present", plan, existingNodeIds: plan.nodes.filter((node) => existingById.has(node.id)).map((node) => node.id) };
        }
        // 已存在但形态不同（且不是上面那种已完成实例）→ 拒绝覆盖，提示换实例序号
        const conflicts = plan.nodes.filter((node) => existingById.has(node.id) && canvasNodeShapeKey(existingById.get(node.id)!) !== canvasNodeShapeKey(node));
        if (conflicts.length) {
            throw new WorkflowInstanceError("slot_violation", `实例节点已存在且形态不一致：${conflicts.slice(0, 5).map((node) => node.id).join(",")}（拒绝覆盖，请换实例序号）`, { conflicts: conflicts.map((node) => node.id) });
        }
    }

    const materials = input.materials;
    const intent = String(materials.intentText ?? "").trim();
    let chain: LapianExecuteResult;
    try {
        chain = await executeLapianChain(
            {
                storageKey: materials.sourceVideo.storageKey,
                title: materials.sourceVideo.title,
                intent,
                config: input.lapianConfig,
                nodeModel: input.nodeLapianModel,
            },
            { getBlob: deps.getBlob, fetch: deps.fetch, log: deps.log },
        );
    } catch (error) {
        // 上传/发起失败：此时 ops 一条未提交 → 画布零变化（原子）
        return {
            status: "incomplete",
            plan,
            incomplete: { instanceId: plan.instanceId, templateId: plan.templateId, instanceIndex: plan.instanceIndex, stage: "lapian_chain", reason: (error as Error).message, createdNodeIds: [], createdConnectionIds: [], rolledBack: true },
        };
    }

    let content = "";
    try {
        content = await waitLapianContent(
            { taskId: chain.taskId, jobId: chain.jobId, ...input.contentWait },
            { fetch: deps.fetch },
        );
    } catch (error) {
        // 拉片已发起（有费用）但正文没回来：落节点（带 taskId，可在画布上「重查」续）并显式标 incomplete，不冒充完整
        const lapian: WorkflowInstanceLapian = { taskId: chain.taskId, jobId: chain.jobId, intent: chain.intent, lapianModel: chain.lapianModel, content: "" };
        const ops = applyLapianToPlan(plan, lapian);
        try {
            await deps.applyOps(ops);
        } catch {
            /* 落库失败不再抛：下面统一按 incomplete 报告，回滚由调用方用 buildInstanceRollbackOps 执行 */
        }
        return {
            status: "incomplete",
            plan,
            lapian,
            appliedOps: ops,
            incomplete: { instanceId: plan.instanceId, templateId: plan.templateId, instanceIndex: plan.instanceIndex, stage: "lapian_content", reason: (error as Error).message, createdNodeIds: plan.nodes.map((node) => node.id), createdConnectionIds: plan.connections.map((connection) => connection.id), rolledBack: false },
        };
    }

    const lapian: WorkflowInstanceLapian = { taskId: chain.taskId, jobId: chain.jobId, intent: chain.intent, lapianModel: chain.lapianModel, content };
    const ops = applyLapianToPlan(plan, lapian);
    try {
        await deps.applyOps(ops);
    } catch (error) {
        let rolledBack = false;
        try {
            await deps.applyOps(buildInstanceRollbackOps(plan));
            rolledBack = true;
        } catch {
            rolledBack = false;
        }
        return {
            status: "incomplete",
            plan,
            lapian,
            incomplete: { instanceId: plan.instanceId, templateId: plan.templateId, instanceIndex: plan.instanceIndex, stage: "apply_ops", reason: (error as Error).message, createdNodeIds: plan.nodes.map((node) => node.id), createdConnectionIds: plan.connections.map((connection) => connection.id), rolledBack },
        };
    }
    return { status: "created", plan, lapian, appliedOps: ops };
}
