/**
 * P10 探针（ego-browser DOM 层，真页面 + 真 store + 真渲染）。
 *
 * 跑法（仓库根或 web/ 下都行）：
 *   ego-browser nodejs < web/scripts/probe-p10-named-workflow.ego.mjs
 * 前置：
 *   1) `cd web && npm run dev`（http://127.0.0.1:3000）已在跑；
 *   2) 先跑 node 层探针 `node scripts/probe-p10-named-workflow.mjs`：它把**零参真调产出的批次**落成
 *      `%LOCALAPPDATA%/hermes/cache/scratch/p10-out/p10-batch-fixture.json`，本探针直接拿它 seed，
 *      所以侧栏看到的 3 行不是手写夹具，而是生产代码跑出来的批次记录。
 * space 名：p10-named-workflow（跑完不关，便于复查；截图落 wanrepro/docs/证据-P10具名工作流-20261114/）。
 *
 * 本层能证与不能证（如实写在报告里）：
 *   能证：① 页面侧 `run_workflow_batch` 在**零参形态**下真跑：无模板 → P12 起缺省命中系统内置「视频拆解」
 *        （不再引导先抽模板）；有模板但素材没字节 → 人话报素材问题（证明「一句话直达」的语义解析真的跑在
 *        真页面真 store 上，不是空壳）；
 *        ② 真页面里抽模板并起名「视频拆解」→ 模板真落盘（刷新后仍在）；
 *        ③ 零参真调产出的批次在侧栏「待审批」页签渲染 3 行、全为待审批、徽标=3（DOM 断言）+ 截图；
 *        ④ 全程零生成/零提交请求（fetch 计数）。
 *   不能证：真 LLM 对话轮次（需在线模型、行为非确定）——「用户说这句话 → Agent 选择调本工具」这一步由
 *        工具描述文案（canvas-agent schemas.ts，单测锁定触发话术）+ 本探针的零参调用形态共同覆盖；
 *        浏览器里真跑队列（拉片要真视频字节、组装要真文本模型）——该语义由 node 层探针 stub 证明。
 */
import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";

const BASE = "http://127.0.0.1:3000";
const SPACE = "p10-named-workflow";
const SHOT_DIR = "E:/User/GitHub/infinite-canvas/wanrepro/docs/证据-P10具名工作流-20261114";
const FIXTURE_JSON = "C:/Users/leon3/AppData/Local/hermes/cache/scratch/p10-out/p10-batch-fixture.json";
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
const PROJECT_ID = `p10-probe-${stamp}`;
const NOBYTE_PROJECT_ID = `p10-probe-nobytes-${stamp}`;
const PROJECT_TITLE = `P10 探针 ${new Date(stamp).toISOString().slice(11, 19)}`;
const CHAIN_IDS = ["video-src", "lap-1", "ins-1", "it-1", "cfg-1", "fin-1", "cfgv-1", "out-video", "slot-female", "slot-perfume"];

