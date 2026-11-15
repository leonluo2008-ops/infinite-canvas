/**
 * 卡C3/C4 · 变体派生页面侧集成函数（方案 docs/方案-卡C一拆多用变体派生-20261115.md §三 D1/D2b/D7/D8
 * + 路线 A 修正 2026-11-15：拆解完成即可派生，不要求源先出片）。
 *
 * 两个入口共用同一执行体（拍板 1：双入口同函数）：
 * - C3 agent 工具 `derive_workflow_variant`（web/src/lib/agent/agent-site-tools.ts 分发到本文件）；
 * - C4 源实例拉片节点悬浮栏「派生变体」（web/src/components/canvas/lapian-variant-modal.tsx）。
 *
 * 顺序（路线 A：直建变体任务，不走 fork）：
 * ① 校验源（拉片节点 taskId/content 双全 = 拆解产物在位；metadata.taskId 是**桥侧拆解任务号
 *    =bridge_id**，正确用途是拆解产物引用——不是 hub localId，禁拿去 fork）；
 * ② 变体直建（POST /api/hub/variant/create，bridge_id + intent → 新 localId；不经 ingest 的
 *    视频 sha 幂等，同源视频 N 个变体各得独立任务号）；
 * ③ deriveWorkflowVariant（C1 纯逻辑：子图副本/id 重写/槽起手空/意图继承，newTaskId=新 localId）；
 * ④ bindVariantToHub（C2b 直绑通道：零 ingest，awaiting_assets→awaiting_approval，停待批）；
 * ⑤ canvas_apply_ops 落新分支节点+连线；
 * ⑥ 回执（节点 id 清单/新 localId/槽绑定结果/停点状态，D8）。
 *
 * 付费红线：本文件从不调用 approve/submit/generate；终点=awaiting_approval 等人。
 */

import { createHubVariant, confirmHubReplace, getHubTask, saveHubPrompt, uploadHubImage } from "@/pages/hub/hub-api";
import { classifyTemplateRole } from "@/lib/canvas/workflow-template";
import { deriveWorkflowVariant, type VariantAssetBinding } from "@/lib/canvas/workflow-variant";
export type { VariantAssetBinding };
import { bindVariantToHub, type VariantForkIdentity, type VariantHubDirectClient } from "@/lib/canvas/workflow-variant-bind";
import type { CanvasAgentOp } from "@/lib/canvas/canvas-agent-ops";
import type { CanvasConnection, CanvasNodeData } from "@/types/canvas";

export type DeriveWorkflowVariantToolInput = {
    /** 源实例拉片节点 id（画布上该实例的拉片节点）。 */
    sourceInstanceLapianNodeId: string;
    /** 槽绑定（同 run_workflow_batch 口径：槽名/槽类型 + 图节点 id）；缺省槽留空交人工。 */
    assetBindings?: VariantAssetBinding[];
    /** 变体意图文本；缺省继承源实例意图。 */
    intentText?: string;
    /** 渲染等待上限（直绑推进 awaiting_assets→awaiting_approval；缺省 240s）。 */
    timeoutMs?: number;
};

/** 变体直建回执最小形状（createHubVariant 的 local_id/bridge_id；探针可注入 stub）。 */
export type VariantCreateResponse = { local_id?: string; bridge_id?: string };

export type DeriveWorkflowVariantToolDeps = {
    project: () => { nodes: CanvasNodeData[]; connections: CanvasConnection[] } | undefined;
    /** 落库通道（页面=canvas_apply_ops 通道；探针=收集器）。 */
    applyOps: (ops: CanvasAgentOp[]) => void | Promise<void>;
    /** 缺省 = createHubVariant（卡C路A 直建端点，红线 25 不复制第二份）；探针注入 stub。 */
    create?: (bridgeId: string, intent?: string) => Promise<VariantCreateResponse>;
    /** C2b 直绑客户端；缺省 = hub-api 既有函数包成窄接口；探针注入 stub。 */
    hub?: VariantHubDirectClient;
    /** 画布图节点字节（替换图上传 hub 用）；缺省不可得时如实降级为「全沿用」空确认。 */
    getBlob?: (storageKey: string) => Promise<Blob | null>;
    /** bindVariantToHub 注入口（探针 stub 用）；缺省真函数。 */
    bind?: typeof bindVariantToHub;
    log?: (message: string) => void;
};

export type DeriveWorkflowVariantToolResult = {
    ok: true;
    templateId: string;
    instanceIndex: number;
    sourceLapianNodeId: string;
    newLapianNodeId: string;
    nodeIds: string[];
    connectionIds: string[];
    newLocalId: string;
    bridgeId: string;
    sourceLocalId: string;
    bindings: { slotNodeId: string; slotName: string; imageNodeId: string; reason: string }[];
    hubState: string;
    stopPoint: "awaiting_approval";
    assetsUploaded: number;
    confirmedEmpty: boolean;
    promptSaved: boolean;
    warnings: string[];
};

