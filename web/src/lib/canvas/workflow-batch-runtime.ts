/**
 * P8 · 批次队列的**页面运行时薄壳**（把 P6 队列 + P4 编排接到画布页上；禁另写队列/另写编排）。
 *
 * 依据：
 * - 卡面红线 25：队列一律走 P6 `createBatchQueue/planBatchResume/runBatchQueue`；单实例编排一律走
 *   P4 `runWorkflowInstance`；幂等落库一律走 P3 `materializeWorkflowInstance`。本文件**只有胶水**：
 *   把画布节点/ops 应用/生成链/hub 客户端/落盘 这几样现成能力包成队列要的依赖。
 * - 卡面断言：本文件**不发起任何视频创建**——`runGeneration` 只会被 P4 以 `mode:"text"` 调用一次
 *   （`workflow-instance-run.ts` 的既有约束），本文件不直接调它、也不拼生成请求。
 * - 状态一律现查派生（C5）：续跑计划看 `deriveInstanceStatus` 的当下结论（hub 现查），
 *   队列条目只当簿记。
 *
 * 为什么是模块级单例：队列运行时依赖画布页的 `applyOps`/`generateNodeRef`/`flushProjects`，
 * 这些只有页面里有。侧栏（`canvas-pending-review-tab.tsx`）拿不到它们，所以由 `project.tsx`
 * 在挂载时注册、卸载时注销；未注册时所有动作如实返回 `{ok:false, reason:"..."}`（**不假跑**）。
 */

import type { CanvasConnection, CanvasNodeData } from "@/types/canvas";

import type { CanvasAgentOp } from "@/lib/canvas/canvas-agent-ops";
import { findBatchMember, findWorkflowBatch, type WorkflowBatchMember, type WorkflowBatchRecord } from "@/lib/canvas/workflow-batch";
import { createBatchQueue, planBatchResume, runBatchQueue, type BatchQueueInstanceOutcome, type BatchQueueJob, type BatchQueueRunResult, type WorkflowBatchQueueRecord } from "@/lib/canvas/workflow-batch-queue";
import { deriveInstanceStatus, type InstanceStatus } from "@/lib/canvas/workflow-batch-status";
import type { WorkflowInstanceHubClient, WorkflowInstanceIdentityRecord, InstanceRunDeps } from "@/lib/canvas/workflow-instance-run";
import { runWorkflowInstance } from "@/lib/canvas/workflow-instance-run";
import { materializeWorkflowInstance, planWorkflowInstance, type WorkflowInstanceMaterials, type WorkflowInstancePlan } from "@/lib/canvas/workflow-instance";
import type { LapianExecuteConfig } from "@/lib/canvas/lapian-execute";
import type { WorkflowAssetVetoRecord } from "@/lib/canvas/workflow-asset-match";
import type { CanvasWorkflowTemplate } from "@/lib/canvas/workflow-template";
import { buildMaterialsFromMember } from "@/lib/canvas/workflow-pending-review";

/**
 * 画布项目的结构下限（只读：运行时从不改项目对象，写一律走注入的 save* 回调）。
 * 用结构类型而不是 import store 类型，避免 `lib → stores` 的运行时依赖环。
 */
export type BatchRuntimeProject = {
    id: string;
    title?: string;
    nodes: CanvasNodeData[];
    connections: CanvasConnection[];
    workflowTemplate?: CanvasWorkflowTemplate;
    workflowBatches?: WorkflowBatchRecord[];
};

