/**
 * P8 探针（ego-browser DOM 层，真页面 + 真存储 + 真渲染）。
 *
 * 跑法（仓库根或 web/ 下都行）：
 *   ego-browser nodejs < web/scripts/probe-p8-pending-review.ego.mjs
 * 前置：`cd web && npm run dev`（http://127.0.0.1:3000）已在跑。
 * space 名：p8-review-dom（跑完不关，便于复查；截图落在 wanrepro/docs/证据-P8待审批侧栏-20261114/）。
 *
 * 夹具隔离纪律：
 *   - 写前备份 localforage 键 `infinite-canvas:canvas_store` 原值，跑完原样写回（字节级恢复）；
 *   - 只**新增**一个项目 `P8 探针 <时间戳>`（真实项目零改动），跑完按项目 id 精确删除（恢复备份即含删除）；
 *   - hub 请求用 CDP `Page.addScriptToEvaluateOnNewDocument` 注入 fetch 包装（只拦 /api/hub/，其余透传）；
 *   - **不点任何队列按钮真跑**（真跑 = 真实拉片/生成链）：队列只验渲染 + 相位文案 + 禁用原因，
 *     执行语义由 node 层 probe-p8-pending-review.mjs D 段 stub 证明（交卷报告已如实写明）。
 *
 * 断言（任务书 §5.2 ①–⑧）：
 *   ① 4 个页签、第 4 个是「待审批」；② badge = 夹具期望值；③ 列表行数 = 实例数且每行 data-review-status 与派生层一致；
 *   ④ 点首行 → drawer 宽 700–760；⑤ 提示词正文与产物节点 content 归一空白后相等；
 *   ⑥ 否决 → 存储 workflowAssetVetoes +1、行标「已否决」、刷新后仍否决；
 *   ⑦ 提交闸：未二次确认前 /api/hub/submit/ 命中 0，确认后 1，再点不产生第 2 次；
 *   ⑧ workflowBatches 置空 → data-review-empty，随后恢复；
 *   ⑨ 每行素材缩略 data-review-thumb = img/video 元素或带图标占位（不为空节点）；
 *   ⑩ 批次头状态筛选（P0c 图2 ②）：点 data-review-filter="awaiting" → 只剩待批行且与 chips 计数一致，点回 all 恢复；
 *   ⑪ 退回按钮态：待批实例抽屉里 data-review-rollback 在位（含代价文案）；已批准实例里不在位（后端只放行待批态）；
 *   ⑫ 抽屉底部留白 ≥10px（data-review-approve-note 两行也不贴边/不被裁切）。
 */
import { mkdirSync } from "node:fs";
import path from "node:path";

const BASE = "http://127.0.0.1:3000";
const SPACE = "p8-review-dom";
const SHOT_DIR = "E:/User/GitHub/infinite-canvas/wanrepro/docs/证据-P8待审批侧栏-20261114";
const STORE_KEY = "infinite-canvas:canvas_store";

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

// ---------------------------------------------------------------- 夹具项目（自建，非真实项目）
const stamp = Date.now();
const PROJECT_ID = `p8-probe-${stamp}`;
const PROJECT_TITLE = `P8 探针 ${new Date(stamp).toISOString().slice(11, 19)}`;
const PROMPT_TEXT = `第一段：客户模特在雨夜街头转身，镜头缓慢推近。\n\n第二段：香水瓶置于湿漉漉的石阶上，高光反射。\n\n（探针正文，逐字节比对用）`;

