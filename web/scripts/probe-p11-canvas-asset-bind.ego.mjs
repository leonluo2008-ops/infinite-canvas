/**
 * P11 探针（ego-browser DOM 层，真页面 + 真 store + 真渲染）。
 *
 * 跑法（仓库根或 web/ 下都行）：
 *   ego-browser nodejs < web/scripts/probe-p11-canvas-asset-bind.ego.mjs
 * 前置：
 *   1) `cd web && npm run dev`（http://127.0.0.1:3000）已在跑；
 *   2) 先跑 node 层探针 `node scripts/probe-p11-canvas-asset-bind.mjs`：它把**生产代码真跑**
 *      出来的实例（含挂图副本 / 挂图对照 / 批次记录）落成
 *      `%LOCALAPPDATA%/hermes/cache/scratch/p11-out/p11-single-instance.json`，本探针直接拿它 seed，
 *      所以侧栏看到的挂图位不是手写夹具，而是 `materializeWorkflowInstance` 真产物。
 * space 名：p11-canvas-asset-bind（跑完不关，便于复查）。
 *
 * 本层能证与不能证（如实写在报告里）：
 *   能证：① **真页面 + 真 store** 里调 P11 解析层：人物图绑人物槽、产品图绑主体槽，回执带理由/命中词/分数；
 *        ② 真页面里零参形态的 run_workflow_batch：P12 起无模板缺省命中系统内置（不再引导抽模板），
 *           解析层放行（失败只可能来自素材字节，证明模板不再是使用前提）；
 *        ③ 生产实例落到真 store 后，侧栏「待审批」渲染 1 行 + 抽屉挂图位 2 个（图名/理由可见）+ 一键否决按钮在位
 *           （DOM 断言 + 截图）；
 *        ④ 全程零生成 / 零提交请求（fetch 计数）。
 *   不能证：真机「一句话 → 浏览器里真跑完拉片链」——浏览器侧拉片要真视频字节（IndexedDB 文件库），
 *        该语义由 node 层探针 stub 字节证明；真 LLM 对话轮次同样不在本层（行为非确定）。
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

const BASE = "http://127.0.0.1:3000";
const SPACE = "p11-canvas-asset-bind";
const SHOT_DIR = "E:/User/GitHub/infinite-canvas/wanrepro/docs/证据-P11画布替换图绑定-20261114";
const FIXTURE_JSON = "C:/Users/leon3/AppData/Local/hermes/cache/scratch/p11-out/p11-single-instance.json";
const STORE_KEY = "infinite-canvas:canvas_store";

mkdirSync(SHOT_DIR, { recursive: true });
const dumped = JSON.parse(readFileSync(FIXTURE_JSON, "utf8"));

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
const PROJECT_ID = `p11-probe-${stamp}`;
const PROJECT_TITLE = `P11 探针 ${new Date(stamp).toISOString().slice(11, 19)}`;
const CHAIN_IDS = ["video-src", "lap-1", "ins-1", "it-1", "cfg-1", "fin-1", "cfgv-1", "out-video", "slot-female", "slot-perfume", "ref-img"];

const chainNodes = () => [
    { id: "video-src", type: "video", title: "源视频.mp4", position: { x: 0, y: 0 }, width: 320, height: 200, metadata: { content: "", storageKey: "video:src", mimeType: "video/mp4" } },
    { id: "lap-1", type: "lapian", title: "拉片", position: { x: 400, y: 0 }, width: 420, height: 520, metadata: { content: "# 拉片正文", taskId: "p11-lap-task", jobId: "p11-lap-job", lapPhase: "done" } },
    { id: "ins-1", type: "text", title: "转换指令", position: { x: 900, y: 0 }, width: 320, height: 200, metadata: { content: "指令正文", prompt: "指令正文", promptTemplate: "seedance-flow" } },
    { id: "it-1", type: "text", title: "改造意图", position: { x: 900, y: 260 }, width: 320, height: 200, metadata: { content: "旧意图（12 字左右）", prompt: "旧意图（12 字左右）", lapianIntent: true } },
    { id: "cfg-1", type: "config", title: "提示词生成配置", position: { x: 1300, y: 0 }, width: 360, height: 220, metadata: { generationMode: "text", model: "default::m", lapianSourceId: "lap-1", composerContent: "拉片：@[node:lap-1]\n指令：@[node:ins-1]\n意图：@[node:it-1]" } },
    { id: "fin-1", type: "text", title: "终稿（占位）", position: { x: 1700, y: 0 }, width: 360, height: 240, metadata: { fontSize: 14, model: "default::m", textCount: 1, content: "" } },
    { id: "cfgv-1", type: "config", title: "视频生成配置", position: { x: 2100, y: 0 }, width: 360, height: 200, metadata: { generationMode: "video", model: "k::sd2.5", size: "720x1280", composerContent: "提示词：@[node:fin-1]" } },
    { id: "out-video", type: "video", title: "生成结果", position: { x: 2500, y: 0 }, width: 320, height: 200, metadata: { content: "", storageKey: "", status: "idle", videoTaskId: "task-out", videoProgress: 100 } },
    { id: "slot-female", type: "image", title: "资产位·人物·女性A", position: { x: 520, y: 600 }, width: 200, height: 200, metadata: { content: "", storageKey: "", assetSlotSourceId: "lap-1", assetSlotOrder: 0, assetSlotName: "女性A", assetSlotType: "人物", assetSlot: { desc: "原片模特，红色风衣", span: "00:00-02.00" } } },
    { id: "slot-perfume", type: "image", title: "资产位·主体·香水", position: { x: 760, y: 600 }, width: 200, height: 200, metadata: { content: "", storageKey: "", assetSlotSourceId: "lap-1", assetSlotOrder: 1, assetSlotName: "香水", assetSlotType: "主体", assetSlot: { desc: "细长圆柱体", span: "02.00-05.00" } } },
    { id: "ref-img", type: "image", title: "女性-参考图.png", position: { x: 1000, y: 600 }, width: 200, height: 200, metadata: { content: "", storageKey: "image:ref", mimeType: "image/png" } },
];
const chainConnections = () => [
    { id: "e1", fromNodeId: "video-src", toNodeId: "lap-1" },
    { id: "e2", fromNodeId: "lap-1", toNodeId: "cfg-1" },
    { id: "e3", fromNodeId: "ins-1", toNodeId: "cfg-1" },
    { id: "e4", fromNodeId: "it-1", toNodeId: "cfg-1" },
    { id: "e5", fromNodeId: "cfg-1", toNodeId: "fin-1" },
    { id: "e6", fromNodeId: "fin-1", toNodeId: "cfgv-1" },
    { id: "e7", fromNodeId: "cfgv-1", toNodeId: "out-video" },
];
/** 画布上的替换图（用户拖进来的产品图/人物图）+ 1 个源视频。 */
const replacementNodes = () => [
    { id: "m1", type: "video", title: "素材一.mp4", position: { x: 2000, y: 400 }, width: 320, height: 200, metadata: { content: "blob:m1", storageKey: "video:m1", mimeType: "video/mp4" } },
    { id: "img-person", type: "image", title: "人物-女性.png", position: { x: 2400, y: 400 }, width: 200, height: 200, metadata: { content: "blob:person", storageKey: "image:person", mimeType: "image/png" } },
    { id: "img-product", type: "image", title: "主体-香水.png", position: { x: 2700, y: 400 }, width: 200, height: 200, metadata: { content: "blob:product", storageKey: "image:product", mimeType: "image/png" } },
];

