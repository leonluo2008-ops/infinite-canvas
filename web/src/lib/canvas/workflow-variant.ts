/**
 * 画布工作流「变体派生」纯逻辑层（卡C1，方案 docs/方案-卡C一拆多用变体派生-20261115.md §三 D3/D4/D5）。
 *
 * 职责边界：**只做子图定位 / 裁剪 / id 重映射 / 绑定与意图落点**，不 fork 任务、不写 store、不碰集成层
 * （直绑通道 C2b、agent 工具 C3、悬浮栏入口 C4 各自另行实现）。
 *
 * 复用优先说明——本文件以两个既有函数为**蓝本新写**，原函数一字未改（模板抽取零回归）：
 * - `rewriteTemplateIds`（workflow-template-ids.ts:86）：七类引用重写表照搬，但映射键改为
 *   「源前缀反解后的模板原 id」——直接喂实例 id 会产出 `t<tid>-<n>-t<tid>-<m>-...` 嵌套双前缀（路2 N3）。
 * - `stripTemplateRuntime`（workflow-template.ts:219）：剥离表逐角色 override——lapian 保留
 *   taskId/jobId/content/intent（产物引用复用核心），只剥展示派生字段；生成侧照原表剥运行痕迹（路2 N4）。
 *
 * 纯函数层：不读 i18n、不摸 React、不碰 store/localStorage，探针可直接打包真跑。
 */

import { templateInstanceConnectionId, templateInstanceNodeId, type TemplateIdMap } from "@/lib/canvas/workflow-template-ids";
import { classifyTemplateRole, type CanvasTemplateNodeRole } from "@/lib/canvas/workflow-template";
import type { CanvasConnection, CanvasNodeData, CanvasNodeMetadata } from "@/types/canvas";

/** 派生边界错误：code ∈ source_not_ready（源拉片未就绪）/ binding_target_missing（绑定目标缺图）。 */
export type DeriveVariantErrorCode = "source_not_ready" | "binding_target_missing";

export class DeriveVariantError extends Error {
    readonly code: DeriveVariantErrorCode;
    constructor(code: DeriveVariantErrorCode, message: string) {
        super(message);
        this.name = "DeriveVariantError";
        this.code = code;
    }
}

/** 实例 id 前缀（与 templateInstanceNodeId 产出形互为逆）：`t<tid>-<idx>-`。 */
function instanceNodePrefix(templateId: string, instanceIndex: number): string {
    return `t${templateId}-${instanceIndex}-`;
}

/** 实例连线 id 前缀：`c<tid>-<idx>-`。 */
function instanceConnectionPrefix(templateId: string, instanceIndex: number): string {
    return `c${templateId}-${instanceIndex}-`;
}

/**
 * 源前缀反解：`t<tid>-<idx>-<原id>` → `原id`；非实例 id（含模板裸 id）原样返回。
 * 禁嵌套双前缀的关键：先反解取模板原 id，再套新前缀（路2 N3）。
 */
export function stripInstancePrefix(id: string, templateId: string, instanceIndex: number): string {
    const prefix = instanceNodePrefix(templateId, instanceIndex);
    return id.startsWith(prefix) ? id.slice(prefix.length) : id;
}

export type LocateInstanceSubgraphResult = {
    /** 子图节点（含并入的执行期产物节点，保持原 id 原样——重写在派生层做）。 */
    nodes: CanvasNodeData[];
    /** 连线闭包：两端都在子图集合内才保留。 */
    connections: CanvasConnection[];
    /** 无模板前缀的执行期产物节点 id（生成输出/挂图对照等），派生层据此合成确定性键。 */
    extraNodeIds: string[];
};

/**
 * 按实例定位子图（D3）：节点 id 前缀 `t<tid>-<idx>-` 圈定骨架，连线 id 前缀 `c<tid>-<idx>-` 同步收；
 * 执行期新建的产物节点（nanoid，无模板前缀）通过「与子图节点相连线 / 被子图节点 metadata 引用」
 * 闭包并入（不动到不动点），供派生层给它们合成确定性键（`extra-<原id>` 入映射）。
 */
