/**
 * P9 · 批量入口三工具的**调用壳**（把已交付的 P2/P3/P4/P6/P8 引擎接成画布 Agent 可调的工具）。
 *
 * 依据（卡面红线 25 / 27）：
 * - 红线 27：批量必须由画布 Agent 对话窗口驱动 → 本文件是 site tool 的实现体，**不是新 UI**。
 * - 红线 25：本文件**不新写任何引擎**——
 *   抽模板 = P2 `extractWorkflowTemplate`；幂等落库 = P3 `materializeWorkflowInstance`；
 *   单实例编排 = P4 `runWorkflowInstance`（经 `workflow-batch-runtime` 的队列作业）；
 *   并发/对账 = P6 `runBatchQueue`（经 `startBatch`）；批次身份 = P5 `createWorkflowBatch/buildBatchMember`。
 *   这里只有「取参 → 调引擎 → 汇总回执」的胶水，引擎文件一行不改。
 * - 付费归属：本文件**从不**调用生成链。整条路径只经 P4，而 P4 的 `runGeneration` 只会以
 *   `mode:"text"` 被调一次；视频创建一律停在「待审批」等人工在侧栏批准（`paidVideoCreations` 恒 0）。
 *
 * 为什么是纯胶水（不 import store）：`pages/canvas/project.tsx` 把页面现状（project/applyOps/hub/落盘）
 * 通过 `registerWorkflowBatchRuntime` 注册成 deps，浏览器壳（`agent-site-tools.ts`）把它们喂进来；
 * 探针用同形状的 stub deps 跑同一份实现（P1/P8 一贯口径：探针跑生产代码本身）。
 */

import type { CanvasConnection, CanvasNodeData } from "@/types/canvas";

import type { CanvasAgentOp } from "@/lib/canvas/canvas-agent-ops";
import { buildBatchMember, batchSignature, createWorkflowBatch, findWorkflowBatch, summarizeMaterials, type WorkflowBatchMember, type WorkflowBatchRecord } from "@/lib/canvas/workflow-batch";
import { createBatchQueue, type BatchQueueRunResult, type WorkflowBatchQueueRecord } from "@/lib/canvas/workflow-batch-queue";
import { deriveInstanceStatus, type InstanceStatus } from "@/lib/canvas/workflow-batch-status";
import type { InstanceRunDeps, WorkflowInstanceHubClient, WorkflowInstanceIdentityRecord } from "@/lib/canvas/workflow-instance-run";
import { materializeWorkflowInstance, type WorkflowInstanceMaterials, type WorkflowInstanceSourceVideo } from "@/lib/canvas/workflow-instance";
import type { LapianExecuteConfig } from "@/lib/canvas/lapian-execute";
import type { WorkflowAssetVetoRecord } from "@/lib/canvas/workflow-asset-match";
import { classifyTemplateRole, extractWorkflowTemplate, isSkeletonRole, stableDigest, type CanvasWorkflowTemplate } from "@/lib/canvas/workflow-template";

/** 画布项目的结构下限（**只读**：写一律走注入的 save 与 applyOps 回调）。 */
export type EntryProject = {
    id: string;
    title?: string;
    nodes: CanvasNodeData[];
    connections: CanvasConnection[];
    workflowTemplate?: CanvasWorkflowTemplate;
    workflowBatches?: WorkflowBatchRecord[];
    workflowAssetVetoes?: WorkflowAssetVetoRecord[];
};

/** 队列跑一次的结果（页面 = `workflow-batch-runtime.startBatch`）。 */
export type EntryBatchRunOutcome = {
    ok: boolean;
    reason?: string;
    queue?: WorkflowBatchQueueRecord;
    result?: BatchQueueRunResult;
};

/**
 * 入口工具依赖：**全部**取自 `registerWorkflowBatchRuntime` 已注册的 deps（不新起第二套运行时），
 * 额外两项是 P9 首次需要的写通道（模板声明 / 批次记录），也由 `project.tsx` 注册进同一份 deps。
 */
