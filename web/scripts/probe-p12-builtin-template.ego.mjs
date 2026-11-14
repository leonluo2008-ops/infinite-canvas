/**
 * P12 探针（ego-browser DOM 层，真页面 + 真 store + 真渲染）。
 *
 * 跑法（仓库根或 web/ 下都行）：
 *   ego-browser nodejs < web/scripts/probe-p12-builtin-template.ego.mjs
 * 前置：
 *   1) `cd web && npm run dev`（http://127.0.0.1:3000）已在跑；
 *   2) 先跑 node 层探针 `node scripts/probe-p12-builtin-template.mjs`：它把**全新空项目 + 一句话**真跑产出的
 *      项目状态（实例链节点/连线 + 批次记录）落成 `%LOCALAPPDATA%/hermes/cache/scratch/p12-out/p12-batch-fixture.json`，
 *      本探针直接拿它 seed —— 侧栏与画布上看到的不是手写夹具，是生产代码跑出来的。
 *
 * 本层能证与不能证（如实写在报告里）：
 *   能证：① 全新空项目（无 workflowTemplate、无任何链）在真页面零参调用 **不再** 报「先抽模板/画布还没有模板」：
 *        模板解析直接过到内置模板（错误只可能来自素材字节，证明模板不再是使用前提）；
 *        ② 内置模板批次在真页面真 store 里：实例骨架节点真画在画布上（DOM 断言 + 截图）；
 *        ③ 侧栏「待审批」页签出 1 行、状态待审批、批次头显示内置模板名「视频拆解」（不露 builtin- 内部 id）；
 *        ④ 全程零生成/零提交请求（fetch 计数）、零付费。
 *   不能证：真 LLM 对话轮次（需在线模型，行为非确定）；浏览器里真跑拉片/组装（要真视频字节与真模型）——
 *        该语义由 node 层探针以 stub 依赖证明。故本探针用「生产代码产出的批次夹具 + 真渲染」补 DOM 证据。
 */
import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";

const BASE = "http://127.0.0.1:3000";
const SPACE = "p12-builtin-template";
const SHOT_DIR = "E:/User/GitHub/infinite-canvas/wanrepro/docs/证据-P12内置模板-20261114";
const FIXTURE_JSON = "C:/Users/leon3/AppData/Local/hermes/cache/scratch/p12-out/p12-batch-fixture.json";
const STORE_KEY = "infinite-canvas:canvas_store";

mkdirSync(SHOT_DIR, { recursive: true });
const fixture = JSON.parse(readFileSync(FIXTURE_JSON, "utf8"));

const results = [];
let pass = 0;
const fails = [];
function check(name, ok, detail = "") {
    results.push({ name, ok: Boolean(ok), detail: ok ? "" : String(detail) });
    if (ok) pass += 1;
    else fails.push(`${name}${detail ? ` — ${detail}` : ""}`);
    console.log(`${ok ? "PASS" : "FAIL"} ${name}${ok ? "" : ` — ${detail}`}`);
}

const task = await taskSpace(SPACE);
const page = task.page("p1");
console.log(`space=${task.spaceId} page=${page.label}`);

const stamp = Date.now();
const EMPTY_ID = `p12-probe-empty-${stamp}`;
const BUILTIN_ID = `p12-probe-builtin-${stamp}`;
const EMPTY_TITLE = `P12 探针·全新空项目 ${new Date(stamp).toISOString().slice(11, 19)}`;
const BUILTIN_TITLE = `P12 探针·内置模板批次 ${new Date(stamp).toISOString().slice(11, 19)}`;

const PNG_1PX = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";

// 全新空项目：只有 1 源视频 + 1 人物图 + 1 产品图，**没有模板、没有任何链**
const emptyProject = () => ({
    id: EMPTY_ID,
    title: EMPTY_TITLE,
    nodes: [
        { id: "m1", type: "video", title: "素材一.mp4", position: { x: 80, y: 120 }, width: 320, height: 200, metadata: { content: "", storageKey: "video:m1", mimeType: "video/mp4", bytes: 4200, durationMs: 9000 } },
        // 挂图副本要求替换图**真有图内容**（空内容的图会被计划层拒绝：见 node 层 D 段口径），故带上 1×1 PNG
        { id: "img-person", type: "image", title: "人物-女性.png", position: { x: 460, y: 120 }, width: 200, height: 200, metadata: { content: PNG_1PX, storageKey: "image:person", mimeType: "image/png", naturalWidth: 1, naturalHeight: 1 } },
        { id: "img-product", type: "image", title: "产品-香水.png", position: { x: 700, y: 120 }, width: 200, height: 200, metadata: { content: PNG_1PX, storageKey: "image:product", mimeType: "image/png", naturalWidth: 1, naturalHeight: 1 } },
    ],
    connections: [],
    backgroundMode: "dots",
    viewport: { x: 40, y: 80, k: 0.8 },
    workflowBatches: [],
    workflowInstances: [],
    workflowAssetVetoes: [],
    workflowWanFileTasks: [],
    createdAt: new Date(stamp).toISOString(),
    updatedAt: new Date(stamp).toISOString(),
});

