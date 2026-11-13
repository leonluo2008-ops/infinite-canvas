/**
 * P7a · 资产自动匹配 + 一键否决（B0-D3 落地）。
 *
 * 依据（条款冻结）：
 * - B0 合同 D3：「置换=Agent 按资产库自动匹配 + 展示理由，所有者一键否决」；C2 槽③资产位挂图
 *   按 `assetSlotOrder` 写 `attachedImageId`。
 * - 审计 G17：`assets_list` 只是基础列表能力，「**选择原因 / 所属槽 / 否决后不复用旧匹配**」都不是
 *   列表工具自带流程——本文件补的就是这三件事，且必须能验（不是只验上传成功）。
 * - 审计 §9.2 P7a 判据：资产选择按实例/槽归属；展示选择理由；一键否决后旧选择不会悄悄再次进入请求；
 *   最终图号/请求 images 与槽序一致；候选不足明确交人。
 * - P0a §5.3：「身份」可存、「状态」禁存。否决记录是**人工决策的身份侧事实**（谁在哪一槽拒了哪张图），
 *   不是「现在怎么样」，落 `CanvasProject.workflowAssetVetoes`（与 workflowInstances/workflowBatches
 *   同族显式扩展字段：随项目持久化 + 随 v3 导出同路）。
 * - 红线 25：槽源/槽序/清单行/短说明一律复用 `lapian-assets.ts`（`collectLapianAssetReferences`、
 *   `attachedImageIdOf`、`buildLapianAssetManifest` 同源口径），本文件**不新写槽发现逻辑**；
 *   匹配只用确定性关键词/槽语义打分，**不引外部模型调用**（卡面红线：确需 LLM 须回卡说明并默认关闭，
 *   本卡没有需要，所以没有开关，也没有模型调用）。
 *
 * 本文件全是纯函数（不读 i18n、不摸 React、不碰 store），探针可 esbuild 打包真跑。
 */

import { attachedImageIdOf, buildLapianAssetManifest, collectLapianAssetReferences, imageTitleCaption, truncateAssetCaption } from "@/lib/canvas/lapian-assets";
import { stableDigest } from "@/lib/canvas/workflow-template";
import { CanvasNodeType, type CanvasNodeData } from "@/types/canvas";
import type { WorkflowInstanceAssetAttach, WorkflowInstancePlan } from "@/lib/canvas/workflow-instance";

export const WORKFLOW_ASSET_VETO_SCHEMA_VERSION = 1;

/** 匹配打分权重（名字命中是硬条件，权重最高；类型次之；原片描述只做加权）。 */
export const ASSET_MATCH_WEIGHTS = { name: 6, type: 2, desc: 1 } as const;
/** 命中门槛：至少一个槽名 token 命中 + 总分达到该值，否则判「候选不足，交人」。 */
export const ASSET_MATCH_MIN_SCORE = 6;

// ─────────────────────────────────────────────── 槽（本实例的资产位，槽序=图号序）

/** 本实例的一个资产位：来源=画布节点，槽序与图号序同源（`assetSlotOrder`）。 */
export type InstanceAssetSlot = {
    /** 实例内资产位节点 id（P3 确定性重映射产物）。 */
    slotNodeId: string;
    /** 模板内资产位节点 id（挂图材料要按模板 id 传，P3 自己重映射）。 */
    slotTemplateNodeId: string;
    /** 语义键 `类型|名`：跨实例复核否决记录用（节点 id 逐实例不同）。 */
    slotKey: string;
    slotOrder: number;
    name: string;
    type: string;
    /** 原片对应物描述（IR 条目原文）；没有就是空串。 */
    desc: string;
    /** 当前挂的图（未挂=空串）。 */
    currentImageId: string;
};

/**
 * 列出本实例的资产位（槽序 = `assetSlotOrder`，与 `collectLapianAssetReferences` 同一排序口径）。
 * 归属判据只有一条：`assetSlotSourceId === 本实例拉片节点 id`——别的实例的资产位永远不进本实例的槽表。
 */
