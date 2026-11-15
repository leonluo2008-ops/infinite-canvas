/**
 * 路由全局 ErrorBoundary 探针（ego-browser DOM 层，真页面 + 真 router + 真渲染）。
 *
 * 跑法（仓库任意位置）：
 *   ego-browser nodejs < web/scripts/probe-route-error-boundary.ego.mjs
 * 前置：`cd web && npm run dev`（http://127.0.0.1:3000）已在跑。
 *
 * 缺陷复现口径（2026-11-14 所有者实测）：vite 重新预构建依赖后，旧页面持有的旧 hash chunk
 * 动态 import 会 404/504 → `Failed to fetch dynamically imported module`。画布 Agent 对话里
 * streamdown 的代码高亮块（highlighted-body-*.js）就是这样一个懒加载 chunk。
 * 本探针用 CDP Network.setBlockedURLs 把该 chunk 精确封掉，在真页面里造出**同一句报错**，
 * 断言出现人话刷新页而不是裸错误页。
 *
 * 能证：① 真 router 的两条顶层路由都挂 errorElement，9 个既有路由一个不少（零结构改动）；
 *      ② 正常路由零回归（画布/首页真渲染，无兜底页）；
 *      ③ 懒加载 chunk 真被请求（控制组）→ 封掉后异常真发生（对照组）→ 真页面出人话页（有刷新按钮、
 *         无英文报错文案）；④ 解除封锁后刷新即恢复（兜底页的引导真的有效）。
 * 不能证：生产构建产物（探针跑在 dev 服务器上）；此处不伪造生产构建结论。
 */
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

const BASE = "http://127.0.0.1:3000";
const SPACE = "route-error-boundary";
const SHOT_DIR = "E:/User/GitHub/infinite-canvas/wanrepro/docs/证据-路由ErrorBoundary-20261114";
const STORE_KEY = "infinite-canvas:canvas_store";

mkdirSync(SHOT_DIR, { recursive: true });

const results = [];
let pass = 0;
const fails = [];
function check(name, ok, detail = "") {
    results.push({ name, ok: Boolean(ok), detail: ok ? "" : String(detail) });
    if (ok) pass += 1;
    else fails.push(`${name}${detail ? ` — ${detail}` : ""}`);
    console.log(`${ok ? "PASS" : "FAIL"} ${name}${ok ? "" : ` — ${detail}`}`);
}

/** 人话兜底页是否在 DOM 里（生产组件打的两个 data-* 标记）+ 主按钮可读性（防白底白字）。 */
const errorPageInDom = () =>
    `(() => {
        const root = document.querySelector("[data-route-error]");
        const title = document.querySelector("[data-route-error-title]");
        const reload = document.querySelector("[data-route-error-reload]");
        const body = document.body.innerText || "";
        const reloadStyle = reload ? getComputedStyle(reload) : null;
        return {
            present: Boolean(root && title && reload),
            kind: root ? root.getAttribute("data-route-error") : null,
            title: title ? (title.textContent || "").trim() : "",
            reloadText: reload ? (reload.textContent || "").trim() : "",
            reloadStyle: reloadStyle ? { color: reloadStyle.color, background: reloadStyle.backgroundColor } : null,
            reloadReadable: Boolean(reloadStyle && reloadStyle.color !== reloadStyle.backgroundColor && !/rgba\\(0, 0, 0, 0\\)/.test(reloadStyle.backgroundColor)),
            bodyExcerpt: body.replace(/\\s+/g, " ").slice(0, 300),
            leaksEnglishError: /Failed to fetch dynamically imported module|Error:|Unhandled|stack|chunk/i.test(body),
        };
    })()`;

const task = await taskSpace(SPACE);
const page = task.page("p1");
console.log(`space=${task.spaceId} page=${page.label}`);

/**
 * 页面里**正在跑的那份**模块实例：vite dev 的模块 URL 带 `?t=<hmr 版本>`，
 * 裸路径 import 会另起一份模块（store 会变成两个实例，写进去的状态组件根本收不到）。
 * 各 evaluate 里统一用 `live("/src/xxx", fallback)` 按 performance 里真实加载过的 URL 取。
 */