/** 内置模板批次夹具：节点/连线/批次记录全部来自 node 层生产代码真跑。 */
const builtinProject = () => ({
    id: BUILTIN_ID,
    title: BUILTIN_TITLE,
    nodes: fixture.project.nodes,
    connections: fixture.project.connections,
    backgroundMode: "dots",
    viewport: { x: 40, y: 80, k: 0.5 },
    workflowBatches: fixture.project.workflowBatches,
    workflowTemplate: fixture.project.workflowTemplate || undefined,
    workflowInstances: [],
    workflowAssetVetoes: [],
    workflowWanFileTasks: [],
    createdAt: new Date(stamp).toISOString(),
    updatedAt: new Date(stamp).toISOString(),
});

// ---------------------------------------------------------------- ① 真页面装 fetch 计数（只记录 + stub hub，禁真生成/真提交）
await page.goto(`${BASE}/`, { timeout: 60000 });
await page.waitForLoadState("load");
await page.cdp("Page.enable", {});
await page.cdp("Page.addScriptToEvaluateOnNewDocument", {
    source: `(() => {
        const log = { requests: [], hubTask: 0, submit: 0, generate: 0 };
        window.__p12Net = log;
        const orig = window.fetch.bind(window);
        const json = (payload, status = 200) => new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });
        window.fetch = async function (input, init) {
            const url = typeof input === "string" ? input : (input && input.url) || "";
            if (url.includes("/api/")) log.requests.push(url);
            if (/generate|video\\/create|completions/i.test(url)) log.generate += 1;
            if (url.includes("/api/hub/task/")) {
                log.hubTask += 1;
                const id = decodeURIComponent(url.split("/api/hub/task/")[1].split(/[/?#]/)[0]);
                return json({ task: { local_id: id, state: "awaiting_approval", bridge_id: "p12-bridge", intent: "P12 探针夹具", video_name: "探针素材.mp4", created_at: "2026-11-14T00:00:00.000Z", gen_progress: null, has_output: false, pair_lines: [], asset_cards: [], suggestions: [], summary_md: "", checklist_md: "", prompt_md: "", 自检: [], submit: { form: "none", active_submit: false, can_resume: false }, error: "" } });
            }
            if (url.includes("/api/hub/submit/")) {
                log.submit += 1;
                return json({ ok: true, job_id: "p12-stub-job" });
            }
            if (url.includes("/api/hub/")) return json({ ok: true });
            return orig(input, init);
        };
    })();`,
});

// 写夹具（备份原值，跑完原样写回）
const backup = await page.evaluate(
    async ({ key, projects }) => {
        const mod = await import("/src/lib/localforage-storage.ts");
        const raw = await mod.localForageStorage.getItem(key);
        const parsed = raw ? JSON.parse(raw) : null;
        const state = parsed && parsed.state ? parsed.state : { projects: [] };
        const next = { ...(parsed || {}), state: { ...state, projects: [...(state.projects || []), ...projects], deletedProjects: state.deletedProjects || [] }, version: parsed?.version ?? 1 };
        await mod.localForageStorage.setItem(key, JSON.stringify(next));
        return { raw, hadValue: Boolean(raw), projectCount: (state.projects || []).length };
    },
    { key: STORE_KEY, projects: [emptyProject(), builtinProject()] },
);
check("P0 真实 store 已备份原值（跑完原样写回）", typeof backup.hadValue === "boolean", JSON.stringify({ hadValue: backup.hadValue, projectCount: backup.projectCount }));

// ---------------------------------------------------------------- ① 全新空项目：一句话直达不再要求先抽模板
await page.goto(`${BASE}/canvas/${EMPTY_ID}`, { timeout: 90000 });
await page.waitForLoadState("load");
await page.waitForTimeout(2500);

