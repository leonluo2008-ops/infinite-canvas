/**
 * P2 探针：模板抽取 / 七类 id 重写 / 运行态剥离 / 五槽声明 / v3 导出（免费，真跑生产代码）。
 *
 * 分两段：
 *   A. 单测：esbuild 打包 `src/lib/canvas/workflow-template.test.ts` → `node --test` 真跑（七类引用逐项断言）。
 *   B. 真实样本端到端：从真实「无限画布」项目数据（IndexedDB canvas_store 落盘 JSON）里挑一条已跑通的 Wan 拉片链，
 *      按 P0a §1.1 骨架边界抽模板 → 断言拓扑/指令/固定素材一致 + 图外引用报告 + 五槽声明 + 重写器 + v3 导出缺字节报告，
 *      并把模板项目 JSON / v3 导出物落盘给浏览器导入轮使用。
 *
 * 跑法：cd web && node scripts/probe-p2-template.mjs
 *   可选：--store <real-canvas-store.json> --project <项目标题或 id> --out <输出目录>
 * 失败 rc=1（禁降级：任何断言失败如实计入 fails）。
 */
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const args = process.argv.slice(2);
const argOf = (name, fallback) => {
    const index = args.indexOf(`--${name}`);
    return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
};
const storePath = path.resolve(argOf("store", "C:/Users/leon3/AppData/Local/hermes/cache/scratch/p2-ego/real-canvas-store.json"));
const projectKey = argOf("project", "无限画布 3");
const outDir = path.resolve(argOf("out", "C:/Users/leon3/AppData/Local/hermes/cache/scratch/p2-ego/out"));
mkdirSync(outDir, { recursive: true });
const bundleDir = path.resolve("node_modules/.probe");
mkdirSync(bundleDir, { recursive: true });

let pass = 0;
const fails = [];
function check(name, condition, detail = "") {
    if (condition) pass += 1;
    else fails.push(`${name}${detail ? ` — ${detail}` : ""}`);
}

async function bundle(entry, outName) {
    const outFile = path.join(bundleDir, outName);
    await esbuild.build({
        entryPoints: [path.resolve(entry)],
        bundle: true,
        format: "esm",
        platform: "node",
        outfile: outFile,
        logLevel: "silent",
        alias: { "@": path.resolve("src") },
        external: ["node:test", "node:assert", "node:assert/strict"],
    });
    return outFile;
}