export function listInstanceAssetSlots(input: { plan: Pick<WorkflowInstancePlan, "lapianNodeId" | "nodeIdMap">; nodes: CanvasNodeData[] }): InstanceAssetSlot[] {
    const inverse = new Map<string, string>();
    for (const [oldId, newId] of Object.entries(input.plan.nodeIdMap || {})) inverse.set(newId, oldId);
    return input.nodes
        .filter((node) => String(node.metadata?.assetSlotSourceId || "") === String(input.plan.lapianNodeId || ""))
        .sort((left, right) => (left.metadata?.assetSlotOrder ?? 0) - (right.metadata?.assetSlotOrder ?? 0) || String(left.id).localeCompare(String(right.id)))
        .map((node) => {
            const name = String(node.metadata?.assetSlotName || node.title || "").trim();
            const type = String(node.metadata?.assetSlotType || node.metadata?.assetSlot?.desc || "").trim();
            return {
                slotNodeId: node.id,
                slotTemplateNodeId: inverse.get(node.id) || node.id,
                slotKey: `${type || "未分类"}|${name || node.id}`,
                slotOrder: Number(node.metadata?.assetSlotOrder ?? 0),
                name,
                type: String(node.metadata?.assetSlotType || "").trim(),
                desc: String(node.metadata?.assetSlot?.desc || "").trim(),
                currentImageId: attachedImageIdOf(node),
            };
        });
}

// ─────────────────────────────────────────────── 资产库候选（项目图片，按归属过滤）

export type AssetLibraryCandidate = {
    imageNodeId: string;
    title: string;
    /** 图片名派生的短说明（`imageTitleCaption` 同源：文件名样式的不进清单/不参与打分）。 */
    caption: string;
    /** 候选节点本体（挂图材料里随实例一起建副本用）。 */
    node: CanvasNodeData;
};

/**
 * 收集资产库候选：项目内**图片节点**（有字节或 dataURL），扣掉三类「不属于资产库」的东西：
 * 1. 挂图对照副本（`assetGraftForSlotId`/`assetGraftOfImageId`）——那是实例的派生物，不是素材；
 * 2. 别实例资产位已挂的图（该图归属别的实例槽）——防全项目乱配（卡面判据）；
 * 3. 本实例资产位自己挂的图（它们已经「在槽上」，不需要再被匹配进来；换图走否决/人指定）。
 */
export function collectAssetLibraryCandidates(input: { nodes: CanvasNodeData[]; lapianNodeId: string }): { candidates: AssetLibraryCandidate[]; excluded: { imageNodeId: string; title: string; reason: string }[] } {
    const nodes = input.nodes || [];
    const ownedByOtherInstance = new Map<string, string>();
    const ownedByThisInstance = new Set<string>();
    for (const node of nodes) {
        const owner = String(node.metadata?.assetSlotSourceId || "");
        const attached = attachedImageIdOf(node);
        if (!owner || !attached) continue;
        if (owner === input.lapianNodeId) ownedByThisInstance.add(attached);
        else ownedByOtherInstance.set(attached, owner);
    }
    const candidates: AssetLibraryCandidate[] = [];
    const excluded: { imageNodeId: string; title: string; reason: string }[] = [];
    for (const node of nodes) {
        if (node.type !== CanvasNodeType.Image) continue;
        const hasBytes = Boolean(String(node.metadata?.storageKey || "").trim()) || Boolean(String(node.metadata?.content || "").trim());
        if (!hasBytes) {
            excluded.push({ imageNodeId: node.id, title: node.title || "", reason: "图片没有字节/地址（不可挂）" });
            continue;
        }
        if (node.metadata?.assetGraftForSlotId || node.metadata?.assetGraftOfImageId) {
            excluded.push({ imageNodeId: node.id, title: node.title || "", reason: "挂图对照副本（实例派生物，不是资产库素材）" });
            continue;
        }
        if (node.metadata?.assetSlotSourceId) {
            excluded.push({ imageNodeId: node.id, title: node.title || "", reason: "资产位节点本身（槽，不是素材）" });
            continue;
        }
        const otherOwner = ownedByOtherInstance.get(node.id);
        if (otherOwner) {
            excluded.push({ imageNodeId: node.id, title: node.title || "", reason: `已归属其它实例的资产位（源=${otherOwner}），禁止跨实例乱配` });
            continue;
        }
        if (ownedByThisInstance.has(node.id)) {
            excluded.push({ imageNodeId: node.id, title: node.title || "", reason: "本实例资产位当前已挂这张图（已在槽上，无需重复匹配）" });
            continue;
        }
        candidates.push({ imageNodeId: node.id, title: String(node.title || "").trim(), caption: imageTitleCaption(String(node.title || "")), node });
    }
    return { candidates, excluded };
}

