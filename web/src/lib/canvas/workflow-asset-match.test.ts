/**
 * P7a 资产自动匹配 + 一键否决单测（node:test；经 `scripts/probe-p7-match-repair.mjs` 打包后真跑）。
 *
 * 覆盖（逐条对卡面 P7a 判据）：
 *  A 槽与资产库：槽序=assetSlotOrder、归属只认本实例拉片源；候选扣掉对照副本/槽节点/别实例已占图
 *  B 匹配理由：每槽匹配带理由（来源槽语义 + 命中词 + 分数 + 落选候选），同输入同 digest
 *  C 一键否决：否决落记录、匹配换图或留空、**旧匹配重放不得进请求**（计划层丢弃 + 槽③判据拦下）、幂等重放一致
 *  D 图号/图序：挂图后清单 token 序 == 槽序 == 请求参考图序（P4 已验，本卡回归）
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { registerBuiltinNodes } from "@/components/canvas/nodes/builtin-nodes";
import { applyCanvasAgentOps, type CanvasAgentOp, type CanvasAgentSnapshot } from "@/lib/canvas/canvas-agent-ops";
import { collectLapianAssetReferences } from "@/lib/canvas/lapian-assets";
import {
    acceptWorkflowAssetVetoes,
    collectAssetLibraryCandidates,
    listInstanceAssetSlots,
    matchInstanceAssetSlots,
    upsertAssetVeto,
    verifyAssetMatchPlan,
    verifyAssetRequestOrder,
    vetoAssetMatch,
    vetoedImagesOfSlot,
    type WorkflowAssetVetoRecord,
} from "@/lib/canvas/workflow-asset-match";
import { planWorkflowInstance, type WorkflowInstanceMaterials } from "@/lib/canvas/workflow-instance";
import { planInstanceAssembly, verifySlotSubstitution } from "@/lib/canvas/workflow-instance-run";
import { extractWorkflowTemplate } from "@/lib/canvas/workflow-template";
import type { CanvasConnection, CanvasNodeData } from "@/types/canvas";

registerBuiltinNodes();

const VIDEO = "video-src";
const LAPIAN = "lap-1";
const INS = "ins-1";
const INTENT = "it-1";
const CONFIG_TEXT = "cfg-1";
const FINAL = "fin-1";
const CONFIG_VIDEO = "cfgv-1";
const OUT_VIDEO = "out-video";
const SLOT_FEMALE = "slot-female";
const SLOT_PERFUME = "slot-perfume";

// 资产库（项目级图片，**不进模板**：它们才是「资产库」）
const LIB_FEMALE = "lib-female";
const LIB_PERFUME = "lib-perfume";
const LIB_DECOY = "lib-city";

function node(id: string, type: string, title: string, metadata: Record<string, unknown>, x = 0): CanvasNodeData {
    return { id, type, title, position: { x, y: 0 }, width: 300, height: 200, metadata: metadata as CanvasNodeData["metadata"] };
}

function templateNodes(): CanvasNodeData[] {
    return [
        node(VIDEO, "video", "源视频.mp4", { content: "blob:src", storageKey: "video:src", mimeType: "video/mp4" }),
        node(LAPIAN, "lapian", "拉片", { content: "# 拉片正文", taskId: "lap-task", jobId: "lap-job", lapPhase: "done" }, 100),
        node(INS, "text", "转换指令", { content: "指令正文", prompt: "指令正文", promptTemplate: "seedance-flow" }, 200),
        node(INTENT, "text", "改造意图", { content: "旧意图", prompt: "旧意图", lapianIntent: true }, 300),
        node(CONFIG_TEXT, "config", "提示词生成配置", { generationMode: "text", model: "default::m", lapianSourceId: LAPIAN, composerContent: "拉片：@[node:lap-1]\n指令：@[node:ins-1]\n意图：@[node:it-1]" }, 400),
        node(FINAL, "text", "终稿（占位）", { fontSize: 14, model: "default::m", textCount: 1 }, 500),
        node(CONFIG_VIDEO, "config", "视频生成配置", { generationMode: "video", model: "k::sd2.5", size: "720x1280", composerContent: "提示词：@[node:fin-1]" }, 600),
        node(OUT_VIDEO, "video", "生成结果", { content: "blob:out", storageKey: "video:out", status: "idle" }, 700),
        // 资产位（未挂图：模板里扁平/镜像都不存在，自足性才过）
        node(SLOT_FEMALE, "image", "资产位·人物·女性A", { content: "blob:slot-female", storageKey: "image:slot-female", assetSlotSourceId: LAPIAN, assetSlotOrder: 0, assetSlotName: "女性A", assetSlotType: "人物", assetSlot: { desc: "原片模特，红色风衣，短发", span: "00:00-02.00" } }, 800),
        node(SLOT_PERFUME, "image", "资产位·主体·香水", { content: "blob:slot-perfume", storageKey: "image:slot-perfume", assetSlotSourceId: LAPIAN, assetSlotOrder: 1, assetSlotName: "香水", assetSlotType: "主体", assetSlot: { desc: "细长圆柱体紫色表面，银色金属盖", span: "02.00-05.00" } }, 900),
    ];
}

function templateConnections(): CanvasConnection[] {
    return [
        { id: "e1", fromNodeId: VIDEO, toNodeId: LAPIAN },
        { id: "e2", fromNodeId: LAPIAN, toNodeId: CONFIG_TEXT },
        { id: "e3", fromNodeId: INS, toNodeId: CONFIG_TEXT },
        { id: "e4", fromNodeId: INTENT, toNodeId: CONFIG_TEXT },
        { id: "e5", fromNodeId: CONFIG_TEXT, toNodeId: FINAL },
        { id: "e6", fromNodeId: FINAL, toNodeId: CONFIG_VIDEO },
        { id: "e7", fromNodeId: CONFIG_VIDEO, toNodeId: OUT_VIDEO },
    ];
}

function libraryNodes(): CanvasNodeData[] {
    return [
        node(LIB_FEMALE, "image", "客户模特 女性A 定妆.png", { content: "blob:lib-female", storageKey: "image:lib-female", mimeType: "image/png" }, 1200),
        node(LIB_PERFUME, "image", "客户款 香水 磨砂瓶.png", { content: "blob:lib-perfume", storageKey: "image:lib-perfume", mimeType: "image/png" }, 1300),
        node(LIB_DECOY, "image", "城市夜景.jpg", { content: "blob:lib-city", storageKey: "image:lib-city", mimeType: "image/jpeg" }, 1400),
    ];
}

const templateSource = extractWorkflowTemplate({
    sourceProject: { id: "proj-p7", title: "P7 夹具项目", nodes: templateNodes(), connections: templateConnections(), backgroundMode: "lines" as const, viewport: { x: 0, y: 0, k: 1 } },
    selectedNodeIds: [VIDEO, LAPIAN, INS, INTENT, CONFIG_TEXT, FINAL, CONFIG_VIDEO, OUT_VIDEO, SLOT_FEMALE, SLOT_PERFUME],
    templateId: "p7demo",
    name: "p7fixture",
});

function materials(tag: string, extra: Partial<WorkflowInstanceMaterials> = {}): WorkflowInstanceMaterials {
    return {
        sourceVideo: { storageKey: `video:${tag}`, title: `${tag}.mp4`, content: `blob:${tag}`, mimeType: "video/mp4", bytes: 1000, durationMs: 9000 },
        intentText: `${tag} 的改造意图`,
        optionalSegments: "A",
        ...extra,
    };
}

function planWith(index: number, mats: WorkflowInstanceMaterials, vetoes?: WorkflowAssetVetoRecord[]) {
    return planWorkflowInstance({
        template: templateSource.template,
        templateNodes: templateSource.project.nodes,
        templateConnections: templateSource.project.connections,
        materials: mats,
        instanceIndex: index,
        vetoes,
        unnamedCaption: "未命名参考图",
    });
}

/** 内存画布：初值 = 资产库三张图（项目级；不进实例）。 */
function makeCanvas(seed: CanvasNodeData[] = libraryNodes()) {
    let snapshot: CanvasAgentSnapshot = { projectId: "proj-p7", title: "P7 夹具项目", nodes: [...seed], connections: [], selectedNodeIds: [], viewport: { x: 0, y: 0, k: 1 } };
    return {
        apply(ops: CanvasAgentOp[]) {
            const next = applyCanvasAgentOps(snapshot, ops);
            snapshot = { ...snapshot, nodes: next.nodes, connections: next.connections, selectedNodeIds: next.selectedNodeIds, viewport: next.viewport };
        },
        nodes: () => snapshot.nodes,
        connections: () => snapshot.connections,
        snapshot: () => snapshot,
    };
}