export type WorkflowBatchRuntimeDeps = {
    /** 画布 ops 应用（页面 = `applyAgentOps`；探针 = 真 `applyCanvasAgentOps` 包一层）。 */
    applyOps: (ops: CanvasAgentOp[]) => void | Promise<void>;
    /**
     * 既有生成链（页面 = `generateNodeRef`）。**只用于非付费文本组装**：P4 只会用 `mode:"text"` 调它。
     * 必须回报本次新建的子节点 id（G18 闭合），拿不到就是失败，绝不按「最新节点」猜。
     */
    runGeneration: (input: { nodeId: string; mode: "text" | "image" | "video" | "audio"; prompt: string }) => Promise<{ childNodeId?: string }>;
    /** hub 身份客户端（生产 = `createHubIdentityClient()`）。 */
    hub: WorkflowInstanceHubClient;
    getBlob: (storageKey: string) => Promise<Blob | null>;
    saveIdentity: (record: WorkflowInstanceIdentityRecord) => void | Promise<void>;
    saveBatchMember: (member: WorkflowBatchMember) => void | Promise<void>;
    /**
     * 队列落库（`setBatchQueue(...) + updateProject` 的唯一出口）。
     *
     * 任务书 §2.2 的 deps 清单只给了 `flush(reason)`（落盘）没给「写队列」通道，但同一节的
     * `startBatch` 契约要求 `persist: q => setBatchQueue(...)+updateProject`——没有写通道这条契约
     * 无法成立。所以这里补一个最小写回调（页面用既有 `updateProject` 实现），不新造 store 字段。
     */
    saveBatchQueue: (batchId: string, queue: WorkflowBatchQueueRecord) => void | Promise<void>;
    flush: (reason: string) => void | Promise<void | { ok?: boolean; error?: string }>;
    vetoes: () => WorkflowAssetVetoRecord[] | undefined;
    wanFileTasks: () => Map<string, string> | Record<string, string> | undefined;
    /** 当前画布项目（**只读**）。 */
    project: () => BatchRuntimeProject | undefined;
    /**
     * 拉片模型配置（P3 `materializeWorkflowInstance` 的必填入参）。
     *
     * 任务书 deps 清单没列它，但「实例还没落库时要真拉片」绕不开它。缺省时：实例已在画布
     * （`already_present`）照常跑；需要真拉片的实例如实返回 `{ok:false, reason}`，**不拿空配置假跑**。
     */
    lapianConfig?: LapianExecuteConfig;
    now?: () => string;
    log?: (message: string) => void;
};

let runtime: WorkflowBatchRuntimeDeps | null = null;
/** 已请求暂停的批次（内存态，边界停在 `runBatchQueue` 的 `shouldPause` 里）。 */
const pausedBatches = new Set<string>();

/** 注册页面运行时（返回注销函数：画布页卸载时调用，避免把死掉的 ref 留在单例里）。 */
export function registerWorkflowBatchRuntime(deps: WorkflowBatchRuntimeDeps): () => void {
    runtime = deps;
    return () => {
        if (runtime === deps) runtime = null;
        pausedBatches.clear();
    };
}

export function getWorkflowBatchRuntime(): WorkflowBatchRuntimeDeps | null {
    return runtime;
}

export type BatchActionFailure = { ok: false; reason: string };
export type BatchActionResult = BatchActionFailure | { ok: true; queue: WorkflowBatchQueueRecord; result?: BatchQueueRunResult };

const NOT_REGISTERED = "批次运行时未注册：画布页还没装载队列运行时（刷新页面重试；未注册时不假跑）";

function nowIso(deps: WorkflowBatchRuntimeDeps): string {
    return deps.now ? deps.now() : new Date().toISOString();
}

function nodesOf(deps: WorkflowBatchRuntimeDeps): CanvasNodeData[] {
    return deps.project()?.nodes || [];
}

/** 实例是否已落库（P3 幂等判据同一条：拉片节点带 taskId 且骨架节点都在）。 */
function isMaterialized(member: WorkflowBatchMember, nodes: CanvasNodeData[]): boolean {
    const present = new Set(nodes.map((node) => node.id));
    const lapian = nodes.find((node) => node.id === member.nodes.lapian);
    return Boolean(String(lapian?.metadata?.taskId || "")) && (member.nodes.all || []).every((id) => present.has(id));
}

/**
 * 批次成员的 hub 当前 state（现查；单条失败 → null = 查不到，派生未知，**不当成功**）。
 * 分批并发 ≤6（卡面 §3.10）。
 */
