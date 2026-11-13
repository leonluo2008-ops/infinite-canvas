/**
 * 模板 → 实例 的 id 引用重写器（P2，方案A 批量代办；P0a §2.1/§2.2 七类引用字段总表）。
 *
 * 职责边界：**只做确定性重映射与全文替换**，不生成 ops、不调引擎、不碰 store（实例化器属 P3）。
 *
 * 红线 25 落实：画布侧没有「同项目内克隆 + 引用重写」的实现——导入只换 project.id 不换节点 id
 * （`stores/canvas/use-canvas-store.ts` importProject），同项目克隆必须自建映射（P0a §2.1 已背书本文件为 P2 新建项）。
 * 清单行重算不复用在实例化阶段（M3 由挂图副作用 `upsertLapianAssetManifest` 负责），本文件只改 token id 段。
 *
 * 纯函数层：不读 i18n、不摸 React、不碰 store（探针可直接打包真跑）。
 */

import type { CanvasConnection, CanvasNodeData, CanvasNodeMetadata } from "@/types/canvas";

import { stableDigest } from "@/lib/canvas/workflow-template";

/** 确定性实例 id：`t<模板短id>-<实例序>-<旧id>`（P0a §2.1；同 id 重试即幂等锚）。 */
export function templateInstanceNodeId(templateId: string, instanceIndex: number, oldNodeId: string): string {
    return `t${templateId}-${instanceIndex}-${oldNodeId}`;
}

/** 确定性连线 id：同规则推导，便于重试查重。 */
export function templateInstanceConnectionId(templateId: string, instanceIndex: number, oldConnectionId: string): string {
    return `c${templateId}-${instanceIndex}-${oldConnectionId}`;
}

export type TemplateIdMap = Map<string, string>;

/** 全量映射表（先映射、后出 ops，禁止边建边改——P0a §2.1/§2.3）。 */
export function buildTemplateIdMap(nodeIds: string[], templateId: string, instanceIndex: number): TemplateIdMap {
    const map: TemplateIdMap = new Map();
    for (const nodeId of nodeIds) map.set(nodeId, templateInstanceNodeId(templateId, instanceIndex, nodeId));
    return map;
}

const TOKEN_PATTERN = /@\[node:([^\]]+)\]/g;

/**
 * 组装串 token id 段全文替换（M4）：只替换命中映射表的 id；未命中的 token 原样保留并进 `unresolved`。
 * token 结构与其余文本逐字节不动（禁重排、禁 trim）。
 */
export function rewriteComposerTokens(text: string, map: TemplateIdMap): { text: string; replaced: string[]; unresolved: string[] } {
    const replaced: string[] = [];
    const unresolved: string[] = [];
    const next = String(text || "").replace(TOKEN_PATTERN, (whole: string, id: string) => {
        const mapped = map.get(id);
        if (!mapped) {
            unresolved.push(id);
            return whole;
        }
        replaced.push(id);
        return `@[node:${mapped}]`;
    });
    return { text: next, replaced, unresolved };
}

/** 未解析引用（重写后仍指向模板外节点）：实例化器必须据此拒绝（P0a §2.2 第 1 行）。 */
export type UnresolvedReference = {
    nodeId: string;
    field: string;
    targetId: string;
    token?: string;
};

export type RewriteTemplateIdsInput = {
    nodes: CanvasNodeData[];
    connections: CanvasConnection[];
    templateId: string;
    /** 实例序号（批次内 1-based 或任意稳定序号；同序号重跑幂等）。 */
    instanceIndex: number;
};

export type RewriteTemplateIdsResult = {
    nodes: CanvasNodeData[];
    connections: CanvasConnection[];
    /** 旧 id → 新 id（含连线端点；审计与跨实例零交集校验用）。 */
    nodeIdMap: Record<string, string>;
    connectionIdMap: Record<string, string>;
    replacedTokenCount: number;
    unresolved: UnresolvedReference[];
    /** 校验用签名（映射后端点排序 digest），应与模板 topology.digest 一致（F1）。 */
    topologyDigest: string;
};

/**
 * 七类引用重写（P0a §2.2 总表逐条落实）：
 *  1) composerContent 内 `@[node:<id>]` token 的 id 段；
 *  2) `attachedImageId`（扁平真源）；
 *  3) `assetSlot.attachedImageId`（嵌套镜像，与 2 同一次产出，调用方一次 update 双写）；
 *  4) `assetSlotSourceId`；
 *  5) `lapianSourceId`；
 *  6) `assetGraftOfImageId` / `assetGraftForSlotId`；
 *  7) 连线 `fromNodeId`/`toNodeId` 与 `groupId`（另含节点 id 与连线 id 本身）。
 */
