/**
 * 工作流模板 P2 单测（node:test + node:assert，零依赖；经 `scripts/probe-p2-template.mjs` 打包后 `node --test` 真跑）。
 *
 * 覆盖：
 *  A. 角色判定（P0a §1.1 八类骨架 + 派生角色）
 *  B. 运行态剥离（P0a §3.1 逐角色白名单；通用 status/errorDetails）
 *  C. 子图抽取 + 引用闭包 + 图外引用报告 + 五槽参数声明 + 冻结项
 *  D. id 重写器：七类引用字段逐一命中（含 @[node:] token 逐 token 替换、镜像双写、连线端点、groupId）
 *  E. v3 导出：缺字节显式报告（注入 reader）、信封版本、声明随项目走
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { CanvasConnection, CanvasNodeData } from "@/types/canvas";

import { buildCanvasTemplateExport } from "@/lib/canvas/canvas-export-builder";
import {
    classifyTemplateRole,
    composerTokenIds,
    extractWorkflowTemplate,
    stableDigest,
    stripTemplateRuntime,
    TEMPLATE_INTENT_PLACEHOLDER,
    validateTemplateProject,
} from "@/lib/canvas/workflow-template";
import { buildTemplateIdMap, instanceIdSetsDisjoint, rewriteComposerTokens, rewriteTemplateIds, templateInstanceNodeId } from "@/lib/canvas/workflow-template-ids";

const IMG1 = "img-1";
const IMG2 = "img-2";
const SLOT = "slot-1";
const GRAFT = "graft-1";
const OUT = "out-video";
const GROUP = "group-1";

/** 夹具：一条与真实 pop-3 链同构的最小链（七类引用全部出现）。 */
function fixtureNodes(): CanvasNodeData[] {
    const node = (id: string, type: string, title: string, metadata: Record<string, unknown>, x = 0): CanvasNodeData => ({
        id,
        type,
        title,
        position: { x, y: 0 },
        width: 300,
        height: 200,
        metadata: metadata as CanvasNodeData["metadata"],
    });
    return [
        node("video-src", "video", "源视频.mp4", { content: "blob:http://localhost:3000/aaa", storageKey: "video:aaa", bytes: 1000, mimeType: "video/mp4", durationMs: 15000, status: "success" }),
        node("lap-1", "lapian", "拉片 · pop-3", { content: "# 拉片正文全文", taskId: "pop-3", jobId: "job-9", intent: "旧意图", lapPhase: "done", cameraCoverage: { covered: 1, total: 2, missing: ["推"] }, lapianModel: "c::m", lapianOptional: "A,C", status: "success" }),
        node("ins-1", "text", "转换指令 · 单段流式", { content: "指令正文逐字", prompt: "指令正文逐字", promptTemplate: "seedance-flow", fontSize: 14, status: "success", errorDetails: "旧错误" }),
        node("it-1", "text", "改造意图（可留空）", { content: "把我的人物换成客户模特", prompt: TEMPLATE_INTENT_PLACEHOLDER, lapianIntent: true, status: "success" }),
        node("cfg-1", "config", "提示词生成配置", {
            generationMode: "text",
            model: "default::deepseek-v4-flash-0731",
            count: 1,
            lapianSourceId: "lap-1",
            composerContent: "拉片正文：@[node:lap-1]\n任务说明：@[node:ins-1]\n用户意图：@[node:it-1]\n\n本次参考图：@[node:img-1]=人物、@[node:img-2]=服装B",
            status: "success",
        }),
        node("fin-1", "text", "终稿", { content: "旧终稿正文", prompt: "旧终稿提示词", model: "default::deepseek", status: "success", textCount: 1, primaryTextId: "txt-a", texts: [{ id: "txt-a", status: "success", content: "旧终稿正文" }] }),
        node("cfgv-1", "config", "视频生成配置", {
            generationMode: "video",
            model: "k::sd2.5-720p-ap",
            size: "720x1280",
            seconds: "15",
            vquality: "720",
            videoMode: "reference",
            composerContent: "参考图：@[node:img-1]、@[node:img-2]\n提示词：@[node:fin-1]",
            status: "error",
            errorDetails: "旧失败",
            videoTaskId: "task-1",
            videoProgress: 40,
            references: ["image:aaa"],
            images: [{ id: "img-x", status: "success", content: "", naturalWidth: 1, naturalHeight: 1, bytes: 1, mimeType: "image/png" }],
            primaryImageId: "img-x",
        }),
        node(IMG1, "image", "1.png", { content: "blob:http://localhost:3000/bbb", storageKey: "image:bbb", bytes: 2024, mimeType: "image/png", naturalWidth: 941, naturalHeight: 1672, status: "success" }),
        node(IMG2, "image", "3.png", { content: "blob:http://localhost:3000/ccc", storageKey: "image:ccc", bytes: 3033, mimeType: "image/png", naturalWidth: 800, naturalHeight: 800, status: "success" }),
        node(SLOT, "image", "资产位·人物/动物·人物", {
            content: "/api/asset-frame/pop-3?name=x",
            assetSlotSourceId: "lap-1",
            assetSlotOrder: 0,
            assetSlotName: "人物",
            attachedImageId: IMG1,
            attachedImageDesc: "人物",
            assetSlot: { desc: "原片对应物", span: "全片", frameUrl: "/api/asset-frame/pop-3?name=x", frameAt: 14.5, attachedImageId: IMG1, caption: "人物" },
            status: "success",
        }),
        node(GRAFT, "image", "挂图·人物", { content: "blob:http://localhost:3000/bbb", storageKey: "image:bbb", bytes: 2024, mimeType: "image/png", assetGraftForSlotId: SLOT, assetGraftOfImageId: IMG1, status: "success" }),
        node(OUT, "video", "生成结果", { content: "blob:http://localhost:3000/ddd", storageKey: "video:ddd", bytes: 999, mimeType: "video/mp4", durationMs: 15072, prompt: "旧提示词", videoTaskId: "task-2", videoTaskProvider: "sdgf", references: ["image:bbb"], status: "success" }),
        node(GROUP, "group", "资产组", { status: "success" }, 100),
        { ...node("ins-in-group", "text", "组内指令", { content: "组内", promptTemplate: "seedance-flow", groupId: GROUP, status: "success" }), metadata: { content: "组内", promptTemplate: "seedance-flow", groupId: GROUP, status: "success" } },
    ];
}

