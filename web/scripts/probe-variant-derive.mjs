/**
 * 卡C1 验收探针（方案 docs/方案-卡C一拆多用变体派生-20261115.md §四）：变体派生纯逻辑层行为断言。
 * 先例 probe-video-phase.mjs：esbuild 打包真模块（alias @→src），bundle 落 node_modules/.probe/ 跑完删。
 *   node scripts/probe-variant-derive.mjs
 * 任何一条不成立退出码 1。
 */
import { build } from "esbuild";
import { rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const outdir = path.join(root, "node_modules", ".probe", "variant-derive");
const outfile = path.join(outdir, "workflow-variant.mjs");

await build({
    entryPoints: [path.join(root, "src/lib/canvas/workflow-variant.ts")],
    outfile,
    bundle: true,
    format: "esm",
    platform: "node",
    logLevel: "silent",
    alias: { "@": path.join(root, "src") },
    define: { "import.meta.env.DEV": "false", "import.meta.env": "{}" },
});
const variant = await import(pathToFileURL(outfile).href);
rmSync(outdir, { recursive: true, force: true });

let passed = 0;
const failures = [];
function check(name, condition, detail = "") {
    if (condition) { passed += 1; console.log("PASS | " + name + (detail ? " | " + detail : "")); }
    else { failures.push(name); console.log("FAIL | " + name + (detail ? " | " + detail : "")); }
}

// ---- 夹具：真实实例前缀形态（t<tid>-3-xxx），tid 与基线模板抽取一致 ----
const TID = "wabc12";
const IDX = 3;
const id = (name) => `t${TID}-${IDX}-${name}`;
const mkNode = (name, extra = {}, overrides = {}) => ({ id: id(name), type: overrides.type || "text", title: name, metadata: { ...(extra.content ? { content: extra.content } : {}), ...(extra.rest || {}) } });
const conn = (n) => ({ id: `c${TID}-${IDX}-${n}`, fromNodeId: "", toNodeId: "", ...n });

const lapianId = id("lap-1");
const intentId = id("intent-1");
const composerId = id("composer-1");
const slotAId = id("slot-a");
const slotBId = id("slot-b");
const slotCId = id("slot-c");
const graftId = id("graft-a");
const cfgVideoId = id("cfg-video");
const genOutId = "nanoid-genout-1"; // 执行期产物：无模板前缀
const genImageId = "nanoid-img-1"; // 执行期产物图（槽A 源绑定）

const fixtureNodes = [
    mkNode("lap-1", { content: "拉片正文", rest: { taskId: "task_src", jobId: "job_src", intent: "拉片意图", cameraCoverage: { covered: 2, total: 3, missing: ["推"] }, lapPhase: "done", status: "success" } }, { type: "lapian" }),
    mkNode("intent-1", { content: "默认换主角", rest: { lapianIntent: true } }),
    mkNode("composer-1", { rest: { composerContent: `开头 @[node:${id("lap-1")}] 结尾 @[node:${id("intent-1")}] 外部 @[node:outside-1]`, lapianSourceId: lapianId } }, { type: "config" }),
    mkNode("cfg-video", { rest: { generationMode: "video", videoTaskId: "vt-1", videoTaskProvider: "wan", videoProgress: 60, status: "loading", model: "m1", size: "16:9" } }, { type: "config" }),
    mkNode("slot-a", { rest: { assetSlotSourceId: lapianId, assetSlotName: "主角", assetSlotType: "人物", attachedImageId: genImageId, assetSlot: { desc: "主角", span: "00:00-01:00", attachedImageId: genImageId, frameUrl: "http://frame/1" } } }, { type: "image" }),
    mkNode("slot-b", { rest: { assetSlotSourceId: lapianId, assetSlotName: "产品", assetSlotType: "核心主体", attachedImageId: genImageId, attachedImageDesc: "旧图", assetSlot: { desc: "产品", span: "", attachedImageId: genImageId } } }, { type: "image" }),
    mkNode("graft-a", { rest: { assetGraftOfImageId: genImageId, assetGraftForSlotId: slotAId, status: "success" } }, { type: "image" }),
    mkNode("grp-1", { rest: {} }, { type: "group" }),
    { id: id("child-1"), type: "text", title: "child", metadata: { groupId: id("grp-1") } },
    // 执行期产物节点（nanoid 无前缀）：生成输出 + 产物图
    { id: genOutId, type: "video", title: "成片", metadata: { videoTaskId: "vt-out", videoTaskProvider: "sdgf", videoProgress: 100, status: "success", content: "blobref", storageKey: "sk-1", primaryImageId: genImageId } },
    { id: genImageId, type: "image", title: "产物图", metadata: { content: "data:image/png;base64,xx", storageKey: "sk-img" } },
];
const fixtureConnections = [
    conn({ fromNodeId: id("lap-1"), toNodeId: id("composer-1") }),
    conn({ fromNodeId: id("intent-1"), toNodeId: id("composer-1") }),
    conn({ fromNodeId: id("cfg-video"), toNodeId: genOutId }),
    conn({ fromNodeId: genImageId, toNodeId: graftId }),
];
const canvasNodes = [...fixtureNodes, { id: `t${TID}-1-old`, type: "text", title: "旧实例", metadata: {} }];

// ---- 1. assignVariantIndex：取画布最大 index+1 ----
check("index 分配：最大已有 3 → 新 4（忽略 1 的旧实例）", variant.assignVariantIndex(canvasNodes, TID) === 4, "got=" + variant.assignVariantIndex(canvasNodes, TID));
check("index 分配：无已有实例 → 1", variant.assignVariantIndex([{ id: "plain", type: "text", title: "", metadata: {} }], TID) === 1);

// ---- 2. locateInstanceSubgraph：前缀圈定 + 产物节点闭包 + 连线两端闭包 ----
const sub = variant.locateInstanceSubgraph({ nodes: canvasNodes, connections: fixtureConnections, templateId: TID, instanceIndex: IDX });
check("子图定位：骨架节点按前缀圈定", sub.nodes.every((n) => n.id.startsWith(`t${TID}-${IDX}-`) || sub.extraNodeIds.includes(n.id)));
check("子图定位：旧实例（t-wabc12-1-old）不进子图", !sub.nodes.some((n) => n.id === `t${TID}-1-old`));
check("子图定位：执行期产物节点闭包并入（成片+产物图）", sub.extraNodeIds.includes(genOutId) && sub.extraNodeIds.includes(genImageId), "extras=" + sub.extraNodeIds.join(","));
check("子图定位：连线闭包=两端都在集合内", sub.connections.length === 4 && sub.connections.every((c) => sub.nodes.some((n) => n.id === c.fromNodeId) && sub.nodes.some((n) => n.id === c.toNodeId)));

// ---- 3. deriveStrip 裁剪变体 ----
const lapianNode = fixtureNodes[0];
const strippedLapian = variant.deriveStrip(lapianNode, "lapian");
check("deriveStrip lapian：保 taskId/jobId/content/intent", strippedLapian.node.metadata.taskId === "task_src" && strippedLapian.node.metadata.jobId === "job_src" && strippedLapian.node.metadata.content === "拉片正文" && strippedLapian.node.metadata.intent === "拉片意图");
check("deriveStrip lapian：剥 cameraCoverage/lapPhase/status", !("cameraCoverage" in strippedLapian.node.metadata) && !("lapPhase" in strippedLapian.node.metadata) && !("status" in strippedLapian.node.metadata));
const strippedCfg = variant.deriveStrip(fixtureNodes[3], "configVideo");
check("deriveStrip configVideo：剥 videoTaskId/provider/progress/status，保 model/size", !("videoTaskId" in strippedCfg.node.metadata) && !("videoTaskProvider" in strippedCfg.node.metadata) && !("videoProgress" in strippedCfg.node.metadata) && !("status" in strippedCfg.node.metadata) && strippedCfg.node.metadata.model === "m1");
const strippedGen = variant.deriveStrip(fixtureNodes[9], "generationOutput");
check("deriveStrip generationOutput：剥成片字节/任务痕迹", !("content" in strippedGen.node.metadata) && !("videoTaskId" in strippedGen.node.metadata) && !("primaryImageId" in strippedGen.node.metadata) && !("status" in strippedGen.node.metadata));
const strippedSrc = variant.deriveStrip({ id: "sv", type: "video", title: "", metadata: { content: "blobref", storageKey: "sk", bytes: 123, status: "success" } }, "sourceVideo");
check("deriveStrip sourceVideo：保 content/storageKey（同画布同存储），剥 bytes/status", strippedSrc.node.metadata.content === "blobref" && strippedSrc.node.metadata.storageKey === "sk" && !("bytes" in strippedSrc.node.metadata) && !("status" in strippedSrc.node.metadata));
check("deriveStrip 不改入参", lapianNode.metadata.cameraCoverage !== undefined && lapianNode.metadata.status === "success");

// ---- 4. deriveWorkflowVariant 主流程 ----
const result = variant.deriveWorkflowVariant({
    nodes: canvasNodes,
    connections: fixtureConnections,
    templateId: TID,
    sourceInstanceIndex: IDX,
    newTaskId: "task_fork_9",
    assetBindings: [{ slotName: "主角", imageNodeId: genImageId, reason: "探针绑定" }],
});
const newPrefix = `t${TID}-4-`;
const byId = new Map(result.nodes.map((n) => [n.id, n]));
const newLapianId = newPrefix + "lap-1";
const newIntentId = newPrefix + "intent-1";
const newSlotAId = newPrefix + "slot-a";
const newSlotBId = newPrefix + "slot-b";
const newGraftId = newPrefix + "graft-a";
const newChildId = newPrefix + "child-1";
const newGrpId = newPrefix + "grp-1";
const newGenOutId = newPrefix + "extra-" + genOutId;
const newGenImageId = newPrefix + "extra-" + genImageId;

// ③ id 重写：反解再套新前缀，禁嵌套
check("id 重写：全部单层新前缀（无嵌套双前缀）", result.nodes.every((n) => n.id.startsWith(newPrefix) && !n.id.slice(newPrefix.length - 1).includes(`-t${TID}-`)));
check("id 重写：产物节点合成键 extra-<原id>", Boolean(byId.get(newGenOutId)) && Boolean(byId.get(newGenImageId)), "newGenOut=" + newGenOutId);

// ④ 七类引用重写
const composer = byId.get(newPrefix + "composer-1");
check("七类#1 composerContent token 重写", composer.metadata.composerContent === `开头 @[node:${newLapianId}] 结尾 @[node:${newIntentId}] 外部 @[node:outside-1]`, composer.metadata.composerContent);
check("七类#1 token 未命中保留 + warning", result.warnings.some((w) => w.includes("outside-1")));
check("七类#2+#3 assetSlotSourceId（→拉片）与 groupId 重写", byId.get(newPrefix + "composer-1").metadata.lapianSourceId === newLapianId && byId.get(newChildId).metadata.groupId === newGrpId);
const graft = byId.get(newGraftId);
check("七类#5+#6 assetGraft 两枚重写（图→产物键，槽→新前缀）", graft.metadata.assetGraftOfImageId === newGenImageId && graft.metadata.assetGraftForSlotId === newSlotAId);
check("七类#7 连线端点与连线 id 重写", result.connections.every((c) => c.id.startsWith(`c${TID}-4-`) && c.fromNodeId.startsWith(newPrefix) && c.toNodeId.startsWith(newPrefix)) && result.connections.some((c) => c.toNodeId === newGenOutId || c.fromNodeId === newGenImageId));
check("连线 id 反解无嵌套", result.connections.some((c) => c.id === `c${TID}-4-` + "lap-1>" + "composer-1" || c.id.startsWith(`c${TID}-4-`) && !c.id.slice(`c${TID}-4-`.length - 1).includes(`-c${TID}-`)));

// ⑤ 槽起手全空 + 绑定双写
const slotA = byId.get(newSlotAId);
const slotB = byId.get(newSlotBId);
check("槽起手全空：未绑槽不继承源 attachedImageId（扁平+镜像+desc）", !slotB.metadata.attachedImageId && !slotB.metadata.assetSlot.attachedImageId && !slotB.metadata.attachedImageDesc, JSON.stringify(slotB.metadata.assetSlot));
check("槽绑定：扁平真源写入", slotA.metadata.attachedImageId === genImageId);
check("槽绑定：assetSlot 镜像双写一致", slotA.metadata.assetSlot.attachedImageId === genImageId);
check("绑定回执：slotNodeId/slotName/imageNodeId/reason", result.bindings.length === 1 && result.bindings[0].slotNodeId === newSlotAId && result.bindings[0].slotName === "主角" && result.bindings[0].imageNodeId === genImageId && result.bindings[0].reason === "探针绑定");

// ⑥ 意图缺省继承
check("意图缺省继承源实例 content", byId.get(newIntentId).metadata.content === "默认换主角");
const result2 = variant.deriveWorkflowVariant({ nodes: canvasNodes, connections: fixtureConnections, templateId: TID, sourceInstanceIndex: IDX, newTaskId: "task_fork_9", intentText: "换产品为竞品B" });
check("意图显式 intentText 覆盖", new Map(result2.nodes.map((n) => [n.id, n])).get(`t${TID}-4-intent-1`).metadata.content === "换产品为竞品B");

// ⑦ 拉片节点：taskId 换新 + lapianReusedFrom 标记
const newLapian = byId.get(newLapianId);
check("拉片 taskId 换 fork 新任务号", newLapian.metadata.taskId === "task_fork_9");
check("lapianReusedFrom 标记在位（=源拉片节点 id）", newLapian.metadata.lapianReusedFrom === lapianId, "got=" + newLapian.metadata.lapianReusedFrom);
check("拉片正文复用保留", newLapian.metadata.content === "拉片正文" && newLapian.metadata.intent === "拉片意图");
check("产物节点剥生成痕迹但位置保留", Boolean(byId.get(newGenOutId)) && !("videoTaskId" in byId.get(newGenOutId).metadata));

// 源实例零改动
check("源实例零改动（入参节点未被改写）", canvasNodes.find((n) => n.id === slotAId).metadata.attachedImageId === genImageId && canvasNodes.find((n) => n.id === lapianId).metadata.taskId === "task_src");

// ---- 4b. 画布化闭包模式（2026-11-15 拍板：源=画布任意拉片节点，不要求实例形 id）----
const plainLapianId = "plain-lap-9";
const plainImageId = "plain-img-1";
const plainNodes = [
    { id: plainLapianId, type: "lapian", title: "裸拉片", metadata: { taskId: "task_plain", jobId: "job_plain", content: "裸拉片正文", intent: "裸意图" } },
    { id: plainImageId, type: "image", title: "图", metadata: { content: "data:image/png;base64,yy" } },
    { id: "plain-slot-1", type: "image", title: "槽", metadata: { assetSlotSourceId: plainLapianId, assetSlotName: "主角", assetSlotType: "人物", assetSlot: { desc: "主角", span: "" } } },
];
const closureResult = variant.deriveWorkflowVariant({
    nodes: plainNodes,
    connections: [],
    sourceLapianNodeId: plainLapianId,
    newTaskId: "task_plain", // 画布化口径：新拉片继承源 taskId（拆解产物引用复用）
    assetBindings: [{ imageNodeId: plainImageId, reason: "闭包绑定" }],
});
const closureById = new Map(closureResult.nodes.map((n) => [n.id, n]));
const closureLapian = closureById.get(`t${plainLapianId}-1-${plainLapianId}`);
check("闭包模式：裸拉片节点 id 可作派生源（不要求实例形 id）", Boolean(closureLapian), closureLapian?.id);
check("闭包模式：新拉片继承源 taskId/content", closureLapian.metadata.taskId === "task_plain" && closureLapian.metadata.content === "裸拉片正文");
check("闭包模式：lapianReusedFrom 标记 = 源节点 id", closureLapian.metadata.lapianReusedFrom === plainLapianId);
check("闭包模式：槽绑定双写仍在（扁平+镜像）", closureResult.bindings.length === 1 && closureById.get(closureResult.bindings[0].slotNodeId).metadata.attachedImageId === plainImageId && closureById.get(closureResult.bindings[0].slotNodeId).metadata.assetSlot?.attachedImageId === plainImageId);

// ---- 5. 边界 ----
const noTaskNodes = canvasNodes.map((n) => (n.id === lapianId ? { ...n, metadata: { ...n.metadata, taskId: "" } } : n));
let threw = null;
try { variant.deriveWorkflowVariant({ nodes: noTaskNodes, connections: fixtureConnections, templateId: TID, sourceInstanceIndex: IDX, newTaskId: "t9" }); } catch (e) { threw = e; }
check("边界：源拉片 taskId 空 → source_not_ready", threw && threw.code === "source_not_ready", threw && threw.message);
const noContentNodes = canvasNodes.map((n) => (n.id === lapianId ? { ...n, metadata: { ...n.metadata, content: "" } } : n));
threw = null;
try { variant.deriveWorkflowVariant({ nodes: noContentNodes, connections: fixtureConnections, templateId: TID, sourceInstanceIndex: IDX, newTaskId: "t9" }); } catch (e) { threw = e; }
check("边界：源拉片 content 空 → source_not_ready", threw && threw.code === "source_not_ready");
threw = null;
try { variant.deriveWorkflowVariant({ nodes: canvasNodes, connections: fixtureConnections, templateId: TID, sourceInstanceIndex: IDX, newTaskId: "t9", assetBindings: [{ imageNodeId: "ghost-image" }] }); } catch (e) { threw = e; }
check("边界：绑定图节点不存在 → binding_target_missing", threw && threw.code === "binding_target_missing");
threw = null;
const emptyImage = { id: "img-empty", type: "image", title: "", metadata: {} };
try { variant.deriveWorkflowVariant({ nodes: [...canvasNodes, emptyImage], connections: fixtureConnections, templateId: TID, sourceInstanceIndex: IDX, newTaskId: "t9", assetBindings: [{ imageNodeId: "img-empty" }] }); } catch (e) { threw = e; }
check("边界：绑定图节点无内容 → binding_target_missing", threw && threw.code === "binding_target_missing");

// ---- 6. 入口级（workflow-variant-entry）：纯画布派生（无 hub 腿）----
const entryOutdir = path.join(root, "node_modules", ".probe", "variant-entry");
await build({
    entryPoints: [path.join(root, "src/lib/canvas/workflow-variant-entry.ts")],
    outfile: path.join(entryOutdir, "workflow-variant-entry.mjs"),
    bundle: true,
    format: "esm",
    platform: "node",
    logLevel: "silent",
    alias: { "@": path.join(root, "src") },
    define: { "import.meta.env.DEV": "false", "import.meta.env": "{}" },
});
const entry = await import(pathToFileURL(path.join(entryOutdir, "workflow-variant-entry.mjs")).href);
rmSync(entryOutdir, { recursive: true, force: true });

const entryOps = [];
const entryImageId = genImageId;
let entryResult = null;
try {
    entryResult = await entry.deriveWorkflowVariantTool(
        {
            sourceInstanceLapianNodeId: plainLapianId, // 裸拉片节点 id：源校验放宽的直接证据
            assetBindings: [{ imageNodeId: entryImageId, reason: "入口绑定" }],
        },
        {
            project: () => ({ nodes: [...plainNodes, ...canvasNodes], connections: fixtureConnections }),
            applyOps: (ops) => { entryOps.push(...ops); },
        },
    );
} catch (e) {
    entryResult = { error: e };
}
check("入口：裸拉片源派生成功（源校验放宽，不再要求实例形 id）", entryResult && entryResult.ok === true, entryResult && entryResult.error ? String(entryResult.error) : "");
const hubFieldNames = ["newLocalId", "sourceLocalId", "hubState", "assetsUploaded", "confirmedEmpty", "promptSaved"];
check("入口：回执无 hub 相关字段", entryResult && entryResult.ok && hubFieldNames.every((k) => !(k in entryResult)), JSON.stringify(entryResult && entryResult.ok ? Object.keys(entryResult) : ""));
check("入口：bridgeId = 源拆解任务号", entryResult && entryResult.ok && entryResult.bridgeId === "task_plain");
check("入口：停点=画布侧待批", entryResult && entryResult.ok && entryResult.stopPoint === "awaiting_approval");
check("入口：落画布 ops=add_node/connect_nodes", entryOps.length > 0 && entryOps.every((op) => op.type === "add_node" || op.type === "connect_nodes"));
const entryNodeById = new Map(entryOps.filter((op) => op.type === "add_node").map((op) => [op.id, op]));
const entryLapian = [...entryNodeById.values()].find((op) => op.metadata.taskId === "task_plain" && op.metadata.content === "裸拉片正文");
check("入口：新拉片节点继承源 taskId/content（拆解产物=画布元数据引用）", Boolean(entryLapian), entryLapian?.id || "");
check("入口：槽绑定双写落在画布元数据（attachedImageId+assetSlot）", Boolean(entryLapian) && Boolean(entryLapian.metadata.lapianReusedFrom) && [...entryNodeById.values()].some((op) => op.metadata.attachedImageId === entryImageId && op.metadata.assetSlot?.attachedImageId === entryImageId));
const noContentEntry = plainNodes.map((n) => (n.id === plainLapianId ? { ...n, metadata: { ...n.metadata, content: "" } } : n));
let entryThrew = null;
try { await entry.deriveWorkflowVariantTool({ sourceInstanceLapianNodeId: plainLapianId }, { project: () => ({ nodes: noContentEntry, connections: [] }), applyOps: () => {} }); } catch (e) { entryThrew = e; }
check("入口：拆解产物不在位 → source_not_done", entryThrew && entryThrew.code === "source_not_done", entryThrew && entryThrew.message);

console.log("===SUMMARY=== " + passed + " PASS / " + failures.length + " FAIL" + (failures.length ? " | FAIL: " + failures.join("; ") : ""));
process.exit(failures.length ? 1 : 0);