const stamp = Date.now();
const PROJECT_ID = `route-error-probe-${stamp}`;

// 空画布项目夹具（只为让 /canvas/:id 真渲染出来；跑完原样写回 store）
const projectFixture = {
    id: PROJECT_ID,
    title: `路由兜底探针 ${new Date(stamp).toISOString().slice(11, 19)}`,
    nodes: [{ id: "n1", type: "text", title: "探针节点", position: { x: 120, y: 120 }, width: 240, height: 120, metadata: { content: "路由兜底探针" } }],
    connections: [],
    backgroundMode: "dots",
    viewport: { x: 40, y: 80, k: 0.8 },
    workflowBatches: [],
    workflowInstances: [],
    workflowAssetVetoes: [],
    workflowWanFileTasks: [],
    createdAt: new Date(stamp).toISOString(),
    updatedAt: new Date(stamp).toISOString(),
};

// ---------------------------------------------------------------- ① 真 router 结构：errorElement 在位 + 既有路由零改动
// 全程捕获页面里的异常（React 把 render 异常打到 console.error，React Router 也打一条），
// 用于精确判断「真出的是哪类错」，而不是靠猜。
await page.cdp("Page.enable", {});
await page.cdp("Page.addScriptToEvaluateOnNewDocument", {
    source: `(() => {
        window.__probeErrors = [];
        const push = (kind, args) => { try { window.__probeErrors.push({ kind, text: args.map((a) => (a && a.message) || String(a)).join(" | ").slice(0, 600) }); } catch {} };
        const origError = console.error.bind(console);
        console.error = (...args) => { push("console.error", args); origError(...args); };
        window.addEventListener("error", (event) => push("window.error", [event.message || event.error]));
        window.addEventListener("unhandledrejection", (event) => push("unhandledrejection", [event.reason]));
    })();`,
});
const capturedChunkErrors = () =>
    page.evaluate(() => (window.__probeErrors || []).map((item) => item.text).filter((text) => /dynamically imported module/i.test(text)));
const clearCaptured = () => page.evaluate(() => void (window.__probeErrors = []));

await page.goto(`${BASE}/`, { timeout: 60000 });
await page.waitForLoadState("load");
await page.waitForTimeout(1500);

const routerFacts = await page.evaluate(async () => {
    const live = (fragment, fallback) => performance.getEntriesByType("resource").map((entry) => entry.name).find((name) => name.includes(fragment)) || fallback;
    const mod = await import(live("/src/router.tsx", "/src/router.tsx"));
    const routes = mod.router.routes;
    const layout = routes[0];
    const wildcard = routes.find((route) => route.path === "*");
    const paths = (layout.children || []).map((route) => route.path);
    const render = (node) => (node ? String(node.type?.name || node.type?.displayName || node.type || "") : "");
    return {
        topLevel: routes.length,
        rootHasErrorElement: Boolean(layout.errorElement),
        wildcardHasErrorElement: Boolean(wildcard?.errorElement),
        rootErrorElementType: layout.errorElement ? layout.errorElement.type?.name || layout.errorElement.type?.displayName || typeof layout.errorElement.type : null,
        childPaths: paths,
        childErrorElements: (layout.children || []).filter((route) => route.errorElement).map((route) => route.path),
        wildcardChild: render(wildcard?.element),
    };
});
check(
    "① 真 router 顶层布局路由挂 errorElement（生产组件 RouteErrorPage）",
    routerFacts.rootHasErrorElement === true && routerFacts.rootErrorElementType === "RouteErrorPage",
    JSON.stringify(routerFacts),
);
check("①-2 通配路由（* = 404 兜底）也挂 errorElement", routerFacts.wildcardHasErrorElement === true, JSON.stringify(routerFacts));
check(
    "①-3 路由结构零改动：9 条既有路径原样在位（/ /image /video /hub /assets /prompts /canvas /canvas/:id /config）",
    JSON.stringify(routerFacts.childPaths) === JSON.stringify(["/", "/image", "/video", "/hub", "/assets", "/prompts", "/canvas", "/canvas/:id", "/config"]) && routerFacts.topLevel === 2,
    JSON.stringify({ topLevel: routerFacts.topLevel, paths: routerFacts.childPaths }),
);