// ---------------------------------------------------------------- A. 单测（node --test）
const testBundle = await bundle("src/lib/canvas/workflow-template.test.ts", "p2-template.test.mjs");
let testOutput = "";
let testFailed = false;
try {
    testOutput = execFileSync(process.execPath, ["--test", testBundle], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
} catch (error) {
    testFailed = true;
    testOutput = `${error.stdout || ""}${error.stderr || ""}`;
}
const tapCount = (label) => {
    const match = testOutput.match(new RegExp(`^(?:# |ℹ )${label} (\\d+)$`, "m"));
    return match ? Number(match[1]) : undefined;
};
const tapTests = tapCount("tests");
const tapPass = tapCount("pass");
const tapFails = tapCount("fail");
check("A 单测进程零失败", !testFailed, testFailed ? testOutput.split("\n").filter((line) => line.includes("✖")).join(" | ") : "");
check("A 单测用例数 ≥ 30", Number(tapTests || 0) >= 30, `tests=${tapTests} pass=${tapPass} fail=${tapFails}`);
check("A 单测 fail 计数为 0", tapFails === 0, `fail=${tapFails}`);
writeFileSync(path.join(outDir, "unit-tests.tap"), testOutput, "utf8");

// ---------------------------------------------------------------- B. 真实样本
const workflow = await import(pathToFileURL(await bundle("src/lib/canvas/workflow-template.ts", "p2-workflow-template.mjs")).href);
const rewriter = await import(pathToFileURL(await bundle("src/lib/canvas/workflow-template-ids.ts", "p2-workflow-template-ids.mjs")).href);
const exporter = await import(pathToFileURL(await bundle("src/lib/canvas/canvas-export-builder.ts", "p2-canvas-export-builder.mjs")).href);

const store = JSON.parse(readFileSync(storePath, "utf8"));
const projects = (store.state && store.state.projects) || [];
const project = projects.find((item) => item.id === projectKey) || projects.find((item) => item.title === projectKey);
check("B 找到真实样本项目", Boolean(project), `projects=${projects.map((item) => item.title).join(",")}`);
if (!project) {
    console.log(`\nP2 模板探针：${pass} 通过 / ${fails.length} 失败`);
    fails.forEach((item) => console.log(`  FAIL ${item}`));
    process.exit(1);
}

const nodes = project.nodes || [];
const connections = project.connections || [];
const byId = new Map(nodes.map((node) => [node.id, node]));
const outgoing = (id) => connections.filter((connection) => connection.fromNodeId === id).map((connection) => connection.toNodeId);
const incoming = (id) => connections.filter((connection) => connection.toNodeId === id).map((connection) => connection.fromNodeId);

// 找一条「已跑通」的链：拉片有 jobId，且有 Config(text).lapianSourceId 指向它（说明派生过提示词链）
const lapians = nodes.filter((node) => node.type === "lapian" && node.metadata?.jobId);
const chainRoot = lapians.find((node) => nodes.some((candidate) => candidate.type === "config" && candidate.metadata?.generationMode === "text" && candidate.metadata?.lapianSourceId === node.id));
check("B 找到已跑通的拉片链根", Boolean(chainRoot), `lapians=${lapians.map((node) => node.id).join(",")}`);

const lapianId = chainRoot.id;
const configText = nodes.find((node) => node.type === "config" && node.metadata?.generationMode === "text" && node.metadata?.lapianSourceId === lapianId);
check("B 找到 Config(text)", Boolean(configText));
const sourceVideo = incoming(lapianId)
    .map((id) => byId.get(id))
    .find((node) => node && node.type === "video" && node.metadata?.storageKey);
check("B 找到源视频（带 storageKey）", Boolean(sourceVideo));
const instruction = incoming(configText.id)
    .map((id) => byId.get(id))
    .find((node) => node && node.type === "text" && node.metadata?.promptTemplate);
const intent = incoming(configText.id)
    .map((id) => byId.get(id))
    .find((node) => node && node.type === "text" && node.metadata?.lapianIntent);
check("B 找到指令节点", Boolean(instruction));
check("B 找到意图节点", Boolean(intent));
const finalText = outgoing(configText.id)
    .map((id) => byId.get(id))
    .find((node) => node && node.type === "text");
check("B 找到终稿节点", Boolean(finalText));
const configVideo = outgoing(finalText.id)
    .map((id) => byId.get(id))
    .find((node) => node && node.type === "config" && node.metadata?.generationMode === "video");
check("B 找到 Config(video)", Boolean(configVideo));

// 固定素材：连到 Config(text)/Config(video) 且不是资产位/挂图产物的图片节点
const isFixedAsset = (node) =>
    node && node.type === "image" && Boolean(node.metadata?.storageKey) && !node.metadata?.assetSlotSourceId && !node.metadata?.assetGraftForSlotId && !node.metadata?.assetGraftOfImageId;
const fixedAssets = [...new Set([...incoming(configText.id), ...incoming(configVideo.id)])]
    .map((id) => byId.get(id))
    .filter(isFixedAsset);

const skeletonIds = [sourceVideo?.id, lapianId, instruction?.id, intent?.id, configText?.id, finalText?.id, configVideo?.id, ...fixedAssets.map((node) => node.id)].filter(Boolean);
check("B 骨架边界齐全（视频/拉片/指令/意图/配置x2/终稿/固定素材）", skeletonIds.length === 7 + fixedAssets.length, `边界=${skeletonIds.join(",")}`);
check("B 固定素材 ≥ 1 且都带 storageKey", fixedAssets.length >= 1 && fixedAssets.every((node) => node.metadata.storageKey));

// 图外应有：资产位 / 挂图 / 生成产物（真实项目里存在的那几类）
const slotCount = nodes.filter((node) => node.metadata?.assetSlotSourceId === lapianId).length;
const graftCount = nodes.filter((node) => node.metadata?.assetGraftForSlotId && nodes.some((slot) => slot.id === node.metadata.assetGraftForSlotId && slot.metadata?.assetSlotSourceId === lapianId)).length;
const outputs = outgoing(configVideo.id).map((id) => byId.get(id)).filter((node) => node && node.type === "video");

const extracted = workflow.extractWorkflowTemplate({
    sourceProject: project,
    selectedNodeIds: skeletonIds,
    name: `${project.title} · 模板（pop-3 链）`,
    templateId: "pop3",
    createdAt: "2026-11-13T00:00:00.000Z",
});
const template = extracted.template;
const templateProject = extracted.project;
writeFileSync(path.join(outDir, "template-project.json"), JSON.stringify(templateProject, null, 2), "utf8");
writeFileSync(path.join(outDir, "template-declaration.json"), JSON.stringify(template, null, 2), "utf8");
writeFileSync(path.join(outDir, "external-references.json"), JSON.stringify(extracted.report.externalReferences, null, 2), "utf8");

check("B 模板节点数 = 边界节点数", templateProject.nodes.length === skeletonIds.length, `${templateProject.nodes.length} vs ${skeletonIds.length}`);
check("B 模板不含资产位/挂图/生成产物", !templateProject.nodes.some((node) => node.metadata?.assetSlotSourceId || node.metadata?.assetGraftForSlotId || node.metadata?.videoTaskId), JSON.stringify(templateProject.nodes.map((node) => node.id)));
check("B 骨架连线全部保留（两端在边界内的边）", templateProject.connections.length === connections.filter((connection) => skeletonIds.includes(connection.fromNodeId) && skeletonIds.includes(connection.toNodeId)).length);

// 拓扑同构：模板边集 = 原链骨架边集
const originalEdges = connections
    .filter((connection) => skeletonIds.includes(connection.fromNodeId) && skeletonIds.includes(connection.toNodeId))
    .map((connection) => `${connection.fromNodeId}->${connection.toNodeId}`)
    .sort();
const templateEdges = templateProject.connections.map((connection) => `${connection.fromNodeId}->${connection.toNodeId}`).sort();
check("B 拓扑逐边同构（C1）", JSON.stringify(originalEdges) === JSON.stringify(templateEdges), `${templateEdges.length} vs ${originalEdges.length}`);
check("B 冻结拓扑 digest 与模板边集一致", template.frozen.topology.digest === workflow.topologyDigest(templateProject.connections));

// 指令正文逐字一致（不剥指令）
const templateInstruction = templateProject.nodes.find((node) => node.id === instruction.id);
check("B 指令正文逐字一致（P0a §3.1 保指令）", templateInstruction.metadata.content === instruction.metadata.content && templateInstruction.metadata.prompt === instruction.metadata.prompt);
check("B 指令 promptTemplate 冻结值保留", templateInstruction.metadata.promptTemplate === instruction.metadata.promptTemplate);
check("B 指令无 status/errorDetails（通用剥离）", templateInstruction.metadata.status === undefined && templateInstruction.metadata.errorDetails === undefined);

// 意图重置为占位符
const templateIntent = templateProject.nodes.find((node) => node.id === intent.id);
check("B 意图重置为占位符（槽②）", templateIntent.metadata.content === workflow.TEMPLATE_INTENT_PLACEHOLDER && templateIntent.metadata.prompt === workflow.TEMPLATE_INTENT_PLACEHOLDER);
check("B 意图标记 lapianIntent 保留", templateIntent.metadata.lapianIntent === true);

// 源视频剥字节、拉片剥任务号与正文
const templateVideo = templateProject.nodes.find((node) => node.id === sourceVideo.id);
check("B 源视频字节全剥（槽①待注入）", ["content", "storageKey", "bytes", "mimeType", "durationMs", "naturalWidth", "naturalHeight"].every((key) => templateVideo.metadata[key] === undefined), JSON.stringify(templateVideo.metadata));
const templateLapian = templateProject.nodes.find((node) => node.id === lapianId);
check("B 拉片 taskId/jobId/正文剥离", templateLapian.metadata.taskId === undefined && templateLapian.metadata.jobId === undefined && templateLapian.metadata.content === undefined);
check("B 拉片模型配置保留", templateLapian.metadata.lapianModel === chainRoot.metadata.lapianModel || templateLapian.metadata.lapianModel === undefined);

// 固定素材字节保留
const templateAssets = fixedAssets.map((node) => templateProject.nodes.find((item) => item.id === node.id));
check("B 固定素材 storageKey/bytes 逐项保留", templateAssets.every((node) => node && node.metadata.storageKey && node.metadata.bytes), JSON.stringify(templateAssets.map((node) => node?.metadata?.storageKey)));
check("B 固定素材 status 剥离", templateAssets.every((node) => node.metadata.status === undefined));

// Config(text)：清单行删除、骨架 token 保留；Config(video)：运行态剥离
const templateConfigText = templateProject.nodes.find((node) => node.id === configText.id);
const strippedComposer = templateConfigText.metadata.composerContent;
check("B Config(text) 清单行删除（M3）", !strippedComposer.includes("本次参考图："), strippedComposer);
check("B Config(text) 骨架 token 保留（拉片/指令/意图）", [lapianId, instruction.id, intent.id].every((id) => strippedComposer.includes(`@[node:${id}]`)), strippedComposer);
check("B Config(text) generationMode/model 保留", templateConfigText.metadata.generationMode === "text" && templateConfigText.metadata.model === configText.metadata.model);
const templateConfigVideo = templateProject.nodes.find((node) => node.id === configVideo.id);
check("B Config(video) 运行态全剥", ["status", "references"].every((key) => templateConfigVideo.metadata[key] === undefined) && templateConfigVideo.metadata.videoTaskId === undefined);
check("B Config(video) 生成参数保留", templateConfigVideo.metadata.model === configVideo.metadata.model && templateConfigVideo.metadata.size === configVideo.metadata.size && templateConfigVideo.metadata.seconds === configVideo.metadata.seconds);
const finalTemplateText = templateProject.nodes.find((node) => node.id === finalText.id);
check("B 终稿正文/生成产物剥离", finalTemplateText.metadata.content === undefined && finalTemplateText.metadata.prompt === undefined && finalTemplateText.metadata.texts === undefined);

// grep 级：模板 JSON 无运行态残留
const templateJson = JSON.stringify(templateProject.nodes);
for (const token of ['"taskId"', '"jobId"', '"frameUrl"', '"cameraCoverage"', '"status"', '"errorDetails"', '"texts"', '"primaryTextId"', '"videoTaskId"', '"videoProgress"', '"references"', '"storageKey":"video:']) {
    check(`B grep 无残留 ${token}`, !templateJson.includes(token), token);
}

// 图外引用报告样本
const refs = extracted.report.externalReferences;
const slotIds = nodes.filter((node) => node.metadata?.assetSlotSourceId === lapianId).map((node) => node.id);
check("B 图外引用非空（不静默拉全项目）", refs.length > 0, `${refs.length}`);
check("B 图外引用全部指向边界外节点", refs.every((ref) => !skeletonIds.includes(ref.targetId)), JSON.stringify(refs.map((ref) => ref.targetId)));
check("B 图外引用含资产位/生成产物（入边或出边）", slotIds.some((id) => refs.some((ref) => ref.targetId === id)) && outputs.some((node) => node && refs.some((ref) => ref.targetId === node.id)), `slots=${slotCount} grafts=${graftCount} outputs=${outputs.length}`);
check("B 图外引用条数 = 跨边界边数", refs.filter((ref) => ref.kind === "connection").length === connections.filter((connection) => skeletonIds.includes(connection.fromNodeId) !== skeletonIds.includes(connection.toNodeId)).length);

// 五槽声明
check("B 五槽齐备", template.slots.length === 5, `${template.slots.length}`);
check("B 槽①=源视频节点", JSON.stringify(template.slots[0].targets.map((target) => target.nodeId)) === JSON.stringify([sourceVideo.id]));
check("B 槽②=意图节点", JSON.stringify(template.slots[1].targets.map((target) => target.nodeId)) === JSON.stringify([intent.id]));
check("B 槽③实例期派生（资产位不进模板）", template.slots[2].derived === true && template.slots[2].targets.length === 0);
check("B 槽④覆盖两个配置节点", JSON.stringify(template.slots[3].targets.map((target) => target.nodeId)) === JSON.stringify([configText.id, configVideo.id]));
check("B 槽④字段：文本配置 model/count，视频配置 model/size/seconds/vquality", JSON.stringify(template.slots[3].targets[0].fields) === JSON.stringify(["model", "count", "reasoningEffort"]) && JSON.stringify(template.slots[3].targets[1].fields) === JSON.stringify(["model", "size", "seconds", "vquality"]));
check("B 槽⑤=拉片节点", template.slots[4].targets.some((target) => target.nodeId === lapianId));

// 冻结项
check("B 冻结指令模板 id", template.frozen.instructions.some((item) => item.nodeId === instruction.id && item.promptTemplate === instruction.metadata.promptTemplate));
check("B 冻结 generationMode", template.frozen.generationModes.length === 2 && template.frozen.generationModes.every((item) => item.mode === "text" || item.mode === "video"));
check("B 冻结 composer 骨架 digest 与模板内容一致", template.frozen.composerSkeletons.every((item) => item.digest === workflow.stableDigest(String(templateProject.nodes.find((node) => node.id === item.nodeId).metadata.composerContent || ""))));

// 模板自足性
const problems = workflow.validateTemplateProject(templateProject.nodes, templateProject.connections);
check("B 模板自足性校验零问题", problems.length === 0, problems.join(" | "));

// 七类引用重写（真实链能覆盖到的类别）
const rewritten1 = rewriter.rewriteTemplateIds({ nodes: templateProject.nodes, connections: templateProject.connections, templateId: template.templateId, instanceIndex: 1 });
const rewritten2 = rewriter.rewriteTemplateIds({ nodes: templateProject.nodes, connections: templateProject.connections, templateId: template.templateId, instanceIndex: 2 });
check("B 重写后零未解析引用", rewritten1.unresolved.length === 0, JSON.stringify(rewritten1.unresolved));
check("B 重写节点 id 前缀确定性", rewritten1.nodes.every((node) => node.id.startsWith(`t${template.templateId}-1-`)));
check("B 重写后 token 全部指向新 id", [...rewritten1.nodes].every((node) => {
    const composer = String(node.metadata.composerContent || "");
    return [...composer.matchAll(/@\[node:([^\]]+)\]/g)].every((match) => match[1].startsWith(`t${template.templateId}-1-`));
}));
const rewrittenTokenCount = [...templateProject.nodes].reduce((total, node) => total + [...String(node.metadata.composerContent || "").matchAll(/@\[node:/g)].length, 0);
check("B token 替换计数 = 模板内 token 总数", rewritten1.replacedTokenCount === rewrittenTokenCount, `${rewritten1.replacedTokenCount} vs ${rewrittenTokenCount}`);
check("B 重写后拓扑按旧 id 回算 = 冻结拓扑（F1）", rewritten1.topologyDigest === template.frozen.topology.digest);
check("B 重写后模板自足性零问题", workflow.validateTemplateProject(rewritten1.nodes, rewritten1.connections).length === 0);
check("B 跨实例 id 零交集", rewriter.instanceIdSetsDisjoint(rewritten1.nodes.map((node) => node.id), rewritten2.nodes.map((node) => node.id)));
check("B 重写幂等（同实例序重跑同 id）", JSON.stringify(rewritten1.nodes.map((node) => node.id)) === JSON.stringify(rewriter.rewriteTemplateIds({ nodes: templateProject.nodes, connections: templateProject.connections, templateId: template.templateId, instanceIndex: 1 }).nodes.map((node) => node.id)));
// 七类覆盖矩阵（真实链里哪些类存在；不存在的类别由单测的全边界夹具覆盖）
const templateComposerTokens = [...templateProject.nodes].flatMap((node) => [...String(node.metadata.composerContent || "").matchAll(/@\[node:([^\]]+)\]/g)].map((match) => match[1]));
const coverageMatrix = {
    "1 composerToken": templateComposerTokens.length,
    "2 attachedImageId": templateProject.nodes.filter((node) => node.metadata.attachedImageId).length,
    "3 assetSlot.attachedImageId": templateProject.nodes.filter((node) => node.metadata.assetSlot?.attachedImageId).length,
    "4 assetSlotSourceId": templateProject.nodes.filter((node) => node.metadata.assetSlotSourceId).length,
    "5 lapianSourceId": templateProject.nodes.filter((node) => node.metadata.lapianSourceId).length,
    "6 assetGraft*": templateProject.nodes.filter((node) => node.metadata.assetGraftOfImageId || node.metadata.assetGraftForSlotId).length,
    "7 connections+groupId": templateProject.connections.length + templateProject.nodes.filter((node) => node.metadata.groupId).length,
};
check("B 真实链覆盖第 1/5/7 类（2/3/4/6 属资产位/挂图，不进骨架，由单测覆盖）", coverageMatrix["1 composerToken"] > 0 && coverageMatrix["5 lapianSourceId"] > 0 && coverageMatrix["7 connections+groupId"] > 0, JSON.stringify(coverageMatrix));
check("B 重写后 lapianSourceId 已换新 id", rewritten1.nodes.find((node) => node.metadata.lapianSourceId)?.metadata.lapianSourceId === `t${template.templateId}-1-${lapianId}`);