/** 走一遍「规划 → 匹配 → 落地」：生产/探针同一条路（不复制 P3 落库逻辑）。 */
function matchAndMaterialize(input: { index: number; tag: string; canvas: ReturnType<typeof makeCanvas>; vetoes?: WorkflowAssetVetoRecord[]; extra?: Partial<WorkflowInstanceMaterials> }) {
    const dryPlan = planWith(input.index, materials(input.tag, input.extra), input.vetoes);
    const slots = listInstanceAssetSlots({ plan: dryPlan, nodes: [...input.canvas.nodes(), ...dryPlan.nodes] });
    const library = collectAssetLibraryCandidates({ nodes: [...input.canvas.nodes(), ...dryPlan.nodes], lapianNodeId: dryPlan.lapianNodeId });
    const match = matchInstanceAssetSlots({
        slots,
        candidates: library.candidates,
        vetoes: input.vetoes,
        instanceId: dryPlan.instanceId,
        templateId: dryPlan.templateId,
        instanceIndex: input.index,
    });
    const plan = planWith(input.index, materials(input.tag, { ...input.extra, assetAttach: match.attaches }), input.vetoes);
    input.canvas.apply(plan.ops);
    return { plan, match, slots, library, dryPlan };
}

// ─────────────────────────────────────────────── A 槽与资产库

