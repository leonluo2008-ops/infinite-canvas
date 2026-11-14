/**
 * P9 探针（ego-browser DOM 层，真页面 + 真 store + 真渲染）。
 *
 * 跑法（仓库根或 web/ 下都行）：
 *   ego-browser nodejs < web/scripts/probe-p9-entry-tools.ego.mjs
 * 前置：
 *   1) `cd web && npm run dev`（http://127.0.0.1:3000）已在跑；
 *   2) 先跑 node 层探针 `node scripts/probe-p9-entry-tools.mjs`：它把**真调产出的批次**落成
 *      `%TMPDIR%/hermes/cache/scratch/p9-out/p9-batch-fixture.json`，本探针直接拿它 seed，
 *      所以侧栏看到的 3 行不是手写夹具，而是生产代码跑出来的批次记录。
 * space 名：p9-entry-tools（跑完不关，便于复查；截图落 wanrepro/docs/证据-P9批次入口-20261114/）。
 *
 * 本层能证与不能证（如实写在报告里）：
 *   能证：① 三工具已进页面侧分发面（SITE_TOOL_NAMES/标签）；② 在真页面里**真调** extract_workflow_template
 *        （零网络零付费）→ 回执字段 + workflowTemplate 真落盘（刷新后仍在）；③ batch/instance 的拒绝路径真在跑
 *        （人话报错，不是空壳）；④ 侧栏「待审批」页签对真调批次渲染 3 行、全为待审批、徽标=3；
 *        ⑤ 全程零生成/零提交请求（fetch 计数）。
 *   不能证：浏览器里真跑队列（P4 组装要真调文本模型、拉片要真视频字节）——该语义由 node 层探针 stub 证明。
 */
import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";

const BASE = "http://127.0.0.1:3000";
const SPACE = "p9-entry-tools";
const SHOT_DIR = "E:/User/GitHub/infinite-canvas/wanrepro/docs/证据-P9批次入口-20261114";
const FIXTURE_JSON = "C:/Users/leon3/AppData/Local/hermes/cache/scratch/p9-out/p9-batch-fixture.json";
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
const PROJECT_ID = `p9-probe-${stamp}`;
const PROJECT_TITLE = `P9 探针 ${new Date(stamp).toISOString().slice(11, 19)}`;
const CHAIN_IDS = ["video-src", "lap-1", "ins-1", "it-1", "cfg-1", "fin-1", "cfgv-1", "out-video", "slot-female", "slot-perfume"];