export function locateInstanceSubgraph(input: { nodes: CanvasNodeData[]; connections: CanvasConnection[]; templateId: string; instanceIndex: number }): LocateInstanceSubgraphResult {
    const prefix = instanceNodePrefix(input.templateId, input.instanceIndex);
    const byId = new Map(input.nodes.map((node) => [node.id, node]));
    const members = new Set<string>();
    const extras = new Set<string>();

    for (const node of input.nodes) {
        if (node.id.startsWith(prefix)) members.add(node.id);
    }
    if (!members.size) throw new DeriveVariantError("source_not_ready", `画布上找不到实例 ${input.templateId}#${input.instanceIndex} 的节点（前缀 ${prefix}）`);

    // 节点 metadata 里的 id 引用（七类 + composerContent token）
    const metadataRefs = (node: CanvasNodeData): string[] => {
        const metadata = node.metadata || {};
        const out: string[] = [];
        const push = (value?: string) => {
            if (value) out.push(value);
        };
        push(metadata.attachedImageId);
        push(metadata.assetSlot?.attachedImageId);
        push(metadata.assetSlotSourceId);
        push(metadata.lapianSourceId);
        push(metadata.assetGraftOfImageId);
        push(metadata.assetGraftForSlotId);
        push(metadata.groupId);
        push(metadata.primaryImageId);
        push(metadata.primaryTextId);
        const composer = String(metadata.composerContent || "");
        for (const match of composer.matchAll(/@\[node:([^\]]+)\]/g)) out.push(match[1]);
        return out;
    };

    // 收敛：连线端点与 metadata 引用把产物节点闭包并入
    let changed = true;
    while (changed) {
        changed = false;
        for (const connection of input.connections) {
            for (const endpoint of [connection.fromNodeId, connection.toNodeId]) {
                if (members.has(endpoint) || !byId.has(endpoint)) continue;
                const linkedToMember = input.connections.some((c) => (c.fromNodeId === endpoint && members.has(c.toNodeId)) || (c.toNodeId === endpoint && members.has(c.fromNodeId)));
                const referencedByMember = [...members].some((id) => metadataRefs(byId.get(id)!).includes(endpoint));
                if (linkedToMember || referencedByMember) {
                    members.add(endpoint);
                    extras.add(endpoint);
                    changed = true;
                }
            }
        }
        for (const id of [...members]) {
            for (const ref of metadataRefs(byId.get(id)!)) {
                if (byId.has(ref) && !members.has(ref) && !ref.startsWith(prefix)) {
                    members.add(ref);
                    extras.add(ref);
                    changed = true;
                }
            }
        }
    }

    const nodes = input.nodes.filter((node) => members.has(node.id));
    const connections = input.connections.filter((connection) => members.has(connection.fromNodeId) && members.has(connection.toNodeId));
    return { nodes, connections, extraNodeIds: [...extras] };
}

/**
 * 运行态裁剪·派生变体（stripTemplateRuntime 的裁剪变体，原表为蓝本逐角色 override；禁改原函数）：
 * - `lapian`：**保留** taskId/jobId/content/intent（产物引用复用核心，taskId 由派生层换成 fork 新任务号），
 *   只剥 cameraCoverage 等展示派生字段与执行态；
 * - `sourceVideo`：保留 content/storageKey（同画布同存储），只剥 bytes 死重量与执行态；
 * - `configVideo` / `generationOutput`：照原表剥生成痕迹（videoTaskId 等）；
 * - `intent`：**不重置占位符**（变体缺省继承源意图，替换在派生层做），只剥执行态；
 * - 其余角色照原表口径（status/errorDetails 通用剥离规则同原函数）。
 * 返回新节点，不改入参。
 */
