/**
 * 画布工作流模板：子图抽取 / 引用闭包 / 运行态剥离 / 参数声明（P2，方案A 批量代办）。
 *
 * 设计依据（条款已冻结，不得偏离）：
 * - `wanrepro/docs/方案A探索/设计-P0a批量代办设计裁决-20261113.md` §1（冻结拓扑）、§3（运行态剥离清单 + files 剔除声明）、
 *   §2.2（七类 id 引用字段总表）、§5（身份与状态分离）。
 * - 合同 C1：模板 = 画布项目 JSON 的连通子图，沿用 `types/canvas-export.ts` v3 形态，禁第二套模板格式，模板必须能在画布原样打开。
 * - 合同 C2：五槽可变 + 冻结项（指令模板 id / 连线拓扑 / generationMode / composerContent 结构）。
 *
 * 红线 25（复用优先）落实说明——本文件只新建「画布现有实现确实不存在」的三件东西，其余全部复用：
 * | 新建 | 画布对应物为何不适用 |
 * |---|---|
 * | `extractWorkflowTemplate`（子图抽取） | 画布只有整项目导出（`exportCanvasProjects` 全项目打包）与单节点导出（`exportCanvasNodes` 纯文件），
 * |   | 没有「按节点边界抽取连通子图 + 引用闭包 + 运行态剥离」的实现；P0a §3.2 已背书为 P2 实现项。 |
 * | `classifyTemplateRole` / `stripTemplateRuntime` | 运行态剥离是按骨架角色的字段白名单，画布侧无对应函数（节点构造分散在 project.tsx 各派生动作里）。 |
 * | `buildTemplateDeclaration`（参数声明） | 五槽声明是模板期元数据，画布侧只有零散的 metadata 字段使用点，无声明实体。 |
 * 复用（禁复制）：清单行重删走 `upsertLapianAssetManifest`（`lib/canvas/lapian-assets.ts`，与挂图副作用同源）；
 * 扁平/镜像取值口径走 `attachedImageIdOf`；v3 外壳类型走 `types/canvas-export.ts`；导出取字节走 `canvas-export.ts` 的 reader。
 *
 * 纯函数层：不读 i18n、不摸 React、不碰 store，便于探针直接打包真跑（与 `lib/canvas/lapian-assets.ts` 同口径）。
 */

import { attachedImageIdOf, upsertLapianAssetManifest } from "@/lib/canvas/lapian-assets";
import type { CanvasBackgroundMode } from "@/lib/canvas-theme";
import type { CanvasProject } from "@/stores/canvas/use-canvas-store";
import { CanvasNodeType, type CanvasConnection, type CanvasNodeData, type CanvasNodeMetadata } from "@/types/canvas";

/** 模板声明 schema 版本：字段增删必须升版本（导入侧据此判兼容）。 */
export const WORKFLOW_TEMPLATE_SCHEMA_VERSION = 1;

/** 意图节点占位符（与 `lib/canvas/lapian-prompt-templates.ts` 的 LAPIAN_INTENT_PLACEHOLDER 同值；此处内联避免纯函数层拉入模板清单）。 */
export const TEMPLATE_INTENT_PLACEHOLDER = "（可选）写你的改造意图…";

/** 清单行前缀（i18n `canvas.lapianPrompt.assetManifestPrefix` 同值）；调用方可覆盖。 */
export const DEFAULT_MANIFEST_PREFIXES = ["本次参考图："];

/** 骨架角色：决定运行态剥离白名单（P0a §3.1 按角色列条）。 */
export type CanvasTemplateNodeRole =
    | "sourceVideo"
    | "lapian"
    | "instruction"
    | "intent"
    | "finalText"
    | "configText"
    | "configVideo"
    | "fixedAsset"
    | "assetSlot"
    | "assetGraft"
    | "generationOutput"
    | "group"
    | "other";

export type CanvasTemplateSlotKind = "sourceVideo" | "intentText" | "assetAttach" | "generationParams" | "optionalSegments";