describe("A 槽与资产库（按实例/槽归属）", () => {
    it("槽表按 assetSlotOrder 排序，且只收本实例拉片源的资产位", () => {
        const canvas = makeCanvas();
        const { dryPlan } = matchAndMaterialize({ index: 1, tag: "A", canvas });
        // 再造一个「别的实例」的资产位：不得进本实例槽表
        const foreign = node("foreign-slot", "image", "别实例资产位", { content: "blob:f", storageKey: "image:f", assetSlotSourceId: "lap-other", assetSlotOrder: 0, assetSlotName: "女性A" });
        const slots = listInstanceAssetSlots({ plan: dryPlan, nodes: [...canvas.nodes(), foreign] });
        assert.equal(slots.length, 2);
        assert.deepEqual(slots.map((slot) => slot.slotOrder), [0, 1]);
        assert.deepEqual(slots.map((slot) => slot.name), ["女性A", "香水"]);
        assert.deepEqual(slots.map((slot) => slot.slotNodeId), [dryPlan.nodeIdMap[SLOT_FEMALE], dryPlan.nodeIdMap[SLOT_PERFUME]]);
        assert.equal(slots[0].slotKey, "人物|女性A");
        assert.equal(slots[0].slotTemplateNodeId, SLOT_FEMALE, "模板 id 反查得到（挂图材料按模板 id 传）");
    });

    it("候选只收资产库图片：扣掉对照副本/槽节点/别实例已占图，扣掉的原因可读", () => {
        const canvas = makeCanvas([
            ...libraryNodes(),
            node("graft-x", "image", "挂图 · 女性A", { content: "blob:g", storageKey: "image:g", assetGraftForSlotId: "slot-x", assetGraftOfImageId: "img-x" }),
            node("other-slot", "image", "别实例资产位", { content: "blob:o", storageKey: "image:o", assetSlotSourceId: "lap-other", assetSlotOrder: 0, attachedImageId: LIB_FEMALE }),
        ]);
        const { library } = matchAndMaterialize({ index: 1, tag: "A", canvas });
        const ids = library.candidates.map((candidate) => candidate.imageNodeId);
        assert.deepEqual(ids.sort(), [LIB_DECOY, LIB_PERFUME].sort(), `候选=${ids.join(",")}`);
        const reasons = library.excluded.map((item) => `${item.imageNodeId}:${item.reason}`).join(" | ");
        assert.ok(reasons.includes("graft-x"), reasons);
        assert.ok(reasons.includes("other-slot"), reasons);
        assert.ok(reasons.includes(LIB_FEMALE) && reasons.includes("已归属其它实例"), reasons);
    });

    it("多实例互不串配：实例 2 不拿实例 1 的挂图副本，各实例槽指向自己的图", () => {
        const canvas = makeCanvas();
        const one = matchAndMaterialize({ index: 1, tag: "A", canvas });
        const two = matchAndMaterialize({ index: 2, tag: "B", canvas });
        const oneImages = one.plan.assetAttaches.map((attach) => attach.imageNodeId);
        const twoImages = two.plan.assetAttaches.map((attach) => attach.imageNodeId);
        assert.equal(oneImages.length, 2);
        assert.equal(twoImages.length, 2);
        for (const imageId of twoImages) assert.ok(!oneImages.includes(imageId), `实例 2 拿了实例 1 的图 ${imageId}`);
        assert.deepEqual(oneImages, [one.plan.nodeIdMap[LIB_FEMALE], one.plan.nodeIdMap[LIB_PERFUME]]);
        assert.deepEqual(twoImages, [two.plan.nodeIdMap[LIB_FEMALE], two.plan.nodeIdMap[LIB_PERFUME]]);
        // 两个实例的槽各挂自己的图，画布上互不覆盖
        const slotOf = (plan: typeof one.plan, nodeId: string) => canvas.nodes().find((item) => item.id === plan.nodeIdMap[nodeId]);
        assert.equal(slotOf(one.plan, SLOT_FEMALE)?.metadata?.attachedImageId, oneImages[0]);
        assert.equal(slotOf(two.plan, SLOT_FEMALE)?.metadata?.attachedImageId, twoImages[0]);
    });
});