function fixtureConnections(): CanvasConnection[] {
    return [
        { id: "e1", fromNodeId: "video-src", toNodeId: "lap-1" },
        { id: "e2", fromNodeId: "lap-1", toNodeId: "cfg-1" },
        { id: "e3", fromNodeId: "ins-1", toNodeId: "cfg-1" },
        { id: "e4", fromNodeId: "it-1", toNodeId: "cfg-1" },
        { id: "e5", fromNodeId: IMG1, toNodeId: "cfg-1" },
        { id: "e6", fromNodeId: IMG2, toNodeId: "cfg-1" },
        { id: "e7", fromNodeId: "cfg-1", toNodeId: "fin-1" },
        { id: "e8", fromNodeId: "fin-1", toNodeId: "cfgv-1" },
        { id: "e9", fromNodeId: IMG1, toNodeId: "cfgv-1" },
        { id: "e10", fromNodeId: IMG2, toNodeId: "cfgv-1" },
        { id: "e11", fromNodeId: "cfgv-1", toNodeId: OUT },
        { id: "e12", fromNodeId: "lap-1", toNodeId: SLOT },
        { id: "e13", fromNodeId: SLOT, toNodeId: GRAFT },
        { id: "e14", fromNodeId: GROUP, toNodeId: "ins-in-group" },
    ];
}

const SKELETON = ["video-src", "lap-1", "ins-1", "it-1", "cfg-1", "fin-1", "cfgv-1", IMG1, IMG2, GROUP, "ins-in-group"];
const FULL = SKELETON.concat([SLOT, GRAFT, OUT]);

function project(nodes: CanvasNodeData[], connections: CanvasConnection[]) {
    return { id: "proj-real-1", title: "无限画布 3", createdAt: "2026-11-13T00:00:00.000Z", updatedAt: "2026-11-13T00:00:00.000Z", nodes, connections, backgroundMode: "lines" as const, viewport: { x: 0, y: 0, k: 1 } };
}