export type WorkflowEntryDeps = {
    project: () => EntryProject | undefined;
    applyOps: (ops: CanvasAgentOp[]) => void | Promise<void>;
    getBlob: (storageKey: string) => Promise<Blob | null>;
    hub: WorkflowInstanceHubClient;
    lapianConfig?: LapianExecuteConfig;
    vetoes: () => WorkflowAssetVetoRecord[] | undefined;
    /** 已落库的实例身份（P4 `workflowInstances`）：重放/续跑时复用 hub 绑定，别让同一个实例变成「未绑定」。 */
    identities?: () => WorkflowInstanceIdentityRecord[] | undefined;
    /** 模板声明落盘（页面 = `updateProject(projectId, { workflowTemplate })`）。 */
    saveTemplate: (template: CanvasWorkflowTemplate) => void | Promise<void>;
    /** 批次记录落盘（页面 = `updateProject(projectId, { workflowBatches })`）。 */
    saveBatches: (batches: WorkflowBatchRecord[]) => void | Promise<void>;
    flush: (reason: string) => void | Promise<unknown>;
    /** 跑队列（页面 = 同模块的 `startBatch`；探针 = 同一实现 + stub deps）。 */
    runBatch: (batchId: string) => Promise<EntryBatchRunOutcome>;
    /** 任意画布上按节点 id 找节点（素材可来自源画布；缺省只查当前画布）。 */
    findNode?: (nodeId: string) => CanvasNodeData | undefined;
    now?: () => string;
    log?: (message: string) => void;
};

// ─────────────────────────────────────────────── 入参/回执形状（与 schemas.ts 的 zod 对齐）

export type ExtractTemplateInput = {
    sourceNodeIds?: string[];
    name?: string;
    templateId?: string;
};

export type WorkflowBatchMaterialInput = {
    sourceVideoNodeId?: string;
    storageKey?: string;
    title?: string;
    intentText?: string;
    optionalSegments?: string;
};

export type RunWorkflowBatchInput = {
    templateId?: string;
    materials: WorkflowBatchMaterialInput[];
    concurrency?: number;
    title?: string;
    /** 批次 id 覆盖（探针用；缺省按模板+素材组确定性派生）。 */
    batchId?: string;
};

export type RunWorkflowInstanceInput = {
    templateId?: string;
    material: WorkflowBatchMaterialInput;
    title?: string;
    batchId?: string;
};

export type TemplateSlotBrief = {
    kind: string;
    label: string;
    required: boolean;
    derived?: boolean;
    targets: string[];
};

export type ExtractTemplateResult = {
    ok: true;
    templateId: string;
    name: string;
    sourceNodeIds: string[];
    nodeCount: number;
    slots: TemplateSlotBrief[];
    frozen: {
        topologyDigest: string;
        nodeCount: number;
        edgeCount: number;
        instructions: number;
        generationModes: { nodeId: string; mode: string }[];
        composerSkeletons: number;
    };
    /** P2 逐条如实返回的图外引用（非空 = 模板**不冻结**，实例化会被拦下）。 */
    externalReferences: { kind: string; fromNodeId: string; field: string; targetId: string; reason: string; token?: string }[];
    frozenOk: boolean;
    excluded: { nodeId: string; role: string; reason: string }[];
    warnings: string[];
    next: string;
    paidVideoCreations: 0;
};

export type InstanceRow = {
    instanceId: string;
    instanceIndex: number;
    materialTitle: string;
    sourceVideoStorageKey: string;
    nodes: { sourceVideo: string; lapian: string; all: string[]; videoConfig?: string; videoTarget?: string } | null;
    /** 派生状态（C5：现查 hub + 节点；查不到 = unknown，绝不判成功）。 */
    status: InstanceStatus | null;
    rule?: string;
    reason?: string;
    materialized?: "created" | "already_present" | "incomplete";
    outcome?: string;
    stage?: string;
    outcomeReason?: string;
};

export type RunWorkflowBatchResult = {
    ok: boolean;
    batchId: string;
    templateId: string;
    title?: string;
    concurrency: number;
    queue?: { phase: string; concurrency: number; attempted: number; awaitingApproval: number; failed: number; skipped: number };
    instances: InstanceRow[];
    /** 建实例阶段的失败（P3 incomplete / 计划校验拒绝）：人话原因，不冒充已入批。 */
    materializeFailures: { index: number; title: string; stage: string; reason: string }[];
    /** 付费口径：本工具内视频生成恒 0（停在待审批）。 */
    paidVideoCreations: 0;
    next: string;
    warnings: string[];
};

// ─────────────────────────────────────────────── ① 抽模板