const seedProject = (id, title, { nodes, connections, template = null, batches = [] }) => ({
    id,
    title,
    nodes,
    connections,
    backgroundMode: "dots",
    viewport: { x: 80, y: 60, k: 0.7 },
    workflowTemplate: template || undefined,
    workflowBatches: batches,
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
        const log = { requests: [], hubTask: 0, submit: 0 };
        window.__p11Net = log;
        const orig = window.fetch.bind(window);
        const json = (payload, status = 200) => new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });
        window.fetch = async function (input, init) {
            const url = typeof input === "string" ? input : (input && input.url) || "";
            if (url.includes("/api/")) log.requests.push(url);
            if (url.includes("/api/hub/task/")) {
                log.hubTask += 1;
                const id = decodeURIComponent(url.split("/api/hub/task/")[1].split(/[/?#]/)[0]);
                return json({ task: { local_id: id, state: "awaiting_approval", bridge_id: "p11-bridge", intent: "P11 探针夹具", video_name: "探针素材.mp4", created_at: "2026-11-14T00:00:00.000Z", gen_progress: null, has_output: false, pair_lines: [], asset_cards: [], suggestions: [], summary_md: "", checklist_md: "", prompt_md: "", 自检: [], submit: { form: "none", active_submit: false, can_resume: false }, error: "" } });
            }
            if (url.includes("/api/hub/submit/")) {
                log.submit += 1;
                return json({ ok: true, job_id: "p11-stub-job" });
            }
            if (url.includes("/api/hub/")) return json({ ok: true });
            return orig(input, init);
        };
    })();`,
});

// 先备份真实 store 原值，再写夹具（顺序反了会把夹具一起写回，留下残留）
const backupRaw = await page.evaluate(async ({ key }) => {
    const mod = await import("/src/lib/localforage-storage.ts");
    return mod.localForageStorage.getItem(key);
}, { key: STORE_KEY });
check("P0 真实 store 已备份原值（跑完原样写回）", typeof backupRaw === "string" || backupRaw === null);

// 写夹具：项目 A = 干净画布（链 + 1 源视频 + 2 张替换图 + 生产代码抽出的模板声明）
await page.evaluate(async ({ key, project }) => {
    const mod = await import("/src/lib/localforage-storage.ts");
    const raw = await mod.localForageStorage.getItem(key);
    const parsed = raw ? JSON.parse(raw) : null;
    const state = parsed && parsed.state ? parsed.state : { projects: [] };
    const next = { ...(parsed || {}), state: { ...state, projects: [...(state.projects || []), project], deletedProjects: state.deletedProjects || [] }, version: parsed?.version ?? 1 };
    await mod.localForageStorage.setItem(key, JSON.stringify(next));
    return true;
}, {
    key: STORE_KEY,
    project: seedProject(PROJECT_ID, PROJECT_TITLE, { nodes: [...chainNodes(), ...replacementNodes()], connections: chainConnections(), template: dumped.project.workflowTemplate }),
});

await page.goto(`${BASE}/canvas/${PROJECT_ID}`, { timeout: 90000 });
await page.waitForLoadState("load");
// 页面 store 水合有一拍延迟：轮询等夹具项目真进 store（不猜固定等待）
const hydrated = await page.evaluate(async ({ projectId }) => {
    const storeMod = await import("/src/stores/canvas/use-canvas-store.ts");
    for (let index = 0; index < 60; index += 1) {
        const project = storeMod.useCanvasStore.getState().projects.find((item) => item.id === projectId);
        if (project && Array.isArray(project.nodes) && project.nodes.length) return { ok: true, nodes: project.nodes.length };
        await new Promise((resolve) => setTimeout(resolve, 250));
    }
    return { ok: false, ids: storeMod.useCanvasStore.getState().projects.map((item) => item.id) };
}, { projectId: PROJECT_ID });
check("P1 夹具项目已水合进页面 store", hydrated.ok === true, JSON.stringify(hydrated));

// ---------------------------------------------------------------- ② 真页面 + 真 store 里跑 P11 解析层
const liveResolve = await page.evaluate(async ({ projectId }) => {
    const entryMod = await import("/src/lib/canvas/workflow-batch-entry.ts");
    const storeMod = await import("/src/stores/canvas/use-canvas-store.ts");
    const project = storeMod.useCanvasStore.getState().projects.find((item) => item.id === projectId);
    if (!project) return { error: "页面 store 里找不到夹具项目" };
    try {
        const plan = entryMod.templatePlanInput(project, project.workflowTemplate);
        const resolution = entryMod.resolveCanvasAssetBindings({ project, template: project.workflowTemplate, templateNodes: plan.nodes, instanceIndex: 1, selectedNodeIds: [] });
        return { rows: resolution.rows, summary: resolution.summary, candidates: resolution.candidates, excluded: resolution.excluded };
    } catch (error) {
        return { error: String((error && error.message) || error) };
    }
}, { projectId: PROJECT_ID });

const female = (liveResolve.rows || []).find((row) => row.slotTemplateNodeId === "slot-female");
const perfume = (liveResolve.rows || []).find((row) => row.slotTemplateNodeId === "slot-perfume");
check("① 真页面 + 真 store：人物图绑人物槽（带命中词/分数/来源）", female?.status === "bound" && female?.imageNodeId === "img-person" && female?.matchedTerms?.includes("女性") && female?.score >= 6 && female?.source === "canvas_semantic", JSON.stringify(liveResolve.error || female));
check("② 真页面 + 真 store：产品图绑主体槽（带命中词/分数/来源）", perfume?.status === "bound" && perfume?.imageNodeId === "img-product" && perfume?.matchedTerms?.includes("香水") && perfume?.source === "canvas_semantic", JSON.stringify(perfume));
check("③ 回执摘要写清「图→槽」与理由（用户可核对）", /人物-女性\.png/.test(String(liveResolve.summary)) && /主体-香水\.png/.test(String(liveResolve.summary)) && /槽语义命中/.test(String(liveResolve.summary)), String(liveResolve.summary));
check("④ 模板骨架里的图不进候选池（真页面同口径）", !(liveResolve.candidates || []).some((row) => row.imageNodeId === "ref-img"), JSON.stringify(liveResolve.candidates));

// 零参入口行为（P12 起更新口径：项目没模板时缺省直接命中系统内置「视频拆解」，不再人话引导先抽模板；
// 解析层放行后失败只可能来自素材字节——本夹具 deps 没给 getBlob 字节，走到素材/实例层即证明模板不再是使用前提）
const zeroArg = await page.evaluate(async ({ projectId }) => {
    const entryMod = await import("/src/lib/canvas/workflow-batch-entry.ts");
    const storeMod = await import("/src/stores/canvas/use-canvas-store.ts");
    const project = storeMod.useCanvasStore.getState().projects.find((item) => item.id === projectId);
    try {
        const result = await entryMod.runWorkflowBatchTool({}, {
            project: () => ({ ...project, workflowTemplate: undefined }),
            applyOps: () => {},
            getBlob: async () => null,
            hub: {},
            vetoes: () => [],
            saveTemplate: () => {},
            saveBatches: () => {},
            flush: () => {},
            runBatch: async () => ({ ok: false }),
        });
        return { ok: true, result: { templateFrom: result.templateFrom, templateId: result.templateId } };
    } catch (error) {
        return { error: String((error && error.message) || error) };
    }
}, { projectId: PROJECT_ID });
check("⑤ P12 入口语径：无模板时零参调用缺省命中内置（不再引导先抽模板，解析层放行）", (zeroArg.ok === true && zeroArg.result.templateFrom === "builtin" && zeroArg.result.templateId === "builtin-video-analysis") || (!/画布还没有工作流模板|先抽一次模板|抽一次模板/.test(String(zeroArg.error)) && /字节|模板节点|拉片|素材/.test(String(zeroArg.error))), String(zeroArg.error || JSON.stringify(zeroArg.result)));

// ---------------------------------------------------------------- ③ 生产实例落真 store → 侧栏 DOM
await page.evaluate(async ({ key, projectId, dump, chain }) => {
    const mod = await import("/src/lib/localforage-storage.ts");
    const raw = await mod.localForageStorage.getItem(key);
    const parsed = JSON.parse(raw);
    const next = {
        ...parsed,
        state: {
            ...parsed.state,
            projects: parsed.state.projects.map((item) =>
                item.id === projectId
                    ? {
                          ...item,
                          // 真产物节点（含挂图副本/挂图对照）与批次记录原样搬进来；链节点 id 保持与模板一致
                          nodes: dump.project.nodes.map((node) => (chain.includes(node.id) ? { ...node, position: node.position || { x: 0, y: 0 } } : node)),
                          connections: dump.project.connections,
                          workflowTemplate: dump.project.workflowTemplate,
                          workflowBatches: dump.project.workflowBatches,
                          workflowInstances: [],
                          workflowAssetVetoes: [],
                      }
                    : item,
            ),
        },
    };
    await mod.localForageStorage.setItem(key, JSON.stringify(next));
    return true;
}, { key: STORE_KEY, projectId: PROJECT_ID, dump: dumped, chain: CHAIN_IDS });

await page.goto(`${BASE}/canvas/${PROJECT_ID}`, { timeout: 90000 });
await page.waitForSelector("[data-review-badge]", { state: "visible", timeout: 60000 }).catch(() => null);
await page.waitForTimeout(1500);

const badge = await page.evaluate(() => {
    const node = document.querySelector("[data-review-badge]");
    return node ? node.getAttribute("data-review-badge") : null;
});
check("⑥ 侧栏徽标 = 1（生产实例到待审批）", badge === String((dumped.project.workflowBatches[0]?.members || []).length || 1), String(badge));

const opened = await page.evaluate(async () => {
    const tabs = [...document.querySelectorAll("[data-canvas-no-zoom] button")].filter((node) => node.textContent && node.textContent.trim());
    const review = tabs.find((node) => node.textContent.includes("待审批"));
    if (!review) return { ok: false, reason: "找不到「待审批」页签", tabs: tabs.slice(0, 6).map((node) => node.textContent.trim()) };
    review.click();
    await new Promise((resolve) => setTimeout(resolve, 900));
    return { ok: true };
});
check("⑦ 「待审批」页签可打开", opened.ok, JSON.stringify(opened));
await page.waitForSelector("[data-review-instance]", { state: "visible", timeout: 30000 }).catch(() => null);
await page.waitForTimeout(800);

const rows = await page.evaluate(() => ({
    count: document.querySelectorAll("[data-review-instance]").length,
    status: [...document.querySelectorAll("[data-review-instance]")].map((node) => node.getAttribute("data-review-status")),
    hasThumb: [...document.querySelectorAll("[data-review-instance]")].every((node) => Boolean(node.querySelector("[data-review-thumb]"))),
    batches: document.querySelectorAll("[data-review-batch]").length,
    text: (document.querySelector("[data-canvas-no-zoom]") || document.body).innerText || "",
}));
check("⑧ 列表 1 行、状态待审批、行内有素材缩略位", rows.count === 1 && rows.batches === 1 && rows.status.every((value) => value === "awaiting_approval") && rows.hasThumb, JSON.stringify(rows));
check("⑨ 批次头显示模板名（具名登记在界面上可见）", String(rows.text).includes("视频拆解"), String(rows.text).slice(0, 200));

await page.screenshot({ path: path.join(SHOT_DIR, "P11-侧栏1行待审批.png"), fullPage: false });
const drawerClicked = await page.evaluate(() => {
    const row = document.querySelector("[data-review-instance]");
    if (!row) return false;
    const openBtn = row.querySelector("[data-review-open]") || row;
    openBtn.click();
    return true;
});
await page.waitForTimeout(1200);
const drawer = await page.evaluate(() => {
    const panel = document.querySelector("[data-review-drawer]");
    if (!panel) return { open: false };
    const slots = [...panel.querySelectorAll("[data-review-slot]")].map((node) => ({
        slotNodeId: node.getAttribute("data-review-slot"),
        current: (node.querySelector("[data-review-slot-current]") || {}).textContent?.trim() || "",
        note: (node.querySelector("[data-review-slot-note]") || {}).textContent?.trim() || "",
        hasThumbImg: Boolean(node.querySelector("img")),
        vetoButton: Boolean(node.querySelector("[data-review-slot-veto]")),
    }));
    return { open: true, slots, text: panel.innerText || "" };
});
check("⑩ 实例抽屉真渲染（截图不是空壳）", drawerClicked === true && drawer.open === true, JSON.stringify({ clicked: drawerClicked, open: drawer.open }));
check("⑪ 抽屉挂图位 2 个：人物槽挂「人物-女性.png」、主体槽挂「主体-香水.png」（图名可见）", drawer.slots?.length === 2 && drawer.slots.some((slot) => slot.current.includes("人物-女性.png")) && drawer.slots.some((slot) => slot.current.includes("主体-香水.png")), JSON.stringify(drawer.slots));
check("⑫ 每个挂图位都有匹配理由（侧栏可见「为什么是它」）", (drawer.slots || []).length === 2 && (drawer.slots || []).every((slot) => slot.note.length > 0), JSON.stringify((drawer.slots || []).map((slot) => slot.note)));
check("⑬ 每个挂图位都有「一键否决」按钮（P7 否决链在 UI 上可用）", (drawer.slots || []).length === 2 && (drawer.slots || []).every((slot) => slot.vetoButton), JSON.stringify((drawer.slots || []).map((slot) => slot.vetoButton)));
if (drawer.open) await page.screenshot({ path: path.join(SHOT_DIR, "P11-实例抽屉-图到槽绑定.png"), fullPage: false });

// ---------------------------------------------------------------- ④ 零付费 / 零提交
const net = await page.evaluate(() => ({
    requests: [...new Set((window.__p11Net && window.__p11Net.requests) || [])],
    submit: (window.__p11Net && window.__p11Net.submit) || 0,
}));
const paidHits = net.requests.filter((url) => /generate|video\/create|image\/create|completions/i.test(url));
check("⑭ 全程零生成/零创建请求（付费动作 0）", paidHits.length === 0, JSON.stringify(paidHits));
check("⑮ 全程零提交（没点批准/出片）", net.submit === 0, `submit=${net.submit}`);

// ---------------------------------------------------------------- 复原真实 store（字节级）
await page.evaluate(async ({ key, raw }) => {
    const mod = await import("/src/lib/localforage-storage.ts");
    if (raw === null || raw === undefined) await mod.localForageStorage.removeItem(key);
    else await mod.localForageStorage.setItem(key, raw);
    return true;
}, { key: STORE_KEY, raw: backupRaw ?? null });
const restored = await page.evaluate(async ({ key, ids }) => {
    const mod = await import("/src/lib/localforage-storage.ts");
    const raw = await mod.localForageStorage.getItem(key);
    const parsed = JSON.parse(raw || "{}");
    const projectIds = (parsed.state?.projects || []).map((item) => item.id);
    return { leftovers: ids.filter((id) => projectIds.includes(id)) };
}, { key: STORE_KEY, ids: [PROJECT_ID] });
check("⑯ 真实 store 已复原（夹具项目已删除）", restored.leftovers.length === 0, JSON.stringify(restored));

writeFileSync(path.join(SHOT_DIR, "P11-探针结果-DOM层.json"), JSON.stringify({ at: new Date().toISOString(), pass, fail: fails.length, fails, results }, null, 2), "utf8");

console.log(`\nP11 DOM 探针：${pass} 通过 / ${fails.length} 失败`);
if (fails.length) {
    console.log("失败明细：");
    for (const line of fails) console.log(`  - ${line}`);
}
console.log(`截图目录：${SHOT_DIR}`);
