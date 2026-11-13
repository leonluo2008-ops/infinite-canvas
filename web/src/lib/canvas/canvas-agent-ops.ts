import { nanoid } from "nanoid";

import i18n from "@/i18n";
import { getNodeSpec, isRegisteredNodeType } from "@/lib/canvas/node-registry";
import { CanvasNodeType, type CanvasConnection, type CanvasNodeData, type CanvasNodeMetadata, type CanvasNodeTypeId, type ViewportTransform } from "@/types/canvas";

export type CanvasAgentOp =
    | { type: "add_node"; id?: string; nodeType?: CanvasNodeTypeId; title?: string; position?: { x: number; y: number }; x?: number; y?: number; width?: number; height?: number; metadata?: CanvasNodeMetadata }
    | { type: "update_node"; id: string; patch?: Partial<CanvasNodeData>; metadata?: CanvasNodeMetadata }
    | { type: "delete_node"; id?: string; ids?: string[]; nodeType?: CanvasNodeTypeId }
    | { type: "delete_connections"; id?: string; ids?: string[]; all?: boolean }
    | { type: "connect_nodes"; id?: string; fromNodeId: string; toNodeId: string }
    | { type: "set_viewport"; viewport: ViewportTransform }
    | { type: "select_nodes"; ids: string[] }
    | { type: "run_generation"; nodeId: string; mode?: "text" | "image" | "video" | "audio"; prompt?: string };

export type CanvasAgentSnapshot = {
    projectId: string;
    title: string;
    nodes: CanvasNodeData[];
    connections: CanvasConnection[];
    selectedNodeIds: string[];
    viewport: ViewportTransform;
};

/** 批量 ops 的执行诊断（P3）：哪些节点是真新增、哪些是幂等跳过——重试时据此判断「已建过」。 */
export type CanvasAgentOpDiagnostics = {
    addedNodeIds: string[];
    idempotentNodeIds: string[];
    addedConnectionIds: string[];
    skippedConnectionIds: string[];
};

/**
 * add_node 幂等键冲突（审计 A2/G10）：同一 id 已存在但形态不同 —— 说明不是重试，而是两次不同的写入撞了 id。
 * 直接报错，绝不静默 append 出重复节点（旧行为是 append，重试即翻倍并引发重复付费）。
 */
export class CanvasAgentOpConflictError extends Error {
    readonly nodeId: string;
    readonly opIndex: number;
    constructor(nodeId: string, opIndex: number) {
        super(`add_node 冲突：节点 ${nodeId} 已存在且形态不一致（op #${opIndex}）——拒绝覆盖，请换 id 或改用 update_node`);
        this.name = "CanvasAgentOpConflictError";
        this.nodeId = nodeId;
        this.opIndex = opIndex;
    }
}

/** 稳定序列化（键排序）：节点形态比对用，不受字段插入顺序影响。 */
function stableStringify(value: unknown): string {
    if (value === null || typeof value !== "object") return JSON.stringify(value ?? null);
    if (Array.isArray(value)) return `[${value.map((item) => stableStringify(item)).join(",")}]`;
    const entries = Object.entries(value as Record<string, unknown>)
        .filter(([, item]) => item !== undefined)
        .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0));
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${stableStringify(item)}`).join(",")}}`;
}

/**
 * 节点形态 = 类型 + metadata（含嵌套对象全等）。
 * 布局字段（position/title/width/height）**不入形态**：用户挪过节点/改过标题仍算同一个节点，
 * 判成冲突会把「重放实例化」误伤成错误；而 metadata 变了才是真的两次不同写入。
 */
export function canvasNodeShapeKey(node: CanvasNodeData): string {
    return stableStringify({ type: node.type, metadata: node.metadata || {} });
}

export function sameCanvasNodeShape(left: CanvasNodeData, right: CanvasNodeData): boolean {
    return canvasNodeShapeKey(left) === canvasNodeShapeKey(right);
}

export function summarizeCanvasAgentOps(ops?: CanvasAgentOp[]) {
    const counts = (Array.isArray(ops) ? ops : []).reduce<Record<string, number>>((acc, op) => {
        if (!op?.type) return acc;
        acc[op.type] = (acc[op.type] || 0) + 1;
        return acc;
    }, {});
    return Object.entries(counts)
        .map(([type, count]) => `${opLabel(type)} ${count}`)
        .join("，");
}