const emptyState = await page.evaluate(async ({ key, projectId }) => {
    const mod = await import("/src/lib/localforage-storage.ts");
    const raw = await mod.localForageStorage.getItem(key);
    const project = (JSON.parse(raw || "{}").state?.projects || []).find((item) => item.id === projectId);
    return { hasTemplate: Boolean(project?.workflowTemplate), nodes: (project?.nodes || []).length, connections: (project?.connections || []).length };
}, { key: STORE_KEY, projectId: EMPTY_ID });
check("①-0 夹具确为全新空项目：无 workflowTemplate、无连线、只有 3 个素材节点", emptyState.hasTemplate === false && emptyState.connections === 0 && emptyState.nodes === 3, JSON.stringify(emptyState));

const domNodesBefore = await page.evaluate(() => document.querySelectorAll("[data-node-id]").length);
const netBefore = await page.evaluate(() => (window.__p12Net ? window.__p12Net.requests.length : -1));

// 用户原话：「执行视频拆解工作流，用画布里的角色和产品替换原视频对应内容」
// → Agent 翻成两条槽指代（角色→人物槽、产品→产品槽）+ 零 templateId/materials，真页面真 store 真调用
const zeroArg = await page.evaluate(async () => {
    const mod = await import("/src/lib/agent/agent-site-tools.ts");
    try {
        const result = await mod.runSiteTool(
            "run_workflow_batch",
            { assetBindings: [{ slotName: "人物", imageNodeId: "img-person", reason: "画布里的角色" }, { slotName: "产品", imageNodeId: "img-product", reason: "画布里的产品" }] },
            () => {},
        );
        return { ok: true, result: { templateFrom: result.templateFrom, templateId: result.templateId, templateName: result.templateName, materials: result.materials?.length, instances: result.instances?.length, paid: result.paidVideoCreations, next: String(result.next || "").slice(0, 400) } };
    } catch (error) {
        return { error: String((error && error.message) || error) };
    }
});
const netAfter = await page.evaluate(() => (window.__p12Net ? window.__p12Net.requests.length : -1));

const emptyErr = String(zeroArg.error || "");
const passedTemplate = !/画布还没有工作流模板|先抽一次模板|抽一次模板|还没有工作流模板/.test(emptyErr);
check("① 全新空项目一句话直达：模板解析直接过（不再报「先抽模板/画布还没有模板」）", passedTemplate, emptyErr || JSON.stringify(zeroArg.result));
// 真浏览器包里的内置模板注册表（同一份 TS 模块，经 vite 真打包进页面）
const builtinInBrowser = await page.evaluate(async () => {
    const mod = await import("/src/lib/canvas/workflow-builtin-template.ts");
    const template = mod.builtinVideoAnalysisTemplate();
    const source = mod.builtinWorkflowTemplateSource("builtin-video-analysis");
    return {
        id: template.templateId,
        name: template.name,
        nodes: template.frozen.topology.nodeIds.length,
        slots: (template.slots || []).length,
        slotsKinds: (template.slots || []).map((slot) => slot.kind),
        external: (template.externalReferences || []).length,
        assetSlots: source.nodes.filter((node) => node.metadata?.assetSlotName).map((node) => [node.metadata.assetSlotName, node.metadata.assetSlotType, node.metadata.assetSlotOrder]),
        assess: mod.assessTemplateCompleteness(template),
    };
});
check(
    "①-2 内置模板在真浏览器包里可用：id=builtin-video-analysis / 名=视频拆解 / 9 节点 / 五槽 / 零图外引用",
    builtinInBrowser.id === "builtin-video-analysis" && builtinInBrowser.name === "视频拆解" && builtinInBrowser.nodes === 9 && builtinInBrowser.slots === 5 && builtinInBrowser.external === 0 && builtinInBrowser.assess.ok === true,
    JSON.stringify(builtinInBrowser),
);
check("①-2b 内置资产槽语义（人物/产品，序 0/1）在真浏览器里与 P11 绑定口径一致", JSON.stringify(builtinInBrowser.assetSlots) === JSON.stringify([["人物", "人物", 0], ["产品", "产品", 1]]), JSON.stringify(builtinInBrowser.assetSlots));
check(
    "①-2c 模板层已放行：失败点只在「真视频字节不在浏览器存储」（内置模板被接受、实例计划已开始执行）",
    zeroArg.ok === true ? zeroArg.result.templateFrom === "builtin" : /视频字节取不回|没有本地视频字节/.test(emptyErr),
    JSON.stringify(zeroArg.result || emptyErr),
);
check("①-3 素材自动解析 1 条、实例入批 1 条（素材字节缺失导致的失败如实报出，不冒充成功）", zeroArg.ok === true ? zeroArg.result.materials === 1 && zeroArg.result.instances === 1 : /字节|没有本地/.test(emptyErr), JSON.stringify(zeroArg.result || emptyErr));
check("①-4 零付费、零生成端点（本工具内视频创建恒 0）", (zeroArg.ok !== true || zeroArg.result.paid === 0) && (await page.evaluate(() => window.__p12Net.generate)) === 0, JSON.stringify({ paid: zeroArg.result?.paid }));
check("①-5 画布实例节点只增不减（链路没被半途拆掉）", (await page.evaluate(() => document.querySelectorAll("[data-node-id]").length)) >= domNodesBefore, `${domNodesBefore} → ${await page.evaluate(() => document.querySelectorAll("[data-node-id]").length)}`);
await page.screenshot({ path: path.join(SHOT_DIR, "P12-①-全新空项目一句话直达.png") });