// v3 导出：素材齐备 + 缺字节报告 + 源视频不入 files
const templateKeys = exporter.collectStorageKeys(templateProject);
const fixedKeys = fixedAssets.map((node) => node.metadata.storageKey);
check("B 导出 storageKey 集合 = 固定素材集合（源视频已剥）", JSON.stringify([...templateKeys].sort()) === JSON.stringify([...new Set(fixedKeys)].sort()), `${JSON.stringify([...templateKeys].sort())} vs ${JSON.stringify([...new Set(fixedKeys)].sort())}`);
const presentBuild = await exporter.buildCanvasTemplateExport(templateProject, async (storageKey) => new Blob([`BYTES:${storageKey}`], { type: storageKey.startsWith("video:") ? "video/mp4" : "image/png" }));
check("B v3 信封 app/version", presentBuild.file.app === "infinite-canvas" && presentBuild.file.version === 3);
check("B v3 素材齐备时 missing 为空", presentBuild.missing.length === 0);
check("B v3 files 数 = 去重 storageKey 数", presentBuild.file.projects[0].files.length === [...new Set(fixedKeys)].length, `${presentBuild.file.projects[0].files.length}`);
check("B 模板项目 JSON 内嵌 workflowTemplate 声明（单一事实源）", JSON.parse(JSON.stringify(presentBuild.file)).projects[0].project.workflowTemplate.slots.length === 5);
const missingKey = fixedAssets[0].metadata.storageKey;
const missingBuild = await exporter.buildCanvasTemplateExport(templateProject, async (storageKey) => (storageKey === missingKey ? null : new Blob(["x"], { type: "image/png" })));
check("B 缺字节显式报告（不静默跳过）", missingBuild.missing.length === 1 && missingBuild.missing[0].storageKey === missingKey, JSON.stringify(missingBuild.missing));
check("B 缺字节报告能定位归属节点", missingBuild.missing[0].nodeIds.includes(fixedAssets[0].id), JSON.stringify(missingBuild.missing[0].nodeIds));