// ─────────────────────────────────────────────── 否决记录（一键否决）

/** 所有者否决一条匹配：实例 + 槽 + 被拒的图。落项目（`workflowAssetVetoes`），随导出走。 */
export type WorkflowAssetVetoRecord = {
    schemaVersion: typeof WORKFLOW_ASSET_VETO_SCHEMA_VERSION;
    instanceId: string;
    templateId: string;
    instanceIndex: number;
    /** 实例内资产位节点 id（P3 确定性 id：同实例重跑/续跑是同一个 id）。 */
    slotNodeId: string;
    /** 语义键 `类型|名`（复核用：换模板也能看出拒的是哪个语义槽）。 */
    slotKey: string;
    rejectedImageId: string;
    rejectedImageTitle?: string;
    vetoedAt: string;
    note?: string;
};

/** 一键否决：从一次匹配结果造否决记录（纯函数，调用方负责落项目）。 */
export function vetoAssetMatch(input: {
    match: AssetSlotMatch;
    templateId: string;
    instanceId: string;
    instanceIndex: number;
    vetoedAt: string;
    note?: string;
    imageTitleById?: Map<string, string> | Record<string, string>;
}): WorkflowAssetVetoRecord {
    const imageId = String(input.match.imageNodeId || "").trim();
    if (!imageId) throw new Error(`槽 ${input.match.slotNodeId} 没有可否决的匹配（当前状态=${input.match.status}）：否决只能针对一条已存在的选择`);
    const title = input.imageTitleById instanceof Map ? input.imageTitleById.get(imageId) : input.imageTitleById ? input.imageTitleById[imageId] : undefined;
    return {
        schemaVersion: WORKFLOW_ASSET_VETO_SCHEMA_VERSION,
        instanceId: input.instanceId,
        templateId: input.templateId,
        instanceIndex: input.instanceIndex,
        slotNodeId: input.match.slotNodeId,
        slotKey: input.match.slotKey,
        rejectedImageId: imageId,
        rejectedImageTitle: title || input.match.reason.candidateTitle,
        vetoedAt: input.vetoedAt,
        note: input.note,
    };
}

/** 导入侧校验：形如否决记录就透传（未知新版本不静默丢）。 */
export function acceptWorkflowAssetVetoes(value: unknown): WorkflowAssetVetoRecord[] | undefined {
    if (!Array.isArray(value)) return undefined;
    const records = value.filter((item): item is WorkflowAssetVetoRecord => {
        if (!item || typeof item !== "object") return false;
        const candidate = item as Partial<WorkflowAssetVetoRecord>;
        return Boolean(candidate.instanceId) && Boolean(candidate.slotNodeId) && Boolean(candidate.rejectedImageId);
    });
    return records.length ? records : undefined;
}

