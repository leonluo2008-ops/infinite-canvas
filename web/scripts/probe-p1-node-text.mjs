/**
 * P1 全文读取双处修复自检（免费探针，真跑生产代码）。
 *
 * 覆盖执行卡 t_ca6cfc82 的三条验收：
 *   ① read_node_text：>240 字且尾部带 TAILMARK-9f3e 的节点，读回逐字全文、尾部标识一致、字数对得上
 *   ② generation_get_status：>4000 字返回 promptTruncated=true + promptFullLength；<4000 字返回全文
 *   ③ 既有行为不回归：compactNode 仍压缩（get_state 摘要）；全工具注册表（toolNames ↔ schema ↔ description）完整
 *
 * 覆盖范围说明（不掩饰）：
 *   - agent-node-text.ts 与 canvas-agent 的 schemas/tools 都是纯逻辑，直接打包真跑。
 *   - agent-site-tools.ts 依赖浏览器 store（zustand/localforage/i18n），node 侧无法 import，
 *     因此它的接线（工具名单 / switch 分支 / readNodeTextTool → readNodeFullText）用源码护栏断言。
 *
 * 跑法：cd web && node scripts/probe-p1-node-text.mjs   （失败 rc=1）
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const outDir = path.resolve("node_modules/.probe");
mkdirSync(outDir, { recursive: true });

let pass = 0;
const fails = [];
function check(name, condition, detail = "") {
    if (condition) {
        pass += 1;
        return;
    }
    fails.push(`${name}${detail ? ` — ${detail}` : ""}`);
}

async function bundle(entry, outName, extra = {}) {
    const outFile = path.join(outDir, outName);
    await esbuild.build({
        entryPoints: [path.resolve(entry)],
        bundle: true,
        format: "esm",
        platform: "node",
        outfile: outFile,
        logLevel: "silent",
        alias: { "@": path.resolve("src") },
        ...extra,
    });
    return import(pathToFileURL(outFile).href);
}

const nodeText = await bundle("src/lib/agent/agent-node-text.ts", "p1-node-text.mjs");
const agentSchemas = await bundle("../canvas-agent/src/canvas/schemas.ts", "p1-agent-schemas.mjs");
const agentTools = await bundle("../canvas-agent/src/canvas/tools.ts", "p1-agent-tools.mjs");

// ---------------------------------------------------------------- 夹具：>240 字、尾部唯一标识
const TAIL = "TAILMARK-9f3e";
const longText = `${"画布批量执行需要读到完整提示词，不能只看前 120 个字的摘要。".repeat(8)}${TAIL}`;
check("夹具字数 > 240", longText.length > 240, `实际 ${longText.length}`);
check("夹具尾部是唯一标识", longText.endsWith(TAIL));

const snapshot = {
    projectId: "proj-snapshot-1",
    title: "P1 探针画布",
    nodes: [
        { id: "text-1", type: "text", title: "长提示词节点", position: { x: 0, y: 0 }, width: 320, height: 200, metadata: { content: longText } },
        { id: "config-long", type: "config", title: "超长提示词配置", position: { x: 0, y: 0 }, width: 320, height: 200, metadata: { prompt: `${"长提示词".repeat(1125)}${TAIL}-GS`, status: "success", generationMode: "video" } },
        { id: "config-short", type: "config", title: "短提示词配置", position: { x: 0, y: 0 }, width: 320, height: 200, metadata: { prompt: longText, status: "success", generationMode: "image" } },
        { id: "config-240", type: "config", title: "240 字边界", position: { x: 0, y: 0 }, width: 320, height: 200, metadata: { prompt: `A`.repeat(240), status: "success", generationMode: "image" } },
        { id: "image-1", type: "image", title: "图片节点", position: { x: 0, y: 0 }, width: 320, height: 320, metadata: { content: `data:image/png;base64,${"iVBORw0KGgo".repeat(10)}` } },
    ],
    connections: [],
    selectedNodeIds: [],
    viewport: { x: 0, y: 0, k: 1 },
};

// ---------------------------------------------------------------- ① read_node_text 逐字全文
const readBack = nodeText.readNodeFullText({ nodeId: "text-1", canvasSnapshot: snapshot });
check("① 读到节点", Boolean(readBack));
check("① content 逐字一致", readBack?.content === longText, `读回 ${readBack?.content?.length} 字 / 写入 ${longText.length} 字`);
check("① 尾部标识逐字一致", readBack?.content?.endsWith(TAIL));
check("① 不含省略号（未截断）", !readBack?.content?.includes("..."));
check("① contentLength 与写入一致", readBack?.contentLength === longText.length, `${readBack?.contentLength} vs ${longText.length}`);
check("① length = 主文本字数", readBack?.length === longText.length);
check("① field = content", readBack?.field === "content");
check("① nodeId/projectId 回填", readBack?.nodeId === "text-1" && readBack?.projectId === "proj-snapshot-1", `${readBack?.nodeId}/${readBack?.projectId}`);
check("① type/title 回填", readBack?.type === "text" && readBack?.title === "长提示词节点");

// 持久化项目兜底（没有打开画布时按 nodeId 在项目里找）
const fromProjects = nodeText.readNodeFullText({ nodeId: "text-1", canvasSnapshot: null, projects: [{ id: "proj-store-9", nodes: [{ id: "text-1", type: "text", title: "存储里的节点", metadata: { content: longText } }] }] });
check("① 项目兜底可读全文", fromProjects?.content === longText && fromProjects?.projectId === "proj-store-9");

// 提示词字段同样全文（4500 字不被截）
const longPrompt = `${"超长提示词段落".repeat(800)}${TAIL}-PROMPT`;
const promptNode = { id: "config-prompt", type: "config", title: "提示词", metadata: { prompt: longPrompt } };
const promptRead = nodeText.readNodeFullText({ nodeId: "config-prompt", projects: [{ id: "p1", nodes: [promptNode] }] });
check("① prompt 全文逐字（>4000 不截）", promptRead?.prompt === longPrompt && promptRead?.promptLength === longPrompt.length, `${promptRead?.prompt?.length} vs ${longPrompt.length}`);
check("① prompt 尾部标识一致", promptRead?.prompt?.endsWith(`${TAIL}-PROMPT`));

// composerContent 全文
const composer = `${"配置节点编辑器内容".repeat(60)}${TAIL}-COMPOSER`;
const composerRead = nodeText.readNodeFullText({ nodeId: "config-composer", projects: [{ id: "p1", nodes: [{ id: "config-composer", type: "config", title: "c", metadata: { composerContent: composer } }] }] });
check("① composerContent 全文逐字", composerRead?.composerContent === composer && composerRead?.field === "composerContent");

// 节点不存在
check("① 未找到节点返回 null", nodeText.readNodeFullText({ nodeId: "nope", canvasSnapshot: snapshot }) === null);
check("① 空 nodeId 返回 null", nodeText.readNodeFullText({ nodeId: "   ", canvasSnapshot: snapshot }) === null);

// 媒体 dataURL 只回标记（防数 MB base64 撑爆上下文）
const imageRead = nodeText.readNodeFullText({ nodeId: "image-1", canvasSnapshot: snapshot });
check("① 媒体 dataURL 不回原文", imageRead?.content === undefined && imageRead?.contentOmitted === "media-data-url");
check("① 媒体 dataURL 仍回字数", imageRead?.contentLength === snapshot.nodes[4].metadata.content.length);

// ---------------------------------------------------------------- ② generation_get_status 预览放宽
check("② 预览阈值提到 4000", nodeText.PROMPT_PREVIEW_LIMIT === 4000, `实际 ${nodeText.PROMPT_PREVIEW_LIMIT}`);

const status = nodeText.buildGenerationStatus({ scope: "canvas", limit: 50 }, snapshot, []);
const byId = Object.fromEntries((status.tasks || []).map((task) => [task.id, task]));
const longTask = byId["config-long"];
const shortTask = byId["config-short"];
const boundaryTask = byId["config-240"];

check("② 超长任务在结果里", Boolean(longTask), JSON.stringify(Object.keys(byId)));
check("② 超 4000 → promptTruncated=true", longTask?.promptTruncated === true);
check("② 超 4000 → promptFullLength 完整字数", longTask?.promptFullLength === longPrompt.length || longTask?.promptFullLength === snapshot.nodes[1].metadata.prompt.length, `${longTask?.promptFullLength}`);
check("② 超 4000 → 预览 = 前 4000 字 + ...", longTask?.prompt === `${snapshot.nodes[1].metadata.prompt.slice(0, 4000)}...` && longTask?.prompt.length === 4003, `实际 ${longTask?.prompt?.length}`);
check("② <4000 → 全文不截", shortTask?.prompt === longText && shortTask?.promptTruncated === false, `${shortTask?.prompt?.length} vs ${longText.length}`);
check("② 240 字不再被截（旧 200 阈值已放宽）", boundaryTask?.prompt === "A".repeat(240) && boundaryTask?.promptTruncated === false);
check("② 既有字段未变（total/summary/tasks）", status.total === status.tasks.length + Math.max(0, status.total - status.tasks.length) && typeof status.summary?.succeeded === "number");

// 工作台任务同样带标记
const workbench = nodeText.buildGenerationStatus({ scope: "all" }, null, [{ id: "video-1", kind: "video", status: "queued", prompt: longPrompt, createdAt: "2026-11-13T00:00:00.000Z", updatedAt: "2026-11-13T00:00:01.000Z" }]);
const wbTask = workbench.tasks.find((task) => task.id === "video-1");
check("② 工作台任务超 4000 也带标记", wbTask?.promptTruncated === true && wbTask?.promptFullLength === longPrompt.length);
check("② 工作台任务原有字段保留", wbTask?.source === "video" && wbTask?.kind === "video" && wbTask?.status === "queued");

// ---------------------------------------------------------------- ③ 回归：compactNode 仍压缩 + 工具注册完整
const compacted = agentTools.compactNode(snapshot.nodes[0]);
check("③ get_state 摘要仍压缩", compacted.metadata.content.length === 123 && compacted.metadata.content.endsWith("..."), `实际 ${compacted.metadata.content.length}`);
check("③ 压缩后仍是前 120 字", compacted.metadata.content === `${longText.slice(0, 120)}...`);
const compactedState = agentTools.compactCanvasState(snapshot);
check("③ compactCanvasState 仍压缩", compactedState.nodes.find((node) => node.id === "text-1")?.metadata.content.length === 123);

const requiredTools = ["read_node_text", "generation_get_status", "canvas_get_state", "canvas_apply_ops", "assets_add", "prompts_search", "workbench_video_generate"];
const missingInNames = requiredTools.filter((name) => !agentSchemas.toolNames.includes(name));
check("③ canvas-agent 工具名单齐全", missingInNames.length === 0, missingInNames.join(","));
const missingSchema = agentSchemas.toolNames.filter((name) => !agentSchemas.toolInputSchemas[name]);
const missingDescription = agentSchemas.toolNames.filter((name) => !agentSchemas.toolDescriptions[name]);
check("③ 每个工具都有入参 schema", missingSchema.length === 0, missingSchema.join(","));
check("③ 每个工具都有描述", missingDescription.length === 0, missingDescription.join(","));
const readSchema = agentSchemas.toolInputSchemas.read_node_text;
check("③ read_node_text 入参只需 nodeId", readSchema.safeParse({ nodeId: "text-1" }).success && !readSchema.safeParse({}).success);
check("③ read_node_text 已可被 isToolName 识别", agentTools.isToolName("read_node_text") && agentTools.parseToolInput("read_node_text", { nodeId: "text-1" }).nodeId === "text-1");

// ---------------------------------------------------------------- 接线护栏（浏览器侧模块，源码断言）
const siteToolsSrc = readFileSync(path.resolve("src/lib/agent/agent-site-tools.ts"), "utf8");
check("接线 site-tool 名单含 read_node_text", /SITE_TOOL_NAMES[\s\S]*?"read_node_text"/.test(siteToolsSrc));
check("接线 runSiteTool 有分支", /case "read_node_text":\s*\n\s*return readNodeTextTool\(input, context\.canvasSnapshot\)/.test(siteToolsSrc));
check("接线 有中文标签", /get read_node_text\(\) \{ return siteText\("readNodeText"\); \}/.test(siteToolsSrc));
check("接线 getGenerationStatus 走纯函数", /function getGenerationStatus\([\s\S]{0,220}return buildGenerationStatus\(input, canvasSnapshot, useWorkbenchAgentStore\.getState\(\)\.tasks\)/.test(siteToolsSrc));
check("接线 readNodeTextTool 走 readNodeFullText + store 兜底", /function readNodeTextTool[\s\S]{0,600}readNodeFullText\(\{ nodeId, canvasSnapshot, projects: useCanvasStore\.getState\(\)\.projects \}\)/.test(siteToolsSrc));
check("接线 旧 compactPrompt 已迁出（无 200 字阈值残留）", !siteToolsSrc.includes("slice(0, 200)") && !/function compactPrompt/.test(siteToolsSrc));

const nodeTextSrc = readFileSync(path.resolve("src/lib/agent/agent-node-text.ts"), "utf8");
check("护栏 全文抽取无 slice/省略", !/function buildNodeFullText[\s\S]*?slice\(/.test(nodeTextSrc) && !nodeTextSrc.includes("…"));
const sliceHits = nodeTextSrc.match(/slice\(/g) || [];
const previewSrc = nodeTextSrc.match(/export function previewPrompt[\s\S]*?\n\}/)[0];
const readSrc = nodeTextSrc.match(/function buildNodeFullText[\s\S]*?\n\}/)[0];
check("护栏 仅预览函数截断（另一处是 tasks.slice 分页）", sliceHits.length === 2 && (previewSrc.match(/slice\(/g) || []).length === 1 && !readSrc.includes("slice("), `slice 出现 ${sliceHits.length} 次`);

const sessionSrc = readFileSync(path.resolve("../canvas-agent/src/canvas/session.ts"), "utf8");
check("接线 canvas-agent SITE_TOOLS 含 read_node_text", /SITE_TOOLS = new Set<ToolName>\(\[[\s\S]*?"read_node_text"/.test(sessionSrc));

const zhSrc = readFileSync(path.resolve("src/i18n/locales/zh-CN.ts"), "utf8");
const enSrc = readFileSync(path.resolve("src/i18n/locales/en-US.ts"), "utf8");
const siteToolNames = [...siteToolsSrc.match(/SITE_TOOL_NAMES = \[([\s\S]*?)\] as const/)[1].matchAll(/"([a-z_]+)"/g)].map((match) => match[1]);
const caseNames = [...siteToolsSrc.matchAll(/case "([a-z_]+)":/g)].map((match) => match[1]);
const labelNames = [...siteToolsSrc.matchAll(/get ([a-z_]+)\(\) \{ return siteText\(/g)].map((match) => match[1]);
check("护栏 工具名单每条都有 switch 分支", siteToolNames.every((name) => caseNames.includes(name)), siteToolNames.filter((name) => !caseNames.includes(name)).join(","));
check("护栏 工具名单每条都有标签", siteToolNames.every((name) => labelNames.includes(name)), siteToolNames.filter((name) => !labelNames.includes(name)).join(","));
check("护栏 中英文都有 readNodeText 标签", /readNodeText: "读取节点全文"/.test(zhSrc) && /readNodeText: "Read node text"/.test(enSrc));
check("护栏 中英文都有 nodeTextRead 摘要", /nodeTextRead: "已读取节点全文（\{\{length\}\} 字）"/.test(zhSrc) && /nodeTextRead: "Read the full node text \(\{\{length\}\} chars\)"/.test(enSrc));
check("护栏 探针跑的是生产模块（SITE 工具数 ≥ 10）", siteToolNames.length >= 10, `实际 ${siteToolNames.length}`);

// ---------------------------------------------------------------- 汇总
console.log(`\nP1 全文读取探针：${pass} 通过 / ${fails.length} 失败`);
if (fails.length) {
    fails.forEach((item) => console.log(`  FAIL ${item}`));
    process.exit(1);
}
console.log("全部通过");
