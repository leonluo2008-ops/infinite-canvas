/**
 * 卡C3/C4 · 变体派生页面侧集成函数（2026-11-15 拍板「不要去 hub 上面做，全部在画布里面做」后的纯画布口径）。
 *
 * 两个入口共用同一执行体（拍板 1：双入口同函数）：
 * - C3 agent 工具 `derive_workflow_variant`（web/src/lib/agent/agent-site-tools.ts 分发到本文件）；
 * - C4 拉片节点悬浮栏「派生变体」（web/src/components/canvas/lapian-variant-modal.tsx）。
 *
 * 顺序（派生 = 纯画布操作，零 hub 调用）：
 * ① 校验源：画布上任意 type=lapian 且 metadata.taskId(=bridge_id 拆解产物引用) 与 content 都在位的节点；
 *    不再要求实例形 id（t<模板>-<序号>-<原id>）——实例形节点走前缀子图，裸拉片节点走闭包子图；
 * ② deriveWorkflowVariant（C1 纯逻辑：复制子图 → 新分支节点 → 槽绑定写画布元数据
 *    （attachedImageId + assetSlot 镜像双写）→ 新拉片节点继承源节点 taskId/content（拆解产物复用 =
 *    画布元数据引用，不建任何 hub 任务）；
 * ③ canvas_apply_ops 落新分支节点 + 连线；
 * ④ 回执（节点 id 清单/槽绑定结果/停点=画布侧待批：生成配置节点就绪等用户点生成）。
 *
 * 付费红线：本文件从不调用 approve/submit/generate；终点=画布侧待批等人。
 */

import { classifyTemplateRole } from "@/lib/canvas/workflow-template";
import { deriveWorkflowVariant, type VariantAssetBinding } from "@/lib/canvas/workflow-variant";
export type { VariantAssetBinding };
import type { CanvasAgentOp } from "@/lib/canvas/canvas-agent-ops";
import type { CanvasConnection, CanvasNodeData } from "@/types/canvas";

export type DeriveWorkflowVariantToolInput = {
    /** 源拉片节点 id（画布上任意 type=lapian 且拆解产物在位的节点；实例形 id 亦兼容）。 */
    sourceInstanceLapianNodeId: string;
    /** 槽绑定（同 run_workflow_batch 口径：槽名/槽类型 + 图节点 id）；缺省槽留空交人工。 */
    assetBindings?: VariantAssetBinding[];
    /** 变体意图文本；缺省继承源实例意图。 */
    intentText?: string;
};

export type DeriveWorkflowVariantToolDeps = {
    project: () => { nodes: CanvasNodeData[]; connections: CanvasConnection[] } | undefined;
    /** 落库通道（页面=canvas_apply_ops 通道；探针=收集器）。 */
    applyOps: (ops: CanvasAgentOp[]) => void | Promise<void>;
    log?: (message: string) => void;
};

export type DeriveWorkflowVariantToolResult = {
    ok: true;
    /** 实例形源才有（parseInstanceNodeId 命中）：模板 id 与派生实例序号；裸拉片源缺省。 */
    templateId?: string;
    instanceIndex?: number;
    sourceLapianNodeId: string;
    newLapianNodeId: string;
    nodeIds: string[];
    connectionIds: string[];
    /** 拆解产物引用（=源节点 metadata.taskId，原样继承到新拉片节点），不再有 hub 任务号。 */
    bridgeId: string;
    bindings: { slotNodeId: string; slotName: string; imageNodeId: string; reason: string }[];
    stopPoint: "awaiting_approval";
    warnings: string[];
};

export class DeriveWorkflowVariantToolError extends Error {
    readonly code: "source_not_ready" | "source_not_done" | "land";
    readonly details: Record<string, unknown>;
    constructor(code: DeriveWorkflowVariantToolError["code"], message: string, details: Record<string, unknown> = {}) {
        super(message);
        this.name = "DeriveWorkflowVariantToolError";
        this.code = code;
        this.details = details;
    }
}

/** 实例 id 前缀反解：`t<tid>-<idx>-<原id>` → { templateId, instanceIndex }；非实例形返回 null（闭包模式）。 */
export function parseInstanceNodeId(id: string): { templateId: string; instanceIndex: number } | null {
    const match = /^t(.+)-(\d+)-(.+)$/.exec(id);
    if (!match) return null;
    return { templateId: match[1], instanceIndex: Number(match[2]) };
}