/** 参数声明的一个槽：槽种类 + 可写目标（模板内节点 id + 该槽在该节点上可写的字段）。 */
export type CanvasTemplateSlot = {
    kind: CanvasTemplateSlotKind;
    label: string;
    required: boolean;
    targets: { nodeId: string; fields: string[] }[];
    /** 实例期派生（槽③资产位不进模板，见 P0a §1.1）：targets 为空时实例化器按新拉片派生。 */
    derived?: boolean;
    note: string;
};

/** 冻结项声明（实例化器 F1-F4 校验的比对基准）。 */
export type CanvasTemplateFrozen = {
    /** 指令节点正文 + 指令模板 id（F2：模板 id 冻结，正文本身是指令模板内容）。 */
    instructions: { nodeId: string; promptTemplate: string; contentDigest: string; contentLength: number }[];
    /** Config 的 generationMode（F3）。 */
    generationModes: { nodeId: string; mode: string }[];
    /** composerContent 骨架（F4）：剥离清单行后的逐字节 digest + 长度。 */
    composerSkeletons: { nodeId: string; digest: string; length: number }[];
    /** 骨架连线拓扑（F1）：边数 + (from,to) 排序后的 digest。 */
    topology: { edgeCount: number; digest: string; nodeIds: string[] };
};

export type CanvasTemplateExternalRefKind =
    | "connection"
    | "groupId"
    | "assetSlotSourceId"
    | "lapianSourceId"
    | "attachedImageId"
    | "assetSlotAttachedImageId"
    | "assetGraftOfImageId"
    | "assetGraftForSlotId"
    | "composerToken";

/** 图外引用：边界内节点指向边界外节点/边的每一条引用（不静默拉全项目，逐条列出）。 */
export type CanvasTemplateExternalReference = {
    kind: CanvasTemplateExternalRefKind;
    fromNodeId: string;
    field: string;
    targetId: string;
    reason: string;
    token?: string;
    edgeId?: string;
};

/** 运行态剥离记录（审计用；不复制第二份图 DSL，只记字段名）。 */
export type CanvasTemplateStripRecord = {
    nodeId: string;
    role: CanvasTemplateNodeRole;
    fields: string[];
};

/** 模板声明：随 CanvasProject 显式扩展字段持久化 + 进 v3 导出（C1 单一事实源，不另起 envelope）。 */
export type CanvasWorkflowTemplate = {
    schemaVersion: number;
    /** 模板短 id（确定性派生实例 id 的输入之一，P0a §2.1）。 */
    templateId: string;
    name: string;
    createdAt: string;
    sourceProjectId: string;
    sourceProjectTitle: string;
    /** 源项目里被抽进模板的节点 id（审计：谁抽了谁）。 */
    sourceNodeIds: string[];
    slots: CanvasTemplateSlot[];
    frozen: CanvasTemplateFrozen;
    externalReferences: CanvasTemplateExternalReference[];
    stripped: CanvasTemplateStripRecord[];
};

export type ExtractWorkflowTemplateInput = {
    sourceProject: Pick<CanvasProject, "id" | "title" | "nodes" | "connections"> & {
        backgroundMode?: CanvasBackgroundMode;
        viewport?: CanvasProject["viewport"];
    };
    /** 用户指定的节点边界（源项目节点 id）。不在此列的节点一律不自动并入。 */
    selectedNodeIds: string[];
    /** 模板短 id；缺省按 name+源项目 id 派生 6 位 hash。 */
    templateId?: string;
    name?: string;
    createdAt?: string;
    manifestPrefixes?: string[];
};

export type ExtractWorkflowTemplateResult = {
    /** 模板项目：v3 可直接导出、画布可原样打开（C1）。 */
    project: CanvasProject;
    template: CanvasWorkflowTemplate;
    /** 抽取报告（图外引用 + 逐节点剥离字段 + 被排除节点）。 */
    report: {
        nodes: CanvasTemplateStripRecord[];
        externalReferences: CanvasTemplateExternalReference[];
        excluded: { nodeId: string; role: CanvasTemplateNodeRole; reason: string }[];
        warnings: string[];
    };
};