describe("A 角色判定", () => {
    const nodes = fixtureNodes();
    const byId = new Map(nodes.map((item) => [item.id, item]));
    const expectRole = (id: string, role: string) => assert.equal(classifyTemplateRole(byId.get(id)!), role, id);
    it("八类骨架 + 派生角色全部命中", () => {
        expectRole("video-src", "sourceVideo");
        expectRole("lap-1", "lapian");
        expectRole("ins-1", "instruction");
        expectRole("it-1", "intent");
        expectRole("fin-1", "finalText");
        expectRole("cfg-1", "configText");
        expectRole("cfgv-1", "configVideo");
        expectRole(IMG1, "fixedAsset");
        expectRole(SLOT, "assetSlot");
        expectRole(GRAFT, "assetGraft");
        expectRole(OUT, "generationOutput");
        expectRole(GROUP, "group");
    });
});

describe("B 运行态剥离（P0a §3.1）", () => {
    const nodes = fixtureNodes();
    const byId = new Map(nodes.map((item) => [item.id, item]));
    const strip = (id: string) => {
        const node = byId.get(id)!;
        return stripTemplateRuntime(node, classifyTemplateRole(node));
    };

    it("源视频：字节/媒体字段全剥，节点壳保留", () => {
        const { node, fields } = strip("video-src");
        for (const key of ["content", "storageKey", "bytes", "mimeType", "durationMs", "naturalWidth", "naturalHeight", "status"]) assert.equal(node.metadata?.[key as "content"], undefined, key);
        assert.equal(node.id, "video-src");
        assert.ok(fields.includes("storageKey"));
    });

    it("拉片：taskId/jobId/正文/意图/相位/覆盖度剥离，模型与可选段保留", () => {
        const { node } = strip("lap-1");
        assert.equal(node.metadata?.taskId, undefined);
        assert.equal(node.metadata?.jobId, undefined);
        assert.equal(node.metadata?.content, undefined);
        assert.equal(node.metadata?.intent, undefined);
        assert.equal(node.metadata?.lapPhase, undefined);
        assert.equal(node.metadata?.cameraCoverage, undefined);
        assert.equal(node.metadata?.lapianModel, "c::m");
        assert.equal(node.metadata?.lapianOptional, "A,C");
    });

    it("指令：只剥生成态，指令正文与模板 id 保留", () => {
        const { node } = strip("ins-1");
        assert.equal(node.metadata?.status, undefined);
        assert.equal(node.metadata?.errorDetails, undefined);
        assert.equal(node.metadata?.content, "指令正文逐字");
        assert.equal(node.metadata?.promptTemplate, "seedance-flow");
        assert.equal(node.metadata?.fontSize, 14);
    });

    it("意图：正文重置为占位符（槽②），标记保留", () => {
        const { node } = strip("it-1");
        assert.equal(node.metadata?.content, TEMPLATE_INTENT_PLACEHOLDER);
        assert.equal(node.metadata?.prompt, TEMPLATE_INTENT_PLACEHOLDER);
        assert.equal(node.metadata?.lapianIntent, true);
    });

    it("终稿：正文/提示词/生成产物内部引用全剥", () => {
        const { node } = strip("fin-1");
        for (const key of ["content", "prompt", "status", "texts", "primaryTextId"]) assert.equal(node.metadata?.[key as "content"], undefined, key);
    });

    it("Config(text)：清单行删除 + 生成态剥离，骨架/模型/拉片来源保留", () => {
        const { node } = strip("cfg-1");
        const composer = String(node.metadata?.composerContent || "");
        assert.ok(!composer.includes("本次参考图："), composer);
        assert.ok(composer.includes("@[node:lap-1]") && composer.includes("@[node:ins-1]") && composer.includes("@[node:it-1]"), composer);
        assert.equal(node.metadata?.status, undefined);
        assert.equal(node.metadata?.model, "default::deepseek-v4-flash-0731");
        assert.equal(node.metadata?.count, 1);
        assert.equal(node.metadata?.lapianSourceId, "lap-1");
        assert.equal(node.metadata?.generationMode, "text");
    });

    it("Config(video)：运行态全剥，生成参数与 videoMode 保留", () => {
        const { node } = strip("cfgv-1");
        for (const key of ["status", "errorDetails", "videoTaskId", "videoProgress", "references", "images", "primaryImageId"]) assert.equal(node.metadata?.[key as "content"], undefined, key);
        assert.equal(node.metadata?.model, "k::sd2.5-720p-ap");
        assert.equal(node.metadata?.size, "720x1280");
        assert.equal(node.metadata?.seconds, "15");
        assert.equal(node.metadata?.vquality, "720");
        assert.equal(node.metadata?.videoMode, "reference");
        assert.equal(node.metadata?.generationMode, "video");
    });

    it("固定素材：字节保留，生成态剥离", () => {
        const { node } = strip(IMG1);
        assert.equal(node.metadata?.storageKey, "image:bbb");
        assert.equal(node.metadata?.bytes, 2024);
        assert.equal(node.metadata?.content, "blob:http://localhost:3000/bbb");
        assert.equal(node.metadata?.status, undefined);
    });

    it("资产位（显式纳入时）：截帧地址剥离，槽语义与挂图真源保留", () => {
        const { node } = strip(SLOT);
        assert.equal(node.metadata?.assetSlot?.frameUrl, undefined);
        assert.equal(node.metadata?.assetSlot?.desc, "原片对应物");
        assert.equal(node.metadata?.attachedImageId, IMG1);
        assert.equal(node.metadata?.assetSlot?.attachedImageId, IMG1);
    });

    it("生成产物（显式纳入时）：全部运行态剥空", () => {
        const { node } = strip(OUT);
        for (const key of ["content", "storageKey", "bytes", "mimeType", "durationMs", "prompt", "videoTaskId", "references", "status"]) assert.equal(node.metadata?.[key as "content"], undefined, key);
    });

    it("入参不被就地改写（纯函数）", () => {
        const source = fixtureNodes().find((item) => item.id === "lap-1")!;
        stripTemplateRuntime(source, "lapian");
        assert.equal(source.metadata?.taskId, "pop-3");
    });
});