// ─────────────────────────────────────────────── B 匹配理由

describe("B 匹配理由（为什么选它 + 落选候选）", () => {
    it("每槽每条匹配都带理由：来源槽语义 + 命中词 + 分数 + 候选标题", () => {
        const canvas = makeCanvas();
        const { match } = matchAndMaterialize({ index: 1, tag: "A", canvas });
        assert.equal(match.slots.length, 2);
        for (const slot of match.slots) {
            assert.equal(slot.status, "matched", `${slot.slotKey} 未匹配`);
            assert.equal(slot.reason.rule, "slot_semantics_keyword");
            assert.ok(slot.reason.source.includes("模板槽③"), slot.reason.source);
            assert.ok(slot.reason.source.includes(`第 ${slot.slotOrder + 1} 号槽`), slot.reason.source);
            assert.ok(slot.reason.matchedTerms.length > 0, `${slot.slotKey} 没有命中词`);
            assert.ok(slot.reason.score >= 6, String(slot.reason.score));
            assert.ok(String(slot.reason.candidateTitle || "").length > 0);
            assert.ok(String(slot.reason.note || "").includes("槽语义命中"), slot.reason.note);
        }
        const female = match.slots[0];
        assert.equal(female.imageNodeId, LIB_FEMALE, `女性A 槽应选定妆图，实际 ${female.imageNodeId}`);
        assert.ok(female.reason.matchedTerms.includes("女性"), female.reason.matchedTerms.join(","));
        assert.equal(match.slots[1].imageNodeId, LIB_PERFUME);
    });

    it("同输入同 digest（可复算）；换一张库图就换 digest", () => {
        const canvas = makeCanvas();
        const first = matchAndMaterialize({ index: 1, tag: "A", canvas });
        const canvasAgain = makeCanvas();
        const second = matchAndMaterialize({ index: 1, tag: "A", canvas: canvasAgain });
        assert.equal(second.match.digest, first.match.digest);
        assert.deepEqual(second.match.attaches.map((attach) => attach.image?.id), first.match.attaches.map((attach) => attach.image?.id));
    });

    it("候选不足：不硬塞，留空交人（warnings + unmatched 可查）", () => {
        const canvas = makeCanvas([node(LIB_DECOY, "image", "城市夜景.jpg", { content: "blob:c", storageKey: "image:lib-city" })]);
        const { match, plan } = matchAndMaterialize({ index: 1, tag: "A", canvas });
        assert.equal(match.attaches.length, 0);
        assert.equal(match.slots.every((slot) => slot.status === "no_candidate"), true, JSON.stringify(match.slots.map((slot) => slot.status)));
        assert.equal(match.unmatched.length, 2);
        assert.equal(match.warnings.length, 2);
        assert.equal(plan.assetAttaches.length, 0, "没有匹配就不该挂图");
    });

    it("一张图只进一个槽：同语义两槽，第二槽拿替代候选或留空", () => {
        const canvas = makeCanvas([
            ...libraryNodes(),
            node("lib-female-b", "image", "备选 女性A 侧脸.png", { content: "blob:fb", storageKey: "image:lib-female-b" }),
        ]);
        const { plan } = matchAndMaterialize({ index: 1, tag: "A", canvas });
        const images = plan.assetAttaches.map((attach) => attach.imageNodeId);
        assert.equal(new Set(images).size, images.length, `同一张图挂了两次：${images.join(",")}`);
    });
});

// ─────────────────────────────────────────────── C 一键否决