/** FNV-1a 32 位：跨 node/浏览器/无依赖的稳定短 hash（P0a §2.1 允许「短 hash」推导 id）。 */
export function stableDigest(value: string): string {
    let hash = 0x811c9dc5;
    for (const char of String(value ?? "")) {
        hash ^= char.codePointAt(0) ?? 0;
        hash = Math.imul(hash, 0x01000193) >>> 0;
    }
    return hash.toString(16).padStart(8, "0");
}

/** 模板短 id 派生：name + 源项目 id + 节点数，稳定可复算。 */
export function deriveTemplateId(name: string, sourceProjectId: string, nodeCount: number): string {
    return `w${stableDigest(`${name}|${sourceProjectId}|${nodeCount}`).slice(0, 6)}`;
}

const CONFIG_MODES = new Set(["text", "image", "video", "audio"]);

/**
 * 节点角色判定（P0a §1.1 八类骨架 + §3.1 剥离表覆盖的派生角色）。
 * 判据只用 metadata 既有字段，不猜业务语义：
 * - 资产位：`assetSlotSourceId` 有值（含 assetSlot 容器）→ 派生角色，不进默认骨架；
 * - 挂图对照：`assetGraftForSlotId` / `assetGraftOfImageId` 有值 → 运行态产物；
 * - 生成产物：Video 带 `videoTaskId` 或 `primaryImageId`/`images` → 实例执行期新建；
 * - 固定素材：其余 Image/Audio（模板成员，字节可携带）。
 */
export function classifyTemplateRole(node: CanvasNodeData): CanvasTemplateNodeRole {
    const metadata = node.metadata || {};
    if (node.type === CanvasNodeType.Group) return "group";
    if (metadata.assetGraftForSlotId || metadata.assetGraftOfImageId) return "assetGraft";
    if (metadata.assetSlotSourceId || metadata.assetSlot) return "assetSlot";
    if (node.type === CanvasNodeType.Video) {
        return metadata.videoTaskId || metadata.primaryImageId || (metadata.images && metadata.images.length) ? "generationOutput" : "sourceVideo";
    }
    if (node.type === CanvasNodeType.Lapian) return "lapian";
    if (node.type === CanvasNodeType.Config) {
        const mode = String(metadata.generationMode || "");
        if (mode === "video") return "configVideo";
        if (mode === "text") return "configText";
        return "other";
    }
    if (node.type === CanvasNodeType.Text) {
        if (metadata.promptTemplate) return "instruction";
        if (metadata.lapianIntent) return "intent";
        return "finalText";
    }
    if (node.type === CanvasNodeType.Image || node.type === CanvasNodeType.Audio) {
        const storageKey = String(metadata.storageKey || "");
        const content = String(metadata.content || "");
        if (storageKey || content) return "fixedAsset";
        return "other";
    }
    return "other";
}

/** 骨架角色（会进模板、参与拓扑冻结的角色集合）。 */
const SKELETON_ROLES = new Set<CanvasTemplateNodeRole>(["sourceVideo", "lapian", "instruction", "intent", "finalText", "configText", "configVideo", "fixedAsset", "group"]);

export function isSkeletonRole(role: CanvasTemplateNodeRole): boolean {
    return SKELETON_ROLES.has(role);
}

/** 槽④生成参数字段（C2：model/size/seconds/vquality；文本配置另带 count/reasoningEffort）。 */
const GENERATION_PARAM_FIELDS = ["model", "size", "seconds", "vquality", "count", "reasoningEffort"];

/**
 * 运行态剥离（P0a §3.1 逐角色白名单）。返回新节点，不改入参。
 * 通用规则：`status` 与 `errorDetails` 一律剥离（生成执行态，新实例落地即带 success 会被 C5 派生误判为已完成）。
 */