async function loadHubStates(deps: WorkflowBatchRuntimeDeps, batch: WorkflowBatchRecord): Promise<Record<string, string | null>> {
    const states: Record<string, string | null> = {};
    const pending = batch.members.filter((member) => String(member.hub?.localId || "").trim());
    for (let cursor = 0; cursor < pending.length; cursor += 6) {
        const slice = pending.slice(cursor, cursor + 6);
        const rows = await Promise.all(
            slice.map(async (member) => {
                try {
                    const task = await deps.hub.getTask(String(member.hub!.localId));
                    return [member.instanceId, String(task?.state || "")] as const;
                } catch {
                    return [member.instanceId, null] as const;
                }
            }),
        );
        for (const [instanceId, state] of rows) states[instanceId] = state;
    }
    return states;
}

/** 模板重建（P3 计划入参）：只用项目里现存的模板声明 + 模板节点，不新造第二份模板。 */
function planInputFor(project: BatchRuntimeProject, member: WorkflowBatchMember): { template: CanvasWorkflowTemplate; templateNodes: CanvasNodeData[]; templateConnections: CanvasConnection[] } | { error: string } {
    const template = project.workflowTemplate;
    if (!template) return { error: "项目没有工作流模板声明（workflowTemplate 缺失）：无法重建实例计划，不假跑" };
    if (String(template.templateId) !== String(member.templateId)) return { error: `项目模板 ${template.templateId} 与成员模板 ${member.templateId} 不一致：拒绝按错模板重跑` };
    const declared = new Set<string>(template.frozen?.topology?.nodeIds || []);
    const nodes = project.nodes || [];
    const byId = new Map(nodes.map((node) => [node.id, node]));
    const missing = [...declared].filter((id) => !byId.has(id));
    if (missing.length) return { error: `模板节点已不在画布（缺 ${missing.length} 个：${missing.slice(0, 3).join(",")}）：不能重建实例计划` };
    const templateNodes = [...declared].map((id) => byId.get(id)!).filter(Boolean);
    const connections = project.connections || [];
    return { template, templateNodes, templateConnections: connections.filter((edge) => declared.has(edge.fromNodeId) && declared.has(edge.toNodeId)) };
}

function instanceRunDeps(deps: WorkflowBatchRuntimeDeps): InstanceRunDeps {
    return {
        nodes: () => nodesOf(deps),
        connections: () => deps.project()?.connections || [],
        applyOps: deps.applyOps,
        runGeneration: deps.runGeneration,
        // 槽⑤可选段投影刷新：与 P4 生产同一条链（既有 `/api/render/...` 读取），不新写轮询。
        fetchProjection: (url, init) => fetch(url, init),
        hub: deps.hub,
        getBlob: deps.getBlob,
        saveIdentity: deps.saveIdentity,
        saveBatchMember: deps.saveBatchMember,
        wanFileTasks: deps.wanFileTasks,
        vetoes: deps.vetoes,
        flush: (stage) => deps.flush(`p8-${stage}`),
        ...(deps.now ? { now: deps.now } : {}),
        log: deps.log,
    };
}