/** 同实例同槽同图只留一条（否决幂等：重复点不涨记录）。 */
export function upsertAssetVeto(records: WorkflowAssetVetoRecord[] | undefined, record: WorkflowAssetVetoRecord): WorkflowAssetVetoRecord[] {
    const list = Array.isArray(records) ? records : [];
    const kept = list.filter((item) => !(item.instanceId === record.instanceId && item.slotNodeId === record.slotNodeId && item.rejectedImageId === record.rejectedImageId));
    return [...kept, record];
}

/** 这条匹配是否被所有者否决过（判据只认实例 + 槽 + 图三键，不认标题/序号）。 */
export function findAssetVeto(vetoes: WorkflowAssetVetoRecord[] | undefined, key: { instanceId: string; slotNodeId: string; imageNodeId: string }): WorkflowAssetVetoRecord | undefined {
    return (vetoes || []).find((item) => item.instanceId === key.instanceId && item.slotNodeId === key.slotNodeId && item.rejectedImageId === key.imageNodeId);
}

/** 某槽被否决过的图 id 集合（匹配时整批排除，替代匹配或留空交人）。 */
export function vetoedImagesOfSlot(vetoes: WorkflowAssetVetoRecord[] | undefined, key: { instanceId: string; slotNodeId: string }): string[] {
    return [...new Set((vetoes || []).filter((item) => item.instanceId === key.instanceId && item.slotNodeId === key.slotNodeId).map((item) => item.rejectedImageId))];
}

// ─────────────────────────────────────────────── 匹配（确定性打分 + 理由）

/** 一个候选的打分明细（展示理由与落选候选都用它）。 */
export type AssetMatchScore = { imageNodeId: string; title: string; score: number; matchedTerms: string[] };

export type AssetSlotMatch = {
    slotNodeId: string;
    slotTemplateNodeId: string;
    slotKey: string;
    slotOrder: number;
    slotName: string;
    slotType: string;
    /** matched=选中；no_candidate=候选不足交人；vetoed=候选全被否决交人。 */
    status: "matched" | "no_candidate" | "vetoed";
    imageNodeId?: string;
    reason: {
        rule: "slot_semantics_keyword" | "owner_pinned";
        /** 选择依据的来源：模板槽语义（哪个槽、什么名/类型）+ 素材匹配依据（候选标题、命中词）。 */
        source: string;
        matchedTerms: string[];
        score: number;
        candidateTitle?: string;
        /** 落选候选（按分数降序；P8 侧栏展示「为什么是它」的对照）。 */
        alternatives: AssetMatchScore[];
        /** 该槽被否决过、本轮已排除的图（否决不复用的可见痕迹）。 */
        vetoedImageIds: string[];
        /** 一句人话理由。 */
        note: string;
    };
};

export type InstanceAssetMatchPlan = {
    instanceId: string;
    templateId: string;
    instanceIndex: number;
    slots: AssetSlotMatch[];
    /** 槽③挂图材料（**按槽序**：图号/图序与槽序一致的地基）。 */
    attaches: WorkflowInstanceAssetAttach[];
    unmatched: { slotNodeId: string; slotKey: string; reason: string }[];
    vetoed: { slotNodeId: string; slotKey: string; rejectedImageId: string }[];
    warnings: string[];
    /** 计划 digest（理由集可复算：同输入同 digest，便于重放对账）。 */
    digest: string;
};

/** 文本 → 打分 token：ASCII 词取 ≥2 字；中文按 2-gram（短词整取）。 */
export function assetMatchTokens(text: string): string[] {
    const value = String(text || "").toLowerCase();
    const out = new Set<string>();
    for (const word of value.match(/[a-z0-9]{2,}/g) || []) out.add(word);
    for (const run of value.match(/[\u4e00-\u9fff]+/g) || []) {
        if (run.length <= 4) out.add(run);
        else for (let index = 0; index + 2 <= run.length; index += 1) out.add(run.slice(index, index + 2));
    }
    return [...out];
}

type WeightedToken = { token: string; weight: number; field: "name" | "type" | "desc" };

