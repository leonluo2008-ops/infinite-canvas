/**
 * P7 探针：自动匹配 + 一键否决 + Agent 自修闭环（2 轮上限升级给人）。
 *
 * 跑法：cd web && node scripts/probe-p7-match-repair.mjs
 *   --store <real-canvas-store.json> --project <项目标题或 id> --out <输出目录>
 *
 * 分段：
 *   A. 单测：打包 `workflow-asset-match.test.ts` + `workflow-instance-repair.test.ts` → `node --test` 真跑。
 *   B. 夹具端到端：自动匹配（每实例每槽带理由、按槽归属、两实例互不串配）→ 落地 → 请求图序=槽序 → 五槽 5/5 → 付费 0。
 *   C. 否决闭环：否决槽 X → 重新匹配换图 → **旧材料重放**计划层丢弃 → 幂等重放一致 → 槽③判据拦下。
 *   D. 自修第 1 轮：hub 首检 FAIL（夹具 finding）→ 编排器自修（读全文 → 限定改正文 → 既有保存+重检）→ PASS 同修订可见
 *      → 状态仍是「待批」；冻结项零变化；付费 0。跨语言交接：改写前/后正文与 finding 落盘给 Python 侧真自检复核。
 *   E. 自修 2 轮仍 FAIL → 实例 needs_human（不豁免、不进下一步）+ 派生状态 R4a + 批次 outcome=needs_human + 轮次=2。
 *   H. 生产者三态：hub 只回 WARN 告警 → 不触发自修、不改状态（告警不是可自修对象，不许假升级）。
 *   F. 真实样本零回归：真实「无限画布 3」抽全量模板 → 实例化 → 编排器跑通到待批（不给 repair/vetoes = P4 行为不变）。
 *   G. store 方法面 + 导出透传否决记录 + 画布节点不落批次/状态字段（C5）。
 *
 * 失败 rc=1（禁降级：任何断言失败如实计入 fails，不跳过）。
 */
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";

const memory = new Map();
globalThis.localStorage = {
    getItem: (key) => (memory.has(key) ? memory.get(key) : null),
    setItem: (key, value) => void memory.set(key, String(value)),
    removeItem: (key) => void memory.delete(key),
};
globalThis.window = { localStorage: globalThis.localStorage };

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const args = process.argv.slice(2);
const argOf = (name, fallback) => {
    const index = args.indexOf(`--${name}`);
    return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
};
const storePath = path.resolve(argOf("store", "C:/Users/leon3/AppData/Local/hermes/cache/scratch/p2-ego/real-canvas-store.json"));
const projectKey = argOf("project", "无限画布 3");
const outDir = path.resolve(argOf("out", "C:/Users/leon3/AppData/Local/hermes/cache/scratch/p7-out"));
mkdirSync(outDir, { recursive: true });
const bundleDir = path.resolve("node_modules/.probe-p7");
mkdirSync(bundleDir, { recursive: true });

let pass = 0;
const fails = [];
const results = [];
function check(name, condition, detail = "") {
    const ok = Boolean(condition);
    results.push({ name, ok, detail: ok ? "" : detail });
    if (ok) pass += 1;
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
        banner: { js: "globalThis.localStorage = globalThis.localStorage || { getItem: () => null, setItem: () => {}, removeItem: () => {} };" },
    });
    return outFile;
}