export function stripTemplateRuntime(node: CanvasNodeData, role: CanvasTemplateNodeRole, manifestPrefixes: string[] = DEFAULT_MANIFEST_PREFIXES): { node: CanvasNodeData; fields: string[] } {
    const metadata: CanvasNodeMetadata = { ...(node.metadata || {}) };
    const fields: string[] = [];
    const drop = (key: keyof CanvasNodeMetadata) => {
        if (!(key in metadata)) return;
        delete metadata[key];
        fields.push(String(key));
    };

    switch (role) {
        case "sourceVideo":
            // 槽①待注入位：旧源视频字节对新实例是死重量（Claude 坑C）。
            drop("content");
            drop("storageKey");
            drop("bytes");
            drop("mimeType");
            drop("durationMs");
            drop("naturalWidth");
            drop("naturalHeight");
            break;
        case "lapian":
            drop("taskId");
            drop("jobId");
            drop("content");
            drop("intent");
            drop("lapPhase");
            drop("cameraCoverage");
            break;
        case "instruction":
            drop("status");
            drop("errorDetails");
            break;
        case "intent":
            // 槽②：重置为意图占位符（与创建时的占位形态一致）。
            if (metadata.content !== TEMPLATE_INTENT_PLACEHOLDER) {
                metadata.content = TEMPLATE_INTENT_PLACEHOLDER;
                fields.push("content->placeholder");
            }
            if (metadata.prompt !== TEMPLATE_INTENT_PLACEHOLDER) {
                metadata.prompt = TEMPLATE_INTENT_PLACEHOLDER;
                fields.push("prompt->placeholder");
            }
            drop("status");
            drop("errorDetails");
            break;
        case "finalText":
            drop("content");
            drop("prompt");
            drop("status");
            drop("errorDetails");
            drop("texts");
            drop("primaryTextId");
            // 运镜覆盖度是旧产物的自检度量（只在改写产物落盘时写），属运行态，随产物一起剥离。
            drop("cameraCoverage");
            break;
        case "configText": {
            drop("status");
            drop("errorDetails");
            const composer = String(metadata.composerContent || "");
            if (composer) {
                // M3：清单行按实例重新挂图结果重算 → 模板期删除（复用挂图副作用同一实现，禁自写正则）。
                const stripped = manifestPrefixes.reduce((text, prefix) => upsertLapianAssetManifest(text, "", prefix), composer);
                if (stripped !== composer) {
                    metadata.composerContent = stripped;
                    fields.push("composerContent.manifest");
                }
            }
            break;
        }
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
        case "fixedAsset":
            drop("status");
            drop("errorDetails");
            break;
        case "assetSlot":
            // 资产位默认不进模板（P0a §1.1）；若用户边界显式包含，剥掉运行态截帧地址与生成态，保留槽语义字段。
            drop("status");
            drop("errorDetails");
            if (metadata.assetSlot) {
                const slot = { ...metadata.assetSlot };
                if ("frameUrl" in slot) {
                    delete slot.frameUrl;
                    fields.push("assetSlot.frameUrl");
                }
                metadata.assetSlot = slot;
            }
            break;
        case "assetGraft":
            // 挂图对照节点是运行态产物；显式包含时保留字节，剥掉生成态。
            drop("status");
            drop("errorDetails");
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
            drop("status");
            drop("errorDetails");
            break;
        case "group":
            break;
        default:
            drop("status");
            drop("errorDetails");
            break;
    }

    // 通用规则：任何角色都不保留生成执行态（P0a §3.1 末段）。
    if (role !== "instruction" && role !== "intent" && role !== "configText" && role !== "configVideo") {
        drop("status");
        drop("errorDetails");
    }

    return { node: { ...node, metadata }, fields: [...new Set(fields)] };
}

const TOKEN_PATTERN = /@\[node:([^\]]+)\]/g;