/** 槽语义 token 集：名字（硬条件）、类型、原片描述。 */
function slotTokens(slot: InstanceAssetSlot): WeightedToken[] {
    const out: WeightedToken[] = [];
    for (const token of assetMatchTokens(slot.name)) out.push({ token, weight: ASSET_MATCH_WEIGHTS.name, field: "name" });
    for (const token of assetMatchTokens(slot.type)) out.push({ token, weight: ASSET_MATCH_WEIGHTS.type, field: "type" });
    for (const token of assetMatchTokens(slot.desc)) out.push({ token, weight: ASSET_MATCH_WEIGHTS.desc, field: "desc" });
    // 同名 token 只留权重最高的一条（名字命中优先，不被描述稀释）
    const best = new Map<string, WeightedToken>();
    for (const item of out) {
        const current = best.get(item.token);
        if (!current || current.weight < item.weight) best.set(item.token, item);
    }
    return [...best.values()];
}

/** 候选 token 集（标题 + 文件名派生短说明）。 */
function candidateTokens(candidate: AssetLibraryCandidate): Set<string> {
    return new Set([...assetMatchTokens(candidate.title), ...assetMatchTokens(candidate.caption)]);
}

/** 单槽对单候选打分：必须命中至少一个**槽名** token，否则 0 分（防「同类不同物」乱配）。 */
export function scoreAssetCandidate(slot: InstanceAssetSlot, candidate: AssetLibraryCandidate): { score: number; matchedTerms: string[]; nameHit: boolean } {
    const tokens = candidateTokens(candidate);
    let score = 0;
    let nameHit = false;
    const matchedTerms: string[] = [];
    for (const item of slotTokens(slot)) {
        if (!tokens.has(item.token)) continue;
        score += item.weight;
        matchedTerms.push(item.token);
        if (item.field === "name") nameHit = true;
    }
    return { score: nameHit ? score : 0, matchedTerms, nameHit };
}

/**
 * 自动匹配（纯函数）：按槽序逐槽选图；同一次匹配内一张图只进一个槽；否决过的图整批排除。
 * 候选不足（没有候选/没有槽名命中/被否决光了）→ 该槽 `no_candidate`/`vetoed`，**留空交人**（卡面判据）。
 */