/** 队列作业（prepare = P3 幂等落库；run = P4 单实例编排，止于待批，零付费）。 */
function buildJob(deps: WorkflowBatchRuntimeDeps, batch: WorkflowBatchRecord, member: WorkflowBatchMember): BatchQueueJob {
    const cache: { plan?: WorkflowInstancePlan; materials?: WorkflowInstanceMaterials } = {};
    const prepare = async (): Promise<{ ok: boolean; status?: string; reason?: string }> => {
        const project = deps.project();
        if (!project) return { ok: false, reason: "画布项目还没装载：拿不到节点，不假跑" };
        const planned = planInputFor(project, member);
        if ("error" in planned) return { ok: false, reason: planned.error };
        const nodes = project.nodes || [];
        const materials = buildMaterialsFromMember(member, nodes);
        if (!String(materials.sourceVideo.storageKey || "").trim()) return { ok: false, reason: "成员素材缺源视频 storageKey：源视频节点可能已被删除，重跑会落空（不假跑）" };
        if (!isMaterialized(member, nodes) && !deps.lapianConfig) return { ok: false, reason: "缺拉片模型配置（lapianConfig）：本实例还没落库，需要真拉片才能继续（不拿空配置假跑）" };
        const materialized = await materializeWorkflowInstance(
            {
                ...planned,
                materials,
                instanceIndex: member.instanceIndex,
                vetoes: deps.vetoes(),
                lapianConfig: deps.lapianConfig || { lapianDefaultModel: "", channels: [] },
            },
            { applyOps: deps.applyOps, getBlob: deps.getBlob, fetch: (url, init) => fetch(url, init), existingNodes: () => deps.project()?.nodes || [], log: deps.log },
        );
        if (materialized.status === "incomplete") return { ok: false, reason: `实例化失败（${materialized.incomplete?.stage || "unknown"}）：${materialized.incomplete?.reason || "未给出原因"}` };
        cache.plan = materialized.plan;
        cache.materials = materials;
        return { ok: true, status: materialized.status };
    };
    const run = async () => {
        const project = deps.project();
        if (!project) throw new Error("画布项目还没装载：拿不到节点，拒绝空跑");
        let plan = cache.plan;
        if (!plan) {
            const planned = planInputFor(project, member);
            if ("error" in planned) throw new Error(planned.error);
            // prepare 已被跳过时（例如幂等重放）用当前画布重新计划一次（纯函数，零网络）。
            plan = planWorkflowInstance({ ...planned, materials: cache.materials || buildMaterialsFromMember(member, project.nodes || []), instanceIndex: member.instanceIndex, vetoes: deps.vetoes() });
        }
        const nodes = project.nodes || [];
        const lapianNode = nodes.find((node) => node.id === plan!.lapianNodeId);
        const lapian = {
            taskId: String(lapianNode?.metadata?.taskId || member.backend.lapianTaskId || ""),
            jobId: String(lapianNode?.metadata?.jobId || member.backend.lapianJobId || ""),
        };
        return runWorkflowInstance({ plan, materials: cache.materials || buildMaterialsFromMember(member, nodes), lapian, batchId: batch.batchId }, instanceRunDeps(deps));
    };
    return { instanceId: member.instanceId, instanceIndex: member.instanceIndex, prepare, run };
}

/** 统一取「运行时 + 项目 + 批次 + 成员」；缺任一项如实返回原因（人话，不假跑）。 */
function resolve(batchId: string, instanceId?: string): { deps: WorkflowBatchRuntimeDeps; project: BatchRuntimeProject; batch: WorkflowBatchRecord; member?: WorkflowBatchMember } | BatchActionFailure {
    const deps = runtime;
    if (!deps) return { ok: false, reason: NOT_REGISTERED };
    const project = deps.project();
    if (!project) return { ok: false, reason: "画布项目还没装载：批次记录读不到（不假跑）" };
    const batch = findWorkflowBatch(project.workflowBatches, batchId);
    if (!batch) return { ok: false, reason: `找不到批次 ${batchId}：批次记录可能已被删除` };
    if (!batch.members.length) return { ok: false, reason: "批次没有任何实例成员：不凭空造实例" };
    if (!instanceId) return { deps, project, batch };
    const member = findBatchMember(batch, instanceId);
    if (!member) return { ok: false, reason: `批次 ${batchId} 里没有实例 ${instanceId}` };
    if (!member.nodes?.lapian || !member.nodes.all?.length) return { ok: false, reason: `实例 ${instanceId} 的节点骨架身份缺失：不能重建链路（不假跑）` };
    return { deps, project, batch, member };
}

function isFailure(value: unknown): value is BatchActionFailure {
    return Boolean(value && typeof value === "object" && (value as BatchActionFailure).ok === false);
}

/**
 * 开始/续跑批次：`planBatchResume`（派生状态优先）→ `runBatchQueue`（并发 3 / 拥塞退避 / 未知提交对账不重发）。
 * 每个实例只做「幂等落库 → 单实例编排」，**止于 awaiting_approval，零付费视频创建**。
 */