/** 抽模板用的夹具项目（与 node 层同构的「拆解→生成」链 + 3 个空素材视频节点）。 */
const seedProject = () => ({
    id: PROJECT_ID,
    title: PROJECT_TITLE,
    nodes: [
        { id: "video-src", type: "video", title: "源视频.mp4", position: { x: 0, y: 0 }, width: 320, height: 200, metadata: { content: "", storageKey: "", mimeType: "video/mp4" } },
        { id: "lap-1", type: "lapian", title: "拉片", position: { x: 400, y: 0 }, width: 420, height: 520, metadata: { content: "# 拉片正文", taskId: "p9-lap-task", jobId: "p9-lap-job", lapPhase: "done" } },
        { id: "ins-1", type: "text", title: "转换指令", position: { x: 900, y: 0 }, width: 320, height: 200, metadata: { content: "指令正文", prompt: "指令正文", promptTemplate: "seedance-flow" } },
        { id: "it-1", type: "text", title: "改造意图", position: { x: 900, y: 260 }, width: 320, height: 200, metadata: { content: "旧意图（12 字左右）", prompt: "旧意图（12 字左右）", lapianIntent: true } },
        { id: "cfg-1", type: "config", title: "提示词生成配置", position: { x: 1300, y: 0 }, width: 360, height: 220, metadata: { generationMode: "text", model: "default::m", lapianSourceId: "lap-1", composerContent: "拉片：@[node:lap-1]\n指令：@[node:ins-1]\n意图：@[node:it-1]" } },
        { id: "fin-1", type: "text", title: "终稿（占位）", position: { x: 1700, y: 0 }, width: 360, height: 240, metadata: { fontSize: 14, model: "default::m", textCount: 1, content: "" } },
        { id: "cfgv-1", type: "config", title: "视频生成配置", position: { x: 2100, y: 0 }, width: 360, height: 200, metadata: { generationMode: "video", model: "k::sd2.5", size: "720x1280", composerContent: "提示词：@[node:fin-1]" } },
        { id: "out-video", type: "video", title: "生成结果", position: { x: 2500, y: 0 }, width: 320, height: 200, metadata: { content: "", storageKey: "", status: "idle", videoTaskId: "task-out", videoProgress: 100 } },
        { id: "slot-female", type: "image", title: "资产位·人物·女性A", position: { x: 520, y: 600 }, width: 200, height: 200, metadata: { content: "", storageKey: "", assetSlotSourceId: "lap-1", assetSlotOrder: 0, assetSlotName: "女性A", assetSlotType: "人物", assetSlot: { desc: "原片模特，红色风衣", span: "00:00-02.00" } } },
        { id: "slot-perfume", type: "image", title: "资产位·主体·香水", position: { x: 760, y: 600 }, width: 200, height: 200, metadata: { content: "", storageKey: "", assetSlotSourceId: "lap-1", assetSlotOrder: 1, assetSlotName: "香水", assetSlotType: "主体", assetSlot: { desc: "细长圆柱体", span: "02.00-05.00" } } },
    ],
    connections: [
        { id: "e1", fromNodeId: "video-src", toNodeId: "lap-1" },
        { id: "e2", fromNodeId: "lap-1", toNodeId: "cfg-1" },
        { id: "e3", fromNodeId: "ins-1", toNodeId: "cfg-1" },
        { id: "e4", fromNodeId: "it-1", toNodeId: "cfg-1" },
        { id: "e5", fromNodeId: "cfg-1", toNodeId: "fin-1" },
        { id: "e6", fromNodeId: "fin-1", toNodeId: "cfgv-1" },
        { id: "e7", fromNodeId: "cfgv-1", toNodeId: "out-video" },
    ],
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
        window.__p9Net = log;
        const orig = window.fetch.bind(window);
        const json = (payload, status = 200) => new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });
        window.fetch = async function (input, init) {
            const url = typeof input === "string" ? input : (input && input.url) || "";
            if (url.includes("/api/")) log.requests.push(url);
            if (url.includes("/api/hub/task/")) {
                log.hubTask += 1;
                const id = decodeURIComponent(url.split("/api/hub/task/")[1].split(/[/?#]/)[0]);
                return json({ task: { local_id: id, state: "awaiting_approval", bridge_id: "p9-bridge", intent: "P9 探针夹具", video_name: "探针素材.mp4", created_at: "2026-11-14T00:00:00.000Z", gen_progress: null, has_output: false, pair_lines: [], asset_cards: [], suggestions: [], summary_md: "", checklist_md: "", prompt_md: "", 自检: [], submit: { form: "none", active_submit: false, can_resume: false }, error: "" } });
            }
            if (url.includes("/api/hub/submit/")) {
                log.submit += 1;
                return json({ ok: true, job_id: "p9-stub-job" });
            }
            if (url.includes("/api/hub/")) return json({ ok: true });
            return orig(input, init);
        };
    })();`,
});

// 写夹具（备份原值，跑完原样写回）
const backup = await page.evaluate(async ({ key, project }) => {
    const mod = await import("/src/lib/localforage-storage.ts");
    const raw = await mod.localForageStorage.getItem(key);
    const parsed = raw ? JSON.parse(raw) : null;
    const state = parsed && parsed.state ? parsed.state : { projects: [] };
    const next = { ...(parsed || {}), state: { ...state, projects: [...(state.projects || []), project], deletedProjects: state.deletedProjects || [] }, version: parsed?.version ?? 1 };
    await mod.localForageStorage.setItem(key, JSON.stringify(next));
    return { raw, hadValue: Boolean(raw), projectCount: (state.projects || []).length };
}, { key: STORE_KEY, project: seedProject() });
const backupRaw = backup.raw;
check("P0 真实 store 已备份原值（跑完原样写回）", typeof backup.hadValue === "boolean", JSON.stringify(backup));

await page.goto(`${BASE}/canvas/${PROJECT_ID}`, { timeout: 90000 });
await page.waitForLoadState("load");
await page.waitForTimeout(2500);

// ---------------------------------------------------------------- ① 页面侧分发面：三工具在册
const dispatch = await page.evaluate(async () => {
    const mod = await import("/src/lib/agent/agent-site-tools.ts");
    const wanted = ["extract_workflow_template", "run_workflow_batch", "run_workflow_instance"];
    return {
        names: wanted.map((name) => ({ name, listed: mod.SITE_TOOL_NAMES.includes(name), isSite: mod.isSiteTool(name), label: mod.SITE_TOOL_LABELS[name] || "" })),
        total: mod.SITE_TOOL_NAMES.length,
    };
});
check("① 三工具已进页面分发面（SITE_TOOL_NAMES + isSiteTool + 中文标签）", dispatch.names.every((row) => row.listed && row.isSite && row.label.length > 3), JSON.stringify(dispatch));

// ---------------------------------------------------------------- ② 真调 extract_workflow_template（零网络零付费）
const netBefore = await page.evaluate(() => (window.__p9Net ? window.__p9Net.requests.length : -1));
const extracted = await page.evaluate(async ({ chain }) => {
    const mod = await import("/src/lib/agent/agent-site-tools.ts");
    try {
        const result = await mod.runSiteTool("extract_workflow_template", { sourceNodeIds: chain, name: "P9 页面模板", templateId: "p9page" }, () => {});
        return { ok: true, result };
    } catch (error) {
        return { ok: false, error: String((error && error.message) || error) };
    }
}, { chain: CHAIN_IDS });
const netAfter = await page.evaluate(() => (window.__p9Net ? window.__p9Net.requests.length : -1));
check("② 真页面里调用 extract_workflow_template 成功（callTool 级真调，不是空壳）", extracted.ok, JSON.stringify(extracted.error));
check("②-2 回执含模板 id / 五槽 / 冻结项 / 零图外引用", extracted.ok && extracted.result.templateId === "p9page" && extracted.result.slots.length === 5 && extracted.result.frozenOk === true && extracted.result.externalReferences.length === 0, JSON.stringify(extracted.result && { id: extracted.result.templateId, slots: extracted.result.slots.length, frozen: extracted.result.frozenOk, refs: extracted.result.externalReferences && extracted.result.externalReferences.length }));
check("②-3 抽模板全程零 /api/ 请求（纯本地计算）", netAfter === netBefore, `${netBefore} → ${netAfter}`);

const persisted = await page.evaluate(async ({ key, projectId }) => {
    const mod = await import("/src/lib/localforage-storage.ts");
    const raw = await mod.localForageStorage.getItem(key);
    const parsed = JSON.parse(raw || "{}");
    const project = (parsed.state?.projects || []).find((item) => item.id === projectId);
    return { templateId: project?.workflowTemplate?.templateId || null, frozen: project?.workflowTemplate?.frozen ? Object.keys(project.workflowTemplate.frozen).length : 0 };
}, { key: STORE_KEY, projectId: PROJECT_ID });
check("②-4 模板真落盘（localforage 里 workflowTemplate=p9page，冻结项在位）", persisted.templateId === "p9page" && persisted.frozen >= 3, JSON.stringify(persisted));

await page.reload({ timeout: 90000 });
await page.waitForTimeout(2500);
const afterReload = await page.evaluate(async ({ chain, projectId }) => {
    const mod = await import("/src/lib/agent/agent-site-tools.ts");
    // 同一份输入（同节点集 + 同名）连抽两次：派生 id 必须一致（确定性），且第二次仍落盘同一个模板
    const once = await mod.runSiteTool("extract_workflow_template", { sourceNodeIds: chain, name: "P9 派生模板" }, () => {});
    const twice = await mod.runSiteTool("extract_workflow_template", { sourceNodeIds: chain, name: "P9 派生模板" }, () => {});
    // 显式给 id：原地覆盖（不产生第二份模板）
    const named = await mod.runSiteTool("extract_workflow_template", { sourceNodeIds: chain, name: "P9 页面模板", templateId: "p9page" }, () => {});
    const store = await import("/src/lib/localforage-storage.ts");
    const raw = await store.localForageStorage.getItem("infinite-canvas:canvas_store");
    const parsed = JSON.parse(raw || "{}");
    const project = (parsed.state?.projects || []).find((item) => item.id === projectId);
    return { first: once.templateId, second: twice.templateId, named: named.templateId, frozenOk: twice.frozenOk, stored: project?.workflowTemplate?.templateId || null };
}, { chain: CHAIN_IDS, projectId: PROJECT_ID });
check("②-5 刷新后重抽：同输入派生 id 一致（确定性），显式 id 原地覆盖", afterReload.first === afterReload.second && afterReload.named === "p9page" && afterReload.stored === "p9page" && afterReload.frozenOk === true, JSON.stringify(afterReload));

// ---------------------------------------------------------------- ③ 拒绝路径真在跑（人话报错）
const refusals = await page.evaluate(async () => {
    const mod = await import("/src/lib/agent/agent-site-tools.ts");
    const run = async (name, input) => {
        try {
            await mod.runSiteTool(name, input, () => {});
            return null;
        } catch (error) {
            return String((error && error.message) || error);
        }
    };
    return {
        missingNode: await run("run_workflow_batch", { materials: [{ sourceVideoNodeId: "not-on-canvas" }] }),
        crossTemplate: await run("run_workflow_instance", { templateId: "other-template", material: { storageKey: "video:x" } }),
        noMaterial: await run("run_workflow_instance", {}),
    };
});
check("③ 素材节点不存在 → 人话指名（工具真执行到参数解析层）", /找不到源视频节点 not-on-canvas/.test(String(refusals.missingNode)), String(refusals.missingNode));
check("③-2 跨画布模板 id → 拒绝（不串批次）", /不一致/.test(String(refusals.crossTemplate)), String(refusals.crossTemplate));
check("③-3 缺素材 → 人话提示给 sourceVideoNodeId/storageKey", /sourceVideoNodeId/.test(String(refusals.noMaterial)), String(refusals.noMaterial));

// ---------------------------------------------------------------- ④ 侧栏渲染：真调产出的 3 成员批次 → 3 行
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
await page.reload({ timeout: 90000 });
await page.waitForSelector("[data-review-badge]", { state: "visible", timeout: 60000 }).catch(() => null);
await page.waitForTimeout(1500);

const badge = await page.evaluate(() => {
    const node = document.querySelector("[data-review-badge]");
    return node ? node.getAttribute("data-review-badge") : null;
});
check("④ 侧栏徽标 = 3（批次 3 个实例全部待审批）", badge === "3", String(badge));

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
}));
check("④-3 列表 3 行（与 buildReviewList 一致，不合并不过滤）", rows.count === 3 && rows.batches === 1, JSON.stringify({ count: rows.count, batches: rows.batches }));
check("④-4 3 行状态全是「待审批」（P4 终点）", rows.rows.every((row) => row.status === "awaiting_approval"), JSON.stringify(rows.rows.map((row) => [row.id, row.status, row.label])));
check("④-5 每行都有素材缩略位（不是空壳行）", rows.rows.every((row) => row.hasThumb), JSON.stringify(rows.rows));

await page.screenshot({ path: path.join(SHOT_DIR, "P9-侧栏3行待审批.png"), fullPage: false });
const shot2 = await page.evaluate(() => {
    const row = document.querySelector("[data-review-instance]");
    if (!row) return null;
    row.click();
    return true;
});
await page.waitForTimeout(900);
if (shot2) await page.screenshot({ path: path.join(SHOT_DIR, "P9-实例抽屉.png"), fullPage: false });

// ---------------------------------------------------------------- ⑤ 零付费 / 零提交
const net = await page.evaluate(() => ({
    requests: [...new Set((window.__p9Net && window.__p9Net.requests) || [])],
    submit: (window.__p9Net && window.__p9Net.submit) || 0,
    hubTask: (window.__p9Net && window.__p9Net.hubTask) || 0,
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
const restored = await page.evaluate(async ({ key, projectId }) => {
    const mod = await import("/src/lib/localforage-storage.ts");
    const raw = await mod.localForageStorage.getItem(key);
    const parsed = JSON.parse(raw || "{}");
    return { exists: (parsed.state?.projects || []).some((item) => item.id === projectId) };
}, { key: STORE_KEY, projectId: PROJECT_ID });
check("⑥ 真实 store 已复原（夹具项目已删除）", restored.exists === false, JSON.stringify(restored));

console.log(`\nP9 DOM 探针：${pass} 通过 / ${fails.length} 失败`);
if (fails.length) {
    console.log("失败明细：");
    for (const line of fails) console.log(`  - ${line}`);
}
console.log(`截图目录：${SHOT_DIR}`);