export function matchInstanceAssetSlots(input: {
    slots: InstanceAssetSlot[];
    candidates: AssetLibraryCandidate[];
    vetoes?: WorkflowAssetVetoRecord[];
    instanceId: string;
    templateId: string;
    instanceIndex: number;
    /** 人指定的挂图（优先于自动匹配；同样受否决约束）。 */
    explicit?: WorkflowInstanceAssetAttach[];
}): InstanceAssetMatchPlan {
    const used = new Set<string>();
    const slots: AssetSlotMatch[] = [];
    const attaches: WorkflowInstanceAssetAttach[] = [];
    const unmatched: InstanceAssetMatchPlan["unmatched"] = [];
    const vetoed: InstanceAssetMatchPlan["vetoed"] = [];
    const warnings: string[] = [];
    const bySlotNode = new Map(input.slots.map((slot) => [slot.slotNodeId, slot]));
    const pinnedBySlot = new Map<string, WorkflowInstanceAssetAttach>();
    for (const attach of input.explicit || []) {
        const key = String(attach.slotNodeId || "");
        if (!key) continue;
        pinnedBySlot.set(key, attach);
    }

    for (const slot of input.slots) {
        const vetoedImages = vetoedImagesOfSlot(input.vetoes, { instanceId: input.instanceId, slotNodeId: slot.slotNodeId });
        const pinned = pinnedBySlot.get(slot.slotTemplateNodeId) || pinnedBySlot.get(slot.slotNodeId);

        // ① 人指定优先：同样受否决约束（否决过的旧匹配不得悄悄再次进入请求）
        if (pinned) {
            const pinnedImageId = String(pinned.imageNodeId || pinned.image?.id || "");
            if (pinnedImageId && vetoedImages.includes(pinnedImageId)) {
                slots.push({
                    slotNodeId: slot.slotNodeId,
                    slotTemplateNodeId: slot.slotTemplateNodeId,
                    slotKey: slot.slotKey,
                    slotOrder: slot.slotOrder,
                    slotName: slot.name,
                    slotType: slot.type,
                    status: "vetoed",
                    reason: { rule: "owner_pinned", source: `人指定挂图（已否决：${pinnedImageId}）`, matchedTerms: [], score: 0, candidateTitle: pinned.image?.title, alternatives: [], vetoedImageIds: vetoedImages, note: "这条挂图被所有者否决过：不挂，交人重选" },
                });
                vetoed.push({ slotNodeId: slot.slotNodeId, slotKey: slot.slotKey, rejectedImageId: pinnedImageId });
                unmatched.push({ slotNodeId: slot.slotNodeId, slotKey: slot.slotKey, reason: "人指定的图已被否决：留空交人" });
                warnings.push(`槽 ${slot.name || slot.slotNodeId} 的人指定挂图 ${pinnedImageId} 已被否决：本实例不挂图`);
                continue;
            }
            const node = pinned.image || input.candidates.find((candidate) => candidate.imageNodeId === pinnedImageId)?.node;
            if (!node) {
                unmatched.push({ slotNodeId: slot.slotNodeId, slotKey: slot.slotKey, reason: `人指定的图 ${pinnedImageId} 不在资产库里` });
                slots.push({
                    slotNodeId: slot.slotNodeId,
                    slotTemplateNodeId: slot.slotTemplateNodeId,
                    slotKey: slot.slotKey,
                    slotOrder: slot.slotOrder,
                    slotName: slot.name,
                    slotType: slot.type,
                    status: "no_candidate",
                    reason: { rule: "owner_pinned", source: "人指定挂图（图不在资产库）", matchedTerms: [], score: 0, alternatives: [], vetoedImageIds: vetoedImages, note: "人指定的图不在资产库里：留空交人" },
                });
                continue;
            }
            used.add(pinnedImageId);
            slots.push({
                slotNodeId: slot.slotNodeId,
                slotTemplateNodeId: slot.slotTemplateNodeId,
                slotKey: slot.slotKey,
                slotOrder: slot.slotOrder,
                slotName: slot.name,
                slotType: slot.type,
                status: "matched",
                imageNodeId: pinnedImageId,
                reason: {
                    rule: "owner_pinned",
                    source: `人指定挂图（槽③·${slot.type || "未分类"}·${slot.name || slot.slotNodeId}，第 ${slot.slotOrder + 1} 号槽）`,
                    matchedTerms: [],
                    score: Number.MAX_SAFE_INTEGER,
                    candidateTitle: String(node.title || ""),
                    alternatives: [],
                    vetoedImageIds: vetoedImages,
                    note: `所有者指定：不参与自动打分（候选 ${pinnedImageId}）`,
                },
            });
            attaches.push({ slotNodeId: slot.slotTemplateNodeId, image: node, caption: truncateAssetCaption(pinned.caption || imageTitleCaption(String(node.title || ""))) });
            continue;
        }

        // ② 自动匹配：打分 → 按（分数降序，标题升序，id 升序）确定性排序
        const scored: AssetMatchScore[] = [];
        for (const candidate of input.candidates) {
            if (used.has(candidate.imageNodeId)) continue;
            if (vetoedImages.includes(candidate.imageNodeId)) continue;
            const result = scoreAssetCandidate(slot, candidate);
            if (!result.nameHit || result.score < ASSET_MATCH_MIN_SCORE) continue;
            scored.push({ imageNodeId: candidate.imageNodeId, title: candidate.title, score: result.score, matchedTerms: result.matchedTerms });
        }
        scored.sort((left, right) => right.score - left.score || left.title.localeCompare(right.title) || left.imageNodeId.localeCompare(right.imageNodeId));
        const winner = scored[0];
        const baseReason = { rule: "slot_semantics_keyword" as const, vetoedImageIds: vetoedImages, alternatives: scored.slice(1, 4) };

        if (!winner) {
            const status: AssetSlotMatch["status"] = vetoedImages.length ? "vetoed" : "no_candidate";
            slots.push({
                slotNodeId: slot.slotNodeId,
                slotTemplateNodeId: slot.slotTemplateNodeId,
                slotKey: slot.slotKey,
                slotOrder: slot.slotOrder,
                slotName: slot.name,
                slotType: slot.type,
                status,
                reason: {
                    ...baseReason,
                    source: `模板槽③·${slot.type || "未分类"}·${slot.name || slot.slotNodeId}（第 ${slot.slotOrder + 1} 号槽）`,
                    matchedTerms: [],
                    score: 0,
                    note: status === "vetoed" ? `候选全被否决（${vetoedImages.join(",")}）：留空交人` : "资产库里没有匹配这张槽语义的图：留空交人（不硬塞）",
                },
            });
            unmatched.push({ slotNodeId: slot.slotNodeId, slotKey: slot.slotKey, reason: status === "vetoed" ? "候选全被否决" : "候选不足" });
            if (status === "vetoed") for (const imageId of vetoedImages) vetoed.push({ slotNodeId: slot.slotNodeId, slotKey: slot.slotKey, rejectedImageId: imageId });
            warnings.push(`槽 ${slot.name || slot.slotNodeId} 未自动挂图（${status === "vetoed" ? "候选全被否决" : "候选不足"}）：留空交人`);
            continue;
        }

        const candidate = input.candidates.find((item) => item.imageNodeId === winner.imageNodeId)!;
        used.add(winner.imageNodeId);
        slots.push({
            slotNodeId: slot.slotNodeId,
            slotTemplateNodeId: slot.slotTemplateNodeId,
            slotKey: slot.slotKey,
            slotOrder: slot.slotOrder,
            slotName: slot.name,
            slotType: slot.type,
            status: "matched",
            imageNodeId: winner.imageNodeId,
            reason: {
                ...baseReason,
                source: `模板槽③·${slot.type || "未分类"}·${slot.name || slot.slotNodeId}（第 ${slot.slotOrder + 1} 号槽）`,
                matchedTerms: winner.matchedTerms,
                score: winner.score,
                candidateTitle: winner.title,
                note: `按槽语义命中「${winner.matchedTerms.join("/")}」（分 ${winner.score}）→ 选「${winner.title}」${scored.length > 1 ? `，另有 ${scored.length - 1} 个候选分数更低` : ""}`,
            },
        });
        attaches.push({ slotNodeId: slot.slotTemplateNodeId, image: candidate.node, caption: truncateAssetCaption(candidate.caption) });
    }

    // 显式传入的挂图若指向**不存在于本实例**的槽，如实报出来（不静默丢）
    for (const key of pinnedBySlot.keys()) {
        if (slotByAnyId(bySlotNode, key)) continue;
        warnings.push(`人指定挂图指向的资产位 ${key} 不在本实例槽表里：已忽略（防跨实例乱配）`);
    }

    const digest = stableDigest(
        slots.map((slot) => [slot.slotKey, slot.status, slot.imageNodeId || "", slot.reason.matchedTerms.join("/"), String(slot.reason.score)].join(":")).join("|"),
    );
    return {
        instanceId: input.instanceId,
        templateId: input.templateId,
        instanceIndex: input.instanceIndex,
        slots,
        attaches,
        unmatched,
        vetoed,
        warnings,
        digest,
    };
}