/**
 * `extract_workflow_template`：从当前画布项目抽模板并**落盘到当前项目**（`workflowTemplate`）。
 *
 * `sourceNodeIds` 缺省 = 全项目「可抽象子图」= 骨架角色节点（源视频/拉片/指令/意图/终稿/两种配置/固定素材/分组）；
 * 派生运行态节点（生成产物、挂图、资产位）不进默认子图，但它们造成的跨界引用会**逐条如实报告**
 * （P2 判据：图外引用非空 → 模板不冻结，实例化会被拦下，提示把引用节点一起选进来）。
 */
export async function extractWorkflowTemplateTool(input: ExtractTemplateInput, deps: WorkflowEntryDeps): Promise<ExtractTemplateResult> {
    const project = requireProject(deps);
    const explicit = (input.sourceNodeIds || []).map((id) => String(id || "").trim()).filter(Boolean);
    const selected = explicit.length ? explicit : defaultTemplateScope(project.nodes);
    if (!selected.length) {
        throw new Error("当前画布没有可抽象成模板的节点（至少要有一个源视频/拉片/指令/文本配置节点）：先把「视频拆解→生成」链摆上画布再抽模板");
    }
    const extracted = extractWorkflowTemplate({
        sourceProject: { id: project.id, title: project.title || "", nodes: project.nodes, connections: project.connections },
        selectedNodeIds: selected,
        ...(input.templateId ? { templateId: input.templateId } : {}),
        ...(input.name ? { name: input.name } : {}),
    });
    const template = extracted.template;
    await deps.saveTemplate(template);
    await deps.flush("p9-extract-template");

    const refs = extracted.report.externalReferences;
    const warnings = [...extracted.report.warnings];
    if (refs.length) {
        const extraIds = [...new Set(refs.map((reference) => reference.targetId))];
        warnings.push(`存在 ${refs.length} 条图外引用：模板**不冻结**（实例化时会被自足性校验拦下）。把 ${extraIds.slice(0, 5).join("、")}${extraIds.length > 5 ? " 等" : ""} 一起选进 sourceNodeIds 再抽一次`);
    }
    deps.log?.(`p9-extract ${template.templateId} nodes=${selected.length} refs=${refs.length}`);
    return {
        ok: true,
        templateId: template.templateId,
        name: template.name,
        sourceNodeIds: template.sourceNodeIds,
        nodeCount: template.sourceNodeIds.length,
        slots: template.slots.map((slot) => ({
            kind: slot.kind,
            label: slot.label,
            required: slot.required,
            ...(slot.derived === undefined ? {} : { derived: slot.derived }),
            targets: slot.targets.map((target) => target.nodeId),
        })),
        frozen: {
            topologyDigest: template.frozen.topology.digest,
            nodeCount: template.frozen.topology.nodeIds.length,
            edgeCount: template.frozen.topology.edgeCount,
            instructions: template.frozen.instructions.length,
            generationModes: template.frozen.generationModes.map((item) => ({ nodeId: item.nodeId, mode: item.mode })),
            composerSkeletons: template.frozen.composerSkeletons.length,
        },
        externalReferences: refs.map((reference) => ({
            kind: reference.kind,
            fromNodeId: reference.fromNodeId,
            field: reference.field,
            targetId: reference.targetId,
            reason: reference.reason,
            ...(reference.token ? { token: reference.token } : {}),
        })),
        frozenOk: refs.length === 0,
        excluded: extracted.report.excluded.map((item) => ({ nodeId: item.nodeId, role: item.role, reason: item.reason })),
        warnings,
        next: refs.length
            ? "模板已存到本画布但不能实例化：补全图外引用后重新抽模板（run_workflow_batch 会拒绝不冻结的模板）"
            : `模板已装到本画布（workflowTemplate=${template.templateId}）。接着调 run_workflow_batch 建批次；终点是「待审批」，审批与提交出片在画布「待审批」侧栏操作`,
        paidVideoCreations: 0,
    };
}

/** 缺省抽取范围：骨架角色节点（派生运行态节点不自动并入，越界引用由 P2 报告）。 */
export function defaultTemplateScope(nodes: CanvasNodeData[]): string[] {
    const skeleton = (nodes || []).filter((node) => isSkeletonRole(classifyTemplateRole(node)));
    return (skeleton.length ? skeleton : nodes || []).map((node) => node.id);
}

// ─────────────────────────────────────────────── ② 建批次并跑

/**
 * `run_workflow_batch`：逐素材 materialize（P3 幂等）→ 入册（P5）→ 跑队列（P6 并发/对账，经 startBatch）。
 * 终点 = 每实例 `awaiting_approval`（P4 判据）；**本工具内不产生任何付费视频创建**。
 */