export function deriveStrip(node: CanvasNodeData, role: CanvasTemplateNodeRole): { node: CanvasNodeData; fields: string[] } {
    const metadata: CanvasNodeMetadata = { ...(node.metadata || {}) };
    const fields: string[] = [];
    const drop = (key: keyof CanvasNodeMetadata) => {
        if (!(key in metadata)) return;
        delete metadata[key];
        fields.push(String(key));
    };

    switch (role) {
        case "lapian":
            // 派生 override：产物复用核心字段全保，剥展示派生字段。
            drop("lapPhase");
            drop("cameraCoverage");
            break;
        case "sourceVideo":
            // 派生 override：同画布同存储，content/storageKey 保留（原表剥）；只剥字节死重量。
            drop("bytes");
            break;
        case "configVideo":
            drop("status");
            drop("errorDetails");
            drop("videoTaskId");
            drop("videoTaskProvider");
            drop("videoProgress");
            drop("references");
            drop("images");
            drop("primaryImageId");
            break;
        case "generationOutput":
            drop("content");
            drop("storageKey");
            drop("bytes");
            drop("mimeType");
            drop("durationMs");
            drop("naturalWidth");
            drop("naturalHeight");
            drop("prompt");
            drop("images");
            drop("primaryImageId");
            drop("references");
            drop("videoTaskId");
            drop("videoTaskProvider");
            drop("videoProgress");
            break;
        case "finalText":
            drop("content");
            drop("prompt");
            drop("texts");
            drop("primaryTextId");
            drop("cameraCoverage");
            break;
        case "assetSlot":
            if (metadata.assetSlot && "frameUrl" in metadata.assetSlot) {
                const slot = { ...metadata.assetSlot };
                delete slot.frameUrl;
                metadata.assetSlot = slot;
                fields.push("assetSlot.frameUrl");
            }
            break;
        case "intent":
            // 派生 override：保留 content/prompt（意图继承），不落占位符。
            break;
        case "instruction":
        case "configText":
        case "fixedAsset":
        case "assetGraft":
        case "group":
        default:
            break;
    }

    // 通用规则（与原函数一致）：除白名单角色外一律剥执行态。
    if (role !== "instruction" && role !== "intent" && role !== "configText" && role !== "configVideo") {
        drop("status");
        drop("errorDetails");
    }

    return { node: { ...node, metadata }, fields: [...new Set(fields)] };
}

/** 扫画布上该 templateId 已有最大 instanceIndex，返回 +1（无已有实例 → 1）。 */
export function assignVariantIndex(nodes: CanvasNodeData[], templateId: string): number {
    const pattern = new RegExp(`^t${templateId}-(\\d+)-`);
    let max = 0;
    for (const node of nodes) {
        const match = pattern.exec(node.id);
        if (match) max = Math.max(max, Number(match[1]));
    }
    return max + 1;
}

/** 产物节点确定性键：`t<tid>-<newIdx>-extra-<原id>`（原 id 是执行期 nanoid，无前缀可嵌套）。 */
export function variantExtraNodeId(templateId: string, instanceIndex: number, originalId: string): string {
    return templateInstanceNodeId(templateId, instanceIndex, `extra-${originalId}`);
}

/** 产物连线确定性键：`c<tid>-<newIdx>-extra-<原连线id>`。 */
export function variantExtraConnectionId(templateId: string, instanceIndex: number, originalId: string): string {
    return templateInstanceConnectionId(templateId, instanceIndex, `extra-${originalId}`);
}

export type VariantAssetBinding = {
    /** 目标槽名（metadata.assetSlotName）；缺省取首个资产槽。 */
    slotName?: string;
    /** 槽类型（metadata.assetSlotType），仅匹配辅助。 */
    slotType?: string;
    /** 绑定的图节点 id（画布既有 Image 节点，须有内容）。 */
    imageNodeId: string;
    /** 绑定理由（回执 P11 口径，缺省「调用方指定」）。 */
    reason?: string;
};