/**
 * 变体派生集成主入口（C3/C4 共用，纯画布操作）：
 * ① 源校验（lapian + taskId/content 在位，不要求实例形 id）→ ② C1 纯逻辑派生（子图复制/id 重写/
 * 槽绑定双写/意图继承/拉片产物引用继承）→ ③ 落画布 → ④ 回执（停点=画布侧待批）。
 * 任一步失败即中止：派生失败画布零改动；落库失败如实报错（不假装成功）。
 */
export async function deriveWorkflowVariantTool(input: DeriveWorkflowVariantToolInput, deps: DeriveWorkflowVariantToolDeps): Promise<DeriveWorkflowVariantToolResult> {
    const warnings: string[] = [];
    const sourceLapianNodeId = String(input.sourceInstanceLapianNodeId || "").trim();
    const parsed = parseInstanceNodeId(sourceLapianNodeId);
    const project = deps.project?.();

    // ① 校验源：画布任意拉片节点 + 产物双全（不要求实例形 id；实例形节点走前缀子图，裸拉片走闭包子图）
    if (!sourceLapianNodeId) throw new DeriveWorkflowVariantToolError("source_not_ready", "源拉片节点 id 为空：请指定画布上的拉片节点", { sourceLapianNodeId });
    if (!project) throw new DeriveWorkflowVariantToolError("source_not_ready", "当前画布项目不可得：请先打开画布页再派生");
    const sourceLapian = project.nodes.find((node) => node.id === sourceLapianNodeId);
    if (!sourceLapian) throw new DeriveWorkflowVariantToolError("source_not_ready", `画布上没有找到源拉片节点：${sourceLapianNodeId}`);
    if (classifyTemplateRole(sourceLapian) !== "lapian") throw new DeriveWorkflowVariantToolError("source_not_ready", `节点 ${sourceLapianNodeId} 不是拉片节点，不能作为派生源`);
    // metadata.taskId 是桥侧拆解任务号（=bridge_id），正确用途=拆解产物引用（新拉片节点原样继承）。
    const bridgeId = String(sourceLapian.metadata?.taskId || "").trim();
    if (!bridgeId || !String(sourceLapian.metadata?.content || "").trim()) {
        throw new DeriveWorkflowVariantToolError("source_not_done", `源拉片节点 ${sourceLapianNodeId} 的拆解产物不在位（taskId/content 任一为空）：先等拆解完成再派生`, { sourceLapianNodeId, bridgeId: bridgeId || null });
    }

    // ② 纯逻辑派生（校验/重写/绑定/意图都在 C1 模块内；失败则画布零改动）
    //    新拉片节点 taskId 继承源 bridgeId（拆解产物引用复用），生成侧全新。
    const derived = deriveWorkflowVariant({
        nodes: project.nodes,
        connections: project.connections,
        ...(parsed ? { templateId: parsed.templateId, sourceInstanceIndex: parsed.instanceIndex } : { sourceLapianNodeId }),
        newTaskId: bridgeId,
        assetBindings: input.assetBindings,
        intentText: String(input.intentText ?? "").trim() || String(sourceLapian.metadata?.lapianIntent || "").trim() || String(sourceLapian.metadata?.content || "").trim(),
    });
    warnings.push(...derived.warnings);

    // ③ 落画布（新分支节点 + 连线；编排器同款 add_node/connect 形）
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
        throw new DeriveWorkflowVariantToolError("land", `变体节点落画布失败（bridge ${bridgeId}）：${error instanceof Error ? error.message : String(error)}（画布未改动，可重试派生）`, { bridgeId });
    }

    const newLapian = derived.nodes.find((node) => classifyTemplateRole(node) === "lapian");
    // ④ 回执：停点=画布侧待批（生成配置节点就绪等用户点生成），无任何 hub 字段
    return {
        ok: true,
        ...(parsed ? { templateId: parsed.templateId, instanceIndex: derived.index } : {}),
        sourceLapianNodeId,
        newLapianNodeId: newLapian?.id || "",
        nodeIds: derived.nodes.map((node) => node.id),
        connectionIds: derived.connections.map((connection) => connection.id),
        bridgeId,
        bindings: derived.bindings,
        stopPoint: "awaiting_approval",
        warnings,
    };
}