function slotByAnyId(bySlotNode: Map<string, InstanceAssetSlot>, key: string): boolean {
    if (bySlotNode.has(key)) return true;
    for (const slot of bySlotNode.values()) if (slot.slotTemplateNodeId === key) return true;
    return false;
}

/**
 * 匹配计划 → 落地前置自检（纯函数）：图号/图序与槽序一致、挂图槽都在本实例、否决图零出现。
 * 返回问题清单（空 = 可落地）；编排器/探针据此在**发起请求前**拦下不一致。
 */
export function verifyAssetMatchPlan(input: {
    plan: InstanceAssetMatchPlan;
    vetoes?: WorkflowAssetVetoRecord[];
    /** 当前画布节点（校验挂图槽确实属于本实例）。 */
    nodes?: CanvasNodeData[];
}): string[] {
    const problems: string[] = [];
    const attachOrder = input.plan.attaches.map((attach) => String(attach.slotNodeId || ""));
    const slotOrder = input.plan.slots.map((slot) => slot.slotTemplateNodeId);
    const expected = slotOrder.filter((id) => attachOrder.includes(id));
    if (JSON.stringify(attachOrder) !== JSON.stringify(expected)) problems.push(`挂图材料顺序 ${attachOrder.join(",")} 与槽序 ${expected.join(",")} 不一致`);
    for (const attach of input.plan.attaches) {
        const imageId = String(attach.imageNodeId || attach.image?.id || "");
        const slot = input.plan.slots.find((item) => item.slotTemplateNodeId === attach.slotNodeId);
        if (!slot) {
            problems.push(`挂图落到了本实例槽表外的资产位 ${attach.slotNodeId}（跨实例乱配）`);
            continue;
        }
        if (imageId && findAssetVeto(input.vetoes, { instanceId: input.plan.instanceId, slotNodeId: slot.slotNodeId, imageNodeId: imageId })) {
            problems.push(`否决过的图 ${imageId} 又出现在槽 ${slot.slotKey} 的挂图材料里`);
        }
        const vetoedImages = vetoedImagesOfSlot(input.vetoes, { instanceId: input.plan.instanceId, slotNodeId: slot.slotNodeId });
        if (vetoedImages.length && !slot.reason.vetoedImageIds.includes(vetoedImages[0])) {
            problems.push(`槽 ${slot.slotKey} 的匹配理由没有带否决痕迹（否决不复用不可查）`);
        }
    }
    if (input.nodes) {
        for (const slot of input.plan.slots) {
            const node = input.nodes.find((item) => item.id === slot.slotNodeId);
            if (!node) problems.push(`槽 ${slot.slotKey} 的资产位节点 ${slot.slotNodeId} 不在画布上`);
        }
    }
    return problems;
}