describe("C 子图抽取 + 引用闭包 + 参数声明 + 冻结项", () => {
    const nodes = fixtureNodes();
    const connections = fixtureConnections();
    const skeleton = extractWorkflowTemplate({ sourceProject: project(nodes, connections), selectedNodeIds: SKELETON, name: "pop-3 模板", templateId: "demo1" });
    const full = extractWorkflowTemplate({ sourceProject: project(nodes, connections), selectedNodeIds: FULL, name: "pop-3 全量", templateId: "demo2" });

    it("骨架模板只含边界节点（不静默拉全项目）", () => {
        assert.deepEqual(
            skeleton.project.nodes.map((item) => item.id).sort(),
            [...SKELETON].sort(),
        );
        assert.ok(!skeleton.project.nodes.some((item) => item.id === SLOT || item.id === GRAFT || item.id === OUT));
    });

    it("模板项目 id/标题/声明落点（CanvasProject 显式扩展字段）", () => {
        assert.equal(skeleton.project.id, "tpl-demo1");
        assert.equal(skeleton.project.workflowTemplate?.templateId, "demo1");
        assert.equal(skeleton.project.workflowTemplate?.schemaVersion, 1);
        assert.deepEqual(skeleton.project.workflowTemplate?.sourceNodeIds, SKELETON);
    });

    it("图外引用逐条报告（不静默：连线/挂图/产物）", () => {
        const refs = skeleton.report.externalReferences;
        const kinds = new Set(refs.map((item) => item.kind));
        assert.ok(kinds.has("connection"), JSON.stringify(refs));
        const targets = refs.map((item) => item.targetId);
        assert.ok(targets.includes(SLOT) && targets.includes(OUT), JSON.stringify(targets));
        // 图内引用不得被误报
        assert.ok(!targets.includes("cfg-1") && !targets.includes(IMG1));
        // 跨边界边数 = 报告条数（一进一出各一条：lap-1→slot-1、cfgv-1→out-video）
        const crossing = connections.filter((edge) => SKELETON.includes(edge.fromNodeId) !== SKELETON.includes(edge.toNodeId));
        assert.equal(refs.filter((item) => item.kind === "connection").length, crossing.length);
        // 每条都要能定位到来源节点与字段
        assert.ok(refs.every((item) => Boolean(item.fromNodeId) && Boolean(item.field) && Boolean(item.reason)));
    });

    it("五槽参数声明齐备且逐槽可定位", () => {
        const slots = skeleton.template.slots;
        assert.equal(slots.length, 5);
        assert.deepEqual(slots.map((slot) => slot.kind), ["sourceVideo", "intentText", "assetAttach", "generationParams", "optionalSegments"]);
        assert.deepEqual(slots[0].targets, [{ nodeId: "video-src", fields: ["content", "storageKey", "mimeType", "bytes", "durationMs", "naturalWidth", "naturalHeight"] }]);
        assert.deepEqual(slots[1].targets, [{ nodeId: "it-1", fields: ["content", "prompt"] }]);
        assert.equal(slots[2].derived, true);
        assert.deepEqual(slots[2].targets, []);
        assert.deepEqual(slots[3].targets.map((target) => target.nodeId), ["cfg-1", "cfgv-1"]);
        assert.deepEqual(slots[3].targets[0].fields, ["model", "count", "reasoningEffort"]);
        assert.deepEqual(slots[3].targets[1].fields, ["model", "size", "seconds", "vquality"]);
        assert.deepEqual(slots[4].targets, [{ nodeId: "lap-1", fields: ["lapianOptional"] }]);
    });

    it("五槽在显式纳入资产位后可定位到槽③目标", () => {
        assert.deepEqual(full.template.slots[2].targets.map((target) => target.nodeId), [SLOT]);
        assert.deepEqual(full.template.slots[2].targets[0].fields, ["attachedImageId", "assetSlot.attachedImageId", "attachedImageDesc", "assetSlot.caption"]);
        assert.equal(full.template.slots[2].derived, false);
    });

    it("冻结项：指令模板 id / generationMode / composer 骨架 / 拓扑", () => {
        const frozen = skeleton.template.frozen;
        assert.deepEqual(frozen.instructions.map((item) => [item.nodeId, item.promptTemplate]), [
            ["ins-1", "seedance-flow"],
            ["ins-in-group", "seedance-flow"],
        ]);
        assert.deepEqual(frozen.generationModes, [
            { nodeId: "cfg-1", mode: "text" },
            { nodeId: "cfgv-1", mode: "video" },
        ]);
        assert.equal(frozen.composerSkeletons.length, 2);
        assert.equal(frozen.composerSkeletons[0].digest, stableDigest(String(skeleton.project.nodes.find((item) => item.id === "cfg-1")?.metadata?.composerContent || "")));
        assert.equal(frozen.topology.edgeCount, skeleton.project.connections.length);
        assert.equal(frozen.topology.digest.length, 8);
    });

    it("模板自足性校验通过（token/metadata 引用全在模板内、无运行态残留）", () => {
        assert.deepEqual(validateTemplateProject(full.project.nodes, full.project.connections), []);
    });

    it("剥离报告逐节点记字段（不复制图 DSL）", () => {
        const record = skeleton.template.stripped.find((item) => item.nodeId === "lap-1");
        assert.deepEqual([...(record?.fields || [])].sort(), ["cameraCoverage", "content", "intent", "jobId", "lapPhase", "status", "taskId"]);
        assert.equal(JSON.stringify(skeleton.template).includes('"nodes"'), false);
    });

    it("grep 级：模板 JSON 无 taskId/jobId/frameUrl/自检残留", () => {
        const json = JSON.stringify(skeleton.project.nodes);
        for (const token of ['"taskId"', '"jobId"', '"frameUrl"', '"cameraCoverage"', '"status"', '"errorDetails"', '"texts"', '"primaryTextId"', '"videoTaskId"', '"references"']) assert.equal(json.includes(token), false, token);
    });
});