/**
 * 批量 ops 应用（纯函数）：返回新快照 + 执行诊断。
 *
 * add_node 幂等键（P3 / 审计 A2）：同 id 已存在且形态一致 → 跳过（重放同一实例化请求节点数不翻倍）；
 * 形态不一致 → 抛 `CanvasAgentOpConflictError`（不静默覆盖、不 append 重复节点）。
 * connect_nodes 既有查重保持不变（同 (from,to) 已存在即跳过）。
 */
export function applyCanvasAgentOps(snapshot: CanvasAgentSnapshot, ops?: CanvasAgentOp[]) {
    let nodes = snapshot.nodes;
    let connections = snapshot.connections;
    let selectedNodeIds = snapshot.selectedNodeIds;
    let viewport = snapshot.viewport;
    const diagnostics: CanvasAgentOpDiagnostics = { addedNodeIds: [], idempotentNodeIds: [], addedConnectionIds: [], skippedConnectionIds: [] };

    (Array.isArray(ops) ? ops : []).forEach((op, index) => {
        if (!op?.type) return;
        if (op.type === "add_node") {
            const nodeType = op.nodeType && isRegisteredNodeType(op.nodeType) ? op.nodeType : CanvasNodeType.Text;
            const spec = getNodeSpec(nodeType);
            const node: CanvasNodeData = {
                id: op.id || `${nodeType}-${Date.now()}-${index}`,
                type: nodeType,
                title: op.title || spec.title,
                position: op.position || { x: op.x ?? index * 36, y: op.y ?? index * 36 },
                width: op.width || spec.width,
                height: op.height || spec.height,
                metadata: { ...spec.metadata, ...op.metadata },
            };
            const existing = nodes.find((item) => item.id === node.id);
            if (existing) {
                if (!sameCanvasNodeShape(existing, node)) throw new CanvasAgentOpConflictError(node.id, index);
                diagnostics.idempotentNodeIds.push(node.id);
                return;   // 幂等跳过：节点数与形态都不动
            }
            nodes = [...nodes, node];
            diagnostics.addedNodeIds.push(node.id);
            selectedNodeIds = [node.id];
        }
        if (op.type === "update_node") {
            if (!op.id) return;
            nodes = nodes.map((node) => (node.id === op.id ? { ...node, ...op.patch, metadata: { ...node.metadata, ...op.patch?.metadata, ...op.metadata } } : node));
        }
        if (op.type === "delete_node") {
            const ids = new Set(op.ids || (op.id ? [op.id] : op.nodeType ? nodes.filter((node) => node.type === op.nodeType).map((node) => node.id) : []));
            nodes = nodes.filter((node) => !ids.has(node.id));
            connections = connections.filter((conn) => !ids.has(conn.fromNodeId) && !ids.has(conn.toNodeId));
            selectedNodeIds = selectedNodeIds.filter((id) => !ids.has(id));
        }
        if (op.type === "delete_connections") {
            const ids = new Set(op.ids || (op.id ? [op.id] : []));
            connections = op.all ? [] : connections.filter((conn) => !ids.has(conn.id));
        }
        if (op.type === "connect_nodes") {
            if (!op.fromNodeId || !op.toNodeId) return;
            const exists = connections.some((conn) => conn.fromNodeId === op.fromNodeId && conn.toNodeId === op.toNodeId);
            const hasNodes = nodes.some((node) => node.id === op.fromNodeId) && nodes.some((node) => node.id === op.toNodeId);
            if (exists) {
                diagnostics.skippedConnectionIds.push(op.id || `${op.fromNodeId}->${op.toNodeId}`);
                return;
            }
            if (!hasNodes) return;
            const connectionId = op.id || nanoid();
            connections = [...connections, { id: connectionId, fromNodeId: op.fromNodeId, toNodeId: op.toNodeId }];
            diagnostics.addedConnectionIds.push(connectionId);
        }
        if (op.type === "set_viewport" && op.viewport) viewport = op.viewport;
        if (op.type === "select_nodes") selectedNodeIds = (op.ids || []).filter((id) => nodes.some((node) => node.id === id));
    });

    return { ...snapshot, nodes, connections, selectedNodeIds, viewport, diagnostics };
}

function opLabel(type: string) {
    return i18n.t(`canvas.agentOps.${type}`, { defaultValue: type });
}