export class DeriveWorkflowVariantToolError extends Error {
    readonly code: "source_not_ready" | "source_not_done" | "fork" | "bind" | "land";
    readonly details: Record<string, unknown>;
    constructor(code: DeriveWorkflowVariantToolError["code"], message: string, details: Record<string, unknown> = {}) {
        super(message);
        this.name = "DeriveWorkflowVariantToolError";
        this.code = code;
        this.details = details;
    }
}

/** 实例 id 前缀反解：`t<tid>-<idx>-<原id>` → { templateId, instanceIndex, originalId }。 */
export function parseInstanceNodeId(id: string): { templateId: string; instanceIndex: number } | null {
    const match = /^t(.+)-(\d+)-(.+)$/.exec(id);
    if (!match) return null;
    return { templateId: match[1], instanceIndex: Number(match[2]) };
}

/** 缺省直绑客户端：全部委托 hub-api 既有函数（不复制第二份合同）。 */
export function defaultVariantHubClient(): VariantHubDirectClient {
    return {
        async getTask(localId) {
            const task = await getHubTask(localId);
            return { state: String(task?.state || ""), bridgeId: String(task?.bridge_id ?? "") };
        },
        async uploadAssets(localId, files) {
            const filled: string[] = [];
            for (const file of files) {
                const uploaded = new File([file.blob], file.filename || "variant-asset.png", { type: file.blob.type || "image/png" });
                await uploadHubImage(uploaded, localId);
                filled.push(file.filename);
            }
            return { filled };
        },
        async confirmReplace(localId, input) {
            const result = await confirmHubReplace(localId, input?.emptyConfirm ?? false, input?.ackRows ?? []);
            return { state: result?.state, jobId: result?.job_id };
        },
        async savePrompt({ localId, text }) {
            const saved = await saveHubPrompt(localId, text);
            return { state: saved?.state, approvalSnapshot: saved?.approval_snapshot, selfcheck: Array.isArray(saved?.自检) ? saved.自检 : [] };
        },
    };
}

/**
 * 变体派生集成主入口（C3/C4 共用）：
 * ① 源校验（拉片产物在位：taskId=bridge_id + content）→ ② 变体直建 → ③ derive 纯逻辑 → ④ 直绑推进到待批 → ⑤ 落画布 → ⑥ 回执。
 * 任一步失败即中止：直建前失败画布零改动；落库失败时变体任务已存在，如实报错带 localId（不假装成功）。
 */