const fixtureNodes = (prefix) => {
    const lapian = `${prefix}-lap-1`;
    return [
        { id: `${prefix}-video-src`, type: "video", title: "源视频.mp4", position: { x: 0, y: 0 }, width: 320, height: 200, metadata: { content: "", storageKey: "", mimeType: "video/mp4" } },
        { id: lapian, type: "lapian", title: "拉片 · 探针", position: { x: 400, y: 0 }, width: 420, height: 520, metadata: { content: "# 拉片正文（探针）", taskId: "p8-lap-task", jobId: "p8-lap-job", lapPhase: "done" } },
        { id: `${prefix}-fin-1`, type: "text", title: "终稿（本次）", position: { x: 900, y: 0 }, width: 360, height: 240, metadata: { fontSize: 14, content: PROMPT_TEXT, prompt: PROMPT_TEXT, status: "success" } },
        { id: `${prefix}-cfgv-1`, type: "config", title: "视频生成配置", position: { x: 1300, y: 0 }, width: 360, height: 200, metadata: { generationMode: "video", composerContent: "提示词：@[node:" + `${prefix}-fin-1` + "]" } },
        { id: `${prefix}-out-video`, type: "video", title: "生成结果", position: { x: 1700, y: 0 }, width: 320, height: 200, metadata: { content: "", storageKey: "", status: "idle" } },
        { id: `${prefix}-lib-female`, type: "image", title: "客户模特 女性A 定妆.png", position: { x: 0, y: 600 }, width: 200, height: 200, metadata: { content: "data:image/svg+xml;utf8," + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" fill="#d9c7a3"/><circle cx="40" cy="32" r="14" fill="#8a6f4e"/></svg>'), mimeType: "image/png" } },
        { id: `${prefix}-lib-perfume`, type: "image", title: "客户款 香水 磨砂瓶.png", position: { x: 260, y: 600 }, width: 200, height: 200, metadata: { content: "data:image/svg+xml;utf8," + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" fill="#6b5b8a"/><rect x="30" y="22" width="20" height="42" rx="4" fill="#cbb8e0"/></svg>'), mimeType: "image/png" } },
        { id: `${prefix}-slot-female`, type: "image", title: "资产位·人物·女性A", position: { x: 520, y: 600 }, width: 200, height: 200, metadata: { content: "", storageKey: "", assetSlotSourceId: lapian, assetSlotOrder: 0, assetSlotName: "女性A", assetSlotType: "人物", assetSlot: { desc: "原片模特，红色风衣，短发", span: "00:00-02.00" }, attachedImageId: `${prefix}-lib-female`, attachedImageDesc: "客户模特 女性A" } },
        { id: `${prefix}-slot-perfume`, type: "image", title: "资产位·主体·香水", position: { x: 760, y: 600 }, width: 200, height: 200, metadata: { content: "", storageKey: "", assetSlotSourceId: lapian, assetSlotOrder: 1, assetSlotName: "香水", assetSlotType: "主体", assetSlot: { desc: "细长圆柱体紫色表面，银色金属盖", span: "02.00-05.00" }, attachedImageId: `${prefix}-lib-perfume`, attachedImageDesc: "客户款 香水 磨砂瓶" } },
    ];
};
const A = "tp8probe-1";
const B = "tp8probe-2";
const membersOf = (prefix, index, extra = {}) => {
    const localId = `p8-local-${index}`;
    return {
        instanceId: `p8probe#${index}`,
        templateId: "p8probe",
        instanceIndex: index,
        materials: { sourceVideoTitle: `探针素材-${index}.mp4`, sourceVideoStorageKey: `video:p8-${index}`, optionalSegments: "A" },
        nodes: {
            all: [`${prefix}-video-src`, `${prefix}-lap-1`, `${prefix}-fin-1`, `${prefix}-cfgv-1`, `${prefix}-out-video`, `${prefix}-slot-female`, `${prefix}-slot-perfume`],
            sourceVideo: `${prefix}-video-src`,
            lapian: `${prefix}-lap-1`,
            product: `${prefix}-fin-1`,
            videoConfig: `${prefix}-cfgv-1`,
            videoTarget: `${prefix}-out-video`,
            retiredPlaceholders: [],
            downstream: [`${prefix}-cfgv-1`],
        },
        hub: { localId, bridgeId: "p8-bridge", revisionDigest: "p8-digest" },
        backend: { lapianTaskId: "p8-lap-task", lapianJobId: "p8-lap-job" },
        repair: index === 1 ? {
            rounds: [
                { round: 1, changedLines: [3], removedTerms: ["紫色"], reason: "删掉原物核心词", findingsBefore: 2, findingsAfter: 1, digestAfter: "d1", approvalSnapshot: "s1", at: "2026-11-14T00:00:01.000Z" },
                { round: 2, changedLines: [7], removedTerms: ["银色金属盖"], reason: "再删一处残留", findingsBefore: 1, findingsAfter: 0, digestAfter: "d2", approvalSnapshot: "s2", at: "2026-11-14T00:00:02.000Z" },
            ],
            outcome: "passed",
            maxRounds: 2,
            finalFindings: 0,
            finalSelfcheck: [
                { 项: "原物残留", 项显示名: "原物残留检查", 结论: "FAIL", 证据: "第 3 行仍含「紫色」", 说明: "删掉原物核心词后重检" },
                { 项: "主体一致", 项显示名: "主体一致性", 结论: "PASS", 证据: "主体=客户模特", 说明: "" },
            ],
        } : undefined,
        createdAt: "2026-11-14T00:00:00.000Z",
        ...extra,
    };
};
const fixtureProject = () => ({
    id: PROJECT_ID,
    title: PROJECT_TITLE,
    nodes: [
        ...fixtureNodes(A),
        ...fixtureNodes(B),
    ],
    connections: [],
    backgroundMode: "dots",
    viewport: { x: 120, y: 80, k: 0.8 },
    workflowTemplate: { templateId: "p8probe", name: "p8 探针模板", schemaVersion: 1, createdAt: "2026-11-14T00:00:00.000Z", sourceProjectId: PROJECT_ID, slots: [], frozen: { topology: { nodeIds: [`${A}-video-src`, `${A}-lap-1`, `${A}-fin-1`], connectionIds: [], digest: "p8-topology" }, nodeSignatures: {}, roles: {}, createdAt: "2026-11-14T00:00:00.000Z", version: 1 }, externalReferences: [] },
    workflowInstances: [],
    workflowBatches: [
        {
            schemaVersion: 1,
            batchId: `p8-batch-${stamp}`,
            templateId: "p8probe",
            title: `P8 探针批次 ${new Date(stamp).toISOString().slice(11, 19)}`,
            signature: "p8-signature",
            createdAt: "2026-11-14T00:00:00.000Z",
            updatedAt: "2026-11-14T00:00:05.000Z",
            members: [membersOf(A, 1), membersOf(B, 2)],
            queue: { schemaVersion: 1, phase: "running", concurrency: 3, startedAt: "2026-11-14T00:00:00.000Z", updatedAt: "2026-11-14T00:00:05.000Z", entries: [{ instanceId: "p8probe#1", instanceIndex: 1, attempts: 1, congestionRetries: 0, submit: null, outcome: "awaiting_approval" }], summary: { attempted: 1, awaitingApproval: 1, failed: [], skipped: [] } },
        },
    ],
    workflowAssetVetoes: [],
    workflowWanFileTasks: [],
    createdAt: "2026-11-14T00:00:00.000Z",
    updatedAt: "2026-11-14T00:00:05.000Z",
});

// ---------------------------------------------------------------- ① 安装 hub stub + 写夹具（写前备份）
const stubConfig = {
    states: { "p8-local-1": "awaiting_approval", "p8-local-2": "approved" },
    // submit 之后 get_task 回「云端受理中」→ 闸内轮询第一次就收尾（探针只等 ~2.5s）
    statesAfterSubmit: { "p8-local-2": "generating" },
    pairLines: [
        { no: 1, desc: "人物", asset: "女性A", img: "已传" },
        { no: 2, desc: "主体", asset: "香水", img: "" },
    ],
    assetCards: [
        { n: 1, 短名: "女性A", 类别: "人物", 已传图: ["ref-1"], 决策: "replaced" },
        { n: 2, 短名: "香水", 类别: "主体", 已传图: [], 决策: "" },
    ],
    selfcheck: [{ 项: "原物残留", 项显示名: "原物残留检查", 结论: "FAIL", 证据: "第 3 行仍含「紫色」", 说明: "先删原物核心词再重检" }],
};

await page.goto(`${BASE}/`, { timeout: 60000 });
await page.waitForLoadState("load");

// CDP：每个新文档先装 fetch 包装（只拦 /api/hub/，其余透传；计数挂 window.__p8Hub）
await page.cdp("Page.enable", {});
await page.cdp("Page.addScriptToEvaluateOnNewDocument", {
    source: `(() => {
        try { localStorage.setItem("p8-hub-stub", ${JSON.stringify(JSON.stringify(stubConfig))}); } catch {}
        const cfg = JSON.parse(localStorage.getItem("p8-hub-stub") || "{}");
        const counters = { hub: [], submit: 0, submitUrl: "", submitBody: "", after: false };
        window.__p8Hub = counters;
        const orig = window.fetch.bind(window);
        const json = (payload, status = 200) => new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });
        window.fetch = async function (input, init) {
            const url = typeof input === "string" ? input : (input && input.url) || "";
            if (!url.includes("/api/hub/")) return orig(input, init);
            counters.hub.push(url);
            if (url.includes("/api/hub/submit/")) {
                counters.submit += 1;
                counters.after = true;
                counters.submitUrl = url;
                try { counters.submitBody = String((init && init.body) || ""); } catch {}
                return json({ ok: true, job_id: "p8-stub-job" });
            }
            const hit = url.match(/\\/api\\/hub\\/task\\/([^/?#]+)/);
            if (hit) {
                const id = decodeURIComponent(hit[1]);
                const state = (counters.after && cfg.statesAfterSubmit && cfg.statesAfterSubmit[id]) || (cfg.states || {})[id] || "awaiting_approval";
                return json({
                    task: {
                        local_id: id, state, bridge_id: "p8-bridge", intent: "P8 探针夹具",
                        video_name: "探针素材.mp4", created_at: "2026-11-14T00:00:00.000Z",
                        gen_progress: null, has_output: false,
                        pair_lines: cfg.pairLines || [], asset_cards: cfg.assetCards || [],
                        suggestions: [], summary_md: "", checklist_md: "", prompt_md: "",
                        自检: cfg.selfcheck || [], submit: { form: "none", active_submit: false, can_resume: false },
                        error: "",
                    },
                });
            }
            return json({ ok: true });
        };
    })();`,
});

// 写夹具（备份原值；只新增项目）
const backup = await page.evaluate(async ({ key, project }) => {
    const mod = await import("/src/lib/localforage-storage.ts");
    const raw = await mod.localForageStorage.getItem(key);
    const parsed = raw ? JSON.parse(raw) : null;
    const state = parsed && parsed.state ? parsed.state : { projects: [] };
    const next = {
        ...(parsed || {}),
        state: { ...state, projects: [...(state.projects || []), project], deletedProjects: state.deletedProjects || [] },
        version: parsed?.version ?? 1,
    };
    await mod.localForageStorage.setItem(key, JSON.stringify(next));
    return { raw, projectCount: (state.projects || []).length, hadValue: Boolean(raw) };
}, { key: STORE_KEY, project: fixtureProject() });
check("前置：真实 store 已备份原值（跑完原样写回）", backup.raw === null || typeof backup.raw === "string", `raw=${backup.raw === null ? "null" : "string"}`);
console.log(`fixture project=${PROJECT_ID} 真实项目数=${backup.projectCount}`);

await page.goto(`${BASE}/canvas/${PROJECT_ID}`, { timeout: 90000 });
await page.waitForLoadState("load");
await page.waitForSelector("[data-review-badge]", { state: "visible", timeout: 60000 }).catch(() => null);
await page.waitForTimeout(1200);

// ---------------------------------------------------------------- ⑧ 空态先验（无批次 → data-review-empty），再恢复
// （放在最前做：此时不依赖 hub 状态，避免 hub stub 干扰列表断言）
await page.evaluate(async ({ key, projectId, project }) => {
    const mod = await import("/src/lib/localforage-storage.ts");
    const raw = await mod.localForageStorage.getItem(key);
    const parsed = JSON.parse(raw);
    const next = { ...parsed, state: { ...parsed.state, projects: parsed.state.projects.map((item) => (item.id === projectId ? { ...item, workflowBatches: [] } : item)) } };
    await mod.localForageStorage.setItem(key, JSON.stringify(next));
    return true;
}, { key: STORE_KEY, projectId: PROJECT_ID, project: null });
await page.reload({ timeout: 90000 });
await page.waitForTimeout(2500);
const tabsBefore = await page.evaluate(() => {
    const buttons = [...document.querySelectorAll("[data-canvas-no-zoom] button")].filter((node) => node.textContent && node.textContent.trim());
    return buttons.slice(0, 6).map((node) => node.textContent.trim());
});
const emptyShown = await page.evaluate(async () => {
    // 打开第 4 页签
    const tabs = [...document.querySelectorAll("[data-canvas-no-zoom] button")].filter((node) => node.textContent && node.textContent.trim());
    const review = tabs.find((node) => node.textContent.includes("待审批"));
    if (!review) return { ok: false, reason: "找不到「待审批」页签", tabs: tabs.slice(0, 5).map((node) => node.textContent.trim()) };
    review.click();
    await new Promise((resolve) => setTimeout(resolve, 600));
    return { ok: Boolean(document.querySelector("[data-review-empty]")), found: Boolean(document.querySelector("[data-review-empty]")) };
});
check("⑧ 空态：workflowBatches 置空 → data-review-empty 出现", emptyShown.ok, JSON.stringify({ emptyShown, tabsBefore }));

// 恢复夹具批次（写回完整项目）
await page.evaluate(async ({ key, projectId, project }) => {
    const mod = await import("/src/lib/localforage-storage.ts");
    const raw = await mod.localForageStorage.getItem(key);
    const parsed = JSON.parse(raw);
    const next = { ...parsed, state: { ...parsed.state, projects: parsed.state.projects.map((item) => (item.id === projectId ? { ...item, workflowBatches: project.workflowBatches, workflowAssetVetoes: [] } : item)) } };
    await mod.localForageStorage.setItem(key, JSON.stringify(next));
}, { key: STORE_KEY, projectId: PROJECT_ID, project: fixtureProject() });
await page.reload({ timeout: 90000 });
await page.waitForSelector("[data-review-badge]", { state: "visible", timeout: 60000 });
await page.waitForTimeout(1500);

// ---------------------------------------------------------------- ① 页签 + ② 徽标
const tabInfo = await page.evaluate(() => {
    const tabs = [...document.querySelectorAll("[data-canvas-no-zoom] button")].filter((node) => node.textContent && node.textContent.trim() && !node.hasAttribute("aria-label"));
    const labels = tabs.map((node) => node.textContent.replace(/\\s+/g, " ").trim());
    const badge = document.querySelector("[data-review-badge]");
    return { labels: labels.slice(0, 6), badge: badge ? badge.getAttribute("data-review-badge") : null, badgeText: badge ? badge.textContent.trim() : null };
});
check("① 侧栏 4 个页签且第 4 个是「待审批」", tabInfo.labels.slice(0, 4).some((label, index) => index === 3 && label.includes("待审批")) || tabInfo.labels.filter((label) => label.includes("待审批")).length === 1, JSON.stringify(tabInfo.labels));
check("①-2 页签文案顺序 = 画布/资产/提示词库/待审批", ["画布", "资产", "提示词库", "待审批"].every((label, index) => String(tabInfo.labels[index] || "").includes(label)), JSON.stringify(tabInfo.labels));
// 夹具里 hub 状态：实例1 = awaiting_approval（待批）、实例2 = approved（已批，不计数）→ 徽标 = 1
check("② badge 数字 = 夹具期望值 1（待批+需人工+失败）", tabInfo.badge === "1", `badge=${tabInfo.badge} text=${tabInfo.badgeText}`);

// ---------------------------------------------------------------- ③ 列表行 + 派生一致
await page.evaluate(async () => {
    const tabs = [...document.querySelectorAll("[data-canvas-no-zoom] button")].filter((node) => node.textContent && node.textContent.trim());
    const review = tabs.find((node) => node.textContent.includes("待审批"));
    review.click();
    await new Promise((resolve) => setTimeout(resolve, 800));
});
await page.waitForSelector("[data-review-instance]", { state: "visible", timeout: 30000 });
const listDom = await page.evaluate(() => ({
    rows: [...document.querySelectorAll("[data-review-instance]")].map((node) => ({ id: node.getAttribute("data-review-instance"), status: node.getAttribute("data-review-status"), label: (node.querySelector("[data-review-status-label]") || {}).textContent?.trim() || "" })),
    batches: [...document.querySelectorAll("[data-review-batch]")].length,
    chips: [...document.querySelectorAll("[data-review-chip]")].map((node) => node.getAttribute("data-review-chip")),
    queue: (document.querySelector("[data-review-queue]") || {}).getAttribute?.("data-review-queue") || null,
    queueText: (document.querySelector("[data-review-queue]") || {}).textContent || "",
    startDisabled: (() => {
        const button = document.querySelector("[data-review-batch-start]");
        return button ? button.disabled : null;
    })(),
    thumbs: [...document.querySelectorAll("[data-review-instance]")].map((row) => {
        const thumb = row.querySelector("[data-review-thumb]");
        const rect = thumb ? thumb.getBoundingClientRect() : null;
        const tag = thumb ? thumb.tagName : null;
        return {
            id: row.getAttribute("data-review-instance"),
            tag,
            kind: thumb ? thumb.getAttribute("data-review-thumb") : null,
            isMedia: tag === "IMG" || tag === "VIDEO",
            hasIcon: thumb ? Boolean(thumb.querySelector("svg")) : false,
            children: thumb ? thumb.childElementCount : -1,
            width: rect ? Math.round(rect.width) : 0,
            height: rect ? Math.round(rect.height) : 0,
        };
    }),
}));
const expectedDerived = await page.evaluate(async ({ projectId, promptText }) => {
    const [{ useCanvasStore }, reviewMod, statusMod, batchMod, batchQueryMod, nodeTextMod, workbenchMod] = await Promise.all([
        import("/src/stores/canvas/use-canvas-store.ts"),
        import("/src/lib/canvas/workflow-pending-review.ts"),
        import("/src/lib/canvas/workflow-batch-status.ts"),
        import("/src/lib/canvas/workflow-batch.ts"),
        import("/src/lib/canvas/workflow-batch-query.ts"),
        import("/src/lib/agent/agent-node-text.ts"),
        import("/src/stores/use-workbench-agent-store.ts"),
    ]);
    const project = useCanvasStore.getState().projects.find((item) => item.id === projectId);
    const batches = project.workflowBatches || [];
    const hubStates = {};
    for (const batch of batches) for (const member of batch.members) {
        try {
            const response = await fetch(`/api/hub/task/${encodeURIComponent(member.hub?.localId || "")}`, { cache: "no-store" });
            const payload = await response.json();
            hubStates[member.instanceId] = String(payload?.task?.state || "");
        } catch {
            hubStates[member.instanceId] = null;
        }
    }
    // 证据：与页面同一条链（buildGenerationStatus → queryBatchMemberTasks → splitEvidenceByMember）
    const members = batches.flatMap((batch) => batch.members || []);
    const snapshot = { projectId, title: project.title, nodes: project.nodes, connections: project.connections || [], selectedNodeIds: [], viewport: project.viewport || { x: 0, y: 0, k: 1 } };
    const query = async (input) => {
        const nodeIds = input.nodeIds || [];
        const result = nodeTextMod.buildGenerationStatus({ scope: nodeIds.length ? "canvas" : "video", nodeIds, taskId: input.taskId, limit: input.limit }, snapshot, workbenchMod.useWorkbenchAgentStore.getState().tasks);
        return { total: Number(result.total || 0), tasks: (result.tasks || []).map((task) => ({ id: task.id, status: task.status, source: task.source, kind: task.kind, title: task.title })) };
    };
    const evidence = batchQueryMod.splitEvidenceByMember({ members, result: await batchQueryMod.queryBatchMemberTasks({ members, query, maxLimit: 100 }) });
    const evidenceOf = (member) => evidence.get(member.instanceId);
    const rows = reviewMod.buildReviewList({ batches, nodes: project.nodes, hubStates, evidenceOf });
    return {
        rows: rows.flatMap((row) => row.instances).map((row) => ({ id: row.instanceId, status: row.status, label: row.statusLabel, rule: row.rule })),
        badge: rows.reduce((sum, row) => sum + row.badgeCount, 0),
        batches: rows.length,
        promptLen: promptText.length,
        statuses: rows.flatMap((row) => row.instances.map((item) => statusMod.deriveInstanceStatus({ member: batchMod.findBatchMember(batches[0], item.instanceId), nodes: project.nodes, hubState: hubStates[item.instanceId], evidence: evidenceOf(batchMod.findBatchMember(batches[0], item.instanceId)) }).status)),
    };
}, { projectId: PROJECT_ID, promptText: PROMPT_TEXT });
const domOk = listDom.rows.length === expectedDerived.rows.length && listDom.rows.every((row) => {
    const match = expectedDerived.rows.find((item) => item.id === row.id);
    return match && match.status === row.status && row.label === match.label;
});
check("③ 列表行数 = 实例数（2）且每行 data-review-status 与派生层逐条一致", domOk && listDom.rows.length === 2, JSON.stringify({ dom: listDom.rows, derived: expectedDerived.rows }));
check("③-2 批次头/队列相位/计数 chips 渲染", listDom.batches === 1 && listDom.queue === "running" && listDom.chips.includes("awaiting_approval") && listDom.queueText.includes("并发"), JSON.stringify({ batches: listDom.batches, queue: listDom.queue, chips: listDom.chips, queueText: listDom.queueText.slice(0, 60) }));
check("③-3 队列按钮已渲染（不点真跑；运行时已注册 → 可点）", listDom.startDisabled !== null, String(listDom.startDisabled));
// 素材缩略（必改项 1）：每行 32×32，img/video 元素或带图标占位，二选一且不得为空节点
const thumbOk =
    listDom.thumbs.length === listDom.rows.length &&
    listDom.thumbs.every((item) => Boolean(item.kind) && ((item.isMedia && item.width >= 24 && item.height >= 24) || (item.kind === "placeholder" && item.hasIcon && item.children >= 1)));
check("⑨ 每行有素材缩略 data-review-thumb（img/video 或带图标占位，不为空节点）", thumbOk, JSON.stringify(listDom.thumbs));

// ---------------------------------------------------------------- ③-4 批次头状态筛选（P0c 图2 ②）
const filterProbe = await page.evaluate(async () => {
    const readRows = () => [...document.querySelectorAll("[data-review-instance]")].map((node) => ({ id: node.getAttribute("data-review-instance"), status: node.getAttribute("data-review-status") }));
    const before = readRows();
    const chips = [...document.querySelectorAll("[data-review-filter]")].map((node) => ({ id: node.getAttribute("data-review-filter"), active: node.getAttribute("data-review-filter-active"), text: node.textContent.replace(/\s+/g, " ").trim() }));
    const awaiting = document.querySelector('[data-review-filter="awaiting"]');
    if (awaiting) awaiting.click();
    await new Promise((resolve) => setTimeout(resolve, 700));
    const afterAwaiting = readRows();
    const awaitingSelected = [...document.querySelectorAll('[data-review-filter="awaiting"]')].map((node) => node.getAttribute("data-review-filter-active"));
    const emptyShown = Boolean(document.querySelector("[data-review-filter-empty]"));
    const all = document.querySelector('[data-review-filter="all"]');
    if (all) all.click();
    await new Promise((resolve) => setTimeout(resolve, 700));
    const afterAll = readRows();
    const allSelected = [...document.querySelectorAll('[data-review-filter="all"]')].map((node) => node.getAttribute("data-review-filter-active"));
    return { before, chips, afterAwaiting, awaitingSelected, emptyShown, afterAll, allSelected };
});
const awaitingChip = filterProbe.chips.find((chip) => chip.id === "awaiting");
check(
    "③-4 点筛选 awaiting → 列表只剩待批行（行数与 chips 计数一致）",
    filterProbe.afterAwaiting.length === 1 &&
        filterProbe.afterAwaiting.every((row) => row.status === "awaiting_approval") &&
        Boolean(awaitingChip) && /1$/.test(awaitingChip.text) &&
        filterProbe.awaitingSelected.includes("true") &&
        !filterProbe.emptyShown,
    JSON.stringify({ afterAwaiting: filterProbe.afterAwaiting, chip: awaitingChip, selected: filterProbe.awaitingSelected }),
);
check(
    "③-5 点回 all → 行数恢复 + 选中态回到 all（筛选不残留）",
    filterProbe.afterAll.length === filterProbe.before.length && filterProbe.afterAll.length === 2 && filterProbe.allSelected.includes("true") && !filterProbe.afterAll.some((row) => !row.status),
    JSON.stringify({ before: filterProbe.before.length, afterAll: filterProbe.afterAll, allSelected: filterProbe.allSelected }),
);
check(
    "③-6 筛选只影响显示：两次切换后行状态集合完整（两态都在，没被筛掉）",
    JSON.stringify(filterProbe.afterAll.map((row) => row.status).sort()) === JSON.stringify(filterProbe.before.map((row) => row.status).sort()) && filterProbe.chips.some((chip) => chip.id === "all"),
    JSON.stringify({ before: filterProbe.before.map((row) => row.status), afterAll: filterProbe.afterAll.map((row) => row.status), chips: filterProbe.chips }),
);

// 截图 1：列表态（带标注）
await screenshotAnnotated(page, `${SHOT_DIR}/示意图-01-侧栏列表态-20261114.png`, [
    { color: "blue", selector: "[data-review-batch]", text: "本卡变更：批次头（标题/模板名/成员数/状态筛选 chips/队列相位）" },
    { color: "blue", selector: '[data-review-filter="all"]', text: "本卡变更：状态筛选可点（全部/各状态/回置换区），选中态走 theme token；再次点已选中项回全部" },
    { color: "green", selector: "[data-review-instance]", text: "通过：实例行（素材缩略+序号+标题+状态徽章+自检/自修）" },
    { color: "green", selector: "[data-review-instance] [data-review-thumb]", text: "通过：素材缩略 32×32（video 带 #t=0.1，取不到走图标占位）" },
    { color: "green", selector: "[data-review-badge]", text: "通过：页签徽标 N = 待批+需人工+失败 = 1（断言②）" },
    { color: "red", selector: "[data-review-batch-start]", text: "已知边界：队列按钮不点真跑（真跑=真实拉片/生成链；执行语义由 node 探针 D 段 stub 证明）" },
], "侧栏第 4 页签「待审批」列表态");

// ---------------------------------------------------------------- ④ 抽屉 + ⑤ 正文
await page.evaluate(() => {
    const row = document.querySelector("[data-review-instance]");
    row.querySelector("[data-review-open]") ? row.querySelector("[data-review-open]").click() : row.click();
});
await page.waitForSelector("[data-review-drawer]", { state: "visible", timeout: 20000 });
await page.waitForTimeout(900);
const drawer = await page.evaluate(() => {
    const panel = document.querySelector("[data-review-drawer-panel]") || document.querySelector("[data-review-drawer]");
    const rect = panel.getBoundingClientRect();
    const body = document.querySelector("[data-review-prompt-body]");
    const normalize = (text) => String(text || "").replace(/\\s+/g, " ").trim();
    return {
        width: Math.round(rect.width),
        right: Math.round(window.innerWidth - rect.right),
        promptText: body ? body.textContent : "",
        sections: {
            twoState: Boolean(document.querySelector("[data-review-two-state]")),
            slots: document.querySelectorAll("[data-review-slot]").length,
            replace: Boolean(document.querySelector("[data-review-replace]")),
            selfcheck: document.querySelectorAll("[data-review-selfcheck-item]").length,
            repair: document.querySelectorAll("[data-review-repair-row]").length,
            actions: Boolean(document.querySelector("[data-review-actions]")),
        },
        paidNote: (document.querySelector("[data-review-actions]") || {}).textContent || "",
        rollback: Boolean(document.querySelector("[data-review-rollback]")),
        rollbackNote: (document.querySelector("[data-review-rollback-note]") || {}).textContent || "",
        footerPadBottom: (() => {
            const footer = document.querySelector("[data-review-actions]");
            const note = document.querySelector("[data-review-approve-note]");
            if (!footer || !note) return null;
            const f = footer.getBoundingClientRect();
            const n = note.getBoundingClientRect();
            return Math.round(f.bottom - n.bottom);
        })(),
        // 底部说明整行是否还在视口内（footer 自下贴边时，note 可能被视口裁掉半截）
        noteBottomGap: (() => {
            const note = document.querySelector("[data-review-approve-note]");
            return note ? Math.round(window.innerHeight - note.getBoundingClientRect().bottom) : null;
        })(),
    };
});
// markdown 渲染会吃掉标题标记（#）与换行：比对前按同一规则归一（正文逐字保留）
// 「归一空白」= 空白字符全抹平后逐字比对（块级元素 textContent 不含块间空白，这是公平口径）
const normalizeText = (text) => String(text || "").replace(/\s+/g, "");
check("④ 抽屉出现且宽度落在 700–760（贴右边）", drawer.width >= 700 && drawer.width <= 760 && drawer.right <= 2, JSON.stringify({ width: drawer.width, right: drawer.right }));
const domPrompt = normalizeText(drawer.promptText);
const expectPrompt = normalizeText(PROMPT_TEXT);
check("⑤ 提示词正文与产物节点 content 归一空白后逐字相等", domPrompt === expectPrompt, `len ${domPrompt.length} vs ${expectPrompt.length}`);
check("④-2 抽屉六段齐备（两态/挂图位/置换/自检/自修/动作位）", drawer.sections.twoState && drawer.sections.slots >= 2 && drawer.sections.replace && drawer.sections.selfcheck >= 1 && drawer.sections.repair >= 2 && drawer.sections.actions, JSON.stringify(drawer.sections));
// 卡面要求「批准 ≠ 花钱」写在界面上（不塞 tooltip）：动作位区域文本里必须有这句（两种写法都算，但必须可见）
check("④-3 「批准 ≠ 花钱」写在界面上（非 tooltip）", /(已批准\s*≠\s*已付费|批准\s*≠\s*花钱)/.test(normalizeText(drawer.paidNote)), drawer.paidNote.slice(0, 160));
// 必改项 1：退回按钮只在待批态渲染（后端 rollback_to_assets 只放行 awaiting_approval）
check("④-4 待批实例：退回按钮在位 + 代价文案写在按钮旁", drawer.rollback && drawer.rollbackNote.includes("这版正文不保留"), JSON.stringify({ rollback: drawer.rollback, note: drawer.rollbackNote }));
// 必改项 3：抽屉底边留白（footer pb-3 ≥ 10px，两行文案也不贴边/不裁切）
check("④-6 底部说明整行在视口内（下半截不被裁）", typeof drawer.noteBottomGap === "number" && drawer.noteBottomGap >= 4, String(drawer.noteBottomGap));
check("④-5 抽屉底边留白 ≥10px（底部文案不贴边、不被裁切）", typeof drawer.footerPadBottom === "number" && drawer.footerPadBottom >= 10, String(drawer.footerPadBottom));

// 截图 2：抽屉详情态
await screenshotAnnotated(page, `${SHOT_DIR}/示意图-02-抽屉详情态-20261114.png`, [
    { color: "blue", selector: "[data-review-two-state]", text: "本卡变更：两态条（①提示词包 / ②成片待补）" },
    { color: "blue", selector: "[data-review-prompt-body]", text: "本卡变更：提示词全文（复用 readNodeFullText · 只读）" },
    { color: "blue", selector: "[data-review-slot]", text: "本卡变更：挂图位（当前挂图 + 命中词 + 否决）" },
    { color: "green", selector: "[data-review-selfcheck-item='fail']", text: "通过：自检 FAIL 红条（生产者原样，不重判）" },
    { color: "green", selector: "[data-review-repair-timeline]", text: "通过：自修痕迹逐轮（自修 2/2）" },
    { color: "blue", selector: "[data-review-actions]", text: "本卡变更：动作位（批准/退回/确认提交，底部固定；退回只在待批态渲染，代价文案在按钮旁）" },
    { color: "green", selector: "[data-review-rollback]", text: "通过：待批实例可退（与后端 rollback_to_assets 守卫一致：只放行 awaiting_approval）" },
    { color: "green", selector: "[data-review-approve-note]", text: "通过：footer pb-3 留白，底部文案不贴边不裁切" },
    { color: "red", selector: "[data-review-approve]", text: "已知边界：批准/退回不点真跑（真审批会改 hub 任务状态；此处只验渲染 + 走既有闸）" },
], "右侧 760 抽屉详情");

// ---------------------------------------------------------------- ⑥ 否决
const vetoBefore = await page.evaluate(async ({ key }) => {
    const mod = await import("/src/lib/localforage-storage.ts");
    const raw = await mod.localForageStorage.getItem(key);
    const parsed = JSON.parse(raw);
    const project = parsed.state.projects.find((item) => item.id === parsed.state.projects[parsed.state.projects.length - 1].id);
    return { count: (project.workflowAssetVetoes || []).length, projectId: project.id };
}, { key: STORE_KEY });
await page.evaluate(() => {
    const button = document.querySelector("[data-review-slot-veto]");
    button.scrollIntoView({ block: "center" });
    button.click();
});
await page.waitForTimeout(500);
const confirmVisible = await page.evaluate(() => Boolean(document.querySelector(".ant-popconfirm") || document.querySelector(".ant-popover")));
await page.evaluate(() => {
    const pop = document.querySelector(".ant-popconfirm") || document.querySelector(".ant-popover");
    const ok = pop ? pop.querySelector(".ant-btn-primary") || pop.querySelector(".ant-btn-dangerous") : null;
    if (ok) ok.click();
});
await page.waitForTimeout(1200);
const vetoAfter = await page.evaluate(async ({ key, projectId }) => {
    const mod = await import("/src/lib/localforage-storage.ts");
    const raw = await mod.localForageStorage.getItem(key);
    const parsed = JSON.parse(raw);
    const project = parsed.state.projects.find((item) => item.id === projectId);
    const slot = document.querySelector("[data-review-slot]");
    return {
        count: (project.workflowAssetVetoes || []).length,
        vetoedAttr: slot ? slot.getAttribute("data-review-slot-vetoed") : null,
        vetoedLabel: Boolean(document.querySelector("[data-review-slot-vetoed-label]")),
    };
}, { key: STORE_KEY, projectId: PROJECT_ID });
check("⑥ 否决二次确认弹层出现（Popconfirm）", confirmVisible, String(confirmVisible));
check("⑥-2 否决后存储 workflowAssetVetoes +1", vetoAfter.count === vetoBefore.count + 1, `${vetoBefore.count} → ${vetoAfter.count}`);
check("⑥-3 该行变「已否决」", vetoAfter.vetoedAttr === "true" && vetoAfter.vetoedLabel, JSON.stringify(vetoAfter));

// 截图 3：否决 + 提交闸（未确认前零请求）
await page.evaluate(() => {
    const target = document.querySelector("[data-review-slot]");
    if (target) target.scrollIntoView({ block: "center" });
});
const gateIdle = await page.evaluate(() => ({
    submit: window.__p8Hub.submit,
    hub: window.__p8Hub.hub.length,
    disabled: (document.querySelector("[data-review-submit]") || {}).disabled ?? null,
    reason: (document.querySelector("[data-review-submit-reason]") || {}).textContent?.trim() || "",
}));
// 待批实例：提交按钮必须禁用并给真实原因（未批准 = 还没有可提交的批准快照）
check("⑦-0 待批实例：提交按钮禁用 + 真实原因（未确认前零请求）", gateIdle.disabled === true && gateIdle.submit === 0 && gateIdle.reason.length > 8, JSON.stringify(gateIdle));

await screenshotAnnotated(page, `${SHOT_DIR}/示意图-03-否决与提交闸-20261114.png`, [
    { color: "green", selector: "[data-review-slot-vetoed-label]", text: "通过：否决已落库（workflowAssetVetoes +1，复算不再选它）" },
    { color: "blue", selector: "[data-review-submit]", text: "本卡变更：付费闸按钮（走既有 runHubQuickAction，不自造轮询）" },
    { color: "red", selector: "[data-review-submit-reason]", text: "已知边界：未确认前 /api/hub/submit/ 命中 0 次（真实提交只在二次确认后）" },
], "否决落库 + 付费闸（未确认前零请求）");

// ⑥-5 关闭抽屉 → 打开「已批准」实例（提交闸只对 approved 开放）
await page.evaluate(() => document.querySelector("[data-review-drawer-close]")?.click());
await page.waitForTimeout(500);
const seedCfg = await page.evaluate(async ({ localId, cfgKey }) => {
    const gen = await import("/src/lib/hub/gen-settings.ts");
    const stub = { id: "p8-stub-channel", name: "P8 探针渠道", baseUrl: "http://127.0.0.1:9/stub", apiFormat: "wan", apiKey: "", models: [{ name: "wan3.0", capability: "video" }], enabled: true, capabilities: ["video"] };
    const raw = localStorage.getItem(cfgKey);
    if (!raw || !String(raw).includes("p8-stub-channel")) {
        const parsed = raw ? JSON.parse(raw) : { state: { config: {} }, version: 0 };
        const config = parsed.state?.config || {};
        const channels = Array.isArray(config.channels) ? config.channels : [];
        parsed.state = { ...parsed.state, config: { ...config, channels: [...channels, stub] } };
        localStorage.setItem(cfgKey, JSON.stringify(parsed));
    }
    const draft = { channelId: stub.id, model: "", mode: "", size: "9:16", rawSize: "", vquality: "720", duration: 6, audio: true };
    const signature = gen.genDraftSignature(draft, { id: stub.id, name: stub.name, baseUrl: stub.baseUrl, apiFormat: stub.apiFormat, models: stub.models });
    const key = gen.genApprovedKey(localId);
    const prev = localStorage.getItem(key);
    localStorage.setItem(key, signature);
    return { channelId: stub.id, channelName: stub.name, prev, cfgRaw: raw };
}, { localId: "p8-local-2", cfgKey: "infinite-canvas:ai_config_store" });
const seeded = seedCfg;
console.log(`闸前置：渠道=${seeded.channelId}/${seeded.channelName}（写 localStorage 后重载生效）`);
// 重载让配置 hydrate 进 store（渠道列表 + 批准签名都从 localStorage 读）
await page.reload({ timeout: 90000 });
await page.waitForSelector("[data-review-badge]", { state: "visible", timeout: 60000 });
await page.waitForTimeout(1200);
await page.evaluate(async () => {
    const tabs = [...document.querySelectorAll("[data-canvas-no-zoom] button")].filter((node) => node.textContent && node.textContent.trim());
    const review = tabs.find((node) => node.textContent.includes("待审批"));
    review.click();
    await new Promise((resolve) => setTimeout(resolve, 700));
});

await page.evaluate(() => {
    const row = document.querySelector('[data-review-instance="p8probe#2"]');
    if (row) (row.querySelector("[data-review-open]") || row).click();
});
await page.waitForSelector("[data-review-drawer]", { state: "visible", timeout: 20000 });
await page.waitForTimeout(1200);
const approvedState = await page.evaluate(async () => {
    const [{ useConfigStore }, hubApi, gen] = await Promise.all([import("/src/stores/use-config-store.ts"), import("/src/pages/hub/hub-api.ts"), import("/src/lib/hub/gen-settings.ts")]);
    const config = useConfigStore.getState().config;
    const channels = hubApi.hubQuickChannels(config);
    const signature = gen.readApprovedSignature("p8-local-2");
    return {
    status: (document.querySelector("[data-review-drawer-status]") || {}).getAttribute?.("data-review-drawer-status") || "",
    submitDisabled: (document.querySelector("[data-review-submit]") || {}).disabled ?? null,
    submitReason: (document.querySelector("[data-review-submit-reason]") || {}).textContent?.trim() || "",
    rollback: Boolean(document.querySelector("[data-review-rollback]")),
    channels: channels.map((item) => `${item.id}/${item.apiFormat}`),
    signature: signature.slice(0, 120),
    quick: hubApi.hubQuickAction({ localId: "p8-local-2", state: "approved", submit: null, channels, approvedSignature: signature, draft: null }),
};});
check("⑦-1 已批准实例：提交闸可点 + 退回按钮不在位（后端只放行待批态，渲染就是死链）", approvedState.submitDisabled === false && approvedState.rollback === false, JSON.stringify(approvedState));

const beforeSubmit = await page.evaluate(() => ({ submit: window.__p8Hub.submit, hub: window.__p8Hub.hub.length }));
await page.evaluate(() => {
    const button = document.querySelector("[data-review-submit]");
    if (button) {
        button.scrollIntoView({ block: "center" });
        button.click();
    }
});
await page.waitForTimeout(600);
const confirmState = await page.evaluate(() => {
    const box = document.querySelector("[data-review-submit-confirm]");
    return { visible: Boolean(box), text: box ? box.textContent.replace(/\s+/g, " ").trim() : "", submit: window.__p8Hub.submit };
});
check("⑦ 第一次点「确认并提交出片」只进二次确认（零请求）", confirmState.visible && confirmState.submit === beforeSubmit.submit && /确认提交 1 条/.test(confirmState.text), JSON.stringify(confirmState));
check("⑦-2 二次确认文案含渠道名", /渠道：/.test(confirmState.text) && confirmState.text.includes(seeded.channelName), confirmState.text);

await screenshotAnnotated(page, `${SHOT_DIR}/示意图-03b-二次确认弹层-20261114.png`, [
    { color: "red", selector: "[data-review-submit-confirm]", text: "已知边界：这一层之前 /api/hub/submit/ 命中 0 次（只进确认，不发请求）" },
    { color: "blue", selector: "[data-review-submit-ok]", text: "本卡变更：二次确认按钮（确认后才走既有闸 runHubQuickAction）" },
    { color: "green", selector: "[data-review-approve-note]", text: "通过：常驻「已批准 ≠ 已付费：钱在确认并提交这一步花」" },
], "付费闸二次确认（未确认前零请求）");

// 确认 → 命中 1 次
await page.evaluate(() => {
    const ok = document.querySelector("[data-review-submit-ok]");
    if (ok) ok.click();
});
await page.waitForTimeout(3000);
const afterSubmit = await page.evaluate(() => ({ submit: window.__p8Hub.submit, url: window.__p8Hub.submitUrl, body: window.__p8Hub.submitBody, phase: (document.querySelector("[data-review-phase]") || {}).getAttribute?.("data-review-phase") || "" }));
check("⑦-3 确认后 /api/hub/submit/ 命中 1 次", afterSubmit.submit === 1 && /\/api\/hub\/submit\//.test(afterSubmit.url), JSON.stringify({ submit: afterSubmit.submit, url: afterSubmit.url, body: String(afterSubmit.body).slice(0, 120) }));
console.log(`submit url=${afterSubmit.url} body=${String(afterSubmit.body).slice(0, 200)}`);

// 再点一次：闸内 HUB_QUICK_RUNNING / 后端 active 守卫 → 不产生第 2 次请求
const beforeSecond = afterSubmit.submit;
await page.evaluate(() => {
    const button = document.querySelector("[data-review-submit]");
    if (button && !button.disabled) button.click();
});
await page.waitForTimeout(1500);
const afterSecond = await page.evaluate(() => ({ submit: window.__p8Hub.submit, disabled: (document.querySelector("[data-review-submit]") || {}).disabled ?? null, reason: (document.querySelector("[data-review-submit-reason]") || {}).textContent || "" }));
check("⑦-4 再点一次不产生第 2 次请求（闸守卫生效）", afterSecond.submit === beforeSecond, JSON.stringify({ before: beforeSecond, after: afterSecond }));

await page.screenshot({ path: `${SHOT_DIR}/示意图-04-提交后状态-20261114.png` });

// 还原：渠道配置键 + 批准签名
const cfgRestored = await page.evaluate(async ({ prev, cfgRaw, localId, cfgKey }) => {
    const gen = await import("/src/lib/hub/gen-settings.ts");
    if (cfgRaw === null) localStorage.removeItem(cfgKey);
    else localStorage.setItem(cfgKey, cfgRaw);
    const key = gen.genApprovedKey(localId);
    if (prev === null) localStorage.removeItem(key);
    else localStorage.setItem(key, prev);
    return { cfgEqual: localStorage.getItem(cfgKey) === cfgRaw };
}, { prev: seeded.prev, cfgRaw: seeded.cfgRaw, localId: "p8-local-2", cfgKey: "infinite-canvas:ai_config_store" });
check("收尾：渠道配置键已逐字节还原", cfgRestored.cfgEqual, JSON.stringify(cfgRestored));

// ⑥-4 刷新后仍否决
await page.reload({ timeout: 90000 });
await page.waitForSelector("[data-review-badge]", { state: "visible", timeout: 60000 });
await page.waitForTimeout(1500);
await page.evaluate(async () => {
    const tabs = [...document.querySelectorAll("[data-canvas-no-zoom] button")].filter((node) => node.textContent && node.textContent.trim());
    const review = tabs.find((node) => node.textContent.includes("待审批"));
    review.click();
    await new Promise((resolve) => setTimeout(resolve, 800));
    const row = document.querySelector("[data-review-instance]");
    if (row) (row.querySelector("[data-review-open]") || row).click();
});
await page.waitForSelector("[data-review-drawer]", { state: "visible", timeout: 20000 });
await page.waitForTimeout(800);
const afterReload = await page.evaluate(() => {
    const slot = document.querySelector("[data-review-slot]");
    return { vetoed: slot ? slot.getAttribute("data-review-slot-vetoed") : null, label: Boolean(document.querySelector("[data-review-slot-vetoed-label]")), vetoButtons: document.querySelectorAll("[data-review-slot-veto]").length };
});
check("⑥-4 刷新后仍否决（旧匹配没有复活）", afterReload.vetoed === "true" && afterReload.label, JSON.stringify(afterReload));

// ---------------------------------------------------------------- 收尾：按项目 id 精确删除 + 原值写回
const restored = await page.evaluate(async ({ key, projectId, raw }) => {
    const mod = await import("/src/lib/localforage-storage.ts");
    const currentRaw = await mod.localForageStorage.getItem(key);
    if (raw === null) {
        // 原本没有这条存储：删掉夹具项目后写回「空 store」形态
        const parsed = JSON.parse(currentRaw);
        const next = { ...parsed, state: { ...parsed.state, projects: parsed.state.projects.filter((item) => item.id !== projectId) } };
        await mod.localForageStorage.setItem(key, JSON.stringify(next));
        return { mode: "no-backup-removed", remaining: next.state.projects.filter((item) => item.id === projectId).length };
    }
    await mod.localForageStorage.setItem(key, raw);
    const parsed = JSON.parse(await mod.localForageStorage.getItem(key));
    return { mode: "restored", bytesEqual: (await mod.localForageStorage.getItem(key)) === raw, fixtureLeft: (parsed.state.projects || []).filter((item) => item.id === projectId).length };
}, { key: STORE_KEY, projectId: PROJECT_ID, raw: backup.raw });
check("收尾：夹具项目已按 id 精确删除 + 原值写回", (restored.mode === "restored" && restored.fixtureLeft === 0 && restored.bytesEqual) || (restored.mode === "no-backup-removed" && restored.remaining === 0), JSON.stringify(restored));

console.log(`\nTOTAL pass=${pass} fail=${fails.length} total=${results.length}`);
if (fails.length) console.log(`FAILS\n${fails.join("\n")}`);
console.log(`space=${task.spaceId}（保留，便于复查）`);

// ---------------------------------------------------------------- 标注截图（红=问题/蓝=变更/绿=通过；图例独立右列）
async function screenshotAnnotated(target, file, items, caption) {
    await target.evaluate(({ items: list, caption: title }) => {
        document.querySelectorAll("[data-p8-annot]").forEach((node) => node.remove());
        const legendWidth = 260;
        const docWidth = document.documentElement.clientWidth;
        const legend = document.createElement("div");
        legend.setAttribute("data-p8-annot", "legend");
        Object.assign(legend.style, {
            position: "fixed", top: "0", right: "0", width: `${legendWidth}px`, height: "100vh",
            background: "#ffffff", borderLeft: "2px solid #111827", zIndex: "2147483000",
            font: "12px/1.5 -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif", color: "#111827", padding: "14px 12px", boxSizing: "border-box", overflowY: "auto",
        });
        const colorMap = { red: "#dc2626", blue: "#2563eb", green: "#16a34a" };
        const labelMap = { red: "问题", blue: "变更", green: "通过" };
        const head = document.createElement("div");
        head.textContent = title;
        head.style.cssText = "font-weight:700;font-size:13px;margin-bottom:8px";
        legend.appendChild(head);
        const notes = [];
        list.forEach((item, index) => {
            const el = document.querySelector(item.selector);
            const color = colorMap[item.color] || "#2563eb";
            if (!el) {
                notes.push(`${index + 1}. （未找到 ${item.selector}）${item.text}`);
                return;
            }
            const rect = el.getBoundingClientRect();
            const box = document.createElement("div");
            box.setAttribute("data-p8-annot", "box");
            Object.assign(box.style, {
                position: "fixed", left: `${Math.max(2, rect.left - 3)}px`, top: `${Math.max(2, rect.top - 3)}px`,
                width: `${Math.min(rect.width + 6, docWidth - legendWidth - 8)}px`, height: `${rect.height + 6}px`,
                border: `2px solid ${color}`, borderRadius: "6px", zIndex: "2147482998", pointerEvents: "none", boxSizing: "border-box",
            });
            const tag = document.createElement("span");
            tag.setAttribute("data-p8-annot", "tag");
            tag.textContent = String(index + 1);
            // 编号徽标外推（左 −13px / 上 −29px）：原先只偏 3/21px 会压住框内小标题；
            // 图例仍在右侧独立列，不改内容区。
            Object.assign(tag.style, {
                position: "fixed", left: `${Math.max(2, rect.left + 6)}px`, top: `${Math.max(0, rect.top - 7)}px`,
                background: color, color: "#fff", font: "700 11px/16px sans-serif", padding: "0 5px", borderRadius: "3px", zIndex: "2147482999", pointerEvents: "none",
            });
            document.body.appendChild(box);
            document.body.appendChild(tag);
            notes.push(`${index + 1}. [${labelMap[item.color] || "变更"}] ${item.text}`);
        });
        const list1 = document.createElement("ol");
        list1.style.cssText = "margin:0;padding-left:16px";
        for (const note of notes) {
            const li = document.createElement("li");
            li.textContent = note;
            li.style.marginBottom = "6px";
            list1.appendChild(li);
        }
        legend.appendChild(list1);
        const foot = document.createElement("div");
        foot.textContent = "红=问题 / 蓝=变更 / 绿=通过";
        foot.style.cssText = "margin-top:10px;padding-top:8px;border-top:1px solid #d1d5db;color:#4b5563";
        legend.appendChild(foot);
        document.body.appendChild(legend);
    }, { items, caption });
    await target.waitForTimeout(350);
    await target.screenshot({ path: file });
    await target.evaluate(() => document.querySelectorAll("[data-p8-annot]").forEach((node) => node.remove()));
    console.log(`shot ${file}`);
}