/**
 * 请求参考图序（图号序）复核：清单行 token 序 == 挂图槽序 == 请求 images 序。
 * 复用 `lapian-assets` 的清单行/引用收集（红线 25：不新写图号分配器）。
 */
export function verifyAssetRequestOrder(input: { plan: InstanceAssetMatchPlan; lapianNodeId: string; nodes: CanvasNodeData[]; manifestPrefix: string }): { ok: boolean; slotOrder: string[]; requestOrder: string[]; manifest: string; problems: string[] } {
    const references = collectLapianAssetReferences(input.lapianNodeId, input.nodes, "未命名参考图");
    const requestOrder = references.map((reference) => reference.imageId);
    const manifest = buildLapianAssetManifest(references, input.manifestPrefix);
    const slotOrder = input.plan.slots.filter((slot) => slot.status === "matched" && slot.imageNodeId).map((slot) => (input.nodes.find((node) => node.id === slot.slotNodeId)?.metadata?.attachedImageId as string) || "").filter(Boolean);
    const problems: string[] = [];
    if (slotOrder.length !== requestOrder.length) problems.push(`槽上已挂 ${slotOrder.length} 张、请求参考图 ${requestOrder.length} 张`);
    for (let index = 0; index < Math.min(slotOrder.length, requestOrder.length); index += 1) {
        if (slotOrder[index] !== requestOrder[index]) problems.push(`第 ${index + 1} 号位：槽序=${slotOrder[index]} 请求=${requestOrder[index]}`);
    }
    return { ok: problems.length === 0, slotOrder, requestOrder, manifest, problems };
}