// 归因逻辑在真浏览器包里也是同一份生产模块
const kindFacts = await page.evaluate(async () => {
    const live = (fragment, fallback) => performance.getEntriesByType("resource").map((entry) => entry.name).find((name) => name.includes(fragment)) || fallback;
    const mod = await import(live("/src/lib/route-error.ts", "/src/lib/route-error.ts"));
    const chunkError = new TypeError("Failed to fetch dynamically imported module: http://127.0.0.1:3000/node_modules/.vite/deps/highlighted-body-XXX.js");
    const wrapped = new Error("加载失败", { cause: chunkError });
    return {
        chunk: mod.routeErrorKind(chunkError),
        wrappedCause: mod.routeErrorKind(wrapped),
        generic: mod.routeErrorKind(new Error("Cannot read properties of undefined")),
        notFound: mod.routeErrorKind({ status: 404, statusText: "Not Found", data: null }),
        forbidden: mod.routeErrorKind({ status: 403, statusText: "Forbidden", data: null }),
    };
});
check(
    "①-4 chunk 失效归因 = stale（含被 cause 包一层的情况），404/403/其它各归各位",
    kindFacts.chunk === "stale" && kindFacts.wrappedCause === "stale" && kindFacts.generic === "unknown" && kindFacts.notFound === "notFound" && kindFacts.forbidden === "forbidden",
    JSON.stringify(kindFacts),
);

// ---------------------------------------------------------------- ② 正常路由零回归
const homeDom = await page.evaluate(errorPageInDom());
check("② 首页正常渲染，无兜底页", homeDom.present === false, JSON.stringify(homeDom));

const backup = await page.evaluate(
    async ({ key, project }) => {
        const mod = await import("/src/lib/localforage-storage.ts");
        const raw = await mod.localForageStorage.getItem(key);
        const parsed = raw ? JSON.parse(raw) : null;
        const state = parsed && parsed.state ? parsed.state : { projects: [] };
        const next = { ...(parsed || {}), state: { ...state, projects: [...(state.projects || []), project], deletedProjects: state.deletedProjects || [] }, version: parsed?.version ?? 1 };
        await mod.localForageStorage.setItem(key, JSON.stringify(next));
        return { raw, hadValue: Boolean(raw) };
    },
    { key: STORE_KEY, project: projectFixture },
);
check("②-1 画布项目夹具已写入真 store（原值已备份，跑完写回）", typeof backup.hadValue === "boolean", JSON.stringify(backup));

await page.goto(`${BASE}/canvas/${PROJECT_ID}`, { timeout: 90000 });
await page.waitForLoadState("load");
await page.waitForTimeout(2500);
const canvasDom = await page.evaluate(() => ({
    nodeCount: document.querySelectorAll("[data-node-id]").length,
    body: (document.body.innerText || "").replace(/\s+/g, " ").slice(0, 120),
}));
const canvasErrorDom = await page.evaluate(errorPageInDom());
check("②-2 画布页正常渲染（节点在位、无兜底页）", canvasDom.nodeCount >= 1 && canvasErrorDom.present === false, JSON.stringify({ canvasDom, canvasErrorDom }));