/** 组装串里的 id token（M4 只改 id 段，token 结构与其余文本逐字节不动）。 */
export function composerTokenIds(text: string): string[] {
    const ids: string[] = [];
    const source = String(text || "");
    let match: RegExpExecArray | null;
    TOKEN_PATTERN.lastIndex = 0;
    while ((match = TOKEN_PATTERN.exec(source))) ids.push(match[1]);
    return [...new Set(ids)];
}

/** 节点上的 id 引用字段（七类中的节点级 6 类 + groupId）；返回 field → 目标 id。 */
export function nodeReferenceFields(node: CanvasNodeData): { kind: CanvasTemplateExternalRefKind; field: string; targetId: string }[] {
    const metadata = node.metadata || {};
    const out: { kind: CanvasTemplateExternalRefKind; field: string; targetId: string }[] = [];
    const push = (kind: CanvasTemplateExternalRefKind, field: string, value?: string) => {
        const target = String(value || "").trim();
        if (target) out.push({ kind, field, targetId: target });
    };
    push("assetSlotSourceId", "assetSlotSourceId", metadata.assetSlotSourceId);
    push("lapianSourceId", "lapianSourceId", metadata.lapianSourceId);
    push("assetGraftOfImageId", "assetGraftOfImageId", metadata.assetGraftOfImageId);
    push("assetGraftForSlotId", "assetGraftForSlotId", metadata.assetGraftForSlotId);
    push("groupId", "groupId", metadata.groupId);
    const flat = String(metadata.attachedImageId || "").trim();
    if (flat) out.push({ kind: "attachedImageId", field: "attachedImageId", targetId: flat });
    const mirror = String(metadata.assetSlot?.attachedImageId || "").trim();
    if (mirror) out.push({ kind: "assetSlotAttachedImageId", field: "assetSlot.attachedImageId", targetId: mirror });
    return out;
}

/** 拓扑签名：边 (from,to) 排序后拼串（可选 id 映射，用于实例 ↔ 模板同构比对）。 */
export function topologyDigest(connections: CanvasConnection[], idMap?: Map<string, string>): string {
    const map = (id: string) => (idMap ? idMap.get(id) || id : id);
    const edges = connections.map((connection) => `${map(connection.fromNodeId)}->${map(connection.toNodeId)}`).sort();
    return stableDigest(edges.join("\n"));
}

/**
 * 子图抽取（P2 核心）：按用户给定节点边界取连通子图，逐节点剥运行态，收集图外引用并生成参数声明。
 *
 * 边界语义（不静默拉全项目）：`selectedNodeIds` 是唯一入口；引用了边界外节点的字段/边/token 一律进
 * `externalReferences` 报告，由调用方（P3 实例化器）决定拒绝还是另行补槽。
 */