export function rewriteTemplateIds(input: RewriteTemplateIdsInput): RewriteTemplateIdsResult {
    const map = buildTemplateIdMap(
        input.nodes.map((node) => node.id),
        input.templateId,
        input.instanceIndex,
    );
    const replaceId = (value?: string): string | undefined => {
        if (!value) return value;
        return map.get(value) || value;
    };

    const unresolved: UnresolvedReference[] = [];
    let replacedTokenCount = 0;

    const nodes: CanvasNodeData[] = input.nodes.map((node) => {
        const metadata: CanvasNodeMetadata = { ...(node.metadata || {}) };
        // #1 composerContent token
        if (typeof metadata.composerContent === "string" && metadata.composerContent) {
            const result = rewriteComposerTokens(metadata.composerContent, map);
            metadata.composerContent = result.text;
            replacedTokenCount += result.replaced.length;
            for (const id of result.unresolved) unresolved.push({ nodeId: node.id, field: "composerContent", targetId: id, token: `@[node:${id}]` });
        }
        // #2 扁平真源
        if (metadata.attachedImageId) {
            const target = metadata.attachedImageId;
            metadata.attachedImageId = replaceId(target);
            if (metadata.attachedImageId === target && !map.has(target)) unresolved.push({ nodeId: node.id, field: "attachedImageId", targetId: target });
        }
        // #3 嵌套镜像（一次产出，调用方双写）
        if (metadata.assetSlot?.attachedImageId) {
            const target = metadata.assetSlot.attachedImageId;
            const mapped = replaceId(target);
            metadata.assetSlot = { ...metadata.assetSlot, attachedImageId: mapped };
            if (mapped === target && !map.has(target)) unresolved.push({ nodeId: node.id, field: "assetSlot.attachedImageId", targetId: target });
        }
        // #4 资产位来源拉片
        if (metadata.assetSlotSourceId) {
            const target = metadata.assetSlotSourceId;
            metadata.assetSlotSourceId = replaceId(target);
            if (metadata.assetSlotSourceId === target && !map.has(target)) unresolved.push({ nodeId: node.id, field: "assetSlotSourceId", targetId: target });
        }
        // #5 Config → 拉片
        if (metadata.lapianSourceId) {
            const target = metadata.lapianSourceId;
            metadata.lapianSourceId = replaceId(target);
            if (metadata.lapianSourceId === target && !map.has(target)) unresolved.push({ nodeId: node.id, field: "lapianSourceId", targetId: target });
        }
        // #6 挂图对照双向引用
        for (const field of ["assetGraftOfImageId", "assetGraftForSlotId"] as const) {
            const target = metadata[field];
            if (!target) continue;
            metadata[field] = replaceId(target);
            if (metadata[field] === target && !map.has(target)) unresolved.push({ nodeId: node.id, field, targetId: target });
        }
        // #7 groupId（成员 → Group 节点）
        if (metadata.groupId) {
            const target = metadata.groupId;
            metadata.groupId = replaceId(target);
            if (metadata.groupId === target && !map.has(target)) unresolved.push({ nodeId: node.id, field: "groupId", targetId: target });
        }
        // 节点内部子项 id（运行态产物，正常已剥离）：命中映射表才重写，不命中不报错。
        if (metadata.primaryImageId) metadata.primaryImageId = replaceId(metadata.primaryImageId);
        if (metadata.primaryTextId) metadata.primaryTextId = replaceId(metadata.primaryTextId);
        if (metadata.images) metadata.images = metadata.images.map((image) => ({ ...image, id: replaceId(image.id) || image.id }));
        if (metadata.texts) metadata.texts = metadata.texts.map((text) => ({ ...text, id: replaceId(text.id) || text.id }));

        return {
            ...node,
            id: map.get(node.id) || node.id,
            metadata,
        };
    });

    const connectionIdMap: Record<string, string> = {};
    const connections: CanvasConnection[] = input.connections.map((connection) => {
        const nextId = templateInstanceConnectionId(input.templateId, input.instanceIndex, connection.id);
        connectionIdMap[connection.id] = nextId;
        const fromNodeId = replaceId(connection.fromNodeId) || connection.fromNodeId;
        const toNodeId = replaceId(connection.toNodeId) || connection.toNodeId;
        if (!map.has(connection.fromNodeId)) unresolved.push({ nodeId: connection.id, field: "connection.fromNodeId", targetId: connection.fromNodeId });
        if (!map.has(connection.toNodeId)) unresolved.push({ nodeId: connection.id, field: "connection.toNodeId", targetId: connection.toNodeId });
        return { id: nextId, fromNodeId, toNodeId };
    });

    const nodeIdMap: Record<string, string> = {};
    const inverse = new Map<string, string>();
    for (const [oldId, newId] of map) {
        nodeIdMap[oldId] = newId;
        inverse.set(newId, oldId);
    }

    // 拓扑签名按「旧 id」回算：可直接与模板 `frozen.topology.digest` 比对（F1 骨架同构）。
    const topologyDigest = stableDigest(
        connections
            .map((connection) => `${inverse.get(connection.fromNodeId) || connection.fromNodeId}->${inverse.get(connection.toNodeId) || connection.toNodeId}`)
            .sort()
            .join("\n"),
    );

    return {
        nodes,
        connections,
        nodeIdMap,
        connectionIdMap,
        replacedTokenCount,
        unresolved,
        topologyDigest,
    };
}

/** 跨实例零交集校验（P0a §2.1）：不同实例的 id 集合交集必须为空。 */
export function instanceIdSetsDisjoint(left: string[], right: string[]): boolean {
    const rightSet = new Set(right);
    return left.every((id) => !rightSet.has(id));
}