describe("D id 重写器（七类引用逐一命中）", () => {
    const nodes = fixtureNodes();
    const connections = fixtureConnections();
    const full = extractWorkflowTemplate({ sourceProject: project(nodes, connections), selectedNodeIds: FULL, name: "pop-3 全量", templateId: "demo2" });
    const templateNodes = full.project.nodes;
    const templateConnections = full.project.connections;
    const result = rewriteTemplateIds({ nodes: templateNodes, connections: templateConnections, templateId: "demo2", instanceIndex: 1 });
    const newId = (oldId: string) => templateInstanceNodeId("demo2", 1, oldId);
    const node = (oldId: string) => result.nodes.find((item) => item.id === newId(oldId))!;

    it("节点 id 确定性推导 t<模板短id>-<实例序>-<旧id>", () => {
        assert.equal(result.nodes.length, templateNodes.length);
        for (const item of templateNodes) assert.equal(newId(item.id), `tdemo2-1-${item.id}`);
        assert.equal(node("lap-1").id, "tdemo2-1-lap-1");
    });

    it("#1 composerContent token 逐个替换（清单行剥离后骨架 token 全换）", () => {
        const composer = String(node("cfg-1").metadata?.composerContent || "");
        assert.ok(composer.includes(`@[node:${newId("lap-1")}]`), composer);
        assert.ok(composer.includes(`@[node:${newId("ins-1")}]`), composer);
        assert.ok(composer.includes(`@[node:${newId("it-1")}]`), composer);
        // 清单行已按 M3 剥离（槽③实例期重算），因此 cfg-1 只剩 3 个骨架 token
        assert.equal(composer.match(/@\[node:/g)?.length, 3, composer);
        assert.ok(!composer.includes("@[node:img-1]"), composer);
        const videoComposer = String(node("cfgv-1").metadata?.composerContent || "");
        // 视频配置的「参考图：」不是清单行（前缀不同）：夹具有 2 图 + 1 终稿 = 3 个 token 全部换新
        assert.equal(videoComposer.match(/@\[node:/g)?.length, 3, videoComposer);
        assert.ok(videoComposer.includes(`@[node:${newId(IMG1)}]`) && videoComposer.includes(`@[node:${newId("fin-1")}]`), videoComposer);
        assert.equal(result.replacedTokenCount, 6);
    });

    it("#2 attachedImageId 扁平真源改写", () => {
        assert.equal(node(SLOT).metadata?.attachedImageId, newId(IMG1));
    });

    it("#3 assetSlot.attachedImageId 镜像同批改写（与扁平一致）", () => {
        assert.equal(node(SLOT).metadata?.assetSlot?.attachedImageId, newId(IMG1));
        assert.equal(node(SLOT).metadata?.attachedImageId, node(SLOT).metadata?.assetSlot?.attachedImageId);
        assert.equal(node(SLOT).metadata?.assetSlot?.desc, "原片对应物");
        assert.equal(node(SLOT).metadata?.assetSlot?.caption, "人物");
    });

    it("#4 assetSlotSourceId 改写", () => {
        assert.equal(node(SLOT).metadata?.assetSlotSourceId, newId("lap-1"));
    });

    it("#5 lapianSourceId 改写", () => {
        assert.equal(node("cfg-1").metadata?.lapianSourceId, newId("lap-1"));
        assert.equal(node("cfgv-1").metadata?.lapianSourceId, undefined);
    });

    it("#6 assetGraftOfImageId / assetGraftForSlotId 改写", () => {
        assert.equal(node(GRAFT).metadata?.assetGraftForSlotId, newId(SLOT));
        assert.equal(node(GRAFT).metadata?.assetGraftOfImageId, newId(IMG1));
    });

    it("#7 连线端点与 groupId 改写（连线 id 也确定性重绑定）", () => {
        assert.equal(result.connections.length, templateConnections.length);
        const edge = result.connections.find((item) => item.id === "cdemo2-1-e2")!;
        assert.equal(edge.fromNodeId, newId("lap-1"));
        assert.equal(edge.toNodeId, newId("cfg-1"));
        assert.equal(node("ins-in-group").metadata?.groupId, newId(GROUP));
        assert.equal(result.unresolved.length, 0, JSON.stringify(result.unresolved));
    });

    it("拓扑同构：实例边按旧 id 回算 = 模板冻结拓扑（F1）", () => {
        assert.equal(result.topologyDigest, full.template.frozen.topology.digest);
    });

    it("重写后模板内引用零悬空（含扁平/镜像一致性）", () => {
        assert.deepEqual(validateTemplateProject(result.nodes, result.connections), []);
    });

    it("同模板同实例序重复重写幂等；不同实例 id 集合零交集", () => {
        const again = rewriteTemplateIds({ nodes: templateNodes, connections: templateConnections, templateId: "demo2", instanceIndex: 1 });
        assert.deepEqual(
            again.nodes.map((item) => item.id),
            result.nodes.map((item) => item.id),
        );
        const other = rewriteTemplateIds({ nodes: templateNodes, connections: templateConnections, templateId: "demo2", instanceIndex: 2 });
        assert.ok(instanceIdSetsDisjoint(again.nodes.map((item) => item.id), other.nodes.map((item) => item.id)));
    });

    it("未命中映射表的 token 原样保留并报 unresolved（图外引用拒绝依据）", () => {
        const map = buildTemplateIdMap(["a"], "demo2", 1);
        const rewritten = rewriteComposerTokens("参考：@[node:a]、@[node:outside]", map);
        assert.equal(rewritten.text, "参考：@[node:tdemo2-1-a]、@[node:outside]");
        assert.deepEqual(rewritten.unresolved, ["outside"]);
        assert.deepEqual(composerTokenIds(rewritten.text), ["tdemo2-1-a", "outside"]);
    });
});

describe("E v3 导出（缺字节显式报告）", () => {
    const nodes = fixtureNodes();
    const connections = fixtureConnections();
    const full = extractWorkflowTemplate({ sourceProject: project(nodes, connections), selectedNodeIds: FULL, name: "pop-3 全量", templateId: "demo2" });

    it("模板导出复用 v3 信封，素材齐备时 missing 为空", async () => {
        const build = await buildCanvasTemplateExport(full.project, async (storageKey) => new Blob([`bytes-of-${storageKey}`], { type: storageKey.startsWith("video:") ? "video/mp4" : "image/png" }));
        assert.equal(build.file.app, "infinite-canvas");
        assert.equal(build.file.version, 3);
        assert.equal(build.file.projects.length, 1);
        assert.deepEqual(build.missing, []);
        // 固定素材 2 张（img-1/img-2 同一 storageKey 去重后 2 个：image:bbb / image:ccc）；源视频字节已剥 → 不在 files
        assert.deepEqual(build.file.projects[0].files.map((item) => item.storageKey).sort(), ["image:bbb", "image:ccc"]);
        assert.equal(build.zipFiles.length, 2);
    });

    it("缺字节 → 显式报告 storageKey 与归属节点（不静默跳过）", async () => {
        const build = await buildCanvasTemplateExport(full.project, async (storageKey) => (storageKey === "image:ccc" ? null : new Blob(["x"], { type: "image/png" })));
        assert.equal(build.missing.length, 1);
        assert.equal(build.missing[0].storageKey, "image:ccc");
        assert.deepEqual(build.missing[0].nodeIds.sort(), [IMG2].sort());
        assert.ok(build.missing[0].reason.includes("取不到"));
        assert.equal(build.file.projects[0].files.length, 1);
    });

    it("非模板项目拒绝导出（声明缺失即非模板）", async () => {
        await assert.rejects(() => buildCanvasTemplateExport({ ...full.project, workflowTemplate: undefined }, async () => new Blob(["x"])));
    });

    it("声明随项目 JSON 往返不丢（C1 单一事实源）", () => {
        const json = JSON.stringify({ app: "infinite-canvas", version: 3, exportedAt: "x", projects: [{ project: full.project, files: [] }] });
        const parsed = JSON.parse(json);
        assert.equal(parsed.projects[0].project.workflowTemplate.templateId, "demo2");
        assert.equal(parsed.projects[0].project.workflowTemplate.slots.length, 5);
        assert.deepEqual(parsed.projects[0].project.workflowTemplate.frozen.topology.nodeIds, FULL);
    });
});