// 其它路由同样照常（errorElement 只是兜底，不许把正常页面当异常处理）
const sweep = [];
for (const route of ["/canvas", "/assets", "/prompts", "/hub", "/config", "/image", "/video"]) {
    await page.goto(`${BASE}${route}`, { timeout: 90000 });
    await page.waitForLoadState("load");
    await page.waitForTimeout(1200);
    const facts = await page.evaluate(() => ({
        hasErrorPage: Boolean(document.querySelector("[data-route-error]")),
        textLength: (document.body.innerText || "").replace(/\s+/g, "").length,
        path: location.pathname,
    }));
    sweep.push({ route, ...facts });
}
check(
    "②-3 零回归：/canvas /assets /prompts /hub /config /image /video 七个路由照常渲染，无一落兜底页",
    sweep.every((item) => item.hasErrorPage === false && item.textLength > 20 && item.path === item.route),
    JSON.stringify(sweep),
);
await page.goto(`${BASE}/canvas/${PROJECT_ID}`, { timeout: 90000 });
await page.waitForLoadState("load");
await page.waitForTimeout(1500);

// ---------------------------------------------------------------- ③ 懒加载 chunk 失败 → 人话页（控制组先证明触发路径真的走到 chunk）
/**
 * 把画布页的 Agent 面板切到「对话」并放一条带代码块的回复（= 所有者当时「点 Agent 对话」的动作）。
 *
 * 必须先 disconnectAgent：这台机器上真连着本地 Codex，SSE 会不断把 conversation 状态改成
 * failed/preparing，面板会切到诊断视图、聊天区不渲染，探针就变成看环境脸色。
 * 断开后再一次性写满 store（对话 ready + 一条 assistant 消息），渲染路径仍然完全是生产代码。
 */
const openAgentChatWithCodeBlock = () =>
    page.evaluate(async () => {
        const live = (fragment, fallback) => performance.getEntriesByType("resource").map((entry) => entry.name).find((name) => name.includes(fragment)) || fallback;
        const mod = await import(live("/src/stores/use-agent-store.ts", "/src/stores/use-agent-store.ts"));
        const store = mod.useAgentStore;
        store.getState().disconnectAgent();
        store.getState().setAgentState({
            panelOpen: true,
            panelMounted: true,
            panelClosing: false,
            activeTab: "chat",
            sending: false,
            waiting: false,
            bootstrapStatus: null,
            connectError: "",
            conversation: { revision: 1, conversationId: "probe", threadId: "probe", status: "ready", mcpStatuses: {} },
            messages: [{ id: `probe-${Date.now()}`, role: "assistant", text: "探针用回复：\n\n```js\nconst answer = 42;\nconsole.log(answer);\n```\n" }],
        });
        return { messages: store.getState().messages.length, activeTab: store.getState().activeTab, panelOpen: store.getState().panelOpen };
    });

const chunkRequests = () =>
    page.evaluate(() => performance.getEntriesByType("resource").map((entry) => entry.name).filter((name) => name.includes("highlighted-body")));

await clearCaptured();
const chatState = await openAgentChatWithCodeBlock();
await page.waitForTimeout(2500);
const controlRequests = await chunkRequests();
const controlErrorDom = await page.evaluate(errorPageInDom());
const controlCode = await page.evaluate(() => ({
    pre: document.querySelectorAll("pre").length,
    hasAnswer: (document.body.innerText || "").includes("answer = 42"),
    highlightedSpans: document.querySelectorAll("pre code span").length,
}));
const controlChunkErrors = await capturedChunkErrors();
check(
    "③-0 控制组（未封 chunk）：同一条带代码块的 Agent 回复真渲染，且语法高亮懒加载块真跑过（pre + 着色 span 在位、零异常）",
    controlCode.pre >= 1 && controlCode.hasAnswer === true && controlCode.highlightedSpans >= 1 && controlChunkErrors.length === 0,
    JSON.stringify({ chatState, controlCode, controlChunkErrors: controlChunkErrors.slice(0, 2), controlRequests }),
);
check("③-0b 控制组未封 chunk 时页面照常（无兜底页）", controlErrorDom.present === false, JSON.stringify(controlErrorDom));
await page.screenshot({ path: path.join(SHOT_DIR, "01-控制组-Agent对话代码块正常渲染.png") });