export type DeriveWorkflowVariantInput = {
    nodes: CanvasNodeData[];
    connections: CanvasConnection[];
    templateId: string;
    sourceInstanceIndex: number;
    /** fork 新任务号（调用方先 fork 成功再调本函数——D7⑤ 顺序由调用方保证）。 */
    newTaskId: string;
    assetBindings?: VariantAssetBinding[];
    intentText?: string;
};

export type DeriveWorkflowVariantResult = {
    index: number;
    nodes: CanvasNodeData[];
    connections: CanvasConnection[];
    bindings: { slotNodeId: string; slotName: string; imageNodeId: string; reason: string }[];
    warnings: string[];
};

/** lapianReusedFrom 派生标记（D3）：源拉片节点 id（排障/状态派生可见痕迹）。 */
type VariantMetadata = CanvasNodeMetadata & { lapianReusedFrom?: string };

/** composerContent token id 重写（rewriteComposerTokens 口径：只改命中映射的 id 段，未命中保留）。 */
function rewriteTokens(text: string, map: TemplateIdMap): { text: string; unresolved: string[] } {
    const unresolved: string[] = [];
    const next = String(text || "").replace(/@\[node:([^\]]+)\]/g, (whole, id: string) => {
        const mapped = map.get(id);
        if (!mapped) {
            unresolved.push(id);
            return whole;
        }
        return `@[node:${mapped}]`;
    });
    return { text: next, unresolved };
}

/**
 * 变体派生主函数（D3/D4/D5/D7③/⑦）：
 * ① 边界校验（源拉片 taskId/content 任一为空 → source_not_ready；绑定图节点不存在/无内容 → binding_target_missing）；
 * ② 新 index = assignVariantIndex（画布该 templateId 最大 index+1，F5 绕开批内序号）；
 * ③ 子图定位 → 逐节点 deriveStrip → id 按新前缀重写（原 id 先反解再套新前缀，禁嵌套；产物节点用合成键）；
 * ④ 七类引用同步改写（composerContent token / attachedImageId+assetSlot 镜像双写 / assetSlotSourceId /
 *    lapianSourceId / assetGraft 两枚 / groupId / 连线端点与连线 id）；
 * ⑤ 资产槽起手全空（不继承源绑定，真源=扁平 attachedImageId），再按 assetBindings 双写绑定；
 * ⑥ 意图：intentText 缺省继承源实例意图节点 content；
 * ⑦ 拉片节点 metadata.lapianReusedFrom = 源拉片节点 id，taskId 换 newTaskId。
 * 对源实例零改动：所有节点/连线都是新对象。
 */