export async function deriveWorkflowVariantTool(input: DeriveWorkflowVariantToolInput, deps: DeriveWorkflowVariantToolDeps): Promise<DeriveWorkflowVariantToolResult> {
    const warnings: string[] = [];
    const sourceLapianNodeId = String(input.sourceInstanceLapianNodeId || "").trim();
    const parsed = parseInstanceNodeId(sourceLapianNodeId);
    const project = deps.project?.();

    // ① 校验源：拉片节点在位 + 产物双全（D7① 前半）
    if (!parsed) throw new DeriveWorkflowVariantToolError("source_not_ready", `源节点 id 不是实例拉片节点形（t<模板>-<序号>-<原id>）：${sourceLapianNodeId || "(空)"}`, { sourceLapianNodeId });
    if (!project) throw new DeriveWorkflowVariantToolError("source_not_ready", "当前画布项目不可得：请先打开画布页再派生");
    const sourceLapian = project.nodes.find((node) => node.id === sourceLapianNodeId);
    if (!sourceLapian) throw new DeriveWorkflowVariantToolError("source_not_ready", `画布上没有找到源拉片节点：${sourceLapianNodeId}`);
    if (classifyTemplateRole(sourceLapian) !== "lapian") throw new DeriveWorkflowVariantToolError("source_not_ready", `节点 ${sourceLapianNodeId} 不是拉片节点，不能作为派生源`);
    // 路线 A 字段口径（2026-11-15 修正）：拉片节点 metadata.taskId 是**桥侧拆解任务号（=bridge_id）**，
    // 正确用途=拆解产物引用；变体直建拿它当 bridge_id，绝不把它错当 hub localId 去 fork。
    const bridgeId = String(sourceLapian.metadata?.taskId || "").trim();
    if (!bridgeId || !String(sourceLapian.metadata?.content || "").trim()) {
        throw new DeriveWorkflowVariantToolError("source_not_ready", `源拉片节点 ${sourceLapianNodeId} 的拆解产物不在位（taskId/content 任一为空）：先等拆解完成再派生`, { sourceLapianNodeId, bridgeId: bridgeId || null });
    }
    const sourceLocalId = bridgeId;   // 拆解产物引用（本入口没有「源 hub 任务」，出处记 bridge_id）

    // ② 变体直建（路线 A：拆解完成即可派生；不经 ingest 幂等 → 双变体顺序调用各自建号。
    //    直建失败如实报错，画布零改动——先建号成功、再落画布，顺序保证 D7⑤）
    let createResponse: VariantCreateResponse;
    try {
        createResponse = await (deps.create || createHubVariant)(bridgeId, input.intentText || String(sourceLapian.metadata?.lapianIntent || "").trim() || String(sourceLapian.metadata?.content || "").trim());
    } catch (error) {
        throw new DeriveWorkflowVariantToolError("fork", `变体直建失败（bridge ${bridgeId}）：${error instanceof Error ? error.message : String(error)}`, { bridgeId });
    }
    const newLocalId = String(createResponse?.local_id || "").trim();
    const createdBridgeId = String(createResponse?.bridge_id || "").trim() || bridgeId;
    if (!newLocalId || !createdBridgeId) throw new DeriveWorkflowVariantToolError("fork", `变体直建回执身份不完整（local_id/bridge_id 任一为空）：bridge ${bridgeId} → ${newLocalId || "(空)"}`, { bridgeId, createResponse });
    deps.log?.(`变体直建：bridge ${bridgeId} → ${newLocalId}（出生态 awaiting_assets）`);

    // ③ 纯逻辑派生（校验/重写/绑定/意图都在 C1 模块内；失败则变体任务作废留档，画布仍零改动）
    const derived = deriveWorkflowVariant({
        nodes: project.nodes,
        connections: project.connections,
        templateId: parsed.templateId,
        sourceInstanceIndex: parsed.instanceIndex,
        newTaskId: newLocalId,
        assetBindings: input.assetBindings,
        intentText: input.intentText,
    });
    warnings.push(...derived.warnings);

    // ④ 直绑推进（C2b：零 ingest、零 fork_task，awaiting_assets→awaiting_approval；替换图字节随传）
    const client = deps.hub || defaultVariantHubClient();
    const assets: { blob: Blob; filename: string }[] = [];
    if (deps.getBlob) {
        const nodeById = new Map(project.nodes.map((node) => [node.id, node]));
        for (const binding of derived.bindings) {
            const storageKey = String(nodeById.get(binding.imageNodeId)?.metadata?.storageKey || "");
            if (!storageKey) continue;
            try {
                const blob = await deps.getBlob(storageKey);
                if (blob) assets.push({ blob, filename: storageKey.split("/").pop() || "variant-asset.png" });
                else warnings.push(`替换图 ${binding.imageNodeId} 取不到字节（storageKey ${storageKey}）：该槽走 hub「全沿用」，画布绑定仍生效`);
            } catch (error) {
                warnings.push(`替换图 ${binding.imageNodeId} 读字节失败：${error instanceof Error ? error.message : String(error)}（该槽走 hub「全沿用」）`);
            }
        }
    } else if (derived.bindings.length) {
        warnings.push(`页面未提供取图通道：${derived.bindings.length} 个绑定槽走 hub「全沿用」空确认（画布侧绑定照常落）`);
    }
    const forkIdentity: VariantForkIdentity = { localId: newLocalId, bridgeId, sourceLocalId, boundAt: "" };
    let bindResult;
    try {
        bindResult = await (deps.bind || bindVariantToHub)(
            { fork: forkIdentity, assets, confirm: { emptyConfirm: assets.length === 0 }, timeoutMs: input.timeoutMs },
            { client, log: deps.log },
        );
    } catch (error) {
        throw new DeriveWorkflowVariantToolError("bind", `变体直绑推进失败（${newLocalId}）：${error instanceof Error ? error.message : String(error)}（画布未落节点，hub 任务 ${newLocalId} 留档可查）`, { newLocalId, sourceLocalId });
    }
    warnings.push(...bindResult.warnings);

    // ⑤ 落画布（新分支节点 + 连线；编排器同款 add_node/connect 形）
    const ops: CanvasAgentOp[] = derived.nodes.map((node) => ({
        type: "add_node" as const,
        id: node.id,
        nodeType: node.type,
        title: node.title,
        position: { ...node.position },
        width: node.width,
        height: node.height,
        metadata: { ...node.metadata },
    }));
    for (const connection of derived.connections) ops.push({ type: "connect_nodes", id: connection.id, fromNodeId: connection.fromNodeId, toNodeId: connection.toNodeId });
    try {
        await deps.applyOps(ops);
    } catch (error) {
        throw new DeriveWorkflowVariantToolError("land", `变体节点落画布失败（${newLocalId}）：${error instanceof Error ? error.message : String(error)}（hub 任务 ${newLocalId} 已在待批，可在 /hub 补挂画布）`, { newLocalId });
    }

    const newLapian = derived.nodes.find((node) => classifyTemplateRole(node) === "lapian");
    // ⑥ 回执（D8）
    return {
        ok: true,
        templateId: parsed.templateId,
        instanceIndex: derived.index,
        sourceLapianNodeId,
        newLapianNodeId: newLapian?.id || "",
        nodeIds: derived.nodes.map((node) => node.id),
        connectionIds: derived.connections.map((connection) => connection.id),
        newLocalId,
        bridgeId,
        sourceLocalId,
        bindings: derived.bindings,
        hubState: String(bindResult.state || ""),
        stopPoint: "awaiting_approval",
        assetsUploaded: bindResult.assetsUploaded,
        confirmedEmpty: bindResult.confirmedEmpty,
        promptSaved: bindResult.promptSaved,
        warnings,
    };
}