// 既有导出路径不破坏（源码护栏）
const exportSrc = readFileSync(path.resolve("src/lib/canvas/canvas-export.ts"), "utf8");
check("护栏 exportCanvasProjects 仍存在且走共用 builder", /export async function exportCanvasProjects[\s\S]{0,400}buildCanvasExport\(projects, defaultCanvasAssetReader\)/.test(exportSrc));
check("护栏 exportCanvasNodes 未改（既有单节点导出路径）", /export async function exportCanvasNodes/.test(exportSrc));
check("护栏 导出器未自造第二套信封（无 app:\"infinite-canvas\", version: 3 之外的字面量）", (exportSrc.match(/version: 3/g) || []).length === 0);
const storeSrc = readFileSync(path.resolve("src/stores/canvas/use-canvas-store.ts"), "utf8");
check("护栏 CanvasProject 显式扩展字段 workflowTemplate", /workflowTemplate\?: CanvasWorkflowTemplate/.test(storeSrc));
check("护栏 importProject 透传 workflowTemplate", /importProject[\s\S]{0,1200}workflowTemplate: acceptWorkflowTemplate\(source\.workflowTemplate\)/.test(storeSrc));
check("护栏 updateProject 允许更新 workflowTemplate", /updateProject: \(id: string, patch: Partial<Pick<CanvasProject,[^)]*workflowTemplate/.test(storeSrc));

report();

function report() {
    const summary = {
        project: project ? `${project.title} (${project.id})` : null,
        chainRoot: typeof chainRoot !== "undefined" ? chainRoot?.id : null,
        skeletonIds,
        fixedAssetKeys: typeof fixedAssets !== "undefined" ? fixedAssets.map((node) => node.metadata.storageKey) : [],
        externalReferenceKinds: typeof extracted !== "undefined" ? [...new Set(extracted.report.externalReferences.map((ref) => ref.kind))] : [],
        externalReferenceCount: typeof extracted !== "undefined" ? extracted.report.externalReferences.length : 0,
        coverageMatrix: typeof coverageMatrix !== "undefined" ? coverageMatrix : null,
        outDir,
    };
    writeFileSync(path.join(outDir, "probe-summary.json"), JSON.stringify({ summary, pass, fails }, null, 2), "utf8");
    console.log(JSON.stringify(summary, null, 2));
    console.log(`\nP2 模板探针：${pass} 通过 / ${fails.length} 失败`);
    if (fails.length) {
        fails.forEach((item) => console.log(`  FAIL ${item}`));
        process.exit(1);
    }
    console.log("全部通过");
}