export function extractWorkflowTemplate(input: ExtractWorkflowTemplateInput): ExtractWorkflowTemplateResult {
    const manifestPrefixes = input.manifestPrefixes?.length ? input.manifestPrefixes : DEFAULT_MANIFEST_PREFIXES;
    const sourceNodes = input.sourceProject.nodes || [];
    const sourceConnections = input.sourceProject.connections || [];
    const byId = new Map(sourceNodes.map((node) => [node.id, node]));
    const warnings: string[] = [];
    const excluded: ExtractWorkflowTemplateResult["report"]["excluded"] = [];

    const selected: string[] = [];
    for (const raw of input.selectedNodeIds || []) {
        const id = String(raw || "").trim();
        if (!id) continue;
        if (!byId.has(id)) {
            warnings.push(`边界节点 ${id} 不在源项目里，已忽略`);
            continue;
        }
        if (!selected.includes(id)) selected.push(id);
    }
    if (!selected.length) throw new Error("节点边界为空：至少指定一个源项目节点 id");
    const selectedSet = new Set(selected);

    // ① 逐节点分类 + 剥运行态
    const templateNodes: CanvasNodeData[] = [];
    const stripRecords: CanvasTemplateStripRecord[] = [];
    const roles = new Map<string, CanvasTemplateNodeRole>();
    for (const id of selected) {
        const source = byId.get(id)!;
        const role = classifyTemplateRole(source);
        roles.set(id, role);
        // 派生角色（资产位/挂图/生成产物）默认不进模板；显式选中时保留壳并剥运行态（不静默丢用户指定的节点）。
        const stripped = stripTemplateRuntime(source, role, manifestPrefixes);
        templateNodes.push(stripped.node);
        stripRecords.push({ nodeId: id, role, fields: stripped.fields });
        if (!isSkeletonRole(role)) {
            excluded.push({ nodeId: id, role, reason: `派生角色（${role}）：实例执行期按新拉片/新生成重新派生，模板侧仅保留空壳或壳结构` });
        }
    }

    // ② 连线：两端都在边界内才进模板；跨界边逐条报告
    const externalReferences: CanvasTemplateExternalReference[] = [];
    const templateConnections: CanvasConnection[] = [];
    for (const connection of sourceConnections) {
        const fromIn = selectedSet.has(connection.fromNodeId);
        const toIn = selectedSet.has(connection.toNodeId);
        if (fromIn && toIn) {
            templateConnections.push({ ...connection });
            continue;
        }
        if (!fromIn && !toIn) continue;
        externalReferences.push({
            kind: "connection",
            fromNodeId: fromIn ? connection.fromNodeId : connection.toNodeId,
            field: "connection",
            targetId: fromIn ? connection.toNodeId : connection.fromNodeId,
            reason: fromIn ? "边界内节点 → 图外节点的出边（目标未纳入模板）" : "图外节点 → 边界内节点的入边（来源未纳入模板）",
            edgeId: connection.id,
        });
    }

    // ③ 节点级引用字段 + composerContent token：命不中边界即图外引用
    for (const node of templateNodes) {
        for (const reference of nodeReferenceFields(node)) {
            if (selectedSet.has(reference.targetId)) continue;
            externalReferences.push({
                kind: reference.kind,
                fromNodeId: node.id,
                field: reference.field,
                targetId: reference.targetId,
                reason: `metadata.${reference.field} 指向边界外节点（${byId.get(reference.targetId)?.title || "未知节点"}）`,
            });
        }
        const composer = String(node.metadata?.composerContent || "");
        for (const tokenId of composerTokenIds(composer)) {
            if (selectedSet.has(tokenId)) continue;
            externalReferences.push({
                kind: "composerToken",
                fromNodeId: node.id,
                field: "composerContent",
                targetId: tokenId,
                reason: "组装串 token 指向边界外节点：实例化后该 token 无对应节点（清单/参考图断裂）",
                token: `@[node:${tokenId}]`,
            });
        }
        // 图外引用去重：同一节点同字段同目标只留一条
    }
    const seenRefs = new Set<string>();
    const dedupedRefs = externalReferences.filter((reference) => {
        const key = `${reference.kind}|${reference.fromNodeId}|${reference.field}|${reference.targetId}`;
        if (seenRefs.has(key)) return false;
        seenRefs.add(key);
        return true;
    });

    // ④ 参数声明 + 冻结项（在剥离后的节点上算 digest：实例落库后按同一口径比对）
    const strippedById = new Map(templateNodes.map((node) => [node.id, node]));
    const templateId = String(input.templateId || "").trim() || deriveTemplateId(input.name || input.sourceProject.title || "模板", input.sourceProject.id, selected.length);
    const name = String(input.name || "").trim() || `${input.sourceProject.title || "画布"} · 模板`;
    const createdAt = input.createdAt || new Date().toISOString();

    const slots = buildTemplateDeclaration(selected, roles, strippedById);
    const frozen: CanvasTemplateFrozen = {
        instructions: selected
            .filter((id) => roles.get(id) === "instruction")
            .map((id) => {
                const metadata = strippedById.get(id)?.metadata || {};
                const content = String(metadata.content || metadata.prompt || "");
                return { nodeId: id, promptTemplate: String(metadata.promptTemplate || ""), contentDigest: stableDigest(content), contentLength: content.length };
            }),
        generationModes: selected
            .filter((id) => roles.get(id) === "configText" || roles.get(id) === "configVideo")
            .map((id) => ({ nodeId: id, mode: String(strippedById.get(id)?.metadata?.generationMode || "") }))
            .filter((item) => CONFIG_MODES.has(item.mode)),
        composerSkeletons: selected
            .filter((id) => roles.get(id) === "configText" || roles.get(id) === "configVideo")
            .map((id) => {
                const composer = String(strippedById.get(id)?.metadata?.composerContent || "");
                return { nodeId: id, digest: stableDigest(composer), length: composer.length };
            }),
        topology: { edgeCount: templateConnections.length, digest: topologyDigest(templateConnections), nodeIds: [...selected] },
    };

    const template: CanvasWorkflowTemplate = {
        schemaVersion: WORKFLOW_TEMPLATE_SCHEMA_VERSION,
        templateId,
        name,
        createdAt,
        sourceProjectId: input.sourceProject.id,
        sourceProjectTitle: input.sourceProject.title || "",
        sourceNodeIds: [...selected],
        slots,
        frozen,
        externalReferences: dedupedRefs,
        stripped: stripRecords,
    };

    if (!slots[0].targets.length) warnings.push("模板内没有源视频节点：槽①无待注入位");
    if (dedupedRefs.length) warnings.push(`存在 ${dedupedRefs.length} 条图外引用：模板自足性不完整，P3 实例化器须拒绝或补槽`);

    const now = createdAt;
    const project: CanvasProject = {
        id: `tpl-${templateId}`,
        title: name,
        createdAt: now,
        updatedAt: now,
        nodes: templateNodes,
        connections: templateConnections,
        chatSessions: [],
        activeChatId: null,
        backgroundMode: input.sourceProject.backgroundMode || "lines",
        showImageInfo: false,
        viewport: input.sourceProject.viewport || { x: 0, y: 0, k: 1 },
        workflowTemplate: template,
    };

    return { project, template, report: { nodes: stripRecords, externalReferences: dedupedRefs, excluded, warnings } };
}