// 对照组：封掉 chunk（等价于旧 hash chunk 504/404），刷新后同一条回复触发懒加载失败
await page.cdp("Network.enable", {});
await page.cdp("Network.setBlockedURLs", { urls: ["*highlighted-body*"] });
await page.reload({ timeout: 90000 });
await page.waitForLoadState("load");
await page.waitForTimeout(2000);

const beforeFailDom = await page.evaluate(errorPageInDom());
check("③-1 封锁生效前页面仍正常（异常由懒加载触发，不是加载页面就炸）", beforeFailDom.present === false, JSON.stringify(beforeFailDom));

await clearCaptured();
await openAgentChatWithCodeBlock();
await page.waitForTimeout(3500);
const failDom = await page.evaluate(errorPageInDom());
const failChunkErrors = await capturedChunkErrors();
check(
    "③-2 懒加载 chunk 失败真实发生（页面捕获到 Failed to fetch dynamically imported module: …highlighted-body-*.js，与所有者撞到的是同一句）",
    failChunkErrors.length >= 1 && failChunkErrors.some((text) => /highlighted-body/.test(text)),
    JSON.stringify(failChunkErrors.slice(0, 2)),
);
check(
    "③-3 该异常走全局 errorElement → 出人话页，裸错误页不再出现",
    failDom.present === true,
    JSON.stringify(failDom),
);
check(
    "③-4 人话页说的是「版本过期/加载失败 + 刷新」，不是英文报错",
    failDom.present === true && failDom.kind === "stale" && /版本已过期|加载失败/.test(failDom.title) && failDom.leaksEnglishError === false,
    JSON.stringify(failDom),
);
check("③-5 一键刷新按钮在位（文案含「刷新」）", failDom.present === true && /刷新/.test(failDom.reloadText), JSON.stringify(failDom));
check(
    "③-6 主按钮可读（文字色 ≠ 底色、底不为透明；白底白字这类主题事故的回归守卫）",
    failDom.present === true && failDom.reloadReadable === true,
    JSON.stringify(failDom.reloadStyle),
);
await page.screenshot({ path: path.join(SHOT_DIR, "02-懒加载失败-人话刷新页.png") });

// ---------------------------------------------------------------- ④ 引导有效：解除封锁后刷新即恢复
await page.cdp("Network.setBlockedURLs", { urls: [] });
await page.reload({ timeout: 90000 });
await page.waitForLoadState("load");
await page.waitForTimeout(2500);
const recovered = await page.evaluate(() => ({ errors: document.querySelectorAll("[data-route-error]").length, nodes: document.querySelectorAll("[data-node-id]").length, url: location.pathname }));
check("④ 按兜底页引导刷新后恢复（同一地址重新正常渲染）", recovered.errors === 0 && recovered.nodes >= 1, JSON.stringify(recovered));
await page.screenshot({ path: path.join(SHOT_DIR, "03-刷新后恢复.png") });

// ---------------------------------------------------------------- ⑤ 收尾：store 原值写回
const restored = await page.evaluate(
    async ({ key, raw }) => {
        const mod = await import("/src/lib/localforage-storage.ts");
        if (raw === null) await mod.localForageStorage.removeItem(key);
        else await mod.localForageStorage.setItem(key, raw);
        return true;
    },
    { key: STORE_KEY, raw: backup.raw ?? null },
);
check("⑤ 探针自建夹具已清（store 原值原样写回）", restored === true, String(restored));

const summary = { at: new Date().toISOString(), space: task.spaceId, pass, fail: fails.length, fails, results, routerFacts, kindFacts, controlCode, failChunkErrors, failDom };
const outJson = path.join(SHOT_DIR, "探针结果-DOM层.json");
writeFileSync(outJson, JSON.stringify(summary, null, 2), "utf8");

console.log(`\n路由兜底 DOM 探针：${pass} 通过 / ${fails.length} 失败（证据 ${outJson}）`);
if (fails.length) {
    console.log("失败明细：");
    for (const line of fails) console.log(`  - ${line}`);
}
process.exit(fails.length ? 1 : 0);
