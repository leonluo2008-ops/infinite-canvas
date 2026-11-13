/**
 * 工作流实例化器 P3 单测（node:test + node:assert；经 `scripts/probe-p3-instance.mjs` 打包后 `node --test` 真跑）。
 *
 * 覆盖：
 *  A. 实例计划：确定性 id / 跨实例零交集 / 七类引用全重绑定 / 拓扑同构
 *  B. 槽值代入：①②④⑤ 写入白名单 + F2/F3/F4 冻结项 + 模板不可变（深冻结两次 plan 零变化）
 *  C. 槽③挂图：扁平真源 + assetSlot 镜像 + 清单行（collect/build/upsert 同源）+ 对照图 + 伴生边
 *  D. add_node 幂等键：重放不翻倍 / 形态不一致报错 / connect_nodes 既有查重 / 旧 op 行为零回归
 *  E. materialize：受控拉片入口（stub 网络）→ 本次正文回填 / 幂等不重复花钱 / 失败原子性（零写入或 incomplete）
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { NODE_SPECS } from "@/constant/canvas";
import { applyCanvasAgentOps, CanvasAgentOpConflictError, type CanvasAgentOp, type CanvasAgentSnapshot } from "@/lib/canvas/canvas-agent-ops";
import { registerNodeDefinitions } from "@/lib/canvas/node-registry";
import { composerTokenIds, extractWorkflowTemplate, TEMPLATE_INTENT_PLACEHOLDER } from "@/lib/canvas/workflow-template";
import { instanceIdSetsDisjoint } from "@/lib/canvas/workflow-template-ids";
import {
    buildInstanceRollbackOps,
    materializeWorkflowInstance,
    planWorkflowInstance,
    WorkflowInstanceError,
    type PlanWorkflowInstanceInput,
    type WorkflowInstanceMaterials,
    type WorkflowInstancePlan,
} from "@/lib/canvas/workflow-instance";
import type { CanvasConnection, CanvasNodeData } from "@/types/canvas";

// 与页面启动同口径的节点注册（尺寸/默认 metadata 取自 constant/canvas，禁手抄数值）
registerNodeDefinitions(
    Object.entries(NODE_SPECS).map(([type, spec]) => ({
        type,
        title: spec.title,
        defaultSize: { width: spec.width, height: spec.height },
        defaultMetadata: spec.metadata,
    })) as never,
    "builtin",
);

const IMG1 = "img-1";
const IMG2 = "img-2";
const SLOT = "slot-1";
const GRAFT = "graft-1";
const OUT = "out-video";
const GROUP = "group-1";

/** 夹具：与真实 pop-3 链同构的最小链（七类引用全出现；资产位/挂接/产物可显式纳入）。 */
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
        node("lap-1", "lapian", "拉片 · pop-3", { content: "# 旧拉片正文", taskId: "pop-3", jobId: "job-9", intent: "旧意图", lapPhase: "loading", cameraCoverage: { covered: 1, total: 2 }, lapianModel: "c::m", lapianOptional: "A,C", status: "success" }),
        node("ins-1", "text", "转换指令", { content: "指令正文逐字", prompt: "指令正文逐字", promptTemplate: "seedance-flow", fontSize: 14, status: "success" }),
        node("it-1", "text", "改造意图（可留空）", { content: "旧意图正文", prompt: TEMPLATE_INTENT_PLACEHOLDER, lapianIntent: true, status: "success" }),
        node("cfg-1", "config", "提示词生成配置", {
            generationMode: "text",
            model: "default::deepseek-v4-flash-0731",
            count: 1,
            lapianSourceId: "lap-1",
            composerContent: "拉片正文：@[node:lap-1]\n任务说明：@[node:ins-1]\n用户意图：@[node:it-1]\n\n本次参考图：@[node:img-1]=人物、@[node:img-2]=服装B",
            status: "success",
        }),
        node("fin-1", "text", "终稿", { content: "旧终稿正文", prompt: "旧终稿提示词", model: "default::deepseek", status: "success", texts: [{ id: "txt-a", status: "success", content: "旧终稿正文" }], primaryTextId: "txt-a" }),
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
        }),
        node(IMG1, "image", "1.png", { content: "blob:http://localhost:3000/bbb", storageKey: "image:bbb", bytes: 2024, mimeType: "image/png", naturalWidth: 941, naturalHeight: 1672, status: "success" }),
        node(IMG2, "image", "3.png", { content: "blob:http://localhost:3000/ccc", storageKey: "image:ccc", bytes: 3033, mimeType: "image/png", naturalWidth: 800, naturalHeight: 800, status: "success" }),
        node(SLOT, "image", "资产位·人物", {
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
        node(OUT, "video", "生成结果", { content: "blob:http://localhost:3000/ddd", storageKey: "video:ddd", bytes: 999, mimeType: "video/mp4", durationMs: 15072, prompt: "旧提示词", videoTaskId: "task-2", references: ["image:bbb"], status: "success" }),
        node(GROUP, "group", "资产组", { status: "success" }, 100),
        { ...node("ins-in-group", "text", "组内指令", {}), metadata: { content: "组内", promptTemplate: "seedance-flow", groupId: GROUP, status: "success" } },
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
const FULL = SKELETON.concat([SLOT, GRAFT]);

function sourceProject() {
    return { id: "proj-real-1", title: "无限画布 3", createdAt: "2026-11-13T00:00:00.000Z", updatedAt: "2026-11-13T00:00:00.000Z", nodes: fixtureNodes(), connections: fixtureConnections(), backgroundMode: "lines" as const, viewport: { x: 0, y: 0, k: 1 } };
}

function template(selected: string[], templateId: string) {
    return extractWorkflowTemplate({ sourceProject: sourceProject(), selectedNodeIds: selected, name: templateId, templateId });
}

const skeletonTemplate = template(SKELETON, "p3demo");
const fullTemplate = template(FULL, "p3full");

function materialsOf(tag: string): WorkflowInstanceMaterials {
    return {
        sourceVideo: { storageKey: `video:${tag}`, title: `${tag}.mp4`, content: `blob:http://localhost:3000/${tag}`, mimeType: "video/mp4", bytes: 100 + tag.length, durationMs: 9000 },
        intentText: `${tag} 的改造意图`,
        optionalSegments: "A,C",
        generationParams: [{ nodeId: "cfg-1", values: { model: `default::${tag}-model`, count: 2 } }],
    };
}

function planInput(idx: number, materials: WorkflowInstanceMaterials, selected = skeletonTemplate): PlanWorkflowInstanceInput {
    return { template: selected.template, templateNodes: selected.project.nodes, templateConnections: selected.project.connections, materials, instanceIndex: idx, unnamedCaption: "未命名参考图" };
}

function deepFreeze<T>(value: T): T {
    if (value && typeof value === "object") {
        Object.freeze(value);
        for (const item of Object.values(value as Record<string, unknown>)) deepFreeze(item);
    }
    return value;
}

function idSet(plan: WorkflowInstancePlan): Set<string> {
    return new Set(plan.nodes.map((node) => node.id));
}

/** 快照 + 真 `applyCanvasAgentOps`（幂等断言用的落库替身，与页面同一条实现）。 */
function makeSnapshotStore(initial?: { nodes?: CanvasNodeData[]; connections?: CanvasConnection[] }) {
    let snapshot: CanvasAgentSnapshot = { projectId: "p", title: "t", nodes: initial?.nodes || [], connections: initial?.connections || [], selectedNodeIds: [], viewport: { x: 0, y: 0, k: 1 } };
    return {
        apply: async (ops: CanvasAgentOp[]) => {
            const next = applyCanvasAgentOps(snapshot, ops);
            snapshot = { ...snapshot, nodes: next.nodes, connections: next.connections, selectedNodeIds: next.selectedNodeIds, viewport: next.viewport };
            return next;
        },
        nodes: () => snapshot.nodes,
        connections: () => snapshot.connections,
    };
}

type FetchCall = { url: string; method: string; headers: Record<string, string>; body: unknown };
/** stub 网络：拦截 upload / lapian run / job 轮询 / 正文投影（零真实付费）。 */
function makeFetchStub(options: { taskId: string; jobId: string; markdown: string; failUpload?: boolean; failJob?: string | "error" }) {
    const calls: FetchCall[] = [];
    const fetchImpl = async (url: string, init?: RequestInit) => {
        const method = String(init?.method || "GET").toUpperCase();
        const headers = (init?.headers || {}) as Record<string, string>;
        calls.push({ url, method, headers, body: init?.body });
        const json = (payload: unknown) => ({ ok: true, status: 200, json: async () => payload }) as unknown as Response;
        if (url === "/api/video/upload") return json(options.failUpload ? { error: "上传失败（探针注入）" } : { id: options.taskId });
        if (url === "/api/lapian/run") return json({ job: options.jobId });
        if (url === `/api/job/${options.jobId}`) return json(options.failJob === "error" ? { status: "error", error: "拆解失败（探针注入）" } : { status: "done" });
        if (url === `/api/lapian/${encodeURIComponent(options.taskId)}`) return json({ markdown: options.markdown });
        throw new Error(`stub 未覆盖的请求：${method} ${url}`);
    };
    return { fetchImpl, calls };
}

const noSleep = async () => {};

describe("A 实例计划：确定性 id / 跨实例零交集 / 七类引用全重绑定", () => {
    const planA = planWorkflowInstance(planInput(1, materialsOf("A")));
    const planB = planWorkflowInstance({ ...planInput(1, materialsOf("B")), instanceIndex: 2 });

    it("节点/连线 id 全部确定性推导且实例 1/2 集合零交集", () => {
        for (const node of planA.nodes) assert.ok(node.id.startsWith("tp3demo-1-"), node.id);
        assert.ok(instanceIdSetsDisjoint(planA.nodes.map((node) => node.id), planB.nodes.map((node) => node.id)));
        assert.ok(instanceIdSetsDisjoint(planA.connections.map((item) => item.id), planB.connections.map((item) => item.id)));
        // 模板旧 id 一个都不留在实例里
        for (const oldId of SKELETON) assert.equal(idSet(planA).has(oldId), false, oldId);
    });

    it("同模板同实例序重算 = 同一批 id（幂等锚）", () => {
        const again = planWorkflowInstance(planInput(1, materialsOf("A")));
        assert.deepEqual(again.nodes.map((node) => node.id), planA.nodes.map((node) => node.id));
        assert.deepEqual(again.connections.map((item) => item.id), planA.connections.map((item) => item.id));
    });

    it("七类引用全命中实例内 id（token / 扁平 / 镜像 / 来源 / 嫁接 / groupId / 连线端点）", () => {
        const ids = idSet(planA);
        const byOld = planA.nodeIdMap;
        const cfg = planA.nodes.find((node) => node.id === byOld["cfg-1"])!;
        const tokens = composerTokenIds(String(cfg.metadata?.composerContent || ""));
        assert.equal(tokens.length, 3, tokens.join(","));   // 清单行已剥离，只剩 3 个骨架 token
        for (const token of tokens) assert.ok(ids.has(token), `token 悬空：${token}`);
        assert.equal(cfg.metadata?.lapianSourceId, byOld["lap-1"]);
        const inGroup = planA.nodes.find((node) => node.id === byOld["ins-in-group"])!;
        assert.equal(inGroup.metadata?.groupId, byOld[GROUP]);
        for (const connection of planA.connections) {
            assert.ok(ids.has(connection.fromNodeId), connection.fromNodeId);
            assert.ok(ids.has(connection.toNodeId), connection.toNodeId);
        }
        // 视频配置的 2 图 + 终稿 token 也要换新
        const cfgv = planA.nodes.find((node) => node.id === byOld["cfgv-1"])!;
        for (const token of composerTokenIds(String(cfgv.metadata?.composerContent || ""))) assert.ok(ids.has(token), token);
    });

    it("拓扑逐边同构（F1）：实例 digest = 模板冻结 digest，边数一致", () => {
        assert.equal(planA.topologyDigest, skeletonTemplate.template.frozen.topology.digest);
        assert.equal(planA.connections.length, skeletonTemplate.project.connections.length);
    });

    it("跨实例互不引用对方节点（token/metadata/连线全查）", () => {
        const idsA = idSet(planA);
        const leaked: string[] = [];
        for (const node of planB.nodes) {
            for (const token of composerTokenIds(String(node.metadata?.composerContent || ""))) if (idsA.has(token)) leaked.push(token);
            for (const value of [node.metadata?.lapianSourceId, node.metadata?.assetSlotSourceId, node.metadata?.attachedImageId, node.metadata?.groupId]) {
                if (value && idsA.has(String(value))) leaked.push(String(value));
            }
        }
        for (const connection of planB.connections) {
            if (idsA.has(connection.fromNodeId) || idsA.has(connection.toNodeId)) leaked.push(connection.id);
        }
        assert.deepEqual(leaked, []);
    });
});

describe("B 槽值代入 + 冻结项 + 模板不可变", () => {
    const plan = planWorkflowInstance({ ...planInput(1, materialsOf("A")), origin: { x: 120, y: 40 } });
    const node = (oldId: string) => plan.nodes.find((item) => item.id === plan.nodeIdMap[oldId])!;

    it("槽①源视频：媒体字段按白名单写入，旧运行态零残留", () => {
        const video = node("video-src");
        assert.equal(video.metadata?.storageKey, "video:A");
        assert.equal(video.metadata?.content, "blob:http://localhost:3000/A");
        assert.equal(video.metadata?.bytes, 101);
        assert.equal(video.metadata?.durationMs, 9000);
        assert.equal(video.metadata?.taskId, undefined);
        assert.equal(video.title, "A.mp4");
        assert.ok(plan.slotWrites.some((item) => item.slotKind === "sourceVideo" && item.fields.includes("storageKey")));
    });

    it("拉片节点：模板旧 taskId/jobId/正文/意图全无（等受控入口回填本次）", () => {
        const lapian = node("lap-1");
        for (const key of ["taskId", "jobId", "content", "intent", "lapPhase", "cameraCoverage", "status"] as const) assert.equal(lapian.metadata?.[key], undefined, key);
        assert.equal(lapian.metadata?.lapianModel, "c::m");   // 模板配置保留
    });

    it("槽②意图正文写 content/prompt；槽⑤可选段写 lapianOptional", () => {
        const intent = node("it-1");
        assert.equal(intent.metadata?.content, "A 的改造意图");
        assert.equal(intent.metadata?.prompt, "A 的改造意图");
        assert.equal(intent.metadata?.lapianIntent, true);
        assert.equal(node("lap-1").metadata?.lapianOptional, "A,C");
    });

    it("槽④生成参数只写值（model/count），generationMode 等冻结项不被触碰", () => {
        const cfg = node("cfg-1");
        assert.equal(cfg.metadata?.model, "default::A-model");
        assert.equal(cfg.metadata?.count, 2);
        assert.equal(cfg.metadata?.generationMode, "text");
        assert.throws(
            () => planWorkflowInstance({ ...planInput(1, { ...materialsOf("A"), generationParams: [{ nodeId: "cfg-1", values: { generationMode: "video" } }] }) }),
            (error: unknown) => error instanceof WorkflowInstanceError && error.code === "slot_violation",
        );
    });

    it("指令正文/模板 id 逐字未变（F2）", () => {
        const instruction = node("ins-1");
        assert.equal(instruction.metadata?.content, "指令正文逐字");
        assert.equal(instruction.metadata?.promptTemplate, "seedance-flow");
    });

    it("composerContent 骨架（剔除清单行）逐字节等于模板（F4）", () => {
        const cfg = node("cfg-1");
        const templateCfg = skeletonTemplate.project.nodes.find((item) => item.id === "cfg-1")!;
        const normalize = (text: string) => text.replace(/@\[node:[^\]]+\]/g, "@[node:id]");
        assert.equal(normalize(String(cfg.metadata?.composerContent || "")), normalize(String(templateCfg.metadata?.composerContent || "")));
    });

    it("布局平移只动物理坐标（origin 叠加，metadata 不受影响）", () => {
        const templateVideo = skeletonTemplate.project.nodes.find((node) => node.id === "video-src")!;
        const video = node("video-src");
        assert.equal(video.position.x, templateVideo.position.x + 120);
        assert.equal(video.position.y, templateVideo.position.y + 40);
    });

    it("模板实体深冻结下两次实例化：零改动、零异常（模板不可变）", () => {
        const frozenNodes = deepFreeze(JSON.parse(JSON.stringify(skeletonTemplate.project.nodes)) as CanvasNodeData[]);
        const frozenConnections = deepFreeze(JSON.parse(JSON.stringify(skeletonTemplate.project.connections)) as CanvasConnection[]);
        const frozenTemplate = deepFreeze(JSON.parse(JSON.stringify(skeletonTemplate.template)) as typeof skeletonTemplate.template);
        const before = JSON.stringify({ frozenNodes, frozenConnections, frozenTemplate });
        const input = { template: frozenTemplate, templateNodes: frozenNodes, templateConnections: frozenConnections, materials: materialsOf("A"), instanceIndex: 1 };
        planWorkflowInstance(input);
        planWorkflowInstance({ ...input, instanceIndex: 2, materials: materialsOf("B") });
        assert.equal(JSON.stringify({ frozenNodes, frozenConnections, frozenTemplate }), before);
    });
});

describe("C 槽③挂图：扁平+镜像+清单+对照图+伴生边（同源实现）", () => {
    const newImage: CanvasNodeData = { id: "material-img", type: "image", title: "客户模特.png", position: { x: 10, y: 10 }, width: 300, height: 200, metadata: { content: "blob:http://localhost:3000/model", storageKey: "image:model", bytes: 4321, mimeType: "image/png", naturalWidth: 900, naturalHeight: 1200 } };
    const materials: WorkflowInstanceMaterials = { ...materialsOf("A"), assetAttach: [{ slotNodeId: SLOT, image: newImage, caption: "客户模特" }] };
    const plan = planWorkflowInstance(planInput(1, materials, fullTemplate));
    const slotNode = plan.nodes.find((item) => item.id === plan.nodeIdMap[SLOT])!;
    const imageNodeId = plan.nodeIdMap["material-img"];

    it("材料新图随实例落位（确定性 id）", () => {
        assert.equal(imageNodeId, "tp3full-1-material-img");
        assert.ok(plan.nodes.some((item) => item.id === imageNodeId && item.metadata?.storageKey === "image:model"));
    });

    it("扁平真源与 assetSlot 镜像同批双写（禁裸写一路）", () => {
        assert.equal(slotNode.metadata?.attachedImageId, imageNodeId);
        assert.equal(slotNode.metadata?.assetSlot?.attachedImageId, imageNodeId);
        assert.equal(slotNode.metadata?.attachedImageDesc, "客户模特");
        assert.equal(slotNode.metadata?.assetSlot?.caption, "客户模特");
        assert.equal(slotNode.metadata?.assetSlot?.desc, "原片对应物");   // 镜像其余字段保留
    });

    it("清单行走 collect/build/upsert 同源重算（token 指向本实例新图 + 短说明）", () => {
        const cfg = plan.nodes.find((item) => item.id === plan.nodeIdMap["cfg-1"])!;
        const composer = String(cfg.metadata?.composerContent || "");
        assert.ok(composer.includes("本次参考图："), composer);
        assert.ok(composer.includes(`@[node:${imageNodeId}]=客户模特`), composer);
        assert.ok(!composer.includes("@[node:img-1]"), composer);
        // 幂等：重算一次不改字（避免重复挂图把清单写成两行）
        const again = planWorkflowInstance(planInput(1, materials, fullTemplate));
        const cfgAgain = again.nodes.find((item) => item.id === again.nodeIdMap["cfg-1"])!;
        assert.equal(String(cfgAgain.metadata?.composerContent || ""), composer);
    });

    it("对照图节点 + 双向引用字段 + 伴生边（图→Config / 资产位→对照图）", () => {
        const graft = plan.nodes.find((item) => item.metadata?.assetGraftForSlotId === slotNode.id);
        assert.ok(graft, "缺对照图节点");
        assert.equal(graft!.metadata?.assetGraftOfImageId, imageNodeId);
        assert.equal(graft!.metadata?.storageKey, "image:model");
        assert.ok(plan.connections.some((item) => item.fromNodeId === slotNode.id && item.toNodeId === graft!.id));
        const cfg = plan.nodes.find((item) => item.id === plan.nodeIdMap["cfg-1"])!;
        assert.ok(plan.connections.some((item) => item.fromNodeId === imageNodeId && item.toNodeId === cfg.id));
    });

    it("派生槽模板挂图被拒（P4 派生资产位后才挂，本卡不发明资产位）", () => {
        assert.throws(
            () => planWorkflowInstance(planInput(1, { ...materialsOf("A"), assetAttach: [{ slotNodeId: SLOT, image: newImage }] })),
            (error: unknown) => error instanceof WorkflowInstanceError && error.code === "asset_slot_derivation_required",
        );
    });

    it("挂图目标不存在 → asset_image_missing（不静默跳过）", () => {
        assert.throws(
            () => planWorkflowInstance(planInput(1, { ...materialsOf("A"), assetAttach: [{ slotNodeId: SLOT, imageNodeId: "no-such-image" }] }, fullTemplate)),
            (error: unknown) => error instanceof WorkflowInstanceError && error.code === "asset_image_missing",
        );
    });
});

describe("D add_node 幂等键（审计 A2/G10）+ 旧行为零回归", () => {
    const planA = planWorkflowInstance(planInput(1, materialsOf("A")));

    it("首放 = 全量新增；重放 = 全部幂等跳过（节点数不翻倍）", async () => {
        const store = makeSnapshotStore();
        const first = await store.apply(planA.ops);
        assert.equal(store.nodes().length, planA.nodes.length);
        assert.equal(first.diagnostics.addedNodeIds.length, planA.nodes.length);
        const second = await store.apply(planA.ops);
        assert.equal(store.nodes().length, planA.nodes.length);
        assert.equal(second.diagnostics.addedNodeIds.length, 0);
        assert.equal(second.diagnostics.idempotentNodeIds.length, planA.nodes.length);
        assert.equal(store.connections().length, planA.connections.length);
        assert.equal(second.diagnostics.skippedConnectionIds.length, planA.connections.length);
    });

    it("同 id 形态不一致 → 报错（不静默 append、不覆盖）", async () => {
        const store = makeSnapshotStore();
        await store.apply(planA.ops);
        const tampered = planA.ops.map((op) => (op.type === "add_node" && op.id === planA.lapianNodeId ? { ...op, metadata: { ...(op.metadata || {}), taskId: "别的任务" } } : op));
        await assert.rejects(async () => { await store.apply(tampered as CanvasAgentOp[]); }, (error: unknown) => error instanceof CanvasAgentOpConflictError);
        assert.equal(store.nodes().length, planA.nodes.length);
    });

    it("只挪了位置（用户拖动）不算冲突：仍幂等跳过", async () => {
        const store = makeSnapshotStore();
        await store.apply(planA.ops);
        const moved = store.nodes().map((node) => ({ ...node, position: { x: node.position.x + 50, y: node.position.y + 20 } }));
        const store2 = makeSnapshotStore({ nodes: moved, connections: store.connections() });
        const result = await store2.apply(planA.ops);
        assert.equal(result.diagnostics.addedNodeIds.length, 0);
        assert.equal(store2.nodes().length, planA.nodes.length);
    });

    it("旧 op 行为零回归：无 id 的 add_node 仍自动生成、update/delete/select/viewport 照旧", () => {
        const snapshot: CanvasAgentSnapshot = { projectId: "p", title: "t", nodes: [], connections: [], selectedNodeIds: [], viewport: { x: 0, y: 0, k: 1 } };
        const added = applyCanvasAgentOps(snapshot, [{ type: "add_node", nodeType: "text", title: "自动 id", metadata: { content: "x" } }]);
        assert.equal(added.nodes.length, 1);
        const autoId = added.nodes[0].id;
        assert.ok(autoId.startsWith("text-"), autoId);
        const updated = applyCanvasAgentOps({ ...snapshot, nodes: added.nodes }, [{ type: "update_node", id: autoId, metadata: { content: "y" } }]);
        assert.equal(updated.nodes[0].metadata?.content, "y");
        const connected = applyCanvasAgentOps({ ...snapshot, nodes: updated.nodes }, [
            { type: "add_node", id: "second", nodeType: "text" },
            { type: "connect_nodes", id: "edge-1", fromNodeId: autoId, toNodeId: "second" },
            { type: "connect_nodes", id: "edge-1-dup", fromNodeId: autoId, toNodeId: "second" },
        ]);
        assert.equal(connected.connections.length, 1);
        assert.equal(connected.connections[0].id, "edge-1");
        const selected = applyCanvasAgentOps(connected, [{ type: "select_nodes", ids: [autoId, "ghost"] }]);
        assert.deepEqual(selected.selectedNodeIds, [autoId]);
        const deleted = applyCanvasAgentOps(selected, [{ type: "delete_node", ids: ["second"] }]);
        assert.equal(deleted.nodes.length, 1);
        assert.equal(deleted.connections.length, 0);
        const viewport = applyCanvasAgentOps(deleted, [{ type: "set_viewport", viewport: { x: 5, y: 6, k: 2 } }]);
        assert.deepEqual(viewport.viewport, { x: 5, y: 6, k: 2 });
        // run_generation 由 bridge 另路执行（不在此函数内建节点）：加幂等键不改这条路径
        const withRun = applyCanvasAgentOps(viewport, [{ type: "run_generation", nodeId: autoId, mode: "text", prompt: "x" }]);
        assert.deepEqual(withRun.nodes.map((node) => node.id), viewport.nodes.map((node) => node.id));
    });
});

describe("E materialize：受控拉片入口 + 本次正文 + 失败原子性", () => {
    const snapshotOf = (plan: WorkflowInstancePlan) => ({ nodes: plan.nodes, connections: plan.connections });

    function materializeDeps(options: { taskId: string; jobId: string; markdown: string; failUpload?: boolean; failJob?: "error"; blob?: Blob | null; existing?: () => CanvasNodeData[]; apply?: (ops: CanvasAgentOp[]) => Promise<void> }) {
        const stub = makeFetchStub({ taskId: options.taskId, jobId: options.jobId, markdown: options.markdown, failUpload: options.failUpload, failJob: options.failJob });
        const store = makeSnapshotStore();
        return {
            stub,
            store,
            deps: {
                getBlob: async () => (options.blob === undefined ? new Blob(["video-bytes"], { type: "video/mp4" }) : options.blob),
                fetch: stub.fetchImpl,
                applyOps: options.apply || (async (ops: CanvasAgentOp[]) => { await store.apply(ops); }),
                existingNodes: options.existing,
            },
        };
    }

    it("换源视频实例：上传→拉片→回填的是**本次**正文（旧正文零残留）", async () => {
        const { deps, stub, store } = materializeDeps({ taskId: "task-A", jobId: "job-A", markdown: "# 本次正文 A" });
        const result = await materializeWorkflowInstance({ ...planInput(1, materialsOf("A")), lapianConfig: { lapianDefaultModel: "", channels: [] }, contentWait: { sleep: noSleep } }, deps);
        assert.equal(result.status, "created");
        assert.equal(result.lapian?.taskId, "task-A");
        assert.equal(result.lapian?.jobId, "job-A");
        assert.equal(result.lapian?.content, "# 本次正文 A");
        const lapianNode = store.nodes().find((node) => node.id === result.plan.lapianNodeId)!;
        assert.equal(lapianNode.metadata?.content, "# 本次正文 A");
        assert.equal(lapianNode.metadata?.taskId, "task-A");
        assert.ok(!JSON.stringify(store.nodes()).includes("旧拉片正文"), "旧正文残留");
        // 上传合同：X-Filename 编码 + octet-stream；发起合同：{id,intent}，未配模型不发半截请求
        const upload = stub.calls.find((call) => call.url === "/api/video/upload")!;
        assert.equal(upload.method, "POST");
        assert.equal(upload.headers["X-Filename"], encodeURIComponent("A.mp4"));
        assert.equal(upload.headers["Content-Type"], "application/octet-stream");
        const run = stub.calls.find((call) => call.url === "/api/lapian/run")!;
        assert.deepEqual(JSON.parse(String(run.body)), { id: "task-A", intent: "A 的改造意图" });
        // 付费闸：实例化不发任何 run_generation（零付费创建）
        assert.equal(result.appliedOps?.some((op) => op.type === "run_generation"), false);
    });

    it("两组实例：不同 taskId/正文，互不引用对方节点", async () => {
        const first = materializeDeps({ taskId: "task-A", jobId: "job-A", markdown: "# 正文 A" });
        const a = await materializeWorkflowInstance({ ...planInput(1, materialsOf("A")), lapianConfig: {}, contentWait: { sleep: noSleep } }, first.deps);
        const second = materializeDeps({ taskId: "task-B", jobId: "job-B", markdown: "# 正文 B", existing: () => first.store.nodes() });
        const b = await materializeWorkflowInstance({ ...planInput(1, materialsOf("B")), instanceIndex: 2, lapianConfig: {}, contentWait: { sleep: noSleep } }, second.deps);
        assert.equal(a.lapian?.content, "# 正文 A");
        assert.equal(b.lapian?.content, "# 正文 B");
        assert.notEqual(a.plan.lapianNodeId, b.plan.lapianNodeId);
        const nodesA = new Set(a.plan.nodes.map((node) => node.id));
        assert.ok(b.plan.nodes.every((node) => !nodesA.has(node.id)));
    });

    it("重放同一实例请求：命中已落库（already_present），**不重复上传/拉片（不重复花钱）**", async () => {
        const first = materializeDeps({ taskId: "task-A", jobId: "job-A", markdown: "# 正文 A" });
        const created = await materializeWorkflowInstance({ ...planInput(1, materialsOf("A")), lapianConfig: {}, contentWait: { sleep: noSleep } }, first.deps);
        assert.equal(created.status, "created");
        const replay = materializeDeps({ taskId: "task-A2", jobId: "job-A2", markdown: "# 正文 A2", existing: () => first.store.nodes() });
        const again = await materializeWorkflowInstance({ ...planInput(1, materialsOf("A")), lapianConfig: {}, contentWait: { sleep: noSleep } }, replay.deps);
        assert.equal(again.status, "already_present");
        assert.equal(replay.stub.calls.length, 0, JSON.stringify(replay.stub.calls));
        assert.equal(first.store.nodes().length, created.plan.nodes.length);
    });

    it("素材缺失（storageKey 空）→ plan 期即拒，零写入零网络", async () => {
        const { deps, stub, store } = materializeDeps({ taskId: "task-A", jobId: "job-A", markdown: "# 正文 A" });
        await assert.rejects(
            async () => { await materializeWorkflowInstance({ ...planInput(1, { ...materialsOf("A"), sourceVideo: { storageKey: "" } }), lapianConfig: {} }, deps); },
            (error: unknown) => error instanceof WorkflowInstanceError && error.code === "material_missing",
        );
        assert.equal(stub.calls.length, 0);
        assert.equal(store.nodes().length, 0);
    });

    it("视频字节取不回 → incomplete(lapian_chain)，画布零变化", async () => {
        const { deps, store } = materializeDeps({ taskId: "task-A", jobId: "job-A", markdown: "# 正文 A", blob: null });
        const result = await materializeWorkflowInstance({ ...planInput(1, materialsOf("A")), lapianConfig: {}, contentWait: { sleep: noSleep } }, deps);
        assert.equal(result.status, "incomplete");
        assert.equal(result.incomplete?.stage, "lapian_chain");
        assert.equal(result.incomplete?.createdNodeIds.length, 0);
        assert.equal(result.incomplete?.rolledBack, true);
        assert.equal(store.nodes().length, 0);
    });

    it("上传失败 → incomplete(lapian_chain)，无半条链", async () => {
        const { deps, store, stub } = materializeDeps({ taskId: "task-A", jobId: "job-A", markdown: "# 正文 A", failUpload: true });
        const result = await materializeWorkflowInstance({ ...planInput(1, materialsOf("A")), lapianConfig: {}, contentWait: { sleep: noSleep } }, deps);
        assert.equal(result.status, "incomplete");
        assert.equal(result.incomplete?.stage, "lapian_chain");
        assert.ok(String(result.incomplete?.reason).includes("上传失败"));
        assert.equal(stub.calls.some((call) => call.url === "/api/lapian/run"), false);
        assert.equal(store.nodes().length, 0);
    });

    it("正文没回来 → incomplete(lapian_content)：节点带 taskId 落库但正文为空（不冒充完整）", async () => {
        const { deps, store } = materializeDeps({ taskId: "task-A", jobId: "job-A", markdown: "# 正文 A", failJob: "error" });
        const result = await materializeWorkflowInstance({ ...planInput(1, materialsOf("A")), lapianConfig: {}, contentWait: { sleep: noSleep } }, deps);
        assert.equal(result.status, "incomplete");
        assert.equal(result.incomplete?.stage, "lapian_content");
        assert.equal(result.incomplete?.rolledBack, false);
        const lapianNode = store.nodes().find((node) => node.id === result.plan.lapianNodeId)!;
        assert.equal(lapianNode.metadata?.taskId, "task-A");
        assert.equal(lapianNode.metadata?.content, undefined);
        assert.equal(result.incomplete?.createdNodeIds.length, result.plan.nodes.length);
    });

    it("落库失败 → incomplete(apply_ops) 且回滚 op 已下发（画布零残留）", async () => {
        const store = makeSnapshotStore();
        let calls = 0;
        const { deps } = materializeDeps({
            taskId: "task-A",
            jobId: "job-A",
            markdown: "# 正文 A",
            apply: async (ops) => {
                calls += 1;
                if (calls === 1) throw new Error("落库失败（探针注入）");
                await store.apply(ops);
            },
        });
        const result = await materializeWorkflowInstance({ ...planInput(1, materialsOf("A")), lapianConfig: {}, contentWait: { sleep: noSleep } }, deps);
        assert.equal(result.status, "incomplete");
        assert.equal(result.incomplete?.stage, "apply_ops");
        assert.equal(result.incomplete?.rolledBack, true);
        assert.equal(store.nodes().length, 0);
        assert.equal(store.connections().length, 0);
        assert.equal(calls, 2);   // 首次失败 + 回滚一次
    });

    it("回滚 op 只删本次实例建的节点/连线", async () => {
        const plan = planWorkflowInstance(planInput(1, materialsOf("A")));
        const rollback = buildInstanceRollbackOps(plan);
        const deletedNodes = rollback.filter((op) => op.type === "delete_node").flatMap((op) => (op.type === "delete_node" ? op.ids || [] : []));
        const deletedEdges = rollback.filter((op) => op.type === "delete_connections").flatMap((op) => (op.type === "delete_connections" ? op.ids || [] : []));
        assert.deepEqual(deletedNodes.sort(), plan.nodes.map((node) => node.id).sort());
        assert.deepEqual(deletedEdges.sort(), plan.connections.map((item) => item.id).sort());
    });

    it("模板不自足 / token 悬空 → 拒绝实例化（template_incomplete）", () => {
        const broken = JSON.parse(JSON.stringify(skeletonTemplate)) as typeof skeletonTemplate;
        const cfg = broken.project.nodes.find((node) => node.id === "cfg-1")!;
        cfg.metadata = { ...(cfg.metadata || {}), composerContent: `${cfg.metadata?.composerContent}\n外链：@[node:outside-node]` };
        assert.throws(
            () => planWorkflowInstance({ template: broken.template, templateNodes: broken.project.nodes, templateConnections: broken.project.connections, materials: materialsOf("A"), instanceIndex: 1 }),
            (error: unknown) => error instanceof WorkflowInstanceError && (error.code === "template_incomplete"),
        );
    });
});