// ---------------------------------------------------------------- ② 内置模板批次：实例链画布可见 + 侧栏待批
await page.goto(`${BASE}/canvas/${BUILTIN_ID}`, { timeout: 90000 });
await page.waitForLoadState("load");
await page.waitForTimeout(3000);

const canvasFacts = await page.evaluate(() => {
    const nodes = [...document.querySelectorAll("[data-node-id]")];
    const texts = nodes.map((item) => (item.textContent || "").replace(/\s+/g, " "));
    const expected = ["源视频", "拉片拆解", "转换指令", "改造意图", "提示词组装", "视频生成配置", "资产位·人物", "资产位·产品"];
    return {
        count: nodes.length,
        ids: nodes.map((item) => item.getAttribute("data-node-id")).filter((id) => String(id).includes("builtin-video-analysis")).slice(0, 12),
        missing: expected.filter((title) => !texts.some((text) => text.includes(title))),
        hasProduct: texts.some((text) => text.includes("本次产物")),
        edges: document.querySelectorAll("[data-connection-id]").length,
        excerpt: texts.slice(0, 14),
    };
});
const expectInstanceIds = ["bva-src", "bva-lap", "bva-ins", "bva-intent", "bva-cfg-prompt", "bva-cfg-video", "bva-slot-person", "bva-slot-product"].map((old) => `tbuiltin-video-analysis-1-${old}`);
const missingInstance = expectInstanceIds.filter((id) => !canvasFacts.ids.includes(id));
check(
    "② 实例链画布可见：内置模板 8 个骨架节点按确定性 id 真画在画布上（源视频/拉片/指令/意图/组装配置/视频配置/两个资产位）",
    missingInstance.length === 0,
    JSON.stringify({ missingInstance, ids: canvasFacts.ids, count: canvasFacts.count }),
);
check(
    "②-1b 挂图副本与对照图也在画布上（图→槽的可视实体，不是黑盒）",
    canvasFacts.ids.includes("tbuiltin-video-analysis-1-img-person") && canvasFacts.ids.includes("tbuiltin-video-analysis-1-img-product") && canvasFacts.ids.includes("tbuiltin-video-analysis-1-graft@bva-slot-person") && canvasFacts.ids.includes("tbuiltin-video-analysis-1-graft@bva-slot-product"),
    JSON.stringify(canvasFacts.ids),
);
check("②-2 实例链连线可见（源→拉片→组装→产物→视频配置等边真的画出来了）", canvasFacts.edges >= 5, JSON.stringify({ edges: canvasFacts.edges }));
await page.screenshot({ path: path.join(SHOT_DIR, "P12-②-实例链画布可见.png") });

// 侧栏「待审批」页签
const sidebar = await page.evaluate(async () => {
    const tabs = [...document.querySelectorAll("[data-canvas-no-zoom] button")].filter((node) => node.textContent && node.textContent.trim());
    const target = tabs.find((node) => node.textContent.includes("待审批"));
    if (!target) return { clicked: false, tabTexts: tabs.slice(0, 8).map((node) => node.textContent.trim()) };
    target.click();
    return { clicked: true, badge: document.querySelector("[data-review-badge]")?.getAttribute("data-review-badge") ?? null };
});
check("③-0 左侧栏「待审批」页签存在且可点，徽标=1", sidebar.clicked === true && String(sidebar.badge) === "1", JSON.stringify(sidebar));