// ---------------------------------------------------------------- A. 单测
const unitCases = [
    ["src/lib/canvas/workflow-asset-match.test.ts", "p7-match.test.mjs"],
    ["src/lib/canvas/workflow-instance-repair.test.ts", "p7-repair.test.mjs"],
];
let unitPass = 0;
let unitFail = 0;
const unitLogs = [];
for (const [entry, outName] of unitCases) {
    const bundleFile = await bundle(entry, outName);
    let output = "";
    try {
        output = execFileSync(process.execPath, ["--test", bundleFile], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
    } catch (error) {
        output = `${error.stdout || ""}${error.stderr || ""}`;
    }
    unitLogs.push(`# ${entry}\n${output}`);
    const num = (label) => Number((output.match(new RegExp(`^(?:# |ℹ )${label} (\\d+)$`, "m")) || [])[1] || -1);
    const tests = num("tests");
    const passed = num("pass");
    const failed = num("fail");
    check(`A 单测 ${path.basename(entry)} 真跑（tests=${tests} pass=${passed} fail=${failed}）`, tests > 0 && failed === 0 && passed === tests, output.slice(-800));
    unitPass += Math.max(passed, 0);
    unitFail += Math.max(failed, 0);
}
writeFileSync(path.join(outDir, "unit-tests.log"), unitLogs.join("\n\n"), "utf8");

// ---------------------------------------------------------------- 模块装载
const modEntry = path.join(bundleDir, "p7-entry.ts");
writeFileSync(
    modEntry,
    [
        'export * as builtin from "@/components/canvas/nodes/builtin-nodes";',
        'export * as workflow from "@/lib/canvas/workflow-template";',
        'export * as instancer from "@/lib/canvas/workflow-instance";',
        'export * as runner from "@/lib/canvas/workflow-instance-run";',
        'export * as matcher from "@/lib/canvas/workflow-asset-match";',
        'export * as repair from "@/lib/canvas/workflow-instance-repair";',
        'export * as batch from "@/lib/canvas/workflow-batch";',
        'export * as batchStatus from "@/lib/canvas/workflow-batch-status";',
        'export * as ops from "@/lib/canvas/canvas-agent-ops";',
        'export * as genctx from "@/components/canvas/canvas-node-generation";',
        'export * as lapianAssets from "@/lib/canvas/lapian-assets";',
        'export * as store from "@/stores/canvas/use-canvas-store";',
        `export * as paid from ${JSON.stringify(path.resolve("..", "canvas-agent/src/canvas/paid-actions.ts").split(path.sep).join("/"))};`,
        "",
    ].join("\n"),
    "utf8",
);
const m = await import(pathToFileURL(await bundle(modEntry, "p7-modules.mjs")).href);
const { builtin, workflow, instancer, runner, matcher, repair, batch, batchStatus, ops, genctx, lapianAssets, paid } = m;
const useCanvasStore = m.store.useCanvasStore;
builtin.registerBuiltinNodes();

// ---------------------------------------------------------------- 夹具（与单测同构；真实样本只用于 F 段零回归）
const VIDEO = "video-src", LAPIAN = "lap-1", INS = "ins-1", INTENT = "it-1", CONFIG_TEXT = "cfg-1", FINAL = "fin-1", CONFIG_VIDEO = "cfgv-1", OUT_VIDEO = "out-video";
const SLOT_FEMALE = "slot-female", SLOT_PERFUME = "slot-perfume";
const LIB_FEMALE = "lib-female", LIB_PERFUME = "lib-perfume", LIB_DECOY = "lib-city";
/** 注入的已知异常词（尾部违规词，TAILMARK 式定位）：只在正文尾段出现。 */
const MARK = "细长圆柱体紫色表面";

const node = (id, type, title, metadata, x = 0) => ({ id, type, title, position: { x, y: 0 }, width: 300, height: 200, metadata });
const fixtureNodes = () => [
    node(VIDEO, "video", "源视频.mp4", { content: "blob:src", storageKey: "video:src", mimeType: "video/mp4" }),
    node(LAPIAN, "lapian", "拉片", { content: "# 拉片正文", taskId: "lap-task", jobId: "lap-job", lapPhase: "done" }, 100),
    node(INS, "text", "转换指令", { content: "指令正文", prompt: "指令正文", promptTemplate: "seedance-flow" }, 200),
    node(INTENT, "text", "改造意图", { content: "旧意图", prompt: "旧意图", lapianIntent: true }, 300),
    node(CONFIG_TEXT, "config", "提示词生成配置", { generationMode: "text", model: "default::m", lapianSourceId: LAPIAN, composerContent: `拉片：@[node:${LAPIAN}]\n指令：@[node:${INS}]\n意图：@[node:${INTENT}]` }, 400),
    node(FINAL, "text", "终稿（占位）", { fontSize: 14, model: "default::m", textCount: 1 }, 500),
    node(CONFIG_VIDEO, "config", "视频生成配置", { generationMode: "video", model: "k::sd2.5", size: "720x1280", composerContent: `提示词：@[node:${FINAL}]` }, 600),
    node(OUT_VIDEO, "video", "生成结果", { content: "blob:out", storageKey: "video:out", status: "idle" }, 700),
    node(SLOT_FEMALE, "image", "资产位·人物·女性A", { content: "blob:sf", storageKey: "image:sf", assetSlotSourceId: LAPIAN, assetSlotOrder: 0, assetSlotName: "女性A", assetSlotType: "人物", assetSlot: { desc: "原片模特，红色风衣，短发", span: "00:00-02.00" } }, 800),
    node(SLOT_PERFUME, "image", "资产位·主体·香水", { content: "blob:sp", storageKey: "image:sp", assetSlotSourceId: LAPIAN, assetSlotOrder: 1, assetSlotName: "香水", assetSlotType: "主体", assetSlot: { desc: `细长圆柱体紫色表面，银色金属盖`, span: "02.00-05.00" } }, 900),
];
const fixtureConnections = () => [
    { id: "e1", fromNodeId: VIDEO, toNodeId: LAPIAN },
    { id: "e2", fromNodeId: LAPIAN, toNodeId: CONFIG_TEXT },
    { id: "e3", fromNodeId: INS, toNodeId: CONFIG_TEXT },
    { id: "e4", fromNodeId: INTENT, toNodeId: CONFIG_TEXT },
    { id: "e5", fromNodeId: CONFIG_TEXT, toNodeId: FINAL },
    { id: "e6", fromNodeId: FINAL, toNodeId: CONFIG_VIDEO },
    { id: "e7", fromNodeId: CONFIG_VIDEO, toNodeId: OUT_VIDEO },
];
const libraryNodes = () => [
    node(LIB_FEMALE, "image", "客户模特 女性A 定妆.png", { content: "blob:lf", storageKey: "image:lf", mimeType: "image/png" }, 1200),
    node(LIB_PERFUME, "image", "客户款 香水 磨砂瓶.png", { content: "blob:lp", storageKey: "image:lp", mimeType: "image/png" }, 1300),
    node(LIB_DECOY, "image", "城市夜景.jpg", { content: "blob:lc", storageKey: "image:lc", mimeType: "image/jpeg" }, 1400),
    node("lib-female-b", "image", "备选 女性A 侧脸.png", { content: "blob:lfb", storageKey: "image:lfb", mimeType: "image/png" }, 1500),
];

const projectShell = (nodes, connections) => ({ id: "proj-p7", title: "P7 夹具项目", nodes, connections, backgroundMode: "lines", viewport: { x: 0, y: 0, k: 1 } });
const fixture = workflow.extractWorkflowTemplate({
    sourceProject: projectShell(fixtureNodes(), fixtureConnections()),
    selectedNodeIds: [VIDEO, LAPIAN, INS, INTENT, CONFIG_TEXT, FINAL, CONFIG_VIDEO, OUT_VIDEO, SLOT_FEMALE, SLOT_PERFUME],
    templateId: "p7demo",
    name: "p7fixture",
    createdAt: "2026-11-13T00:00:00.000Z",
});
check("P0 夹具模板自足（无悬空引用）", workflow.validateTemplateProject(fixture.project.nodes, fixture.project.connections).length === 0, JSON.stringify(workflow.validateTemplateProject(fixture.project.nodes, fixture.project.connections).slice(0, 3)));
check("P0 夹具模板三个资产槽在册（③ 槽按 assetSlotOrder）", fixture.template.slots[2].targets.length === 2 && fixture.template.slots[2].targets.map((item) => item.nodeId).join(",") === `${SLOT_FEMALE},${SLOT_PERFUME}`, JSON.stringify(fixture.template.slots[2].targets.map((item) => item.nodeId)));

const makeCanvas = (seed = libraryNodes()) => {
    let snapshot = { projectId: "proj-p7", title: "P7 夹具项目", nodes: [...seed], connections: [], selectedNodeIds: [], viewport: { x: 0, y: 0, k: 1 } };
    return {
        apply: async (batchOps) => {
            const next = ops.applyCanvasAgentOps(snapshot, batchOps);
            snapshot = { ...snapshot, nodes: next.nodes, connections: next.connections, selectedNodeIds: next.selectedNodeIds, viewport: next.viewport };
            return next;
        },
        nodes: () => snapshot.nodes,
        connections: () => snapshot.connections,
        snapshot: () => snapshot,
    };
};

const materials = (tag, extra = {}) => ({
    sourceVideo: { storageKey: `video:new-${tag}`, title: `新素材-${tag}.mp4`, content: `blob:http://127.0.0.1:3000/new-${tag}`, mimeType: "video/mp4", bytes: 5000 + tag.length, durationMs: 12000 },
    intentText: `把主体换成客户模特（${tag}）`,
    optionalSegments: "A",
    ...extra,
});
const planInput = (index, mats, vetoes) => ({ template: fixture.template, templateNodes: fixture.project.nodes, templateConnections: fixture.project.connections, materials: mats, instanceIndex: index, vetoes, unnamedCaption: "未命名参考图" });

/** 规划 → 匹配 → 规划（带匹配结果）：生产/探针同一条路（不复制 P3 落库逻辑）。 */
function matchPlan({ index, tag, canvas, vetoes, extra = {} }) {
    const dry = instancer.planWorkflowInstance(planInput(index, materials(tag, extra), vetoes));
    const nodes = [...canvas.nodes(), ...dry.nodes];
    const slots = matcher.listInstanceAssetSlots({ plan: dry, nodes });
    const library = matcher.collectAssetLibraryCandidates({ nodes, lapianNodeId: dry.lapianNodeId });
    const match = matcher.matchInstanceAssetSlots({ slots, candidates: library.candidates, vetoes, instanceId: dry.instanceId, templateId: dry.templateId, instanceIndex: index });
    const plan = instancer.planWorkflowInstance(planInput(index, materials(tag, { ...extra, assetAttach: match.attaches }), vetoes));
    return { dry, plan, slots, library, match };
}

function makeLapianFetch(tag, markdown) {
    const calls = [];
    const fetchImpl = async (url, init) => {
        calls.push({ url, method: String(init?.method || "GET").toUpperCase() });
        const json = (payload) => ({ ok: true, status: 200, json: async () => payload });
        if (url === "/api/video/upload") return json({ id: `task-p7-${tag}` });
        if (url === "/api/lapian/run") return json({ job: `job-p7-${tag}` });
        if (url === `/api/job/job-p7-${tag}`) return json({ status: "done" });
        if (url === `/api/lapian/${encodeURIComponent(`task-p7-${tag}`)}`) return json({ markdown });
        throw new Error(`stub 未覆盖：${url}`);
    };
    return { fetchImpl, calls };
}

async function materialize({ index, tag, canvas, vetoes, extra = {} }) {
    const matched = matchPlan({ index, tag, canvas, vetoes, extra });
    const stub = makeLapianFetch(tag, `# 本次拉片正文 · ${tag} · 唯一标记 ${tag.toLowerCase()}7f3a\n## 一 剧情\n客户模特转身\n## 二 镜头\n推近`);
    const created = await instancer.materializeWorkflowInstance(
        { ...planInput(index, materials(tag, { ...extra, assetAttach: matched.match.attaches }), vetoes), lapianConfig: { lapianDefaultModel: "", channels: [] }, contentWait: { sleep: async () => {} } },
        { getBlob: async () => new Blob(["new-video-bytes"], { type: "video/mp4" }), fetch: stub.fetchImpl, applyOps: canvas.apply, existingNodes: canvas.nodes },
    );
    return { ...matched, created, stub };
}

function makeProjectionFetch() {
    const calls = [];
    const fetchImpl = async (url) => {
        calls.push(url);
        const hit = String(url).match(/\/api\/render\/([^/]+)\/([^?]+)\?optional=(.*)$/);
        if (!hit) return { ok: false, status: 404, json: async () => ({ error: `stub 不认：${url}` }) };
        const optional = decodeURIComponent(hit[3] || "");
        const text = [`# 本次投影正文 · ${hit[1]}`, `可选段：${optional || "（全关）"}`, optional ? `可选段内容 ${optional}：客户模特在雨夜街头转身。` : "正片结构：开场/推进/收尾。"].join("\n");
        return { ok: true, status: 200, json: async () => ({ text }) };
    };
    return { fetchImpl, calls };
}

let generationSeq = 0;
function makeGenerationChain(canvas, opts) {
    const calls = [];
    return {
        calls,
        run: async (input) => {
            calls.push({ nodeId: input.nodeId, mode: input.mode });
            const context = genctx.buildNodeGenerationContext(input.nodeId, canvas.nodes(), canvas.connections(), input.prompt);
            generationSeq += 1;
            const childNodeId = `${opts.prefix}-out-${generationSeq}`;
            // 注入已知异常（TAILMARK 式：只有尾段带 MARK），模拟「模型产出里留了原物核心词」
            const content = `【本次产物 ${opts.token}-${generationSeq}】\n${context.prompt}\n尾段残留：${MARK}的香水瓶静置桌面，镜头缓慢推近。`;
            await canvas.apply([
                { type: "add_node", id: childNodeId, nodeType: "text", title: "终稿（本次）", position: { x: 4000 + generationSeq * 100, y: 0 }, width: 320, height: 240, metadata: { content, prompt: content, status: "success" } },
                { type: "connect_nodes", fromNodeId: input.nodeId, toNodeId: childNodeId },
            ]);
            return { childNodeId, context };
        },
    };
}

const PASS_ITEMS = [{ 项: "次序对账", 结论: "PASS" }, { 项: "绑图行残留", 结论: "PASS" }, { 项: "锁点对账", 结论: "PASS" }, { 项: "组绑定一致性", 结论: "PASS" }];
/**
 * hub 客户端替身：**复刻端点契约**（ingest/uploadAsset/getTask/saveRevision/approve…），
 * 自检条目是**夹具**（真自检由 python 侧探针对同一份字节复核）；保存时真算审批快照（stableDigest 同源）。
 */
function makeHubStub(mode) {
    const calls = [];
    const texts = [];
    let text = "";
    let saves = 0;
    const client = {
        async ingest(input) {
            calls.push({ kind: "ingest", filename: input.filename, localId: "auto-9101" });
            return { localId: "auto-9101", bridgeId: "bridge-p7-01", result: "OK" };
        },
        async uploadAsset(input) {
            calls.push({ kind: "asset", filename: input.filename, localId: input.localId });
            return { ref: `local:p7/${input.filename}`, source: "local" };
        },
        async getTask() {
            calls.push({ kind: "getTask" });
            return { state: "awaiting_approval", bridgeId: "bridge-p7-01" };
        },
        async saveRevision(input) {
            saves += 1;
            texts.push(input.text);
            const tainted = input.text.includes(MARK);
            const failRound = mode === "alwaysfail" ? true : mode === "failonce" ? tainted : false;
            calls.push({ kind: "prompt", chars: input.text.length, expectText: input.expectText === undefined ? undefined : input.expectText === null ? null : input.expectText.length, tainted, failRound });
            text = input.text;
            const finding = { 项: "绑图行残留", 结论: "FAIL", 证据: `已传图行的原物核心词「${MARK}」仍留在正文`, 词: MARK, 行号: input.text.split("\n").length, 原文: `尾段残留：${MARK}的香水瓶静置桌面，镜头缓慢推近。`, 说明: { 结论人话: `提示词里还留着 ${MARK}`, 发现: [{ 词: MARK, 行号: input.text.split("\n").length }], 统计: { 词数: 1 } } };
            /** 读路径汇总的「拆解门禁」合成条目：生产者三态里的 WARN（告警，不是 FAIL）。 */
            const warnItem = { 项: "拆解门禁", 结论: "WARN", 证据: "告警：镜头 3 缺运镜词（绿项 3/4）" };
            return {
                state: "awaiting_approval",
                approvalSnapshot: `snap-p7-${workflow.stableDigest(input.text).slice(0, 8)}`,
                promptEditedAt: "2026-11-13T00:00:00.000Z",
                selfcheck: failRound ? [PASS_ITEMS[0], finding, PASS_ITEMS[2], PASS_ITEMS[3]] : mode === "warn" ? [...PASS_ITEMS, warnItem] : PASS_ITEMS,
            };
        },
        async approve() {
            calls.push({ kind: "approve" });
            return { state: "approved", snapshot_sha256: "snap-p7" };
        },
        async rollback() {
            calls.push({ kind: "rollback" });
            return { state: "awaiting_assets" };
        },
        async revoke() {
            calls.push({ kind: "revoke" });
            return { state: "awaiting_approval" };
        },
    };
    return { client, calls, texts, text: () => text, saves: () => saves };
}

const frozenOf = (canvas) => repair.frozenSnapshotOf(canvas.nodes(), "topo-digest");
const frozenSig = (canvas) =>
    JSON.stringify(
        canvas
            .nodes()
            .filter((item) => item.type === "text" || item.type === "config")
            .map((item) => ({ id: item.id, tpl: item.metadata?.promptTemplate || "", composer: item.metadata?.composerContent || "" })),
    );

// ---------------------------------------------------------------- B. 自动匹配端到端
const canvasB = makeCanvas();
const matchedA = await materialize({ index: 1, tag: "A", canvas: canvasB });
check("B 实例 1 落库（created）", matchedA.created.status === "created", JSON.stringify(matchedA.created.incomplete || {}));
check("B 自动匹配两槽都选定（不是留空）", matchedA.match.attaches.length === 2 && matchedA.match.slots.every((slot) => slot.status === "matched"), JSON.stringify(matchedA.match.slots.map((slot) => `${slot.slotKey}:${slot.status}`)));
check(
    "B 每槽匹配都带理由（来源槽语义 + 命中词 + 分数 + 候选标题）",
    matchedA.match.slots.every((slot) => slot.reason?.rule === "slot_semantics_keyword" && slot.reason.source.includes("第") && slot.reason.matchedTerms.length > 0 && slot.reason.score >= 6 && String(slot.reason.candidateTitle || "").length > 0),
    JSON.stringify(matchedA.match.slots.map((slot) => slot.reason)),
);
check("B 匹配按语义选中对的图（女性A 槽只可能是两张女性A 图之一，香水槽=香水图，夜景永不被选）", [LIB_FEMALE, "lib-female-b"].includes(matchedA.match.slots[0].imageNodeId) && matchedA.match.slots[1].imageNodeId === LIB_PERFUME && !matchedA.match.slots.some((slot) => slot.imageNodeId === LIB_DECOY), JSON.stringify(matchedA.match.slots.map((slot) => `${slot.slotKey}→${slot.imageNodeId}`)));
const matchedB = await materialize({ index: 2, tag: "B", canvas: canvasB });
const imageA = matchedA.created.plan.assetAttaches.map((attach) => attach.imageNodeId);
const imageB = matchedB.created.plan.assetAttaches.map((attach) => attach.imageNodeId);
check("B 多实例互不串配（实例 2 不拿实例 1 的挂图副本）", imageA.length === 2 && imageB.length === 2 && imageB.every((id) => !imageA.includes(id)), `A=${imageA.join(",")} B=${imageB.join(",")}`);
check("B 每实例槽各挂自己的图（画布上互不覆盖）", canvasB.nodes().find((item) => item.id === matchedA.created.plan.nodeIdMap[SLOT_FEMALE])?.metadata?.attachedImageId === imageA[0] && canvasB.nodes().find((item) => item.id === matchedB.created.plan.nodeIdMap[SLOT_FEMALE])?.metadata?.attachedImageId === imageB[0]);
check("B 候选被扣掉的原因可读（对照副本/槽节点/别实例已占）", matchedB.library.excluded.length > 0 && matchedB.library.excluded.every((item) => String(item.reason || "").length > 0), JSON.stringify(matchedB.library.excluded.slice(0, 3)));
const orderCheckB = matcher.verifyAssetRequestOrder({ plan: matchedA.match, lapianNodeId: matchedA.created.plan.lapianNodeId, nodes: canvasB.nodes(), manifestPrefix: "本次参考图：" });
check("B 请求图序 = 槽序（P4 回归）", orderCheckB.ok === true, orderCheckB.problems.join(" | "));
check("B 计划自检零问题", matcher.verifyAssetMatchPlan({ plan: matchedA.match, nodes: canvasB.nodes() }).length === 0, JSON.stringify(matcher.verifyAssetMatchPlan({ plan: matchedA.match, nodes: canvasB.nodes() })));

const projectionA = makeProjectionFetch();
const generationA = makeGenerationChain(canvasB, { prefix: "p7a", token: "P7TOKENA" });
const hubA = makeHubStub("failonce");
const matsA = materials("A", { assetAttach: matchedA.match.attaches });
const runA = await runner.runWorkflowInstance(
    { plan: matchedA.created.plan, materials: matsA, lapian: { taskId: "task-p7-A", jobId: "job-p7-A" }, batchId: "p7-batch" },
    {
        nodes: canvasB.nodes,
        connections: canvasB.connections,
        applyOps: canvasB.apply,
        runGeneration: generationA.run,
        fetchProjection: projectionA.fetchImpl,
        hub: hubA.client,
        repair: { repairer: repair.createTermStrippingRepairer() },
        getBlob: async (storageKey) => new Blob([`bytes-of-${storageKey}`], { type: String(storageKey).startsWith("image:") ? "image/png" : "video/mp4" }),
        newRunToken: () => "runTokenA",
        now: () => "2026-11-13T00:00:00.000Z",
    },
);
check("B 实例 1 全链无 failure", !runA.failure, JSON.stringify(runA.failure));
check("B 五槽代入校验 5/5 PASS", runA.slotChecks.length === 5 && runA.slotChecks.every((item) => item.ok), JSON.stringify(runA.slotChecks.filter((item) => !item.ok)));
check("B 组装只调文本链一次（零付费）", runA.generationCalls.length === 1 && runA.generationCalls[0].mode === "text", JSON.stringify(runA.generationCalls));
check("B 付费视频创建 = 0", runA.paidVideoCreations === 0, String(runA.paidVideoCreations));
check("B ops 里零 run_generation", runA.appliedOps.every((op) => op.type !== "run_generation"));
check("B P0b 闸复验：这些 ops 过付费提取 = 0 条", paid.collectPaidActions("canvas_apply_ops", { ops: runA.appliedOps }, { projectId: "proj-p7", title: "P7", nodes: canvasB.nodes(), connections: canvasB.connections(), selectedNodeIds: [], viewport: { x: 0, y: 0, k: 1 } }).length === 0);
check("B 挂图按槽序进 hub 资产（图号/图序一致）", runA.identity?.hub?.assets?.length === 2 && runA.identity.hub.assets.every((asset, i) => asset.imageNodeId === imageA[i]), JSON.stringify(runA.identity?.hub?.assets || []));

// ---------------------------------------------------------------- C. 自修第 1 轮（同修订可见）
const repairRecord = runA.repair;
check("C 首检 FAIL 触发自修（不是直接交人）", Boolean(repairRecord) && repairRecord.outcome === "passed", JSON.stringify(repairRecord?.escalation || repairRecord?.outcome));
check("C 只花 1 轮就修好（计数=1，不按 finding 计）", repairRecord?.rounds.length === 1 && repairRecord.rounds[0].findingsBefore === 1 && repairRecord.rounds[0].findingsAfter === 0, JSON.stringify(repairRecord?.rounds?.map((r) => [r.findingsBefore, r.findingsAfter])));
check("C 自修删的正是 finding 给的词（限定范围，不自造修法）", JSON.stringify(repairRecord?.rounds[0].removedTerms) === JSON.stringify([MARK]), JSON.stringify(repairRecord?.rounds[0].removedTerms));
const productNodeA = canvasB.nodes().find((item) => item.id === runA.assembly?.productNodeId);
const savedTextA = hubA.text();
check("C 同修订可见：hub 保存的正文 == 画布产物正文（逐字节）", savedTextA.length > 0 && savedTextA === String(productNodeA?.metadata?.content || "").trim(), `hub=${savedTextA.length} canvas=${String(productNodeA?.metadata?.content || "").trim().length}`);
check("C 同修订可见：身份记录的修订 digest = 保存正文 digest", runA.identity?.hub?.revision.digest === workflow.stableDigest(savedTextA), `${runA.identity?.hub?.revision.digest} vs ${workflow.stableDigest(savedTextA)}`);
check("C 同修订可见：自修轮次里记的 digestAfter 也与同一份字节一致", repairRecord?.rounds[0].digestAfter === workflow.stableDigest(savedTextA));
check("C 新自检结果绑定同一修订（末次自检 4 项全 PASS，失败数 0）", runA.hub?.selfcheckFailures === 0 && runA.hub?.selfcheck?.length === 4 && runA.hub.selfcheck.every((item) => item.结论 !== "FAIL"), JSON.stringify(runA.hub?.selfcheck));
check("C 修好后仍停在待批（不自动进下一步、不自动豁免）", runA.status === "awaiting_approval", runA.status);
check("C 保存走了乐观锁（第二次保存带 expect_text=改前正文）", hubA.calls.filter((call) => call.kind === "prompt").length === 2 && hubA.calls.filter((call) => call.kind === "prompt")[1].expectText > 0, JSON.stringify(hubA.calls.filter((call) => call.kind === "prompt")));
check("C 自修不碰冻结项：指令模板 id/composer 骨架零变化", JSON.stringify(repairRecord?.frozen?.problems || []) === "[]" && repairRecord?.frozen?.unchanged === true, JSON.stringify(repairRecord?.frozen));
check("C 自修付费动作恒 0", repairRecord?.paidActions === 0, String(repairRecord?.paidActions));
check("C 自修痕迹可查（每轮改了什么/为什么/结果 → P8 数据基础）", Boolean(repairRecord?.rounds[0].reason) && repairRecord.rounds[0].scope.join("").includes("行") && String(repairRecord.rounds[0].approvalSnapshot || "").length > 0, JSON.stringify({ reason: repairRecord?.rounds[0].reason, scope: repairRecord?.rounds[0].scope }));
// 跨语言交接：真自检复核用同一份字节（改写前/后两次保存的正文原样落盘）
writeFileSync(path.join(outDir, "repair-before.txt"), String(hubA.texts[0] ?? ""), "utf8");
writeFileSync(path.join(outDir, "repair-after.txt"), savedTextA, "utf8");
writeFileSync(
    path.join(outDir, "repair-findings-before.json"),
    JSON.stringify({ mark: MARK, finding: { 项: "绑图行残留", 结论: "FAIL", 词: MARK, 原文: `尾段残留：${MARK}的香水瓶静置桌面，镜头缓慢推近。` }, saves: hubA.texts.length, beforeChars: String(hubA.texts[0] ?? "").length, afterChars: savedTextA.length }, null, 2),
    "utf8",
);

// ---------------------------------------------------------------- D. 2 轮上限 → 交人
const canvasD = makeCanvas();
const matchedD = await materialize({ index: 3, tag: "D", canvas: canvasD });
const hubD = makeHubStub("alwaysfail");
/** 修不好但每轮都动、理由各异的执行体（模拟 Agent 修错方向）：用于验「2 轮上限 → 升级」。 */
const ineffective = (input) => ({ text: `${input.text}\n补充说明 ${input.round}：镜头保持平稳。`, reason: `第 ${input.round} 轮把镜头描述改写为平稳推进（没删到违规词）`, removedTerms: [], changedLines: [] });
const runD = await runner.runWorkflowInstance(
    { plan: matchedD.created.plan, materials: materials("D", { assetAttach: matchedD.match.attaches }), lapian: { taskId: "task-p7-D", jobId: "job-p7-D" }, batchId: "p7-batch" },
    {
        nodes: canvasD.nodes,
        connections: canvasD.connections,
        applyOps: canvasD.apply,
        runGeneration: makeGenerationChain(canvasD, { prefix: "p7d", token: "P7TOKEND" }).run,
        fetchProjection: makeProjectionFetch().fetchImpl,
        hub: hubD.client,
        repair: { repairer: ineffective, maxRounds: 5 },
        getBlob: async () => new Blob(["bytes"], { type: "image/png" }),
        newRunToken: () => "runTokenD",
        now: () => "2026-11-13T00:00:00.000Z",
    },
);
const recordD = runD.repair;
check("D 2 轮仍 FAIL → 实例=需人工（needs_human）", runD.status === "needs_human", runD.status);
check("D 上限被夹回 2（D11 是上限不是旋钮）", recordD?.maxRounds === 2 && recordD?.rounds.length === 2, JSON.stringify({ max: recordD?.maxRounds, rounds: recordD?.rounds.length }));
check("D 升级码 U1 + 原因可读", recordD?.outcome === "escalated" && recordD?.escalation?.code === "U1" && String(recordD?.escalation?.detail || "").length > 0, JSON.stringify(recordD?.escalation));
check("D 不自动豁免/不自动通过（hub 上仍是待批，自检仍有 FAIL）", hubD.calls.some((call) => call.kind === "prompt") && !hubD.calls.some((call) => call.kind === "approve") && runD.hub?.selfcheckFailures > 0, JSON.stringify({ approve: hubD.calls.filter((c) => c.kind === "approve").length, fails: runD.hub?.selfcheckFailures }));
check("D 升级状态不落画布（派生层：hub 待批 + 自修升级 → needs_human/R4a）", (() => {
    const derived = batchStatus.deriveInstanceStatus({ member: runD.batchMember, nodes: canvasD.nodes(), hubState: "awaiting_approval" });
    return derived.status === "needs_human" && derived.rule === "R4a", JSON.stringify(derived);
})(), JSON.stringify(runD.batchMember?.repair || null));
check("D 批次聚合口径：成员 needs_human → 批次 outcome=needs_human（不静默当 in_progress）", (() => {
    const view = batchStatus.deriveBatchStatus({ batch: { batchId: "p7-batch", templateId: "p7demo", title: "P7 批次", members: [runD.batchMember] }, nodes: canvasD.nodes(), hubStates: { [runD.batchMember.instanceId]: "awaiting_approval" } });
    return view.outcome === "needs_human" && view.counts.needs_human === 1, JSON.stringify({ outcome: view.outcome, counts: view.counts });
})(), JSON.stringify(batchStatus.deriveBatchStatus({ batch: { batchId: "p7-batch", templateId: "p7demo", title: "P7 批次", members: [runD.batchMember] }, nodes: canvasD.nodes(), hubStates: { [runD.batchMember.instanceId]: "awaiting_approval" } }).counts));
check("D 修好的那个实例在派生层仍是 awaiting_approval（不误判 needs_human）", (() => {
    const derived = batchStatus.deriveInstanceStatus({ member: runA.batchMember, nodes: canvasB.nodes(), hubState: "awaiting_approval" });
    return derived.status !== "needs_human", JSON.stringify(derived);
})());
check("D 付费调用恒 0（自修不跨付费闸）", runD.paidVideoCreations === 0 && recordD?.paidActions === 0, `${runD.paidVideoCreations}/${recordD?.paidActions}`);

// ---------------------------------------------------------------- H. 告警≠未通过（生产者三态，真编排器层）
const canvasH = makeCanvas();
const matchedH = await materialize({ index: 5, tag: "H", canvas: canvasH });
const hubH = makeHubStub("warn");
const runH = await runner.runWorkflowInstance(
    { plan: matchedH.created.plan, materials: materials("H", { assetAttach: matchedH.match.attaches }), lapian: { taskId: "task-p7-H", jobId: "job-p7-H" }, batchId: "p7-batch" },
    {
        nodes: canvasH.nodes,
        connections: canvasH.connections,
        applyOps: canvasH.apply,
        runGeneration: makeGenerationChain(canvasH, { prefix: "p7h", token: "P7TOKENH" }).run,
        fetchProjection: makeProjectionFetch().fetchImpl,
        hub: hubH.client,
        repair: { repairer: repair.createTermStrippingRepairer() },
        getBlob: async () => new Blob(["bytes"], { type: "image/png" }),
        newRunToken: () => "runTokenH",
        now: () => "2026-11-13T00:00:00.000Z",
    },
);
check("H hub 只回 WARN 告警（三态里的 WARN）→ 不触发自修（告警不是可自修对象）", runH.repair === undefined && hubH.saves() === 1, JSON.stringify({ repair: runH.repair?.outcome, saves: hubH.saves() }));
check("H 告警不改变实例状态：仍待批（不冒 needs_human、不空耗轮次）", runH.status === "awaiting_approval" && !runH.warnings.some((line) => line.includes("自修")), JSON.stringify({ status: runH.status, warnings: runH.warnings }));

// ---------------------------------------------------------------- E. 一键否决闭环
const canvasV = makeCanvas();
const matchedV = await materialize({ index: 4, tag: "V", canvas: canvasV });
const veto = matcher.vetoAssetMatch({ match: matchedV.match.slots[0], templateId: matchedV.created.plan.templateId, instanceId: matchedV.created.plan.instanceId, instanceIndex: 4, vetoedAt: "2026-11-13T00:00:00.000Z", note: "所有者：这张不用" });
check("E 否决记录三键齐（实例+槽+图）", veto.instanceId === matchedV.created.plan.instanceId && veto.slotNodeId === matchedV.match.slots[0].slotNodeId && veto.rejectedImageId === matchedV.match.slots[0].imageNodeId, JSON.stringify(veto));
const rematch = matcher.matchInstanceAssetSlots({ slots: matchedV.slots, candidates: matchedV.library.candidates, vetoes: [veto], instanceId: matchedV.created.plan.instanceId, templateId: matchedV.created.plan.templateId, instanceIndex: 4 });
check("E 否决后重新匹配：换替代图（不复用被否决那张）", rematch.slots[0].imageNodeId !== veto.rejectedImageId && rematch.slots[0].reason.vetoedImageIds.includes(veto.rejectedImageId), JSON.stringify({ now: rematch.slots[0].imageNodeId, vetoed: veto.rejectedImageId }));
const staleAttach = matchedV.match.attaches.map((attach) => ({ ...attach }));
const replay = instancer.planWorkflowInstance(planInput(4, materials("V", { assetAttach: staleAttach }), [veto]));
check("E 旧匹配重放（幂等场景）：计划层丢弃否决过的挂图", replay.vetoedAttaches.length === 1 && replay.vetoedAttaches[0].imageNodeId === replay.nodeIdMap[veto.rejectedImageId] && String(canvasV.nodes().find((item) => item.id === replay.vetoedAttaches[0].imageNodeId)?.metadata?.assetLibrarySourceId || veto.rejectedImageId) === veto.rejectedImageId, JSON.stringify({ dropped: replay.vetoedAttaches, vetoed: veto.rejectedImageId }));
check("E 旧匹配重放：否决图不进挂图清单（请求里没有它）", !replay.assetAttaches.some((attach) => attach.imageNodeId === veto.rejectedImageId) && replay.assetAttaches.length === 1, JSON.stringify(replay.assetAttaches.map((a) => a.imageNodeId)));
check("E 旧匹配重放：warnings 明示否决（不静默）", replay.warnings.some((line) => line.includes("否决")), replay.warnings.join(" | "));
const replayAgain = instancer.planWorkflowInstance(planInput(4, materials("V", { assetAttach: staleAttach }), [veto]));
check("E 幂等重放一致（两次计划结论逐字相同）", JSON.stringify(replayAgain.vetoedAttaches) === JSON.stringify(replay.vetoedAttaches) && JSON.stringify(replayAgain.assetAttaches) === JSON.stringify(replay.assetAttaches));
const assemblyReplay = runner.planInstanceAssembly({ plan: replay, nodes: canvasV.nodes(), connections: canvasV.connections() });
const vetoChecks = runner.verifySlotSubstitution({ plan: replay, materials: materials("V", { assetAttach: staleAttach }), nodes: canvasV.nodes(), connections: canvasV.connections(), assembly: assemblyReplay, lapianText: "# 拉片正文", vetoes: [veto] });
check("E 槽③判据拦下「否决过的匹配又进请求」", vetoChecks.find((item) => item.slotKind === "assetAttach")?.ok === false && String(vetoChecks.find((item) => item.slotKind === "assetAttach")?.detail || "").includes("否决"), JSON.stringify(vetoChecks.filter((item) => !item.ok)));
check("E 否决记录持久化口径：upsert 幂等 + 导出透传", (() => {
    const twice = matcher.upsertAssetVeto(matcher.upsertAssetVeto([], veto), { ...veto, vetoedAt: "2026-11-13T00:05:00.000Z" });
    return twice.length === 1 && matcher.acceptWorkflowAssetVetoes([veto, null, { instanceId: "" }]).length === 1;
})());

// ---------------------------------------------------------------- F. 真实样本零回归（P4 路径不变）
const storePayload = JSON.parse(readFileSync(storePath, "utf8"));
const realProjects = (storePayload.state && storePayload.state.projects) || [];
const realProject = realProjects.find((item) => item.id === projectKey) || realProjects.find((item) => item.title === projectKey);
check("F 找到真实样本项目", Boolean(realProject), realProjects.map((item) => item.title).join(","));
const realSnapshotBefore = JSON.stringify(realProject || {}).length;
if (realProject) {
    const rn = realProject.nodes || [];
    const rc = realProject.connections || [];
    const byId = new Map(rn.map((item) => [item.id, item]));
    const incoming = (id) => rc.filter((edge) => edge.toNodeId === id).map((edge) => edge.fromNodeId);
    const outgoing = (id) => rc.filter((edge) => edge.fromNodeId === id).map((edge) => edge.toNodeId);
    const lapians = rn.filter((item) => item.type === "lapian" && item.metadata?.jobId);
    const chainRoot = lapians.find((item) => rn.some((candidate) => candidate.type === "config" && candidate.metadata?.generationMode === "text" && candidate.metadata?.lapianSourceId === item.id));
    check("F 真实样本有已跑通的拉片链根", Boolean(chainRoot), lapians.map((item) => item.id).join(","));
    if (chainRoot) {
        const configText = rn.find((item) => item.type === "config" && item.metadata?.generationMode === "text" && item.metadata?.lapianSourceId === chainRoot.id);
        const sourceVideo = incoming(chainRoot.id).map((id) => byId.get(id)).find((item) => item && item.type === "video" && item.metadata?.storageKey);
        const instruction = incoming(configText.id).map((id) => byId.get(id)).find((item) => item && item.type === "text" && item.metadata?.promptTemplate);
        const intent = incoming(configText.id).map((id) => byId.get(id)).find((item) => item && item.type === "text" && item.metadata?.lapianIntent);
        const finalText = outgoing(configText.id).map((id) => byId.get(id)).find((item) => item && item.type === "text");
        const configVideo = outgoing(finalText.id).map((id) => byId.get(id)).find((item) => item && item.type === "config" && item.metadata?.generationMode === "video");
        const skeletonIds = [sourceVideo?.id, chainRoot.id, instruction?.id, intent?.id, configText?.id, finalText?.id, configVideo?.id].filter(Boolean);
        const realSlots = rn.filter((item) => item.metadata?.assetSlotSourceId === chainRoot.id && item.metadata?.attachedImageId);
        const slotImageIds = realSlots.map((slot) => slot.metadata?.attachedImageId).filter((id) => id && byId.has(id));
        const grafts = rn.filter((item) => item.metadata?.assetGraftForSlotId && realSlots.some((slot) => slot.id === item.metadata.assetGraftForSlotId));
        const graftImageIds = grafts.map((item) => item.metadata?.assetGraftOfImageId).filter((id) => id && byId.has(id));
        const fullIds = [...new Set([...skeletonIds, ...realSlots.map((slot) => slot.id), ...slotImageIds, ...grafts.map((item) => item.id), ...graftImageIds])];
        const realTemplate = workflow.extractWorkflowTemplate({ sourceProject: realProject, selectedNodeIds: fullIds, templateId: "p7real", name: "真实样本全量模板", createdAt: "2026-11-13T00:00:00.000Z" });
        check("F 真实样本全量模板自足", workflow.validateTemplateProject(realTemplate.project.nodes, realTemplate.project.connections).length === 0);
        const realCanvas = makeCanvas([]);
        const realMats = materials("REAL", { assetAttach: realSlots.map((slot) => ({ slotNodeId: slot.id, imageNodeId: slot.metadata.attachedImageId })) });
        const realStub = makeLapianFetch("REAL", "# 本次拉片正文 · REAL\n## 一 剧情\n客户模特转身");
        const realCreated = await instancer.materializeWorkflowInstance(
            { template: realTemplate.template, templateNodes: realTemplate.project.nodes, templateConnections: realTemplate.project.connections, materials: realMats, instanceIndex: 1, lapianConfig: { lapianDefaultModel: "", channels: [] }, contentWait: { sleep: async () => {} }, unnamedCaption: "未命名参考图" },
            { getBlob: async () => new Blob(["bytes"], { type: "video/mp4" }), fetch: realStub.fetchImpl, applyOps: realCanvas.apply, existingNodes: realCanvas.nodes },
        );
        check("F 真实样本实例化成功（P3 零回归）", realCreated.status === "created", JSON.stringify(realCreated.incomplete || {}));
        const realRun = await runner.runWorkflowInstance(
            { plan: realCreated.plan, materials: realMats, lapian: { taskId: "task-p7-REAL", jobId: "job-p7-REAL" } },
            { nodes: realCanvas.nodes, connections: realCanvas.connections, applyOps: realCanvas.apply, runGeneration: makeGenerationChain(realCanvas, { prefix: "p7real", token: "P7REAL" }).run, fetchProjection: makeProjectionFetch().fetchImpl, hub: makeHubStub("clean").client, getBlob: async () => new Blob(["bytes"], { type: "video/mp4" }), newRunToken: () => "runTokenREAL" },
        );
        check("F 真实样本跑通到待批（不给 repair/vetoes = P4 行为不变）", !realRun.failure && realRun.status === "awaiting_approval", JSON.stringify(realRun.failure || realRun.status));
        check("F 真实样本五槽 5/5、付费 0、无 repair 记录（未给执行体就不自修）", realRun.slotChecks.every((item) => item.ok) && realRun.paidVideoCreations === 0 && realRun.repair === undefined, JSON.stringify({ repair: realRun.repair, paid: realRun.paidVideoCreations }));
    }
    check("F 真实样本只读（跑完 store 字节数不变）", JSON.stringify(realProject).length === realSnapshotBefore);
}

// ---------------------------------------------------------------- G. store / 导出 / 画布洁净
const stateKeys = Object.keys(useCanvasStore.getState());
check("G store 既有方法面零破坏（7 个方法都在）", ["createProject", "importProject", "openProject", "renameProject", "deleteProjects", "replaceProjects", "updateProject"].every((name) => typeof useCanvasStore.getState()[name] === "function"), JSON.stringify(stateKeys));
check("G store 记录项目级否决（workflowAssetVetoes 字段可写可读）", (() => {
    const id = useCanvasStore.getState().importProject({ title: "P7 veto 夹具", nodes: [], connections: [], createdAt: "2026-11-13T00:00:00.000Z" });
    useCanvasStore.getState().updateProject(id, { workflowAssetVetoes: [veto] });
    const project = useCanvasStore.getState().projects.find((item) => item.id === id);
    return (project?.workflowAssetVetoes || []).length === 1 && project.workflowAssetVetoes[0].rejectedImageId === veto.rejectedImageId;
})(), JSON.stringify(stateKeys.slice(0, 12)));
check("G 画布节点不落批次/状态字段（C5：状态只在派生层）", [...canvasB.nodes(), ...canvasD.nodes(), ...canvasV.nodes()].every((item) => !("workflowBatchId" in (item.metadata || {})) && !("instanceStatus" in (item.metadata || {})) && !("needsHuman" in (item.metadata || {}))));
check("G 画布节点带库图溯源（副本记得自己来自资产库哪张）", canvasB.nodes().filter((item) => imageA.includes(item.id)).every((item) => String(item.metadata?.assetLibrarySourceId || "").length > 0), JSON.stringify(canvasB.nodes().filter((item) => imageA.includes(item.id)).map((item) => [item.id, item.metadata?.assetLibrarySourceId])));

// ---------------------------------------------------------------- 汇总
writeFileSync(path.join(outDir, "probe-summary.json"), JSON.stringify({ pass, fail: fails.length, unit: { pass: unitPass, fail: unitFail }, results }, null, 2), "utf8");
console.log(`\nP7 自动匹配+否决+自修探针：${pass} 通过 / ${fails.length} 失败（单测断言 ${unitPass} 条）`);
fails.forEach((item) => console.log(`  FAIL ${item}`));
process.exit(fails.length ? 1 : 0);