/** 五槽声明（C2）：每槽给出可写目标与字段白名单（P3 F5 越槽校验的比对基准）。 */
export function buildTemplateDeclaration(selectedIds: string[], roles: Map<string, CanvasTemplateNodeRole>, nodes: Map<string, CanvasNodeData>): CanvasTemplateSlot[] {
    const idsOf = (role: CanvasTemplateNodeRole) => selectedIds.filter((id) => roles.get(id) === role);
    const sourceVideos = idsOf("sourceVideo");
    const intents = idsOf("intent");
    const lapians = idsOf("lapian");
    const configs = [...idsOf("configText"), ...idsOf("configVideo")];
    const slots = idsOf("assetSlot");

    return [
        {
            kind: "sourceVideo",
            label: "①源视频",
            required: true,
            targets: sourceVideos.map((nodeId) => ({ nodeId, fields: ["content", "storageKey", "mimeType", "bytes", "durationMs", "naturalWidth", "naturalHeight"] })),
            note: "槽①待注入：模板内源视频节点已剥字节，实例化时写新视频媒体字段并重跑拉片",
        },
        {
            kind: "intentText",
            label: "②意图文本",
            required: false,
            targets: intents.map((nodeId) => ({ nodeId, fields: ["content", "prompt"] })),
            note: "槽②：意图正文（lapianIntent 标记节点的 content/prompt，合同 X1 口径修正）",
        },
        {
            kind: "assetAttach",
            label: "③资产位挂图",
            required: false,
            targets: slots.map((nodeId) => ({ nodeId, fields: ["attachedImageId", "assetSlot.attachedImageId", "attachedImageDesc", "assetSlot.caption"] })),
            derived: slots.length === 0,
            note: slots.length
                ? "槽③：按 assetSlotOrder 写挂图，扁平 attachedImageId 与 assetSlot 镜像须同一次 update 双写"
                : "槽③：资产位不进模板（P0a §1.1），实例期按新拉片派生资产位后挂图；清单行由挂图副作用重算",
        },
        {
            kind: "generationParams",
            label: "④生成参数",
            required: false,
            targets: configs
                .map((nodeId) => {
                    const mode = String(nodes.get(nodeId)?.metadata?.generationMode || "");
                    // 文本配置只认 model/count/reasoningEffort；视频配置认 model/size/seconds/vquality。
                    const allowed = mode === "text" ? ["model", "count", "reasoningEffort"] : GENERATION_PARAM_FIELDS.filter((field) => field !== "count" && field !== "reasoningEffort");
                    return { nodeId, fields: allowed };
                })
                .filter((target) => target.fields.length > 0),
            note: "槽④：model/size/seconds/vquality（文本配置另带 count/reasoningEffort）；值来自模板默认值",
        },
        {
            kind: "optionalSegments",
            label: "⑤可选段",
            required: false,
            targets: lapians.map((nodeId) => ({ nodeId, fields: ["lapianOptional"] })),
            note: "槽⑤：lapianOptional 渲染可选段字母（空/缺省 = 全关）；默认值随模板携带",
        },
    ];
}