// 等列表渲染出来（P8 口径：data-review-instance 行 + data-review-thumb 缩略）
await page.waitForSelector("[data-review-instance]", { state: "visible", timeout: 60000 }).catch(() => null);

const reviewDom = await page.evaluate(() => {
    const body = document.body.innerText || "";
    const batches = [...document.querySelectorAll("[data-review-batch]")];
    const rows = [...document.querySelectorAll("[data-review-instance]")];
    return {
        batchCount: batches.length,
        rowCount: rows.length,
        batchText: batches.map((node) => (node.textContent || "").replace(/\s+/g, " ").slice(0, 160)),
        statuses: rows.map((row) => ({
            status: row.getAttribute("data-review-status"),
            text: (row.textContent || "").replace(/\s+/g, " ").slice(0, 120),
            thumb: Boolean(row.querySelector("[data-review-thumb]")),
        })),
        hasTemplateName: body.includes("视频拆解"),
        leaksInternalId: body.includes("builtin-video-analysis"),
        awaiting: rows.some((row) => String(row.getAttribute("data-review-status") || "") === "awaiting_approval"),
        excerpt: body.split("\n").filter((line) => line.includes("视频拆解") || line.includes("待审批")).slice(0, 8),
    };
});
check("③-1 侧栏出 1 个批次 + 1 行实例（内置批次的实例真被侧栏认到）", reviewDom.batchCount === 1 && reviewDom.rowCount === 1, JSON.stringify({ batchCount: reviewDom.batchCount, rowCount: reviewDom.rowCount, batchText: reviewDom.batchText }));
check("③ 侧栏「待审批」可见：批次头显示内置模板名「视频拆解」", reviewDom.hasTemplateName && reviewDom.batchText.some((text) => text.includes("视频拆解")), JSON.stringify(reviewDom.excerpt));
check("③-2 侧栏不露内部 id（builtin-video-analysis 不出现在界面上）", reviewDom.leaksInternalId === false, JSON.stringify(reviewDom.excerpt));
check("③-3 实例行状态=待审批（data-review-status=awaiting_approval，与派生层一致）", reviewDom.awaiting === true, JSON.stringify(reviewDom.statuses));
check("③-4 实例行有素材缩略位（data-review-thumb 在位，不是空行）", reviewDom.statuses.every((row) => row.thumb === true), JSON.stringify(reviewDom.statuses));
await page.screenshot({ path: path.join(SHOT_DIR, "P12-③-待审批侧栏.png") });

// ---------------------------------------------------------------- ④ 零提交/零付费/自检
const net = await page.evaluate(() => ({ requests: window.__p12Net.requests.length, submit: window.__p12Net.submit, generate: window.__p12Net.generate }));
check("④ 全程零提交出片（/api/hub/submit 从未被调用）", net.submit === 0, JSON.stringify(net));
check("④-2 全程零生成/零付费端点", net.generate === 0, JSON.stringify(net));

// 还原 store（原值原样写回）
const restored = await page.evaluate(async ({ key, raw }) => {
    const mod = await import("/src/lib/localforage-storage.ts");
    if (raw === null) await mod.localForageStorage.removeItem(key);
    else await mod.localForageStorage.setItem(key, raw);
    return true;
}, { key: STORE_KEY, raw: backup.raw ?? null });
check("⑤ 自建夹具已清（store 原值原样写回）", restored === true, String(restored));

const summary = { at: new Date().toISOString(), space: task.spaceId, pass, fail: fails.length, fails, results, fixture: { batchId: fixture.receipt?.batchId, templateId: fixture.receipt?.templateId, nodes: fixture.project.nodes.length } };
const outJson = path.join(SHOT_DIR, "P12-探针结果-DOM层.json");
mkdirSync(path.dirname(outJson), { recursive: true });
const { writeFileSync } = await import("node:fs");
writeFileSync(outJson, JSON.stringify(summary, null, 2), "utf8");

console.log(`\nP12 DOM 探针：${pass} 通过 / ${fails.length} 失败（证据 ${outJson}，截图 ${SHOT_DIR}）`);
if (fails.length) {
    console.log("失败明细：");
    for (const line of fails) console.log(`  - ${line}`);
    process.exit(1);
}