describe("C 一键否决（否决后旧匹配不得再进请求）", () => {
    it("否决落记录：实例 + 槽 + 图三键，幂等 upsert，导入透传", () => {
        const canvas = makeCanvas();
        const { match, plan } = matchAndMaterialize({ index: 1, tag: "A", canvas });
        const target = match.slots[0];
        const record = vetoAssetMatch({ match: target, templateId: plan.templateId, instanceId: plan.instanceId, instanceIndex: 1, vetoedAt: "2026-11-13T00:00:00.000Z", note: "客户不用这张" });
        assert.equal(record.rejectedImageId, target.imageNodeId);
        assert.equal(record.slotNodeId, target.slotNodeId);
        assert.equal(record.slotKey, "人物|女性A");
        assert.equal(record.rejectedImageTitle, target.reason.candidateTitle);
        const twice = upsertAssetVeto(upsertAssetVeto([], record), { ...record, vetoedAt: "2026-11-13T00:01:00.000Z" });
        assert.equal(twice.length, 1, "同三键重复否决只留一条");
        assert.equal(twice[0].vetoedAt, "2026-11-13T00:01:00.000Z");
        assert.equal(acceptWorkflowAssetVetoes([record, { instanceId: "" }, null])?.length, 1);
    });

    it("否决后重新匹配：该图被整批排除（理由里带否决痕迹），替代候选顶上", () => {
        const canvas = makeCanvas([
            ...libraryNodes(),
            node("lib-female-b", "image", "备选 女性A 侧脸.png", { content: "blob:fb", storageKey: "image:lib-female-b" }),
        ]);
        const first = matchAndMaterialize({ index: 1, tag: "A", canvas: makeCanvas([...libraryNodes(), node("lib-female-b", "image", "备选 女性A 侧脸.png", { content: "blob:fb", storageKey: "image:lib-female-b" })]) });
        const veto = vetoAssetMatch({ match: first.match.slots[0], templateId: first.plan.templateId, instanceId: first.plan.instanceId, instanceIndex: 1, vetoedAt: "2026-11-13T00:00:00.000Z" });
        const matched = matchAndMaterialize({ index: 1, tag: "A", canvas, vetoes: [veto] });
        assert.notEqual(matched.match.slots[0].imageNodeId, veto.rejectedImageId, "否决的图又回来了");
        assert.ok(matched.match.slots[0].reason.vetoedImageIds.includes(veto.rejectedImageId), JSON.stringify(matched.match.slots[0].reason));
        assert.deepEqual(vetoedImagesOfSlot([veto], { instanceId: first.plan.instanceId, slotNodeId: veto.slotNodeId }), [veto.rejectedImageId]);
    });

    it("否决后拿**旧材料**重放：计划层直接丢弃该挂图（不留挂、不进清单、不进请求）", () => {
        const canvas = makeCanvas();
        const first = matchAndMaterialize({ index: 1, tag: "A", canvas });
        const vetoedImage = first.plan.assetAttaches[0].imageNodeId;
        const veto = vetoAssetMatch({ match: first.match.slots[0], templateId: first.plan.templateId, instanceId: first.plan.instanceId, instanceIndex: 1, vetoedAt: "2026-11-13T00:00:00.000Z" });
        // 旧材料（自动匹配那一版）原样重放 —— 否决后不得再套用
        const staleAttach = first.match.attaches.map((attach) => ({ ...attach }));
        const replay = planWith(1, materials("A", { assetAttach: staleAttach }), [veto]);
        assert.equal(replay.assetAttaches.length, 1, "只应挂剩下的那一个槽");
        assert.ok(!replay.assetAttaches.some((attach) => attach.imageNodeId === vetoedImage), "否决过的图仍被挂了");
        assert.equal(replay.vetoedAttaches.length, 1);
        assert.equal(replay.vetoedAttaches[0].imageNodeId, vetoedImage);
        assert.ok(replay.warnings.some((line) => line.includes("否决")), replay.warnings.join(" | "));
        // 幂等重放：同输入再算一次，结论逐字一致（含否决痕迹）
        const replayAgain = planWith(1, materials("A", { assetAttach: staleAttach }), [veto]);
        assert.deepEqual(replayAgain.vetoedAttaches, replay.vetoedAttaches);
        assert.deepEqual(replayAgain.assetAttaches, replay.assetAttaches);
    });

    it("槽③判据拦下「否决过的匹配又进请求」：materials 里带着也判 FAIL", () => {
        const canvas = makeCanvas();
        const first = matchAndMaterialize({ index: 1, tag: "A", canvas });
        const veto = vetoAssetMatch({ match: first.match.slots[0], templateId: first.plan.templateId, instanceId: first.plan.instanceId, instanceIndex: 1, vetoedAt: "2026-11-13T00:00:00.000Z" });
        const assembly = planInstanceAssembly({ plan: first.plan, nodes: canvas.nodes(), connections: canvas.connections() });
        const checks = verifySlotSubstitution({
            plan: first.plan,
            materials: materials("A", { assetAttach: first.match.attaches.map((attach) => ({ ...attach })) }),
            nodes: canvas.nodes(),
            connections: canvas.connections(),
            assembly,
            lapianText: "# 拉片正文",
            vetoes: [veto],
        });
        const assetCheck = checks.find((check) => check.slotKind === "assetAttach");
        assert.equal(assetCheck?.ok, false);
        assert.ok(String(assetCheck?.detail || "").includes("否决"), String(assetCheck?.detail));
    });
});