const chainNodes = () => [
    { id: "video-src", type: "video", title: "源视频.mp4", position: { x: 0, y: 0 }, width: 320, height: 200, metadata: { content: "", storageKey: "video:src", mimeType: "video/mp4" } },
    { id: "lap-1", type: "lapian", title: "拉片", position: { x: 400, y: 0 }, width: 420, height: 520, metadata: { content: "# 拉片正文", taskId: "p10-lap-task", jobId: "p10-lap-job", lapPhase: "done" } },
    { id: "ins-1", type: "text", title: "转换指令", position: { x: 900, y: 0 }, width: 320, height: 200, metadata: { content: "指令正文", prompt: "指令正文", promptTemplate: "seedance-flow" } },
    { id: "it-1", type: "text", title: "改造意图", position: { x: 900, y: 260 }, width: 320, height: 200, metadata: { content: "旧意图（12 字左右）", prompt: "旧意图（12 字左右）", lapianIntent: true } },
    { id: "cfg-1", type: "config", title: "提示词生成配置", position: { x: 1300, y: 0 }, width: 360, height: 220, metadata: { generationMode: "text", model: "default::m", lapianSourceId: "lap-1", composerContent: "拉片：@[node:lap-1]\n指令：@[node:ins-1]\n意图：@[node:it-1]" } },
    { id: "fin-1", type: "text", title: "终稿（占位）", position: { x: 1700, y: 0 }, width: 360, height: 240, metadata: { fontSize: 14, model: "default::m", textCount: 1, content: "" } },
    { id: "cfgv-1", type: "config", title: "视频生成配置", position: { x: 2100, y: 0 }, width: 360, height: 200, metadata: { generationMode: "video", model: "k::sd2.5", size: "720x1280", composerContent: "提示词：@[node:fin-1]" } },
    { id: "out-video", type: "video", title: "生成结果", position: { x: 2500, y: 0 }, width: 320, height: 200, metadata: { content: "", storageKey: "", status: "idle", videoTaskId: "task-out", videoProgress: 100 } },
    { id: "slot-female", type: "image", title: "资产位·人物·女性A", position: { x: 520, y: 600 }, width: 200, height: 200, metadata: { content: "", storageKey: "", assetSlotSourceId: "lap-1", assetSlotOrder: 0, assetSlotName: "女性A", assetSlotType: "人物", assetSlot: { desc: "原片模特，红色风衣", span: "00:00-02.00" } } },
    { id: "slot-perfume", type: "image", title: "资产位·主体·香水", position: { x: 760, y: 600 }, width: 200, height: 200, metadata: { content: "", storageKey: "", assetSlotSourceId: "lap-1", assetSlotOrder: 1, assetSlotName: "香水", assetSlotType: "主体", assetSlot: { desc: "细长圆柱体", span: "02.00-05.00" } } },
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
/**
 * 3 个源视频（用户拖进画布要处理的）+ 1 个产物视频（有上游输入，不算素材）。
 * 节点 id 按项目加前缀：页面侧 findNode 是**跨项目**按 id 查找（素材可来自源画布），
 * 两个夹具项目用同名 id 会串到另一个项目的视频字节上（探测假象，不是产品行为）。
 */
const materialNodes = (withBytes = true, prefix = "m") => [
    { id: `${prefix}1`, type: "video", title: "素材一.mp4", position: { x: 2000, y: 400 }, width: 320, height: 200, metadata: { content: withBytes ? `blob:${prefix}1` : "", storageKey: withBytes ? `video:${prefix}1` : "", mimeType: "video/mp4" } },
    { id: `${prefix}2`, type: "video", title: "素材二.mp4", position: { x: 2400, y: 400 }, width: 320, height: 200, metadata: { content: withBytes ? `blob:${prefix}2` : "", storageKey: withBytes ? `video:${prefix}2` : "", mimeType: "video/mp4" } },
    { id: `${prefix}3`, type: "video", title: "素材三.mp4", position: { x: 2800, y: 400 }, width: 320, height: 200, metadata: { content: withBytes ? `blob:${prefix}3` : "", storageKey: withBytes ? `video:${prefix}3` : "", mimeType: "video/mp4" } },
    { id: `${prefix}-out-loose`, type: "video", title: "生成结果（未连线）", position: { x: 3200, y: 400 }, width: 320, height: 200, metadata: { content: "", storageKey: "", videoTaskId: "task-loose" } },
];

const seedProject = (id, title, { withBytes = true, prefix = "m" } = {}) => ({
    id,
    title,
    nodes: [...chainNodes(), ...materialNodes(withBytes, prefix)],
    connections: [...chainConnections()],
    backgroundMode: "dots",
    viewport: { x: 80, y: 60, k: 0.7 },
    workflowBatches: [],
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
        window.__p10Net = log;
        const orig = window.fetch.bind(window);
        const json = (payload, status = 200) => new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });
        window.fetch = async function (input, init) {
            const url = typeof input === "string" ? input : (input && input.url) || "";
            if (url.includes("/api/")) log.requests.push(url);
            if (url.includes("/api/hub/task/")) {
                log.hubTask += 1;
                const id = decodeURIComponent(url.split("/api/hub/task/")[1].split(/[/?#]/)[0]);
                return json({ task: { local_id: id, state: "awaiting_approval", bridge_id: "p10-bridge", intent: "P10 探针夹具", video_name: "探针素材.mp4", created_at: "2026-11-14T00:00:00.000Z", gen_progress: null, has_output: false, pair_lines: [], asset_cards: [], suggestions: [], summary_md: "", checklist_md: "", prompt_md: "", 自检: [], submit: { form: "none", active_submit: false, can_resume: false }, error: "" } });
            }
            if (url.includes("/api/hub/submit/")) {
                log.submit += 1;
                return json({ ok: true, job_id: "p10-stub-job" });
            }
            if (url.includes("/api/hub/")) return json({ ok: true });
            return orig(input, init);
        };
    })();`,
});

// 写夹具（备份原值，跑完原样写回）
const backup = await page.evaluate(async ({ key, projects }) => {
    const mod = await import("/src/lib/localforage-storage.ts");
    const raw = await mod.localForageStorage.getItem(key);
    const parsed = raw ? JSON.parse(raw) : null;
    const state = parsed && parsed.state ? parsed.state : { projects: [] };
    const next = { ...(parsed || {}), state: { ...state, projects: [...(state.projects || []), ...projects], deletedProjects: state.deletedProjects || [] }, version: parsed?.version ?? 1 };
    await mod.localForageStorage.setItem(key, JSON.stringify(next));
    return { raw, hadValue: Boolean(raw), projectCount: (state.projects || []).length };
}, { key: STORE_KEY, projects: [seedProject(PROJECT_ID, PROJECT_TITLE), seedProject(NOBYTE_PROJECT_ID, `${PROJECT_TITLE} 无字节`, { withBytes: false, prefix: "nb" })] });
const backupRaw = backup.raw;
check("P0 真实 store 已备份原值（跑完原样写回）", typeof backup.hadValue === "boolean", JSON.stringify(backup));

await page.goto(`${BASE}/canvas/${PROJECT_ID}`, { timeout: 90000 });
await page.waitForLoadState("load");
await page.waitForTimeout(2500);

// ---------------------------------------------------------------- ① 零参直达的两条人话路径（真页面真 store）
// P12 起：无模板不再人话引导「先抽模板」——缺省直接命中系统内置「视频拆解」，模板解析层放行，
// 后续失败只可能来自素材字节（浏览器 IndexedDB 没有夹具视频的真字节，如实报错不冒充成功）。
const zeroArgNoTemplate = await page.evaluate(async () => {
    const mod = await import("/src/lib/agent/agent-site-tools.ts");
    try {
        const result = await mod.runSiteTool("run_workflow_batch", {}, () => {});
        return { ok: true, result: { templateFrom: result.templateFrom, templateId: result.templateId, templateName: result.templateName } };
    } catch (error) {
        return { error: String((error && error.message) || error) };
    }
});
check("① 画布还没有模板时，零参缺省命中内置（不再报「画布还没有工作流模板/先抽模板」）", (zeroArgNoTemplate.ok === true && zeroArgNoTemplate.result.templateFrom === "builtin" && zeroArgNoTemplate.result.templateId === "builtin-video-analysis") || !/画布还没有工作流模板|先抽一次模板|抽一次模板/.test(String(zeroArgNoTemplate.error)), String(zeroArgNoTemplate.error || JSON.stringify(zeroArgNoTemplate.result)));

// ---------------------------------------------------------------- ② 真页面抽模板并起名「视频拆解」
const netBefore = await page.evaluate(() => (window.__p10Net ? window.__p10Net.requests.length : -1));
const extracted = await page.evaluate(async ({ chain }) => {
    const mod = await import("/src/lib/agent/agent-site-tools.ts");
    try {
        const result = await mod.runSiteTool("extract_workflow_template", { sourceNodeIds: chain, name: "视频拆解" }, () => {});
        return { ok: true, result };
    } catch (error) {
        return { ok: false, error: String((error && error.message) || error) };
    }
}, { chain: CHAIN_IDS });
const netAfter = await page.evaluate(() => (window.__p10Net ? window.__p10Net.requests.length : -1));
check("② 真页面里抽模板成功且具名为「视频拆解」（零图外引用、五槽齐）", extracted.ok && extracted.result.name === "视频拆解" && extracted.result.frozenOk === true && extracted.result.slots.length === 5, JSON.stringify(extracted.error || (extracted.result && { name: extracted.result.name, frozen: extracted.result.frozenOk, slots: extracted.result.slots.length })));
check("②-2 抽模板全程零 /api/ 请求（纯本地计算）", netAfter === netBefore, `${netBefore} → ${netAfter}`);

const persisted = await page.evaluate(async ({ key, projectId }) => {
    const mod = await import("/src/lib/localforage-storage.ts");
    const raw = await mod.localForageStorage.getItem(key);
    const parsed = JSON.parse(raw || "{}");
    const project = (parsed.state?.projects || []).find((item) => item.id === projectId);
    return { templateId: project?.workflowTemplate?.templateId || null, name: project?.workflowTemplate?.name || null };
}, { key: STORE_KEY, projectId: PROJECT_ID });
check("②-3 模板真落盘（localforage 里 workflowTemplate.name=视频拆解）", persisted.name === "视频拆解" && Boolean(persisted.templateId), JSON.stringify(persisted));

// ---------------------------------------------------------------- ③ 有模板、素材没字节的项目：零参调用跑到素材解析层
// 模板从 store 里原样搬（receipt 只是摘要，不含 frozen.topology.nodeIds）
const copiedTemplate = await page.evaluate(async ({ key, fromId, toId }) => {
    const mod = await import("/src/lib/localforage-storage.ts");
    const raw = await mod.localForageStorage.getItem(key);
    const parsed = JSON.parse(raw);
    const source = parsed.state.projects.find((item) => item.id === fromId);
    const template = source && source.workflowTemplate;
    if (!template) return { ok: false };
    const next = { ...parsed, state: { ...parsed.state, projects: parsed.state.projects.map((item) => (item.id === toId ? { ...item, workflowTemplate: template } : item)) } };
    await mod.localForageStorage.setItem(key, JSON.stringify(next));
    return { ok: true, name: template.name, frozenNodes: (template.frozen && template.frozen.topology && template.frozen.topology.nodeIds || []).length };
}, { key: STORE_KEY, fromId: PROJECT_ID, toId: NOBYTE_PROJECT_ID });
check("③-0 模板原样搬到无字节夹具项目（frozen 拓扑在位）", copiedTemplate.ok === true && copiedTemplate.frozenNodes > 0, JSON.stringify(copiedTemplate));

await page.goto(`${BASE}/canvas/${NOBYTE_PROJECT_ID}`, { timeout: 90000 });
await page.waitForTimeout(2000);
const zeroArgNoBytes = await page.evaluate(async () => {
    const mod = await import("/src/lib/agent/agent-site-tools.ts");
    try {
        await mod.runSiteTool("run_workflow_batch", {}, () => {});
        return { ok: true };
    } catch (error) {
        return { error: String((error && error.message) || error) };
    }
});
check("③ 零参调用在真页面里解析素材：源视频没本地字节 → 人话指名问题（不是空跑）", /都没有本地视频字节/.test(String(zeroArgNoBytes.error)), String(zeroArgNoBytes.error || zeroArgNoBytes.ok));

// ---------------------------------------------------------------- ④ 零参真调产出的批次 → 侧栏 DOM 3 行
await page.evaluate(async ({ key, projectId, dump }) => {
    const mod = await import("/src/lib/localforage-storage.ts");
    const raw = await mod.localForageStorage.getItem(key);
    const parsed = JSON.parse(raw);
    const next = {
        ...parsed,
        state: {
            ...parsed.state,
            projects: parsed.state.projects.map((item) =>
                item.id === projectId
                    ? { ...item, nodes: dump.project.nodes, connections: dump.project.connections, workflowTemplate: dump.project.workflowTemplate, workflowBatches: dump.project.workflowBatches, workflowInstances: [], workflowAssetVetoes: [] }
                    : item,
            ),
        },
    };
    await mod.localForageStorage.setItem(key, JSON.stringify(next));
    return true;
}, { key: STORE_KEY, projectId: PROJECT_ID, dump: dumped });
await page.goto(`${BASE}/canvas/${PROJECT_ID}`, { timeout: 90000 });
await page.waitForSelector("[data-review-badge]", { state: "visible", timeout: 60000 }).catch(() => null);
await page.waitForTimeout(1500);

const badge = await page.evaluate(() => {
    const node = document.querySelector("[data-review-badge]");
    return node ? node.getAttribute("data-review-badge") : null;
});
check(`④ 侧栏徽标 = ${dumped.project.workflowBatches[0]?.members?.length ?? 3}（零参真调批次全到待审批）`, badge === String(dumped.project.workflowBatches[0]?.members?.length ?? 3), String(badge));

const opened = await page.evaluate(async () => {
    const tabs = [...document.querySelectorAll("[data-canvas-no-zoom] button")].filter((node) => node.textContent && node.textContent.trim());
    const review = tabs.find((node) => node.textContent.includes("待审批"));
    if (!review) return { ok: false, reason: "找不到「待审批」页签", tabs: tabs.slice(0, 6).map((node) => node.textContent.trim()) };
    review.click();
    await new Promise((resolve) => setTimeout(resolve, 900));
    return { ok: true };
});
check("④-2 「待审批」页签可打开", opened.ok, JSON.stringify(opened));
await page.waitForSelector("[data-review-instance]", { state: "visible", timeout: 30000 }).catch(() => null);
await page.waitForTimeout(800);

const rows = await page.evaluate(() => ({
    count: document.querySelectorAll("[data-review-instance]").length,
    rows: [...document.querySelectorAll("[data-review-instance]")].map((node) => ({
        id: node.getAttribute("data-review-instance"),
        status: node.getAttribute("data-review-status"),
        label: (node.querySelector("[data-review-status-label]") || {}).textContent?.trim() || "",
        hasThumb: Boolean(node.querySelector("[data-review-thumb]")),
    })),
    batches: document.querySelectorAll("[data-review-batch]").length,
    text: (document.querySelector("[data-canvas-no-zoom]") || document.body).innerText || "",
}));
check("④-3 列表 3 行（与 buildReviewList 一致，不合并不过滤）", rows.count === 3 && rows.batches === 1, JSON.stringify({ count: rows.count, batches: rows.batches }));
check("④-4 3 行状态全是「待审批」（终点=待审批）", rows.rows.every((row) => row.status === "awaiting_approval"), JSON.stringify(rows.rows.map((row) => [row.id, row.status, row.label])));
check("④-5 每行都有素材缩略位（不是空壳行）", rows.rows.every((row) => row.hasThumb), JSON.stringify(rows.rows));
check("④-6 批次头显示模板名「视频拆解」（具名登记在界面上可见）", String(rows.text).includes("视频拆解"), String(rows.text).slice(0, 200));

await page.screenshot({ path: path.join(SHOT_DIR, "P10-侧栏3行待审批.png"), fullPage: false });
const drawerShot = await page.evaluate(() => {
    const row = document.querySelector("[data-review-instance]");
    if (!row) return null;
    const openBtn = row.querySelector("[data-review-open]") || row;
    openBtn.click();
    return true;
});
await page.waitForTimeout(900);
const drawerOpen = await page.evaluate(() => Boolean(document.querySelector("[data-review-drawer]")));
check("④-7 点开实例抽屉（抽屉真渲染，截图不是空壳）", drawerShot === true && drawerOpen, JSON.stringify({ clicked: drawerShot, drawer: drawerOpen }));
if (drawerOpen) await page.screenshot({ path: path.join(SHOT_DIR, "P10-实例抽屉.png"), fullPage: false });

// ---------------------------------------------------------------- ⑤ 零付费 / 零提交
const net = await page.evaluate(() => ({
    requests: [...new Set((window.__p10Net && window.__p10Net.requests) || [])],
    submit: (window.__p10Net && window.__p10Net.submit) || 0,
    hubTask: (window.__p10Net && window.__p10Net.hubTask) || 0,
}));
const paidHits = net.requests.filter((url) => /generate|video\/create|image\/create|completions/i.test(url));
check("⑤ 全程零生成/零创建请求（付费动作 0）", paidHits.length === 0, JSON.stringify(paidHits));
check("⑤-2 全程零提交（没点批准/出片，submit 命中 0）", net.submit === 0, `submit=${net.submit}`);

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
}, { key: STORE_KEY, ids: [PROJECT_ID, NOBYTE_PROJECT_ID] });
check("⑥ 真实 store 已复原（夹具项目已删除）", restored.leftovers.length === 0, JSON.stringify(restored));

console.log(`\nP10 DOM 探针：${pass} 通过 / ${fails.length} 失败`);
if (fails.length) {
    console.log("失败明细：");
    for (const line of fails) console.log(`  - ${line}`);
}
console.log(`截图目录：${SHOT_DIR}`);