export async function startBatch(batchId: string): Promise<BatchActionResult> {
    const resolved = resolve(batchId);
    if (isFailure(resolved)) return resolved;
    const { deps, project, batch } = resolved;
    pausedBatches.delete(batchId);
    const nodes = project.nodes || [];
    const hubStates = await loadHubStates(deps, batch);
    const statuses: Record<string, InstanceStatus> = {};
    for (const member of batch.members) statuses[member.instanceId] = deriveInstanceStatus({ member, nodes, hubState: hubStates[member.instanceId] === null ? null : hubStates[member.instanceId] }).status;
    const queue = batch.queue || createBatchQueue({ members: batch.members, updatedAt: nowIso(deps) });
    const resume = planBatchResume({ batch, queue, statuses, nodes, log: deps.log });
    const jobs = resume.run.map((instanceId) => {
        const member = findBatchMember(batch, instanceId);
        return member ? buildJob(deps, batch, member) : null;
    }).filter((job): job is BatchQueueJob => Boolean(job));
    const result = await runBatchQueue({
        batch,
        jobs,
        queue,
        resume,
        nodes,
        persist: async (next) => {
            await deps.saveBatchQueue(batchId, next);
            await deps.flush("p8-queue");
        },
        shouldPause: () => pausedBatches.has(batchId),
        log: deps.log,
    });
    return { ok: true, queue: result.queue, result };
}

/** 暂停：相位写 paused 并落库；`runBatchQueue` 在**边界**停（在途实例跑完后不再起新的）。 */
export async function pauseBatch(batchId: string): Promise<BatchActionResult> {
    const resolved = resolve(batchId);
    if (isFailure(resolved)) return resolved;
    const { deps, batch } = resolved;
    pausedBatches.add(batchId);
    const base = batch.queue || createBatchQueue({ members: batch.members, updatedAt: nowIso(deps) });
    const queue: WorkflowBatchQueueRecord = { ...base, phase: "paused", updatedAt: nowIso(deps) };
    await deps.saveBatchQueue(batchId, queue);
    await deps.flush("p8-pause");
    return { ok: true, queue };
}

/**
 * 单实例重跑（对账语义）：同一条链（materialize 幂等 → run）。
 * `done` 实例拒绝并给原因（「已完成实例不重跑」），不静默跳过。
 */
export async function rerunInstance(batchId: string, instanceId: string): Promise<{ ok: boolean; status?: string; reason?: string; outcome?: BatchQueueInstanceOutcome }> {
    const resolved = resolve(batchId, instanceId);
    if (isFailure(resolved)) return { ok: false, reason: resolved.reason };
    const { deps, project, batch, member } = resolved;
    if (!member) return { ok: false, reason: `批次 ${batchId} 里没有实例 ${instanceId}` };
    const nodes = project.nodes || [];
    const hubStates = await loadHubStates(deps, batch);
    const status = deriveInstanceStatus({ member, nodes, hubState: hubStates[instanceId] === undefined ? undefined : hubStates[instanceId] }).status;
    if (status === "done") return { ok: false, status, reason: `实例 ${instanceId} 派生状态 done：已完成实例不重跑` };
    const job = buildJob(deps, batch, member);
    const prepared = job.prepare ? await job.prepare() : { ok: true as const };
    if (!prepared.ok) return { ok: false, status, reason: prepared.reason };
    const outcome = await job.run();
    return { ok: outcome.status !== "failed", status: outcome.status, reason: outcome.failure?.reason, outcome: { instanceId, instanceIndex: member.instanceIndex, outcome: outcome.status === "needs_human" ? "failed" : outcome.status === "canvas_ready" ? "canvas_ready" : outcome.status === "awaiting_approval" ? "awaiting_approval" : "failed", stage: outcome.failure?.stage, reason: outcome.failure?.reason, attempts: 1, congestionRetries: 0, resumed: true } };
}

/** 探针/页面共用：队列相位人话（相位名本身走 i18n，这里只给相位代号）。 */
export function batchQueuePhaseOf(batch: WorkflowBatchRecord | undefined): WorkflowBatchQueueRecord["phase"] | "none" {
    return batch?.queue?.phase || "none";
}