export function deriveWorkflowVariant(input: DeriveWorkflowVariantInput): DeriveWorkflowVariantResult {
    const warnings: string[] = [];

    // ① 边界校验：源拉片节点须 taskId+content 双全
    const prefix = instanceNodePrefix(input.templateId, input.sourceInstanceIndex);
    const byId = new Map(input.nodes.map((node) => [node.id, node]));
    const sourceLapian = input.nodes.find((node) => node.id.startsWith(prefix) && classifyTemplateRole(node) === "lapian");
    if (!sourceLapian) throw new DeriveVariantError("source_not_ready", `实例 ${input.templateId}#${input.sourceInstanceIndex} 无拉片节点，不可派生`);
    const sourceLapianMetadata = sourceLapian.metadata || {};
    if (!String(sourceLapianMetadata.taskId || "").trim() || !String(sourceLapianMetadata.content || "").trim()) {
        throw new DeriveVariantError("source_not_ready", `源拉片节点 ${sourceLapian.id} 的 taskId/content 任一为空，拆解未完成不可派生`);
    }
    if (!String(input.newTaskId || "").trim()) throw new DeriveVariantError("source_not_ready", "newTaskId 为空：须先 fork 成功再派生");

    // ② 新 index
    const newIndex = assignVariantIndex(input.nodes, input.templateId);
    if (newIndex === input.sourceInstanceIndex) warnings.push(`新 index ${newIndex} 与源实例相同：源实例不在画布上？`);

    // ③ 子图定位 + id 映射（反解 → 新前缀；产物节点合成键）
    const subgraph = locateInstanceSubgraph({ nodes: input.nodes, connections: input.connections, templateId: input.templateId, instanceIndex: input.sourceInstanceIndex });
    const nodeMap: TemplateIdMap = new Map();
    for (const node of subgraph.nodes) {
        if (subgraph.extraNodeIds.includes(node.id)) {
            nodeMap.set(node.id, variantExtraNodeId(input.templateId, newIndex, node.id));
            continue;
        }
        const originalId = stripInstancePrefix(node.id, input.templateId, input.sourceInstanceIndex);
        nodeMap.set(node.id, templateInstanceNodeId(input.templateId, newIndex, originalId));
    }
    const replaceId = (value?: string): string | undefined => (value ? nodeMap.get(value) || value : value);

    // ④ 逐节点裁剪 + 七类引用重写
    const sourceIntentContent = (() => {
        const intentNode = subgraph.nodes.find((node) => classifyTemplateRole(node) === "intent");
        return String(intentNode?.metadata?.content || "");
    })();

    const bindings: DeriveWorkflowVariantResult["bindings"] = [];
    const unresolvedWarnings: string[] = [];

    const nodes: CanvasNodeData[] = subgraph.nodes.map((node) => {
        const role = classifyTemplateRole(node);
        const stripped = deriveStrip(node, role);
        const metadata: CanvasNodeMetadata = { ...(stripped.node.metadata || {}) };

        // #1 composerContent token
        if (typeof metadata.composerContent === "string" && metadata.composerContent) {
            const result = rewriteTokens(metadata.composerContent, nodeMap);
            metadata.composerContent = result.text;
            for (const id of result.unresolved) unresolvedWarnings.push(`composerContent token @[node:${id}] 未命中子图，原样保留（节点 ${node.id}）`);
        }
        // #2/#3/#4/#5/#6/#7：七类 id 引用（字段总表 workflow-template-ids.ts:86-95）
        if (metadata.attachedImageId) metadata.attachedImageId = replaceId(metadata.attachedImageId);
        if (metadata.assetSlot?.attachedImageId) metadata.assetSlot = { ...metadata.assetSlot, attachedImageId: replaceId(metadata.assetSlot.attachedImageId) };
        if (metadata.assetSlotSourceId) metadata.assetSlotSourceId = replaceId(metadata.assetSlotSourceId);
        if (metadata.lapianSourceId) metadata.lapianSourceId = replaceId(metadata.lapianSourceId);
        if (metadata.assetGraftOfImageId) metadata.assetGraftOfImageId = replaceId(metadata.assetGraftOfImageId);
        if (metadata.assetGraftForSlotId) metadata.assetGraftForSlotId = replaceId(metadata.assetGraftForSlotId);
        if (metadata.groupId) metadata.groupId = replaceId(metadata.groupId);
        // 节点内部子项 id（命中才重写，同原函数口径）
        if (metadata.primaryImageId) metadata.primaryImageId = replaceId(metadata.primaryImageId);
        if (metadata.primaryTextId) metadata.primaryTextId = replaceId(metadata.primaryTextId);
        if (metadata.images) metadata.images = metadata.images.map((image) => ({ ...image, id: replaceId(image.id) || image.id }));
        if (metadata.texts) metadata.texts = metadata.texts.map((text) => ({ ...text, id: replaceId(text.id) || text.id }));

        // ⑦ 拉片节点：taskId 换 fork 新任务号 + 复用来源标记
        if (role === "lapian") {
            metadata.taskId = input.newTaskId;
            (metadata as VariantMetadata).lapianReusedFrom = sourceLapian.id;
        }

        // ⑤ 资产槽起手全空（不继承源实例绑定；真源=扁平 attachedImageId + assetSlot 镜像）
        if (role === "assetSlot") {
            const hadBinding = Boolean(metadata.attachedImageId || metadata.assetSlot?.attachedImageId);
            delete metadata.attachedImageId;
            delete metadata.attachedImageDesc;
            if (metadata.assetSlot) {
                const slot = { ...metadata.assetSlot };
                delete slot.attachedImageId;
                delete slot.caption;
                metadata.assetSlot = slot;
            }
            if (hadBinding) warnings.push(`槽节点 ${node.id} 起手清空既有绑定（变体槽不继承源实例）`);
        }

        const newId = nodeMap.get(node.id)!;
        return { ...node, id: newId, metadata };
    });

    // ⑤ 绑定落位（写在裁剪+清空之后；扁平真源 + assetSlot 镜像双写）
    const slotNodes = nodes.filter((node) => Boolean((node.metadata?.assetSlotSourceId ?? "") !== "" || node.metadata?.assetSlot));
    for (const binding of input.assetBindings || []) {
        const imageNode = byId.get(binding.imageNodeId);
        const imageHasContent = Boolean(imageNode && (String(imageNode.metadata?.content || "").trim() || String(imageNode.metadata?.storageKey || "").trim() || (imageNode.metadata?.images?.length ?? 0) > 0));
        if (!imageNode || !imageHasContent) {
            throw new DeriveVariantError("binding_target_missing", `绑定图节点 ${binding.imageNodeId} 不存在或无内容`);
        }
        const slot = slotNodes.find((node) => (!binding.slotName || node.metadata?.assetSlotName === binding.slotName) && (!binding.slotType || node.metadata?.assetSlotType === binding.slotType)) || (!binding.slotName ? slotNodes[0] : undefined);
        if (!slot) {
            warnings.push(`绑定槽「${binding.slotName || "(默认)"}」在变体子图未找到，已跳过（留空交人工，P7 口径）`);
            continue;
        }
        slot.metadata = { ...slot.metadata, attachedImageId: binding.imageNodeId };
        if (slot.metadata.assetSlot) slot.metadata.assetSlot = { ...slot.metadata.assetSlot, attachedImageId: binding.imageNodeId };
        bindings.push({ slotNodeId: slot.id, slotName: String(slot.metadata?.assetSlotName || binding.slotName || ""), imageNodeId: binding.imageNodeId, reason: String(binding.reason || "调用方指定") });
    }

    // ⑥ 意图文本：intentText 优先，缺省继承源意图节点 content
    const intentNode = nodes.find((node) => node.metadata?.lapianIntent || classifyTemplateRole({ ...node, type: node.type }) === "intent");
    if (intentNode) {
        const text = String(input.intentText ?? "").trim() ? String(input.intentText).trim() : sourceIntentContent;
        if (text) intentNode.metadata = { ...intentNode.metadata, content: text };
        else warnings.push("意图节点在但无可落文本（intentText 缺省且源意图为空）");
    } else if (String(input.intentText ?? "").trim()) {
        warnings.push("变体子图无意图节点，intentText 未落位");
    }

    // 连线：端点 + 连线 id 全部重映射（实例连线反解；产物连线合成键）
    const connPrefix = instanceConnectionPrefix(input.templateId, input.sourceInstanceIndex);
    const connections: CanvasConnection[] = subgraph.connections.map((connection) => {
        const nextId = connection.id.startsWith(connPrefix) ? templateInstanceConnectionId(input.templateId, newIndex, connection.id.slice(connPrefix.length)) : variantExtraConnectionId(input.templateId, newIndex, connection.id);
        return { ...connection, id: nextId, fromNodeId: replaceId(connection.fromNodeId) || connection.fromNodeId, toNodeId: replaceId(connection.toNodeId) || connection.toNodeId };
    });

    warnings.push(...unresolvedWarnings);
    return { index: newIndex, nodes, connections, bindings, warnings };
}