export async function runWorkflowBatchTool(input: RunWorkflowBatchInput, deps: WorkflowEntryDeps): Promise<RunWorkflowBatchResult> {
    const project = requireProject(deps);
    const { template } = resolveTemplate(project, input.templateId);
    // P2 契约：图外引用非空 = 模板不冻结 → 实例化器必须拒绝（不能带着断链去批量花钱）。
    if ((template.externalReferences || []).length) {
        const targets = [...new Set((template.externalReferences || []).map((reference) => reference.targetId))];
        throw new Error(
            `模板 ${template.templateId} 不冻结（${template.externalReferences.length} 条图外引用：${targets.slice(0, 5).join("、")}${targets.length > 5 ? " 等" : ""}）：先补全这些节点后用 extract_workflow_template 重抽模板，再建批次`,
        );
    }
    const rawMaterials = Array.isArray(input.materials) ? input.materials : [];
    if (!rawMaterials.length) throw new Error("materials 不能为空：至少给一个素材（sourceVideoNodeId 或 storageKey）");
    const concurrency = clampConcurrency(input.concurrency);
    const now = deps.now ? deps.now() : new Date().toISOString();

    const materialList = rawMaterials.map((material) => materialOf(material, project, deps));
    const batchId = String(input.batchId || "").trim() || deriveEntryBatchId(template.templateId, materialList, input.title);

    const templatePlan = templatePlanInput(project, template);
    const warnings: string[] = [];
    const materializeFailures: RunWorkflowBatchResult["materializeFailures"] = [];
    const members: WorkflowBatchMember[] = [];
    const created: { member: WorkflowBatchMember; material: WorkflowInstanceMaterials; materialized: "created" | "already_present"; nodes: CanvasNodeData[] }[] = [];

    // 幂等重放：同批次（同模板 + 同素材组 + 同标题 → 同一 batchId）已存在 → **不重建实例**，直接续跑队列。
    // 已到待批/已批/已完成的实例由队列按派生状态跳过（P6 planBatchResume），因此重复调用零重建、零重复拉片。
    const replay = findWorkflowBatch(project.workflowBatches, batchId);
    if (replay) {
        if (replay.members.length !== materialList.length) {
            throw new Error(`批次 ${batchId} 已存在但成员数不同（现存 ${replay.members.length} / 本次 ${materialList.length}）：同批次不换成员，换素材请换 title 或用新的模板`);
        }
        const existingNodes = deps.project()?.nodes || project.nodes;
        for (const member of replay.members) created.push({ member, material: materialOf(memberToMaterialInput(member), project, { ...deps, findNode: () => undefined }), materialized: "already_present", nodes: existingNodes });
        deps.log?.(`p9-batch ${batchId} replay members=${replay.members.length}`);
        const replayed = await deps.runBatch(batchId);
        const replayBatch = findWorkflowBatch(deps.project()?.workflowBatches, batchId) || replay;
        return buildBatchResult({ batchId, template, record: replayBatch, concurrency, run: replayed, instances: await summarizeInstances(replayBatch, created, replayed, deps), materializeFailures, warnings });
    }

    // 同号实例已被别的素材占住 → 拒绝（P3 幂等键 = 模板 + 实例序号；already_present 会静默忽略新素材，不能让它发生）
    for (const [index, material] of materialList.entries()) {
        const clash = findInstanceClash(project.workflowBatches, template.templateId, index + 1, material, warnings);
        if (clash) {
            materializeFailures.push({ index: index + 1, title: String(material.sourceVideo.title || ""), stage: "instance_index_taken", reason: clash });
        }
    }
    if (materializeFailures.length) {
        throw new Error(`拒绝建批次：${materializeFailures.map((item) => `#${item.index} ${item.reason}`).join("；")}`);
    }

    for (const [index, material] of materialList.entries()) {
        const instanceIndex = index + 1;
        const title = String(material.sourceVideo.title || "").trim() || `素材 ${instanceIndex}`;
        let result: Awaited<ReturnType<typeof materializeWorkflowInstance>>;
        try {
            result = await materializeWorkflowInstance(
                {
                    template,
                    templateNodes: templatePlan.nodes,
                    templateConnections: templatePlan.connections,
                    materials: material,
                    instanceIndex,
                    vetoes: deps.vetoes(),
                    lapianConfig: deps.lapianConfig || { lapianDefaultModel: "", channels: [] },
                },
                {
                    applyOps: deps.applyOps,
                    getBlob: deps.getBlob,
                    fetch: (url, init) => fetch(url, init),
                    existingNodes: () => deps.project()?.nodes || project.nodes,
                    log: deps.log,
                },
            );
        } catch (error) {
            materializeFailures.push({ index: instanceIndex, title, stage: "plan", reason: (error as Error).message });
            continue;
        }
        if (result.status === "incomplete") {
            materializeFailures.push({ index: instanceIndex, title, stage: result.incomplete?.stage || "unknown", reason: result.incomplete?.reason || "实例化失败（未给出原因）" });
            continue;
        }
        if (result.plan.carriedExternalReferences.length) {
            warnings.push(`实例 ${result.plan.instanceId} 携带 ${result.plan.carriedExternalReferences.length} 条模板图外引用（不影响本次自足性，供复核）`);
        }
        const nodes = mergeNodes(deps.project()?.nodes || project.nodes, result.plan.nodes);
        const connections = mergeConnections(deps.project()?.connections || project.connections, result.plan.connections);
        const identity = (deps.identities ? deps.identities() : undefined)?.find((record) => record.instanceId === result.plan.instanceId);
        const member = buildBatchMember({
            plan: result.plan,
            materials: material,
            nodes,
            connections,
            ...(identity ? { identity } : {}),
            ...(result.lapian ? { lapian: { taskId: result.lapian.taskId, jobId: result.lapian.jobId } } : {}),
            createdAt: now,
        });
        members.push(member);
        created.push({ member, material, materialized: result.status, nodes });
    }

    if (!members.length) {
        throw new Error(`没有实例成功落库：${materializeFailures.map((item) => `#${item.index} ${item.stage}：${item.reason}`).join("；") || "未给出原因"}`);
    }

    const record = createWorkflowBatch({ templateId: template.templateId, batchId, ...(input.title ? { title: input.title } : {}), createdAt: now, members });
    const withQueue: WorkflowBatchRecord = { ...record, queue: createBatchQueue({ members, concurrency, startedAt: now, updatedAt: now }) };
    const batches = upsertBatchRecord(deps.project()?.workflowBatches || project.workflowBatches, withQueue, now);
    await deps.saveBatches(batches);
    await deps.flush("p9-batch-enqueue");
    deps.log?.(`p9-batch ${batchId} members=${members.length} concurrency=${concurrency}`);

    const run = await deps.runBatch(batchId);
    const liveBatch = findWorkflowBatch(deps.project()?.workflowBatches, batchId) || withQueue;
    const instances = await summarizeInstances(liveBatch, created, run, deps);

    return buildBatchResult({ batchId, template, record: liveBatch, concurrency, run, instances, materializeFailures, warnings });
}

/** 回执组装（首跑与幂等重放共用一份口径）。 */
function buildBatchResult(input: {
    batchId: string;
    template: CanvasWorkflowTemplate;
    record: WorkflowBatchRecord;
    concurrency: number;
    run: EntryBatchRunOutcome;
    instances: InstanceRow[];
    materializeFailures: RunWorkflowBatchResult["materializeFailures"];
    warnings: string[];
}): RunWorkflowBatchResult {
    const { batchId, template, record, run, instances } = input;
    return {
        ok: instances.length > 0,
        batchId,
        templateId: template.templateId,
        ...(record.title ? { title: record.title } : {}),
        concurrency: run.queue?.concurrency || input.concurrency,
        queue: run.queue
            ? {
                  phase: run.queue.phase,
                  concurrency: run.queue.concurrency,
                  attempted: run.queue.summary?.attempted || 0,
                  awaitingApproval: run.queue.summary?.awaitingApproval || 0,
                  failed: (run.queue.summary?.failed || []).length,
                  skipped: (run.queue.summary?.skippedDone || []).length,
              }
            : undefined,
        instances,
        materializeFailures: input.materializeFailures,
        paidVideoCreations: 0,
        next: run.ok
            ? `批次 ${batchId} 已跑到待审批（${instances.filter((row) => row.status === "awaiting_approval").length}/${instances.length}）：审批与提交出片请到画布左侧「待审批」页签操作，付费视频生成不在本工具内发生`
            : `批次 ${batchId} 已入册但队列没跑成：${run.reason || "未给出原因"}（实例仍在，可在侧栏「待审批」页签重试）`,
        warnings: input.warnings,
    };
}

/**
 * `run_workflow_instance`：单素材单实例（调试用）。实现 = 单成员批次（同一队列/同一编排，零第二套路径），
 * 批次 id 按「模板 + 素材」确定性派生 → 同素材重复调用落在同一批次同一实例（P3 幂等，零冲突）。
 */
export async function runWorkflowInstanceTool(input: RunWorkflowInstanceInput, deps: WorkflowEntryDeps): Promise<RunWorkflowBatchResult> {
    if (!input.material) throw new Error("material 必填：给 sourceVideoNodeId 或 storageKey");
    return await runWorkflowBatchTool(
        {
            ...(input.templateId ? { templateId: input.templateId } : {}),
            materials: [input.material],
            concurrency: 1,
            title: input.title || "单实例调试",
            ...(input.batchId ? { batchId: input.batchId } : {}),
        },
        deps,
    );
}

// ─────────────────────────────────────────────── 内部胶水

function requireProject(deps: WorkflowEntryDeps): EntryProject {
    const project = deps.project();
    if (!project) throw new Error("当前没有打开的画布项目：先 site_navigate 打开 /canvas/:id 再调本工具");
    if (!Array.isArray(project.nodes)) throw new Error("画布项目还没装载完（nodes 缺失）：稍后重试");
    return project;
}

/** 模板解析：优先显式 templateId，缺省用当前画布的声明；跨画布一律拒绝（不串批次）。 */
function resolveTemplate(project: EntryProject, templateId?: string): { template: CanvasWorkflowTemplate } {
    const current = project.workflowTemplate;
    const wanted = String(templateId || "").trim();
    if (!current) {
        throw new Error("当前画布还没有工作流模板：先调 extract_workflow_template（模板声明随画布项目保存），再建批次");
    }
    if (wanted && wanted !== current.templateId) {
        throw new Error(`当前画布的模板是 ${current.templateId}，与请求的 ${wanted} 不一致：批次必须在模板所在画布上建（不跨画布串批次）`);
    }
    return { template: current };
}

/**
 * 模板计划入参：`planWorkflowInstance` 要求**已剥运行态**的模板节点（`validateTemplateProject` 会拦
 * taskId/status 残留）。画布上的模板节点是用户真节点（带运行态），所以按冻结拓扑的 nodeIds 用 P2
 * 同一函数重抽一次取「剥离后节点」——纯函数、确定性、零副作用（模板声明仍用落盘那份，不动冻结项）。
 */
export function templatePlanInput(project: EntryProject, template: CanvasWorkflowTemplate): { nodes: CanvasNodeData[]; connections: CanvasConnection[] } {
    const declared = template.frozen?.topology?.nodeIds || [];
    if (!declared.length) throw new Error("模板冻结拓扑没有节点 id：模板声明不完整（重新抽模板）");
    const present = new Set((project.nodes || []).map((node) => node.id));
    const missing = declared.filter((id) => !present.has(id));
    if (missing.length) throw new Error(`模板节点已不在画布（缺 ${missing.length} 个：${missing.slice(0, 3).join(",")}）：不能建实例`);
    const extracted = extractWorkflowTemplate({
        sourceProject: { id: project.id, title: project.title || "", nodes: project.nodes, connections: project.connections },
        selectedNodeIds: declared,
        templateId: template.templateId,
        name: template.name,
        createdAt: template.createdAt,
    });
    return { nodes: extracted.project.nodes, connections: extracted.project.connections };
}

/** 素材 → 引擎素材（槽①必填 storageKey；节点优先，其次显式 storageKey）。 */
function materialOf(input: WorkflowBatchMaterialInput, project: EntryProject, deps: WorkflowEntryDeps): WorkflowInstanceMaterials {
    const nodeId = String(input.sourceVideoNodeId || "").trim();
    const node = nodeId ? deps.findNode?.(nodeId) || (project.nodes || []).find((item) => item.id === nodeId) : undefined;
    if (nodeId && !node) throw new Error(`画布上找不到源视频节点 ${nodeId}：确认节点 id（可用 canvas_get_state 查）`);
    const storageKey = String(input.storageKey || node?.metadata?.storageKey || "").trim();
    if (!storageKey) {
        throw new Error(
            nodeId
                ? `节点 ${nodeId} 上没有本地字节（storageKey 为空，可能是外部链接或已剥离的模板节点）：换成带本地字节的视频节点，或直接给 storageKey`
                : "素材缺源视频：给 sourceVideoNodeId（画布上带本地字节的视频节点）或直接给 storageKey",
        );
    }
    const sourceVideo: WorkflowInstanceSourceVideo = {
        storageKey,
        ...(String(input.title || node?.title || "").trim() ? { title: String(input.title || node?.title).trim() } : {}),
        ...(String(node?.metadata?.content || "") ? { content: String(node?.metadata?.content) } : {}),
        ...(String(node?.metadata?.mimeType || "") ? { mimeType: String(node?.metadata?.mimeType) } : {}),
        ...(Number(node?.metadata?.bytes) > 0 ? { bytes: Number(node?.metadata?.bytes) } : {}),
        ...(Number(node?.metadata?.durationMs) > 0 ? { durationMs: Number(node?.metadata?.durationMs) } : {}),
        ...(Number(node?.metadata?.naturalWidth) > 0 ? { naturalWidth: Number(node?.metadata?.naturalWidth) } : {}),
        ...(Number(node?.metadata?.naturalHeight) > 0 ? { naturalHeight: Number(node?.metadata?.naturalHeight) } : {}),
    };
    const intentText = String(input.intentText || "").trim();
    const optionalSegments = String(input.optionalSegments || "").trim();
    return {
        sourceVideo,
        ...(intentText ? { intentText } : {}),
        ...(optionalSegments ? { optionalSegments } : {}),
    };
}

/** 批次 id：同模板 + 同素材组（含标题）+ 同序号 → 同一 id（重放不产生第二份批次）。 */
function deriveEntryBatchId(templateId: string, materials: WorkflowInstanceMaterials[], title?: string): string {
    const digest = materials
        .map((material, index) => `${index + 1}:${material.sourceVideo.storageKey}|${material.sourceVideo.title || ""}|${String(material.intentText || "")}|${String(material.optionalSegments || "")}`)
        .join(",");
    return `batch-${stableDigest([templateId, digest, String(title || "")].join("|")).slice(0, 8)}`;
}

/** 成员身份 → 素材输入（幂等重放时重建「本次素材」视图；素材摘要里已存 storageKey/标题/可选段）。 */
function memberToMaterialInput(member: WorkflowBatchMember): WorkflowBatchMaterialInput {
    const summary = member.materials;
    return {
        ...(summary?.sourceVideoStorageKey ? { storageKey: summary.sourceVideoStorageKey } : {}),
        ...(summary?.sourceVideoTitle ? { title: summary.sourceVideoTitle } : {}),
        ...(summary?.optionalSegments ? { optionalSegments: summary.optionalSegments } : {}),
    };
}

/**
 * 同号实例冲突检查（P3 幂等键 = 模板 + 实例序号）：已有成员用**别的素材组**占了这个序号 → 拒绝。
 * 为什么必须拦：`materializeWorkflowInstance` 见到已落库拉片节点会直接 `already_present`，
 * 换素材会静默沿用旧素材（P3 幂等优先），这里把「静默」换成「明说」。
 */
function findInstanceClash(
    records: WorkflowBatchRecord[] | undefined,
    templateId: string,
    instanceIndex: number,
    material: WorkflowInstanceMaterials,
    warnings: string[],
): string | null {
    const summary = summarizeMaterials(material);
    for (const record of records || []) {
        for (const member of record.members || []) {
            if (member.instanceIndex !== instanceIndex || String(member.templateId) !== String(templateId)) continue;
            const prior = member.materials;
            const priorKey = String(prior?.sourceVideoStorageKey || "");
            // 同源视频 = 同一个实例（P3 幂等键：模板 + 序号 + 已落库拉片节点）→ 允许重复调用。
            if (!priorKey || priorKey === summary.sourceVideoStorageKey) {
                const intentDiffers = (prior?.intentChars ?? 0) !== summary.intentChars;
                const segmentsDiffer = String(prior?.optionalSegments || "") !== String(summary.optionalSegments || "");
                if (intentDiffers || segmentsDiffer) {
                    warnings.push(
                        `实例 #${instanceIndex}（${record.batchId}）已用同一素材落库：本次传入的${intentDiffers ? "意图" : ""}${intentDiffers && segmentsDiffer ? "与" : ""}${segmentsDiffer ? "可选段" : ""}不会覆盖已落库链条（P3 幂等），要改请换实例序号/模板`,
                    );
                }
                continue;
            }
            return `实例序号 #${instanceIndex} 已被别的素材占住（批次 ${record.batchId} 用的是 ${priorKey}）：换素材请换模板或换实例序号（同号实例不覆盖已落库链条）`;
        }
    }
    return null;
}

/** 同批次同实例只留最新一条（P5 `upsertBatchMember` 语义；队列簿记清空 → 新一轮排队由 startBatch 重建）。 */
function upsertBatchRecord(records: WorkflowBatchRecord[] | undefined, record: WorkflowBatchRecord, now: string): WorkflowBatchRecord[] {
    const list = Array.isArray(records) ? records : [];
    const index = list.findIndex((item) => item.batchId === record.batchId);
    if (index < 0) return [...list, record];
    const current = list[index];
    const byInstance = new Map(current.members.map((member) => [member.instanceId, member]));
    record.members.forEach((member) => byInstance.set(member.instanceId, member));
    const members = [...byInstance.values()];
    const next: WorkflowBatchRecord = {
        ...current,
        title: record.title ?? current.title,
        members,
        signature: batchSignature({ templateId: current.templateId, members }),
        updatedAt: now,
        queue: undefined,
    };
    return [...list.slice(0, index), next, ...list.slice(index + 1)];
}

function mergeNodes(left: CanvasNodeData[], right: CanvasNodeData[]): CanvasNodeData[] {
    const byId = new Map(left.map((node) => [node.id, node]));
    right.forEach((node) => byId.set(node.id, node));
    return [...byId.values()];
}

function mergeConnections(left: CanvasConnection[], right: CanvasConnection[]): CanvasConnection[] {
    const byId = new Map(left.map((connection) => [connection.id, connection]));
    right.forEach((connection) => byId.set(connection.id, connection));
    return [...byId.values()];
}

/** 每实例回执：派生状态（现查 hub）+ 队列结论 + 节点骨架（C5：状态不算、不存）。 */
async function summarizeInstances(
    batch: WorkflowBatchRecord,
    created: { member: WorkflowBatchMember; material: WorkflowInstanceMaterials; materialized: "created" | "already_present"; nodes: CanvasNodeData[] }[],
    run: EntryBatchRunOutcome,
    deps: WorkflowEntryDeps,
): Promise<InstanceRow[]> {
    const liveNodes = deps.project()?.nodes || [];
    const outcomes = new Map((run.result?.perInstance || []).map((item) => [item.instanceId, item]));
    const rows: InstanceRow[] = [];
    for (const entry of created) {
        const member = batch.members.find((item) => item.instanceId === entry.member.instanceId) || entry.member;
        const outcome = outcomes.get(member.instanceId);
        let hubState: string | null | undefined;
        if (member.hub?.localId) {
            try {
                hubState = String((await deps.hub.getTask(member.hub.localId))?.state || "");
            } catch {
                hubState = null;
            }
        }
        // 页面 store 与编排写入之间有一拍延迟：用「落库时的画布快照」补上本实例节点，避免误判成节点缺失。
        const nodes = mergeNodes(entry.nodes, liveNodes);
        const derived = nodes.length ? deriveInstanceStatus({ member, nodes, hubState }) : null;
        rows.push({
            instanceId: member.instanceId,
            instanceIndex: member.instanceIndex,
            materialTitle: String(member.materials?.sourceVideoTitle || entry.material.sourceVideo.title || `素材 ${member.instanceIndex}`),
            sourceVideoStorageKey: String(member.materials?.sourceVideoStorageKey || entry.material.sourceVideo.storageKey),
            nodes: member.nodes?.lapian
                ? { sourceVideo: member.nodes.sourceVideo, lapian: member.nodes.lapian, all: member.nodes.all, ...(member.nodes.videoConfig ? { videoConfig: member.nodes.videoConfig } : {}), ...(member.nodes.videoTarget ? { videoTarget: member.nodes.videoTarget } : {}) }
                : null,
            status: derived?.status ?? null,
            ...(derived?.rule ? { rule: derived.rule } : {}),
            ...(derived?.reason ? { reason: derived.reason } : {}),
            materialized: entry.materialized,
            ...(outcome ? { outcome: outcome.outcome } : {}),
            ...(outcome?.stage ? { stage: outcome.stage } : {}),
            ...(outcome?.reason ? { outcomeReason: outcome.reason } : {}),
        });
    }
    return rows.sort((left, right) => left.instanceIndex - right.instanceIndex);
}

function clampConcurrency(value: unknown): number {
    const number = Math.floor(Number(value));
    if (!Number.isFinite(number) || number <= 0) return 3;
    return Math.min(6, Math.max(1, number));
}