/**
 * 模板自足性校验：组装串 token / metadata 引用必须全部落在模板内（F1-F4 的静态前置）。
 * 返回问题清单（空数组 = 通过）。
 */
export function validateTemplateProject(nodes: CanvasNodeData[], connections: CanvasConnection[]): string[] {
    const problems: string[] = [];
    const ids = new Set(nodes.map((node) => node.id));
    for (const node of nodes) {
        for (const reference of nodeReferenceFields(node)) {
            if (!ids.has(reference.targetId)) problems.push(`${node.id}.${reference.field} → ${reference.targetId} 不在模板内`);
        }
        for (const tokenId of composerTokenIds(String(node.metadata?.composerContent || ""))) {
            if (!ids.has(tokenId)) problems.push(`${node.id}.composerContent token → ${tokenId} 不在模板内`);
        }
        // 扁平真源与 assetSlot 镜像必须一致（双写口径，Claude C2 槽③双写坑）。
        const flat = String(node.metadata?.attachedImageId || "");
        const mirror = String(node.metadata?.assetSlot?.attachedImageId || "");
        if (flat && mirror && flat !== mirror) problems.push(`${node.id} attachedImageId 与 assetSlot 镜像不一致（${flat} vs ${mirror}）`);
        if (flat && !mirror && node.metadata?.assetSlot) problems.push(`${node.id} 有扁平 attachedImageId 但 assetSlot 镜像缺失`);
        if (!flat && mirror) problems.push(`${node.id} 有 assetSlot 镜像但扁平 attachedImageId 缺失（真源缺失）`);
        if (node.metadata?.status || node.metadata?.errorDetails) problems.push(`${node.id} 残留运行态 status/errorDetails`);
        if (node.metadata?.taskId || node.metadata?.jobId) problems.push(`${node.id} 残留 taskId/jobId`);
        if (node.metadata?.videoTaskId || node.metadata?.videoProgress) problems.push(`${node.id} 残留 videoTaskId/videoProgress`);
        if (node.metadata?.assetSlot?.frameUrl) problems.push(`${node.id} 残留截帧地址 frameUrl`);
    }
    for (const connection of connections) {
        if (!ids.has(connection.fromNodeId) || !ids.has(connection.toNodeId)) problems.push(`连线 ${connection.id} 端点不在模板内`);
    }
    return problems;
}

/** 便捷读取：扁平真源（复用 lapian-assets 口径，禁自写 fallback 顺序）。 */
export function templateAttachedImageId(node: CanvasNodeData): string {
    return attachedImageIdOf(node);
}