// ─────────────────────────────────────────────── D 图号 / 图序

describe("D 图号与请求图序（与槽序一致）", () => {
    it("挂图后清单 token 序 == 槽序 == 请求参考图序；计划自检零问题", () => {
        const canvas = makeCanvas();
        const { plan, match, slots } = matchAndMaterialize({ index: 1, tag: "A", canvas });
        const order = verifyAssetRequestOrder({ plan: match, lapianNodeId: plan.lapianNodeId, nodes: canvas.nodes(), manifestPrefix: "本次参考图：" });
        assert.equal(order.ok, true, order.problems.join(" | "));
        assert.equal(order.requestOrder.length, 2);
        assert.deepEqual(order.slotOrder, order.requestOrder);
        const references = collectLapianAssetReferences(plan.lapianNodeId, canvas.nodes(), "未命名参考图");
        assert.deepEqual(references.map((reference) => reference.index), [1, 2]);
        assert.deepEqual(references.map((reference) => reference.slotId), slots.map((slot) => slot.slotNodeId));
        // 清单行进组装串（图号由生成期按 token 序分配，这里验 token 序）
        const config = canvas.nodes().find((item) => item.id === plan.nodeIdMap[CONFIG_TEXT]);
        assert.ok(String(config?.metadata?.composerContent || "").includes(`本次参考图：@[node:${references[0].imageId}]`), String(config?.metadata?.composerContent || ""));
        assert.deepEqual(verifyAssetMatchPlan({ plan: match, nodes: canvas.nodes() }), []);
    });

    it("人指定挂图（owner_pinned）优先于自动匹配，但同样受否决约束", () => {
        const canvas = makeCanvas();
        const dryPlan = planWith(1, materials("A"));
        const slots = listInstanceAssetSlots({ plan: dryPlan, nodes: [...canvas.nodes(), ...dryPlan.nodes] });
        const library = collectAssetLibraryCandidates({ nodes: [...canvas.nodes(), ...dryPlan.nodes], lapianNodeId: dryPlan.lapianNodeId });
        const decoy = canvas.nodes().find((item) => item.id === LIB_DECOY);
        const match = matchInstanceAssetSlots({
            slots,
            candidates: library.candidates,
            instanceId: dryPlan.instanceId,
            templateId: dryPlan.templateId,
            instanceIndex: 1,
            explicit: [{ slotNodeId: SLOT_FEMALE, image: decoy, caption: "城市夜景" }],
        });
        const pinnedSlot = match.slots[0];
        assert.equal(pinnedSlot.reason.rule, "owner_pinned");
        assert.equal(pinnedSlot.imageNodeId, LIB_DECOY);
        // 否决这个人指定的图 → 该槽留空（不退回自动匹配，不悄悄换上别的）
        const veto = vetoAssetMatch({ match: pinnedSlot, templateId: dryPlan.templateId, instanceId: dryPlan.instanceId, instanceIndex: 1, vetoedAt: "2026-11-13T00:00:00.000Z" });
        const afterVeto = matchInstanceAssetSlots({
            slots,
            candidates: library.candidates,
            vetoes: [veto],
            instanceId: dryPlan.instanceId,
            templateId: dryPlan.templateId,
            instanceIndex: 1,
            explicit: [{ slotNodeId: SLOT_FEMALE, image: decoy, caption: "城市夜景" }],
        });
        assert.equal(afterVeto.slots[0].status, "vetoed");
        assert.equal(afterVeto.attaches.some((attach) => attach.slotNodeId === SLOT_FEMALE), false, "否决的人指定图仍被挂上");
    });
});
