/**
 * S3-B2 探针：画布项目服务端同步（**免费**：不连服务端、不打任何真实网络）。
 *
 * esbuild 打包**真模块**（`services/canvas-project-sync.ts` + 真 `use-canvas-store` + 真 zustand persist +
 * 真 localforage），node 里 shim `window/localStorage/fetch` 后跑行为断言：
 *   cd web && node scripts/probe-s3-b2-project-sync.mjs
 * 任一断言不成立 → 退出码 1（不降级、不跳过）。
 *
 * 覆盖（对应任务书 v1.2 的合同条目）：
 *  1. §2.5 单人零改动：登录态 404 → 只探一次 /api/auth/me，落盘字节与基线逐字节一致、零额外字段、零 PUT（V4）
 *  2. §2.3 首帧前零 PUT（V5）+ 启动合并（本地独有保留 / 服务端更新覆盖 / 服务端独有补拉）顺序正确
 *  3. §2.2 写穿双写顺序：先 localForage 立写（400ms 防抖不变）→ 再逐项目防抖 PUT
 *  4. §2.2 防抖分桶：不同项目互不牵连，同一项目连改只发一次
 *  5. §2.1 CAS 409 → 重拉 GET 单项目 → mergeCanvasData：服务端胜则落服务端、本地胜则带 baseUpdatedAt 重试
 *  6. §2.2 / V8 离线 dirty 补推：PUT 网络失败保留 dirty，重启后补推成功
 *  7. §2.4 owner 三分支：本地无 owner（自动迁移）/ owner 不一致（**直接清理、零弹窗零导入**）/ 本账号留档自动并回
 *  8. §2.1 ISO↔ms 往返 + 删除 clamp `deletedAt = max(now, updatedAt)`
 *  9. 顺手①：PUT 401（体可能被 RST 吞掉 → 前端只见网络错误）→ 进 dirty 重试、不报死错
 */
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

async function loadEsbuild() {
    try {
        return await import("esbuild");
    } catch {
        const { readdirSync } = await import("node:fs");
        const store = path.join(root, "node_modules", ".pnpm");
        const dir = readdirSync(store).find((name) => /^esbuild@/.test(name));
        if (!dir) throw new Error("esbuild 不可用：请在 web/ 下执行 pnpm install");
        return import(pathToFileURL(path.join(store, dir, "node_modules", "esbuild", "lib", "main.js")).href);
    }
}
const esbuild = await loadEsbuild();

const workdir = mkdtempSync(path.join(tmpdir(), "probe-s3-b2-"));
const STORE_KEY = "infinite-canvas:canvas_store";

// ---- shim：必须**先于**目标模块求值（ESM import 按序求值）------------------------------
writeFileSync(
    path.join(workdir, "shim.mjs"),
    `export const storage = new Map();
export const calls = [];
const jsonResponse = (status, body) => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
});
globalThis.localStorage = {
    getItem: (key) => (storage.has(key) ? storage.get(key) : null),
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: (key) => storage.delete(key),
    clear: () => storage.clear(),
    key: (index) => Array.from(storage.keys())[index] ?? null,
    get length() { return storage.size; },
};
const listeners = new Map();
globalThis.addEventListener = (name, handler) => { listeners.set(name, handler); };
globalThis.removeEventListener = (name) => { listeners.delete(name); };
globalThis.window = globalThis;
globalThis.document = {
    visibilityState: "visible",
    addEventListener: () => {},
    removeEventListener: () => {},
};
globalThis.location = { origin: "http://localhost:3000", href: "http://localhost:3000/" };
let route = async () => jsonResponse(404, { error: "not found" });
export function setRoute(next) { route = next; }
export function dispatch(name) { const handler = listeners.get(name); if (handler) handler({ type: name }); }
globalThis.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : String(input && input.url);
    const entry = { url, method: (init && init.method) || "GET", body: init && init.body, at: Date.now() };
    calls.push(entry);
    return route(url, init, entry);
};
globalThis.__jsonResponse = jsonResponse;
`,
);

const entryRel = ["src/services/canvas-project-sync.ts", "src/stores/canvas/use-canvas-store.ts", "src/lib/canvas/canvas-merge.ts", "src/lib/canvas/canvas-timestamp.ts", "src/lib/canvas/canvas-sync-mode.ts"];
let entry = `import { setRoute, dispatch } from "./shim.mjs";
export { storage, calls, setRoute, dispatch } from "./shim.mjs";
export { default as lf } from "localforage";
`;
for (const rel of entryRel) {
    entry += `export * as ${rel.replace(/[^a-zA-Z0-9]/g, "_")} from ${JSON.stringify(path.join(root, rel).replace(/\\/g, "/"))};\n`;
}
writeFileSync(path.join(workdir, "entry.ts"), entry);

const result = await esbuild.build({
    entryPoints: [path.join(workdir, "entry.ts")],
    bundle: true,
    format: "esm",
    platform: "browser",
    write: false,
    alias: { "@": path.join(root, "src") },
    nodePaths: [path.join(root, "node_modules")],
    define: { "process.env.NODE_ENV": '"test"' },
    logLevel: "silent",
});
const outFile = path.join(workdir, "bundle.mjs");
writeFileSync(outFile, result.outputFiles[0].text);

const bundle = await import(pathToFileURL(outFile).href);
const sync = bundle.src_services_canvas_project_sync_ts;
const storeModule = bundle.src_stores_canvas_use_canvas_store_ts;
const ts = bundle.src_lib_canvas_canvas_timestamp_ts;
const modeModule = bundle.src_lib_canvas_canvas_sync_mode_ts;
const useCanvasStore = storeModule.useCanvasStore;
const shim = { storage: bundle.storage, calls: bundle.calls, setRoute: bundle.setRoute, dispatch: bundle.dispatch };
const lf = bundle.lf;

// ---- 断言工具 -------------------------------------------------------------------------
let passed = 0;
const failures = [];
function check(name, condition, detail) {
    if (condition) {
        passed += 1;
        return;
    }
    failures.push(`${name}${detail ? ` —— ${detail}` : ""}`);
}
function equal(name, actual, expected) {
    check(name, JSON.stringify(actual) === JSON.stringify(expected), `实际 ${JSON.stringify(actual)} / 期望 ${JSON.stringify(expected)}`);
}
const warn = console.warn;
console.warn = () => {};
console.info = () => {};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const PUTS = () => shim.calls.filter((call) => call.method === "PUT");
const DELETES = () => shim.calls.filter((call) => call.method === "DELETE");
const writes = () => shim.calls.filter((call) => call.method !== "GET");

const T0 = Date.now();   // 动态基准：让「服务端更新」真的比本机 now 新（时间戳全在毫秒域内）
const iso = (ms) => new Date(ms).toISOString();

function project(id, title, updatedAt, nodes = []) {
    return {
        id,
        title,
        createdAt: iso(T0 - 60_000),
        updatedAt: iso(updatedAt),
        nodes,
        connections: [],
        chatSessions: [],
        activeChatId: null,
        backgroundMode: "lines",
        showImageInfo: false,
        viewport: { x: 0, y: 0, k: 1 },
    };
}
function meta(item, over = {}) {
    const updatedAt = ts.toMillis(item.updatedAt);
    return { id: item.id, name: item.title, updatedAt, deleted: false, deletedAt: 0, bytes: 100, version: updatedAt, ...over };
}

async function seed(payload, projects = []) {
    shim.storage.clear();
    await lf.clear();
    if (payload) await lf.setItem(STORE_KEY, JSON.stringify(payload));
    useCanvasStore.setState({ projects, deletedProjects: [] });
}
async function cacheState() {
    const payload = await sync.readCanvasCache();
    return payload?.state ?? null;
}
async function cacheRaw() {
    return (await lf.getItem(STORE_KEY)) || "";
}

const authAlice = () => globalThis.__jsonResponse(200, { username: "alice" });
const authBob = () => globalThis.__jsonResponse(200, { username: "bob" });

// ----------------------------------------------------------------------------------------
// 1. 单人零改动（V4 指纹口径）
// ----------------------------------------------------------------------------------------
{
    const local = [project("solo-1", "单人项目", T0 - 5_000, [{ id: "n1", type: "text" }])];
    const baselinePayload = { state: { projects: local, deletedProjects: [] }, version: 0 };
    await seed(baselinePayload, local);
    await sleep(520);                       // 让 seed 时那次 setState 的 400ms 落盘先跑完（基线自身要稳定）
    const before = await cacheRaw();
    shim.calls.length = 0;
    shim.setRoute(async () => globalThis.__jsonResponse(404, { error: "not found" }));

    const snap = await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 });
    equal("1 单人：模式=local", snap.mode, "local");
    equal("1 单人：只探一次 /api/auth/me", shim.calls.map((call) => `${call.method} ${call.url}`), ["GET /api/auth/me"]);
    equal("1 单人：零写请求（无 PUT/DELETE）", writes().length, 0);
    const afterBoot = await cacheRaw();
    check("1 单人：落盘字节与基线完全一致（V4 指纹口径）", afterBoot === before, `长度 ${before.length} vs ${afterBoot.length}`);
    equal("1 单人：缓存 state 只有 projects/deletedProjects 两个键", Object.keys((await cacheState()) || {}).sort(), ["deletedProjects", "projects"]);
    equal("1 单人：无 dirty/owner 记账", [sync.canvasSyncDiagnostics().dirty.length, sync.canvasSyncDiagnostics().owner], [0, ""]);
    // 单人模式下的一次画布改动：仍然只有两个键、时间戳模型不变（无远端副作用）
    useCanvasStore.getState().updateProject("solo-1", { nodes: [{ id: "n2", type: "text" }] });
    await sleep(520);
    equal("1 单人：改动后仍零写请求", writes().length, 0);
    equal("1 单人：改动后缓存 state 键仍只有两个", Object.keys((await cacheState()) || {}).sort(), ["deletedProjects", "projects"]);
    check("1 单人：改动已落盘（400ms 防抖不变）", ((await cacheState())?.projects?.[0]?.nodes || []).length === 1, JSON.stringify(await cacheState()));
}

// ----------------------------------------------------------------------------------------
// 2. 启动门：首帧前零 PUT（V5）+ 三方合并（本地独有/服务端更新/服务端独有）
// ----------------------------------------------------------------------------------------
{
    const localOnly = project("L1", "本地独有", T0 - 3_000, [{ id: "local-node" }]);
    const staleLocal = project("S1", "旧版本", T0 - 9_000, [{ id: "old-node" }]);
    await seed({ state: { projects: [localOnly, staleLocal], deletedProjects: [] }, version: 0 }, [localOnly, staleLocal]);

    const serverS1 = project("S1", "服务端更新版", T0 - 1_000, [{ id: "fresh-node" }, { id: "fresh-node-2" }]);
    const serverS2 = project("S2", "服务端独有", T0 - 2_000, [{ id: "s2-node" }]);
    shim.calls.length = 0;
    shim.setRoute(async (url, init) => {
        if (url === "/api/auth/me") return authAlice();
        if (url === "/api/user/projects" && !init?.method) {
            return globalThis.__jsonResponse(200, {
                projects: [meta(serverS1), meta(serverS2)],
                owner: "alice",
                serverTime: Date.now(),
                unit: "ms",
            });
        }
        if (init?.method === "PUT") return globalThis.__jsonResponse(200, { ok: true, project: meta(JSON.parse(init.body).content) });
        if (url === "/api/user/projects/S1") return globalThis.__jsonResponse(200, { project: { ...meta(serverS1), content: serverS1 } });
        if (url === "/api/user/projects/S2") return globalThis.__jsonResponse(200, { project: { ...meta(serverS2), content: serverS2 } });
        return globalThis.__jsonResponse(500, { error: `stub 不认：${url}` });
    });

    const snap = await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 });
    equal("2 启动门：模式=remote", snap.mode, "remote");
    equal("2 V5：启动门返回瞬间零 PUT/DELETE（首帧前零远端写）", writes().length, 0, JSON.stringify(writes().map((call) => `${call.method} ${call.url}`)));
    const order = shim.calls.map((call) => `${call.method} ${call.url}`);
    check("2 顺序：先清单、再单项目 content", order.indexOf("GET /api/user/projects") >= 0 && order.indexOf("GET /api/user/projects") < order.indexOf("GET /api/user/projects/S1"), JSON.stringify(order));
    const ids = useCanvasStore.getState().projects.map((item) => item.id).sort();
    equal("2 合并结果：三方都在", ids, ["L1", "S1", "S2"]);
    const s1 = useCanvasStore.getState().projects.find((item) => item.id === "S1");
    equal("2 服务端更新 → 服务端内容胜出", s1.nodes.map((node) => node.id), ["fresh-node", "fresh-node-2"]);
    equal("2 本地独有项目原样保留（不被服务端清单丢弃）", useCanvasStore.getState().projects.find((item) => item.id === "L1").nodes.map((node) => node.id), ["local-node"]);
    equal("2 服务端独有项目补拉回来", useCanvasStore.getState().projects.find((item) => item.id === "S2").nodes.map((node) => node.id), ["s2-node"]);

    // polish①：这条断言原名写「250ms 仍零 PUT」但实际是在启动门返回后立刻判（间隔 0ms）。
    // 这里真的等 250ms 再断言，标题与行为一致（400ms 防抖窗口内不该有任何 PUT）。
    const gateReturnedAt = Date.now();
    await sleep(250);
    check(
        "2 V5：启动门返回后 250ms 仍零 PUT（400ms 防抖窗口内）",
        PUTS().length === 0 && Date.now() - gateReturnedAt >= 240,
        `${PUTS().length} puts / ${Date.now() - gateReturnedAt}ms`,
    );
    await sleep(520);
    const puts = PUTS();
    equal("2 只有本地独有项目被推（服务端一致的零流量）", puts.map((call) => call.url), ["/api/user/projects/L1"]);
    const body = JSON.parse(puts[0].body);
    check("2 推送时间戳是 int ms（不是 ISO）", Number.isInteger(body.updatedAt) && body.updatedAt > 1e11, String(body.updatedAt));
    check("2 服务端没有这行 → 不带 baseUpdatedAt（迁移语义）", body.baseUpdatedAt === undefined, JSON.stringify(body.baseUpdatedAt));

    // ------------------------------------------------------------------------------------
    // 3. 写穿双写顺序 + 4. 防抖分桶
    // ------------------------------------------------------------------------------------
    shim.calls.length = 0;
    const start3 = Date.now();
    const before = (await cacheState()).projects.find((item) => item.id === "S1").nodes.length;
    useCanvasStore.getState().updateProject("S1", { nodes: [{ id: "edit-1" }, { id: "edit-2" }] });
    await sleep(250);
    equal("3 400ms 防抖不变：250ms 时缓存还是旧值", (await cacheState()).projects.find((item) => item.id === "S1").nodes.length, before);
    equal("3 250ms 时还没发 PUT", PUTS().length, 0);
    await sleep(220); // ≈470ms：立写已发生，PUT 还在自己的 400ms 桶里
    equal("3 缓存已立写（写穿第一步）", (await cacheState()).projects.find((item) => item.id === "S1").nodes.length, 2);
    equal("3 立写之后、PUT 之前（写穿第二步在防抖桶里）", PUTS().length, 0);
    // 轮询捕捉「缓存写入」与「PUT 发出」的真实时刻，证明写穿双写的先后（不是靠顺序猜）
    let cacheWriteAt = 0;
    while (!cacheWriteAt && Date.now() - start3 < 3_000) {
        if ((await cacheState()).projects.find((item) => item.id === "S1").nodes.length === 2) cacheWriteAt = Date.now();
        else await sleep(10);
    }
    while (!PUTS().length && Date.now() - start3 < 3_000) await sleep(10);
    await sleep(20);
    equal("3 写穿第二步：PUT 发出", PUTS().map((call) => call.url), ["/api/user/projects/S1"]);
    check(
        "3 顺序：localForage 立写早于远端 PUT",
        cacheWriteAt > 0 && PUTS()[0].at >= cacheWriteAt,
        `cache=${cacheWriteAt} put=${PUTS()[0]?.at}`,
    );
    // 轮询有 10ms 粒度 + 定时器排队，桶宽放宽到 250–1200ms：要证明的是「不是立刻发、也不是很久才发」
    const gap = (PUTS()[0]?.at ?? 0) - cacheWriteAt;
    check("3 PUT 距缓存写入落在 400ms 防抖桶量级（250–1200ms）", gap >= 250 && gap <= 1_200, `${gap}ms`);
    const s1Put = JSON.parse(PUTS()[0].body);
    equal("3 PUT 带 baseUpdatedAt = 服务端 version（CAS 基线）", s1Put.baseUpdatedAt, meta(serverS1).version);

    // 4. 分桶：两个项目各改一次 → 各一次 PUT；同一项目连改 3 次 → 只 1 次
    shim.calls.length = 0;
    useCanvasStore.getState().updateProject("S2", { nodes: [{ id: "s2-a" }] });
    useCanvasStore.getState().updateProject("L1", { nodes: [{ id: "l1-a" }] });
    await sleep(30);
    useCanvasStore.getState().updateProject("S2", { nodes: [{ id: "s2-b" }] });
    useCanvasStore.getState().updateProject("S2", { nodes: [{ id: "s2-c" }] });
    await sleep(1_200);
    equal("4 分桶：两个项目各一次 PUT（互不合并）", PUTS().map((call) => call.url).sort(), ["/api/user/projects/L1", "/api/user/projects/S2"]);
    equal("4 同一项目连改只发一次（内容取最新）", PUTS().filter((call) => call.url === "/api/user/projects/S2").length, 1);

    // ------------------------------------------------------------------------------------
    // 5. 409 → 重拉 GET 单项目 → mergeCanvasData（409 体只有元数据，不能就地合并）
    //    造法：本地基于旧版本产生 dirty（服务端另有更新的改写，例如另一台设备刚改过）→ PUT 必 409
    const serverNewer = project("S2", "服务端又改了", T0 + 5_000, [{ id: "conflict-server" }, { id: "conflict-server-2" }]);
    const serverOlderBase = project("S2", "旧版本基", T0 - 9_000, [{ id: "conflict-old" }]);
    shim.calls.length = 0;
    let sawConflict = 0;
    shim.setRoute(async (url, init) => {
        if (url === "/api/auth/me") return authAlice();
        if (url === "/api/user/projects" && !init?.method) {
            // 服务端清单：S1/S2 都是「旧版本基」（本地随后改出来的改动一定比它新 → dirty）
            return globalThis.__jsonResponse(200, { projects: [meta(serverS1), meta(serverOlderBase)], owner: "alice", serverTime: Date.now(), unit: "ms" });
        }
        if (url === "/api/user/projects/S2" && init?.method === "PUT") {
            sawConflict += 1;
            return globalThis.__jsonResponse(409, { error: "版本冲突", conflict: true, server: meta(serverNewer) });
        }
        if (url === "/api/user/projects/S2") return globalThis.__jsonResponse(200, { project: { ...meta(serverNewer), content: serverNewer } });
        if (url === "/api/user/projects/S1") return globalThis.__jsonResponse(200, { project: { ...meta(serverS1), content: serverS1 } });
        if (init?.method === "PUT") return globalThis.__jsonResponse(200, { ok: true, project: meta(JSON.parse(init.body).content) });
        return globalThis.__jsonResponse(500, { error: `stub 不认：${url}` });
    });
    // 重新走启动门：本地 S2 = S1 那版旧内容，服务端清单说 S2 也是旧的 → 合并后本地仍是「旧版基」
    await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 });
    // 本地改一刀（updatedAt=now，比服务端旧版本新 → dirty → 必 PUT）
    useCanvasStore.getState().updateProject("S2", { nodes: [{ id: "conflict-local" }] });
    await sleep(1_400);
    const seq = shim.calls.map((call) => `${call.method} ${call.url}`);
    check("5 409 之后重拉了单项目 content（不是就地合并）", seq.includes("PUT /api/user/projects/S2") && seq.indexOf("GET /api/user/projects/S2") > seq.indexOf("PUT /api/user/projects/S2"), JSON.stringify(seq));
    equal("5 服务端版本更新 → 服务端内容胜出（LWW，且只打一次 409）", [useCanvasStore.getState().projects.find((item) => item.id === "S2").nodes.map((node) => node.id), sawConflict], [["conflict-server", "conflict-server-2"], 1]);
    check("5 服务端胜出后 dirty 清空（不留待推噪音）", !sync.canvasSyncDiagnostics().dirty.includes("S2"), JSON.stringify(sync.canvasSyncDiagnostics().dirty));

    // 5b. 本地更新（服务端较旧）→ 本地胜，带 baseUpdatedAt 重试一次
    const serverOlder = project("S3", "服务端旧版", T0 - 60_000, [{ id: "conflict-old" }]);
    shim.calls.length = 0;
    let retried = null;
    shim.setRoute(async (url, init) => {
        if (url === "/api/auth/me") return authAlice();
        if (url === "/api/user/projects" && !init?.method) {
            return globalThis.__jsonResponse(200, { projects: [meta(serverS1), meta(serverOlder)], owner: "alice", serverTime: Date.now(), unit: "ms" });
        }
        if (url === "/api/user/projects/S3" && init?.method === "PUT") {
            const body = JSON.parse(init.body);
            if (retried === null) {
                retried = false;
                return globalThis.__jsonResponse(409, { error: "版本冲突", conflict: true, server: meta(serverOlder) });
            }
            retried = body;
            return globalThis.__jsonResponse(200, { ok: true, project: meta(body.content) });
        }
        if (url === "/api/user/projects/S3") return globalThis.__jsonResponse(200, { project: { ...meta(serverOlder), content: serverOlder } });
        if (url === "/api/user/projects/S1") return globalThis.__jsonResponse(200, { project: { ...meta(serverS1), content: serverS1 } });
        if (init?.method === "PUT") return globalThis.__jsonResponse(200, { ok: true, project: meta(JSON.parse(init.body).content) });
        return globalThis.__jsonResponse(500, { error: `stub 不认：${url}` });
    });
    // 本地有一份 S3 的更新版本（不在服务端清单的旧版之上）→ 迁移语义：dirty + 必 PUT
    useCanvasStore.getState().replaceProjects(
        [...useCanvasStore.getState().projects.filter((item) => item.id !== "S3"), project("S3", "本地新版", T0 - 1_000, [{ id: "local-newest" }])],
        useCanvasStore.getState().deletedProjects,
    );
    await sleep(1_400);
    check("5b 本地更新 → 带服务端 version 重试一次", Boolean(retried && retried.baseUpdatedAt === meta(serverOlder).version), JSON.stringify(retried && { base: retried.baseUpdatedAt, want: meta(serverOlder).version }));
    equal("5b 本地内容没有被旧服务端版本回滚", useCanvasStore.getState().projects.find((item) => item.id === "S3").nodes.map((node) => node.id), ["local-newest"]);
}

// ----------------------------------------------------------------------------------------
// 6. 离线 dirty 补推（V8）+ 顺手①：401/网络错误进 dirty
// ----------------------------------------------------------------------------------------
{
    const local = [project("F1", "断网改的项目", T0 - 1_000, [{ id: "offline-node" }])];
    await seed({ state: { projects: local, deletedProjects: [] }, version: 0 }, local);
    let networkDown = true;
    shim.calls.length = 0;
    shim.setRoute(async (url, init) => {
        if (url === "/api/auth/me") return authAlice();
        if (url === "/api/user/projects" && !init?.method) {
            return globalThis.__jsonResponse(200, { projects: [], owner: "alice", serverTime: Date.now(), unit: "ms" });
        }
        if (init?.method === "PUT") {
            if (networkDown) throw new TypeError("Failed to fetch"); // 断网 / 401 体被 RST 吞掉的同一表象
            return globalThis.__jsonResponse(200, { ok: true, project: meta(JSON.parse(init.body).content) });
        }
        return globalThis.__jsonResponse(500, { error: `stub 不认：${url}` });
    });

    await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 });
    await sleep(1_000);
    check("6 断网时 PUT 真的尝试过并失败", PUTS().length >= 1, String(PUTS().length));
    const dirtyCache = await cacheState();
    check("6 失败后 dirty 落进缓存载荷（不丢、不静默）", Boolean(dirtyCache?.dirtyIds?.F1), JSON.stringify(dirtyCache?.dirtyIds));
    check("6 失败文案说明「改动已留在本地」", /留在本地/.test(sync.canvasProjectSyncSnapshot().error), sync.canvasProjectSyncSnapshot().error);
    equal("6 本地项目一个不少", useCanvasStore.getState().projects.map((item) => item.id), ["F1"]);

    // ---- 模拟重启：内存清空 → 重新走启动门（盘上 dirty 仍在）----
    networkDown = false;
    shim.calls.length = 0;
    useCanvasStore.setState({ projects: [], deletedProjects: [] });
    await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 });
    equal("6 重启后从缓存恢复项目", useCanvasStore.getState().projects.map((item) => item.id), ["F1"]);
    await sleep(1_000);
    equal("6 V8 启动补推：dirty 被补推成功", PUTS().map((call) => call.url), ["/api/user/projects/F1"]);
    const cleanCache = await cacheState();
    check("6 补推成功后 dirty 清空", !cleanCache?.dirtyIds || Object.keys(cleanCache.dirtyIds).length === 0, JSON.stringify(cleanCache?.dirtyIds));

    // ---- 顺手①：401 → 进 dirty 重试，不报死错 ----
    shim.calls.length = 0;
    shim.setRoute(async (url, init) => {
        if (url === "/api/user/projects/F1" && init?.method === "PUT") return globalThis.__jsonResponse(401, { error: "未登录" });
        if (url === "/api/auth/me") return authAlice();
        if (url === "/api/user/projects") return globalThis.__jsonResponse(200, { projects: [meta(project("F1", "断网改的项目", T0 - 1000, [{ id: "offline-node" }]))], owner: "alice", serverTime: Date.now(), unit: "ms" });
        return globalThis.__jsonResponse(500, { error: `stub 不认：${url}` });
    });
    await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 });
    useCanvasStore.getState().updateProject("F1", { nodes: [{ id: "after-401" }] });
    await sleep(1_200);
    check("9 PUT 401 → 保留 dirty（视为可能登录过期）", Boolean((await cacheState())?.dirtyIds?.F1), JSON.stringify((await cacheState())?.dirtyIds));
    check("9 401 文案不当作「永久失败」", /过期|重试/.test(sync.canvasProjectSyncSnapshot().error), sync.canvasProjectSyncSnapshot().error);
}

// ----------------------------------------------------------------------------------------
// 7. owner 三分支（§2.4 / V9）
// ----------------------------------------------------------------------------------------
{
    // (a) 本地无 owner → 自动迁移（合并后自然 dirty → 上推）
    const local = [project("M1", "首次迁移", T0 - 1_000, [{ id: "m1" }])];
    await seed({ state: { projects: local, deletedProjects: [{ id: "MD1", deletedAt: iso(T0 - 500) }] }, version: 0 }, local);
    useCanvasStore.setState({ deletedProjects: [{ id: "MD1", deletedAt: iso(T0 - 500) }] });
    shim.calls.length = 0;
    shim.setRoute(async (url, init) => {
        if (url === "/api/auth/me") return authAlice();
        if (url === "/api/user/projects" && !init?.method) return globalThis.__jsonResponse(200, { projects: [], owner: "alice", serverTime: Date.now(), unit: "ms" });
        if (init?.method === "PUT") return globalThis.__jsonResponse(200, { ok: true, project: meta(JSON.parse(init.body).content) });
        if (init?.method === "DELETE") return globalThis.__jsonResponse(200, { ok: true, project: { id: "MD1", name: "", updatedAt: 0, deleted: true, deletedAt: T0 - 500, bytes: 0, version: T0 - 500 } });
        return globalThis.__jsonResponse(500, { error: `stub 不认：${url}` });
    });
    const snapA = await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 });
    equal("7a 本地无 owner → 无弹窗（自动迁移）", snapA.importPrompt, undefined);
    await sleep(1_200);
    equal("7a 迁移：项目 PUT + 墓碑 DELETE 都上推", [PUTS().map((call) => call.url), DELETES().map((call) => call.url)], [["/api/user/projects/M1"], ["/api/user/projects/MD1"]]);
    const migrated = await cacheState();
    equal("7a 迁移后缓存载荷带 owner", migrated.owner, "alice");
    check("7a 迁移后记录 migratedAt", typeof migrated.migratedAt === "string" && migrated.migratedAt.length > 0, JSON.stringify(migrated.migratedAt));

    // (b) owner 不一致 → 直接清理，绝不弹窗、绝不导入（所有者 2026-11-19 裁定：删除跨账号导入）
    const aliceProjects = [project("A1", "A 的项目一", T0 - 2_000, [{ id: "a1" }]), project("A2", "A 的项目二", T0 - 1_000, [{ id: "a2" }])];
    await seed({ state: { projects: aliceProjects, deletedProjects: [], owner: "alice", migratedAt: iso(T0) }, version: 0 }, aliceProjects);
    // 顺带造一份 alice 的历史留档键：新逻辑必须一并清掉
    await lf.setItem(sync.FOREIGN_CACHE_PREFIX + "alice", JSON.stringify({ state: { projects: aliceProjects, deletedProjects: [], owner: "alice" }, version: 0 }));
    shim.calls.length = 0;
    shim.setRoute(async (url, init) => {
        if (url === "/api/auth/me") return authBob();
        if (url === "/api/user/projects" && !init?.method) {
            const bobProject = project("B1", "B 的项目", T0 - 500, [{ id: "b1" }]);
            return globalThis.__jsonResponse(200, { projects: [meta(bobProject)], owner: "bob", serverTime: Date.now(), unit: "ms" });
        }
        if (url === "/api/user/projects/B1") return globalThis.__jsonResponse(200, { project: { ...meta(project("B1", "B 的项目", T0 - 500, [{ id: "b1" }])), content: project("B1", "B 的项目", T0 - 500, [{ id: "b1" }]) } });
        if (init?.method === "PUT") return globalThis.__jsonResponse(200, { ok: true, project: meta(JSON.parse(init.body).content) });
        return globalThis.__jsonResponse(500, { error: `stub 不认：${url}` });
    });
    const snapB = await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 });
    equal("7b 无跨账号弹窗状态（字段已删除，恒 undefined）", snapB.importPrompt, undefined);
    equal("7b 绝不串号：A 的项目不出现在当前账号", useCanvasStore.getState().projects.map((item) => item.id), ["B1"]);
    const bobCache = await cacheState();
    equal("7b 共享缓存归属当前账号且不含 A 的项目", [bobCache.owner, bobCache.projects.map((item) => item.id)], ["bob", ["B1"]]);
    check("7b A 在本机的缓存与历史留档被删除（不询问、不导入）", (await lf.getItem(sync.FOREIGN_CACHE_PREFIX + "alice")) === null, "alice 留档还在");
    await sleep(1_200);
    equal("7b 零 PUT（不会把别人的项目推上去）", PUTS().length, 0, JSON.stringify(PUTS().map((call) => call.url)));

    // (c) 幂等：再走一次启动门，仍无弹窗、仍只有当前账号的项目
    const snapC = await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 });
    equal("7c 重启门仍无弹窗", snapC.importPrompt, undefined);
    equal("7c 仍只有当前账号项目", useCanvasStore.getState().projects.map((item) => item.id), ["B1"]);

    // (d) 本账号自己的留档仍能恢复（删的是「跨账号导入」，不是「断网草稿」）
    await sync.prepareCanvasLogout();
    check("7d 登出把本账号载荷留档", typeof (await lf.getItem(sync.FOREIGN_CACHE_PREFIX + "bob")) === "string");
    const snapD = await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 });
    equal("7d 同账号回来草稿自动并回（无需确认）", useCanvasStore.getState().projects.map((item) => item.id).sort(), ["B1"]);
    check("7d 并回落盘后本账号留档键被清掉", (await lf.getItem(sync.FOREIGN_CACHE_PREFIX + "bob")) === null, "bob 留档还在");
    equal("7d 全程无弹窗", snapD.importPrompt, undefined);

    // (e) logout：留档 + 清空主键（V9）
    await sync.prepareCanvasLogout();
    equal("7e 登出后主键清空（B 登录看不到任何项目）", await lf.getItem(STORE_KEY), null);
    equal("7e 登出后内存也不留项目", [useCanvasStore.getState().projects.length, useCanvasStore.getState().deletedProjects.length], [0, 0]);
    check("7e 登出把本账号载荷留档（断网未推部分不丢）", typeof (await lf.getItem(sync.FOREIGN_CACHE_PREFIX + "bob")) === "string");
}

// ----------------------------------------------------------------------------------------
// 8. ISO↔ms 往返 + 删除 clamp
// ----------------------------------------------------------------------------------------
{
    equal("8 ISO→ms→ISO 往返", ts.toIso(ts.toMillis("2026-11-16T10:00:00.000Z")), "2026-11-16T10:00:00.000Z");
    equal("8 数字串也按 ms 解析", ts.toMillis("1763000000000"), 1763000000000);
    equal("8 非法值返 null（不猜单位）", ts.toMillis("不是时间"), null);
    equal("8 秒级值不在毫秒域（前端自检拦下，省一次必然 422）", ts.inMillisDomain(1763000000), false);
    equal("8 毫秒值在域内", ts.inMillisDomain(1763000000000), true);

    const body = sync.buildProjectPutBody(project("P1", "时间戳", T0), 1763000000000);
    equal("8 PUT body 的时间戳是 int ms", [body.updatedAt, body.baseUpdatedAt], [T0, 1763000000000]);
    equal("8 非法 updatedAt 的项目拒绝推送（不代填时间）", sync.buildProjectPutBody({ ...project("P1", "x", T0), updatedAt: "garbage" }), null);
    const del = sync.buildProjectDeleteBody(iso(T0));
    equal("8 DELETE body 的 deletedAt 是 int ms", del.deletedAt, T0);
    equal("8 非法 deletedAt 拒绝推送", sync.buildProjectDeleteBody("garbage"), null);

    // clamp：本机时钟落后于项目 updatedAt 时，墓碑必须抬到 updatedAt（否则另一台设备的旧 PUT 会复活）
    const future = iso(Date.now() + 120_000);
    equal("8 clamp：deletedAt = max(now, updatedAt)", ts.clampDeletedAtIso(future, Date.now()), future);
    equal("8 clamp：正常时钟下就是 now", ts.clampDeletedAtIso(iso(T0 - 10_000), T0), iso(T0));
    useCanvasStore.setState({ projects: [project("C1", "clamp", Date.parse(future))], deletedProjects: [] });
    useCanvasStore.getState().deleteProjects(["C1"]);
    equal("8 store 删除时也 clamp（墓碑 ≥ 项目最后改写）", useCanvasStore.getState().deletedProjects[0].deletedAt, future);
}

// ----------------------------------------------------------------------------------------
// 9. WebDAV 画布域在 remote 模式被跳过（§2.5）——只断言模式开关，不去跑 WebDAV 网络
// ----------------------------------------------------------------------------------------
{
    equal("9 本地模式：isRemoteCanvasMode=false（WebDAV 画布域照常走）", [modeModule.getCanvasSyncMode(), modeModule.isRemoteCanvasMode()], ["local", false]);
}

// ----------------------------------------------------------------------------------------
// 10. BL1：登出留档必须能恢复 + 二次登出不得覆盖（审计原探针路径 logout→relogin→再 logout）
// ----------------------------------------------------------------------------------------
{
    const offlineProject = project("P_LOCAL", "离线画的项目", T0 - 1_000, [{ id: "local-node-1" }]);
    await seed({ state: { projects: [offlineProject], deletedProjects: [], owner: "alice" }, version: 0 }, [offlineProject]);

    // 断网：推送从未成功过，数据只在本机
    shim.calls.length = 0;
    shim.setRoute(async () => {
        throw new TypeError("Failed to fetch");
    });
    const offlineSnap = await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 });
    equal("10 断网 → 离线态（不是单人模式）", offlineSnap.mode, "offline");
    useCanvasStore.getState().updateProject("P_LOCAL", { nodes: [{ id: "local-node-1" }, { id: "local-node-2" }] });
    await sleep(520);
    check("10 离线改动已落盘（含 dirty 标记）", Boolean((await cacheState())?.dirtyIds?.P_LOCAL), JSON.stringify((await cacheState())?.dirtyIds));

    // ---- 第一步：登出（留档 + 清空共享主键）----
    await sync.prepareCanvasLogout();
    const archivedRaw = await lf.getItem(sync.FOREIGN_CACHE_PREFIX + "alice");
    check("10a 登出留档含离线项目", typeof archivedRaw === "string" && archivedRaw.includes("P_LOCAL"), String(archivedRaw).slice(0, 60));
    equal("10a 登出后共享主键清空（B 登录看不到）", await lf.getItem(STORE_KEY), null);
    equal("10a 登出后内存清空", [useCanvasStore.getState().projects.length, useCanvasStore.getState().deletedProjects.length], [0, 0]);

    // ---- 第二步：同账号登回（服务端清单为空：模拟推送从未成功）----
    shim.calls.length = 0;
    shim.setRoute(async (url, init) => {
        if (url === "/api/auth/me") return authAlice();
        if (url === "/api/user/projects" && !init?.method) return globalThis.__jsonResponse(200, { projects: [], owner: "alice", serverTime: Date.now(), unit: "ms" });
        if (init?.method === "PUT") return globalThis.__jsonResponse(200, { ok: true, project: meta(JSON.parse(init.body).content) });
        return globalThis.__jsonResponse(500, { error: `stub 不认：${url}` });
    });
    const reloginSnap = await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 });
    equal("10b 同账号登回：模式=remote", reloginSnap.mode, "remote");
    equal("10b 登出留档必须恢复回来（BL1①）", useCanvasStore.getState().projects.map((item) => item.id), ["P_LOCAL"]);
    equal("10b 恢复的是离线时最后的版本（2 个节点）", (useCanvasStore.getState().projects[0]?.nodes || []).map((node) => node.id), ["local-node-1", "local-node-2"]);
    check("10b 恢复后留档键已消费（④：不重复 merge 旧快照）", (await lf.getItem(sync.FOREIGN_CACHE_PREFIX + "alice")) === null, "留档键还在");
    await sleep(1_000);
    equal("10b 恢复的项目被补推（服务端本来没有）", PUTS().map((call) => call.url), ["/api/user/projects/P_LOCAL"]);

    // ---- 第三步：再登出（本次载荷已含 P_LOCAL；归档里也必须仍在）----
    await sleep(520);
    await sync.prepareCanvasLogout();
    const archivedAgain = await lf.getItem(sync.FOREIGN_CACHE_PREFIX + "alice");
    check("10c 二次登出后 foreign:alice 仍含 P_LOCAL（BL1②不被小载荷覆盖）", typeof archivedAgain === "string" && archivedAgain.includes("P_LOCAL"), String(archivedAgain).slice(0, 60));

    // ---- 10d：BL1②覆盖前先合并（旧归档有 A_KEEP，本次载荷只有 A_NEW → 两个都要在）----
    const keep = project("A_KEEP", "上一次登出的留档", T0 - 5_000, [{ id: "keep-node" }]);
    const fresh = project("A_NEW", "这次的载荷", T0 - 1_000, [{ id: "new-node" }]);
    await seed({ state: { projects: [fresh], deletedProjects: [], owner: "alice" }, version: 0 }, [fresh]);
    // seed 会清库，所以旧归档必须在 seed 之后写（模拟「上一次登出留下的完整留档」）
    await lf.setItem(sync.FOREIGN_CACHE_PREFIX + "alice", JSON.stringify({ state: { projects: [keep], deletedProjects: [], owner: "alice", dirtyIds: { A_KEEP: T0 - 5_000 } }, version: 0 }));
    // 清单读失败 → 进离线态（owner 建立起来，但**不会**去恢复归档，能干净地只测 logout 的合并语义）
    shim.calls.length = 0;
    shim.setRoute(async (url) => {
        if (url === "/api/auth/me") return authAlice();
        throw new TypeError("Failed to fetch");
    });
    await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 });
    await sleep(520);
    await sync.prepareCanvasLogout();
    const mergedRaw = await lf.getItem(sync.FOREIGN_CACHE_PREFIX + "alice");
    const mergedState = JSON.parse(String(mergedRaw)).state;
    equal("10d 覆盖前先合并旧归档：两份项目都在", mergedState.projects.map((item) => item.id).sort(), ["A_KEEP", "A_NEW"]);
    check("10d dirtyIds 取并集（旧归档的待推项不被丢）", Boolean(mergedState.dirtyIds?.A_KEEP) && Boolean(mergedState.dirtyIds?.A_NEW), JSON.stringify(mergedState.dirtyIds));

    // ---- 10e：① owner 为空时登出**不得**删掉单人数据 ----
    const soloLocal = [project("SOLO_KEEP", "单人项目", T0 - 1_000, [{ id: "solo-node" }])];
    await seed({ state: { projects: soloLocal, deletedProjects: [] }, version: 0 }, soloLocal);
    await sleep(520);
    shim.setRoute(async () => globalThis.__jsonResponse(404, { error: "not found" }));
    await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 }); // 单人：local 模式、owner=""
    const soloBefore = await cacheRaw();
    await sync.prepareCanvasLogout();
    equal("10e owner 为空时登出不删单人数据（①）", await cacheRaw(), soloBefore);
    equal("10e 单人数据还在内存里", useCanvasStore.getState().projects.map((item) => item.id), ["SOLO_KEEP"]);
}

// ----------------------------------------------------------------------------------------
// 11. BL2：online 重跑启动门不得吞掉在途编辑（内存 / 盘上 / 补推 PUT 三处都要有）
// ----------------------------------------------------------------------------------------
{
    const base = project("ON1", "在线编辑", T0 - 1_000, [{ id: "n-old" }]);
    await seed({ state: { projects: [base], deletedProjects: [], owner: "alice" }, version: 0 }, [base]);
    shim.calls.length = 0;
    shim.setRoute(async (url, init) => {
        if (url === "/api/auth/me") return authAlice();
        if (url === "/api/user/projects" && !init?.method) return globalThis.__jsonResponse(200, { projects: [meta(base)], owner: "alice", serverTime: Date.now(), unit: "ms" });
        if (url === "/api/user/projects/ON1" && !init?.method) return globalThis.__jsonResponse(200, { project: { ...meta(base), content: base } });
        if (init?.method === "PUT") return globalThis.__jsonResponse(200, { ok: true, project: meta(JSON.parse(init.body).content) });
        return globalThis.__jsonResponse(500, { error: `stub 不认：${url}` });
    });
    await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 });
    await sleep(600); // 与服务端一致 → 零 dirty、零 PUT，基线稳定
    shim.calls.length = 0;

    // 在途编辑：只改内存，400ms 落盘窗口内立刻收到 online 事件（真实场景：断网时一直在画，突然联网）
    useCanvasStore.getState().updateProject("ON1", { nodes: [{ id: "n-old" }, { id: "n-inflight" }] });
    equal("11 前提：编辑还在防抖窗口里（盘上仍是旧值）", (await cacheState()).projects.find((item) => item.id === "ON1").nodes.length, 1);
    shim.dispatch("online");
    await sleep(1_400);

    equal("11 online 重跑启动门后内存未被旧盘值盖回（BL2）", useCanvasStore.getState().projects.find((item) => item.id === "ON1").nodes.map((node) => node.id), ["n-old", "n-inflight"]);
    equal("11 盘上缓存也是在途编辑后的内容", (((await cacheState()).projects || []).find((item) => item.id === "ON1")?.nodes || []).map((node) => node.id), ["n-old", "n-inflight"]);
    equal("11 在途编辑被补推（PUT 发出且只发一次）", PUTS().map((call) => call.url), ["/api/user/projects/ON1"]);
    check("11 补推内容含在途节点", Boolean(PUTS()[0]) && JSON.parse(PUTS()[0].body).content.nodes.map((node) => node.id).join(",") === "n-old,n-inflight", PUTS()[0] ? PUTS()[0].body.slice(0, 120) : "no put");
    check("11 补推成功后 dirty 清空（不留下重复推）", !(await cacheState())?.dirtyIds?.ON1, JSON.stringify((await cacheState())?.dirtyIds));
}

// ----------------------------------------------------------------------------------------
// 12. ③：时间戳非法、永远进不了 dirty 的项目要**可见**（不静默「本地有云端没有」）
// ----------------------------------------------------------------------------------------
{
    const broken = { ...project("BAD1", "时间戳坏了", T0), updatedAt: "garbage" };
    await seed({ state: { projects: [broken], deletedProjects: [], owner: "alice" }, version: 0 }, [broken]);
    shim.calls.length = 0;
    shim.setRoute(async (url, init) => {
        if (url === "/api/auth/me") return authAlice();
        if (url === "/api/user/projects" && !init?.method) return globalThis.__jsonResponse(200, { projects: [], owner: "alice", serverTime: Date.now(), unit: "ms" });
        if (init?.method === "PUT") return globalThis.__jsonResponse(200, { ok: true, project: meta(JSON.parse(init.body).content) });
        return globalThis.__jsonResponse(500, { error: `stub 不认：${url}` });
    });
    await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 });
    await sleep(1_000);
    equal("12 非法时间戳项目不推（前端自检拦下，不猜单位）", PUTS().length, 0);
    equal("12 invalidCount 可见（③：顶栏据此提示，不再静默）", sync.canvasProjectSyncSnapshot().invalidCount, 1);
    equal("12 非法项目仍在本机（不删用户数据）", useCanvasStore.getState().projects.map((item) => item.id), ["BAD1"]);
    equal("12 它也确实进不了 dirty（dirtyCount 不被脏数据污染）", sync.canvasProjectSyncSnapshot().dirtyCount, 0);
}

// ----------------------------------------------------------------------------------------
// 13. 其他账号留在本机的留档一律清理（多条目也不询问、不串号）
// ----------------------------------------------------------------------------------------
{
    const bobProject = project("NB1", "bob 自己的", T0 - 1_000, [{ id: "nb1" }]);
    await seed({ state: { projects: [bobProject], deletedProjects: [], owner: "bob" }, version: 0 }, [bobProject]);
    await lf.setItem(sync.FOREIGN_CACHE_PREFIX + "alice", JSON.stringify({ state: { projects: [project("X1", "alice 的", T0 - 2_000, [{ id: "x1" }])], deletedProjects: [], owner: "alice" }, version: 0 }));
    await lf.setItem(sync.FOREIGN_CACHE_PREFIX + "carol", JSON.stringify({ state: { projects: [project("Y1", "carol 的", T0 - 2_000, [{ id: "y1" }])], deletedProjects: [], owner: "carol" }, version: 0 }));
    shim.calls.length = 0;
    shim.setRoute(async (url, init) => {
        if (url === "/api/auth/me") return authBob();
        if (url === "/api/user/projects" && !init?.method) return globalThis.__jsonResponse(200, { projects: [meta(bobProject)], owner: "bob", serverTime: Date.now(), unit: "ms" });
        if (init?.method === "PUT") return globalThis.__jsonResponse(200, { ok: true, project: meta(JSON.parse(init.body).content) });
        return globalThis.__jsonResponse(500, { error: `stub 不认：${url}` });
    });
    const snap13 = await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 });
    equal("13 多条目也不询问（无弹窗字段）", snap13.importPrompt, undefined);
    equal("13 其他账号的项目一个都不进当前账号", useCanvasStore.getState().projects.map((item) => item.id), ["NB1"]);
    check("13 alice 留档被清理", (await lf.getItem(sync.FOREIGN_CACHE_PREFIX + "alice")) === null, "alice 留档还在");
    check("13 carol 留档被清理", (await lf.getItem(sync.FOREIGN_CACHE_PREFIX + "carol")) === null, "carol 留档还在");
    await sleep(1_200);
    equal("13 清理过程零 PUT", PUTS().length, 0, JSON.stringify(PUTS().map((call) => call.url)));
}

// ----------------------------------------------------------------------------------------
// 14. ⑥：DELETE 的 409 与 PUT 对齐 + 未来时间墓碑靠 recomputeDirty 出列
// ----------------------------------------------------------------------------------------
{
    const futureTomb = iso(Date.now() + 300_000); // 本机时钟快：墓碑时间在未来
    await seed({ state: { projects: [], deletedProjects: [{ id: "D1", deletedAt: futureTomb }], owner: "alice" }, version: 0 }, []);
    useCanvasStore.setState({ deletedProjects: [{ id: "D1", deletedAt: futureTomb }] });
    const serverTombMs = Date.now() + 600_000;
    let deleteHits = 0;
    shim.calls.length = 0;
    shim.setRoute(async (url, init) => {
        if (url === "/api/auth/me") return authAlice();
        if (url === "/api/user/projects" && !init?.method) return globalThis.__jsonResponse(200, { projects: [], owner: "alice", serverTime: Date.now(), unit: "ms" });
        if (url === "/api/user/projects/D1" && init?.method === "DELETE") {
            deleteHits += 1;
            return globalThis.__jsonResponse(409, { error: "版本冲突", conflict: true, server: { id: "D1", name: "", updatedAt: 0, deleted: true, deletedAt: serverTombMs, bytes: 0, version: serverTombMs } });
        }
        if (url === "/api/user/projects/D1") return globalThis.__jsonResponse(200, { project: { id: "D1", name: "", updatedAt: 0, deleted: true, deletedAt: serverTombMs, bytes: 0, version: serverTombMs } });
        if (init?.method === "PUT") return globalThis.__jsonResponse(200, { ok: true, project: meta(JSON.parse(init.body).content) });
        return globalThis.__jsonResponse(500, { error: `stub 不认：${url}` });
    });
    await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 });
    await sleep(1_400);
    const seq14 = shim.calls.map((call) => `${call.method} ${call.url}`);
    check("14 DELETE 409 后重拉了单项目（不是当永久失败）", seq14.includes("DELETE /api/user/projects/D1") && seq14.indexOf("GET /api/user/projects/D1") > seq14.indexOf("DELETE /api/user/projects/D1"), JSON.stringify(seq14));
    equal("14 只打一次 DELETE（不打死循环重试）", deleteHits, 1);
    equal("14 服务端墓碑更新 → 本地墓碑抬到服务端时间（LWW）", useCanvasStore.getState().deletedProjects[0]?.deletedAt, iso(serverTombMs));
    equal("14 未来时间墓碑靠 recomputeDirty 出列（dirty 清空）", sync.canvasSyncDiagnostics().dirty, []);

    // 14b：本地墓碑比服务端新 → 重试一次 DELETE（与 PUT 的「本地胜带 base 重试」对齐）
    const localTombMs = Date.now() + 900_000;
    await seed({ state: { projects: [], deletedProjects: [{ id: "D2", deletedAt: iso(localTombMs) }], owner: "alice" }, version: 0 }, []);
    useCanvasStore.setState({ deletedProjects: [{ id: "D2", deletedAt: iso(localTombMs) }] });
    let d2Hits = 0;
    shim.calls.length = 0;
    shim.setRoute(async (url, init) => {
        if (url === "/api/auth/me") return authAlice();
        if (url === "/api/user/projects" && !init?.method) return globalThis.__jsonResponse(200, { projects: [], owner: "alice", serverTime: Date.now(), unit: "ms" });
        if (url === "/api/user/projects/D2" && init?.method === "DELETE") {
            d2Hits += 1;
            if (d2Hits === 1) return globalThis.__jsonResponse(409, { error: "版本冲突", conflict: true, server: { id: "D2", name: "", updatedAt: 0, deleted: true, deletedAt: T0 - 1000, bytes: 0, version: T0 - 1000 } });
            return globalThis.__jsonResponse(200, { ok: true, project: { id: "D2", name: "", updatedAt: 0, deleted: true, deletedAt: localTombMs, bytes: 0, version: localTombMs } });
        }
        if (url === "/api/user/projects/D2") return globalThis.__jsonResponse(200, { project: { id: "D2", name: "", updatedAt: 0, deleted: true, deletedAt: T0 - 1000, bytes: 0, version: T0 - 1000 } });
        if (init?.method === "PUT") return globalThis.__jsonResponse(200, { ok: true, project: meta(JSON.parse(init.body).content) });
        return globalThis.__jsonResponse(500, { error: `stub 不认：${url}` });
    });
    await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 });
    await sleep(1_800);
    equal("14b 本地墓碑更新 → 重试一次 DELETE 后成功", [d2Hits, sync.canvasSyncDiagnostics().dirty.includes("D2")], [2, false]);
}

// ----------------------------------------------------------------------------------------
// 15. polish③：zustand merge 只认 store 认识的键（owner/dirtyIds/migratedAt 不得污染 store）
// ----------------------------------------------------------------------------------------
{
    shim.storage.clear();
    await lf.clear();
    const merged = project("M2", "merge 探针", T0 - 1_000, [{ id: "m2" }]);
    await lf.setItem(STORE_KEY, JSON.stringify({ state: { projects: [merged], deletedProjects: [], owner: "alice", dirtyIds: { M2: 1 }, migratedAt: iso(T0) }, version: 0 }));
    useCanvasStore.setState({ projects: [], deletedProjects: [] });
    await useCanvasStore.persist.rehydrate();
    const keys = Object.keys(useCanvasStore.getState());
    check("15 恢复时不把 owner/dirtyIds/migratedAt 灌进 store", !keys.includes("owner") && !keys.includes("dirtyIds") && !keys.includes("migratedAt"), JSON.stringify(keys));
    equal("15 数据本身照常恢复", useCanvasStore.getState().projects.map((item) => item.id), ["M2"]);
}

// ----------------------------------------------------------------------------------------
// 16. polish②：合并结果写盘失败 → 降级 offline + 可见错误（不假装 remote 正常、不发 PUT）
// ----------------------------------------------------------------------------------------
{
    const failProject = project("W1", "写盘失败", T0 - 1_000, [{ id: "w1" }]);
    await seed({ state: { projects: [failProject], deletedProjects: [], owner: "alice" }, version: 0 }, [failProject]);
    shim.calls.length = 0;
    shim.setRoute(async (url, init) => {
        if (url === "/api/auth/me") return authAlice();
        if (url === "/api/user/projects" && !init?.method) return globalThis.__jsonResponse(200, { projects: [], owner: "alice", serverTime: Date.now(), unit: "ms" });
        if (init?.method === "PUT") return globalThis.__jsonResponse(200, { ok: true, project: meta(JSON.parse(init.body).content) });
        return globalThis.__jsonResponse(500, { error: `stub 不认：${url}` });
    });
    const realSetItem = globalThis.localStorage.setItem;
    const realLfSet = lf.setItem;
    globalThis.localStorage.setItem = () => {
        throw new Error("QuotaExceededError");
    };
    lf.setItem = async () => {
        throw new Error("QuotaExceededError");
    };
    let downgraded = null;
    try {
        downgraded = await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 });
    } finally {
        globalThis.localStorage.setItem = realSetItem;
        lf.setItem = realLfSet;
    }
    equal("16 合并结果写盘失败 → 模式降级 offline", downgraded.mode, "offline");
    check("16 降级带可见错误（不静默）", /缓存写入失败/.test(downgraded.error), downgraded.error);
    await sleep(1_000);
    equal("16 降级后不发 PUT（不假装「正在同步」）", PUTS().length, 0);
    equal("16 内存里数据仍在（用户看得到）", useCanvasStore.getState().projects.map((item) => item.id), ["W1"]);
}

// ----------------------------------------------------------------------------------------
// 17. S3-B2.6 ①：恢复留档的**删键必须晚于落盘**——写盘失败时归档必须还在（下次启动还能恢复）
// ----------------------------------------------------------------------------------------
{
    const archived = project("P_ARCH", "登出留档里的项目", T0 - 2_000, [{ id: "arch-node" }]);
    const archiveRaw = JSON.stringify({ state: { projects: [archived], deletedProjects: [], owner: "alice", dirtyIds: { P_ARCH: T0 - 2_000 } }, version: 0 });
    const serverStub = async (url, init) => {
        if (url === "/api/auth/me") return authAlice();
        if (url === "/api/user/projects" && !init?.method) return globalThis.__jsonResponse(200, { projects: [], owner: "alice", serverTime: Date.now(), unit: "ms" });
        if (init?.method === "PUT") return globalThis.__jsonResponse(200, { ok: true, project: meta(JSON.parse(init.body).content) });
        return globalThis.__jsonResponse(500, { error: `stub 不认：${url}` });
    };

    // ---- 17a：合并结果写不进盘 → 归档**不得**被删（BL1 窄口：删键早于落盘 = 关页即永久丢）----
    await seed(null, []); // = prepareCanvasLogout 之后的状态：共享主键已清空
    await lf.setItem(sync.FOREIGN_CACHE_PREFIX + "alice", archiveRaw);
    shim.calls.length = 0;
    shim.setRoute(serverStub);
    const realSetItem = globalThis.localStorage.setItem;
    globalThis.localStorage.setItem = () => {
        throw new Error("QuotaExceededError");
    };
    let failSnap = null;
    try {
        failSnap = await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 });
    } finally {
        globalThis.localStorage.setItem = realSetItem;
    }
    equal("17a 写盘失败 → 降级 offline", failSnap.mode, "offline");
    check("17a 降级错误可见（不是静默）", /缓存写入失败/.test(failSnap.error), failSnap.error);
    const keptRaw = await lf.getItem(sync.FOREIGN_CACHE_PREFIX + "alice");
    check("17a 写盘失败后留档键仍在盘上（①的核心）", typeof keptRaw === "string" && keptRaw.includes("P_ARCH"), String(keptRaw).slice(0, 80));
    equal("17a 共享主键仍为空（没有半份落盘）", await lf.getItem(STORE_KEY), null);
    equal("17a 内存里已有恢复数据（用户看得到，但只靠内存）", useCanvasStore.getState().projects.map((item) => item.id), ["P_ARCH"]);

    // ---- 17b：对照——写盘成功后才删键（④ 的原意不能被 ① 丢掉）----
    await seed(null, []);
    await lf.setItem(sync.FOREIGN_CACHE_PREFIX + "alice", archiveRaw);
    shim.calls.length = 0;
    shim.setRoute(serverStub);
    const okSnap = await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 });
    equal("17b 同账号留档恢复 → 模式=remote", okSnap.mode, "remote");
    equal("17b 恢复的数据进了内存", useCanvasStore.getState().projects.map((item) => item.id), ["P_ARCH"]);
    check("17b 恢复结果已落盘（共享主键含 P_ARCH）", String(await lf.getItem(STORE_KEY)).includes("P_ARCH"), String(await lf.getItem(STORE_KEY)).slice(0, 80));
    equal("17b 落盘确认之后才删留档键", await lf.getItem(sync.FOREIGN_CACHE_PREFIX + "alice"), null);
    await sleep(600);
    equal("17b 恢复的项目被补推（dirty 从留档带出）", PUTS().map((call) => call.url), ["/api/user/projects/P_ARCH"]);
}

// ----------------------------------------------------------------------------------------
// 18. S3-B2.6 ②：离线态顶栏必须显示具体原因（enterOffline 的 message 不能只进内存）
// ----------------------------------------------------------------------------------------
{
    // 18a：清单读不到（断网/超时）→ offline 且 error 带具体原因
    const netProject = project("E1", "本地的", T0 - 1_000, [{ id: "e1" }]);
    await seed({ state: { projects: [netProject], deletedProjects: [], owner: "alice" }, version: 0 }, [netProject]);
    shim.calls.length = 0;
    shim.setRoute(async (url) => {
        if (url === "/api/auth/me") return authAlice();
        throw new TypeError("Failed to fetch");
    });
    const netSnap = await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 });
    equal("18a 清单读失败 → offline", netSnap.mode, "offline");
    check("18a offline 态带具体 error（顶栏渲染它的前提）", netSnap.error.length > 0 && /Failed to fetch|超时/.test(netSnap.error), netSnap.error);

    // 18b：清单 unit 不合约 → 走 enterOffline，错误文案必须落到同步快照上
    const unitProject = project("E2", "本地的", T0 - 1_000, [{ id: "e2" }]);
    await seed({ state: { projects: [unitProject], deletedProjects: [], owner: "alice" }, version: 0 }, [unitProject]);
    shim.calls.length = 0;
    shim.setRoute(async (url, init) => {
        if (url === "/api/auth/me") return authAlice();
        if (url === "/api/user/projects" && !init?.method) return globalThis.__jsonResponse(200, { projects: [], owner: "alice", serverTime: Date.now(), unit: "s" });
        return globalThis.__jsonResponse(500, { error: `stub 不认：${url}` });
    });
    const unitSnap = await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 });
    equal("18b unit 不合约 → offline", unitSnap.mode, "offline");
    check("18b error 是 unit 不合约的具体文案（不是通用「离线」）", /毫秒/.test(unitSnap.error) && /s/.test(unitSnap.error), unitSnap.error);

    // 18c：组件接线（源码断言：组件必须消费 error 并退到通用文案，且读 brokenArchiveCount）
    const statusSrc = readFileSync(path.join(root, "src/components/layout/canvas-project-sync-status.tsx"), "utf8");
    check("18c offline 分支渲染 error（有则显示）", /error \? t\("canvasSync\.offlineError"/.test(statusSrc), "缺少 offlineError 分支");
    check("18c 无 error 时退通用文案", /: t\("canvasSync\.offline"\)/.test(statusSrc), "缺少 canvasSync.offline 兜底");
    check("18c offline 横幅消费 brokenArchiveCount（③ 的可见口）", /state\.brokenArchiveCount/.test(statusSrc) && /canvasSync\.brokenArchive/.test(statusSrc), "缺少 brokenArchive 渲染");

    // 18d：i18n 双语文案都在（组件用的键不能只在一份 locale 里）
    for (const locale of ["zh-CN", "en-US"]) {
        const localeSrc = readFileSync(path.join(root, `src/i18n/locales/${locale}.ts`), "utf8");
        check(`18d ${locale} 定义 canvasSync.offlineError`, /\bofflineError:\s*"/.test(localeSrc), "缺 key");
        check(`18d ${locale} 定义 canvasSync.brokenArchive`, /\bbrokenArchive:\s*"/.test(localeSrc), "缺 key");
    }
}

// ----------------------------------------------------------------------------------------
// 19. 坏归档：本账号的「保留 + 可见」，别的账号的一律随跨账号清理删掉（不弹窗）
// ----------------------------------------------------------------------------------------
{
    const daveProject = project("D_GOOD", "dave 的正常归档", T0 - 2_000, [{ id: "d1" }]);
    const aliceOwn = project("E3", "本地的", T0 - 1_000, [{ id: "e3" }]);
    await seed({ state: { projects: [aliceOwn], deletedProjects: [], owner: "alice" }, version: 0 }, [aliceOwn]);
    // 三份归档：dave 正常 / mallory 坏（他账号）/ alice 坏（本账号，takeQuarantine 解析失败那条路）
    await lf.setItem(sync.FOREIGN_CACHE_PREFIX + "dave", JSON.stringify({ state: { projects: [daveProject], deletedProjects: [], owner: "dave" }, version: 0 }));
    await lf.setItem(sync.FOREIGN_CACHE_PREFIX + "mallory", "{ this is not json");
    await lf.setItem(sync.FOREIGN_CACHE_PREFIX + "alice", "[[[ broken");

    shim.calls.length = 0;
    shim.setRoute(async (url, init) => {
        if (url === "/api/auth/me") return authAlice();
        if (url === "/api/user/projects" && !init?.method) return globalThis.__jsonResponse(200, { projects: [], owner: "alice", serverTime: Date.now(), unit: "ms" });
        if (init?.method === "PUT") return globalThis.__jsonResponse(200, { ok: true, project: meta(JSON.parse(init.body).content) });
        return globalThis.__jsonResponse(500, { error: `stub 不认：${url}` });
    });
    const warnLines = [];
    const silentWarn = console.warn;
    console.warn = (...args) => warnLines.push(args.map((item) => String(item)).join(" "));
    let brokenSnap = null;
    try {
        brokenSnap = await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 });
    } finally {
        console.warn = silentWarn;
    }

    equal("19 坏归档不妨碍启动（照常 remote）", brokenSnap.mode, "remote");
    equal("19 无跨账号弹窗（机制已删除）", brokenSnap.importPrompt, undefined);
    check("19 别的账号的归档（含坏的）被清理", (await lf.getItem(sync.FOREIGN_CACHE_PREFIX + "dave")) === null && (await lf.getItem(sync.FOREIGN_CACHE_PREFIX + "mallory")) === null, "dave/mallory 还在");
    check("19 本账号的坏归档保留在盘上（保守策略：不删）", typeof (await lf.getItem(sync.FOREIGN_CACHE_PREFIX + "alice")) === "string", "alice 坏档被删了");
    equal("19 坏归档内容一个字节都没动", await lf.getItem(sync.FOREIGN_CACHE_PREFIX + "alice"), "[[[ broken");
    equal("19 brokenArchiveCount 只算清理后剩下的本账号坏档", brokenSnap.brokenArchiveCount, 1);
    check("19 console.warn 留痕（本账号 alice 的 takeQuarantine 路径）", warnLines.some((line) => line.includes("alice") && /无法解析/.test(line)), JSON.stringify(warnLines.slice(0, 4)));
    equal("19 别的账号数据不污染当前账号", useCanvasStore.getState().projects.map((item) => item.id), ["E3"]);
}

// ----------------------------------------------------------------------------------------
// 20. 横幅自愈：dirty 出列后「登录状态可能已过期」必须跟着撤（B2.8 回归）
//
// 线上现象：PUT 撞 401 → error=sessionMaybeExpired + dirty 保留（这是对的）；但用户重新登录后
// dirty 那一条成了孤儿（本地 projects/deletedProjects 都找不到这个 id）→ pushOne 出列、
// dirtyCount 归 0，而 error 只在「下一次推送成功」时才清 —— 再没有下一次了，横幅就永久挂着
// （canvas-project-sync-status.tsx 的显示条件是 `mode==="remote" && (error || dirtyCount>0 || …)`）。
// ----------------------------------------------------------------------------------------
{
    const orphan = project("ORPHAN1", "会被别处清掉的项目", T0 - 800, [{ id: "o1" }]);
    await seed({ state: { projects: [orphan], deletedProjects: [] }, version: 0 }, [orphan]);
    shim.calls.length = 0;
    shim.setRoute(async (url, init) => {
        if (url === "/api/auth/me") return authAlice();
        if (url === "/api/user/projects" && !init?.method) return globalThis.__jsonResponse(200, { projects: [], owner: "alice", serverTime: Date.now(), unit: "ms" });
        if (init?.method === "PUT") return globalThis.__jsonResponse(401, { error: "未登录" });
        return globalThis.__jsonResponse(500, { error: `stub 不认：${url}` });
    });
    await sync.bootstrapCanvasProjectSync({ deadline: Date.now() + 4_000 });
    await sleep(1_200); // 启动补推撞 401

    const after401 = sync.canvasProjectSyncSnapshot();
    check(
        "20 401 后横幅两行都在（error + dirtyCount>0，复现线上状态）",
        /过期/.test(after401.error) && after401.dirtyCount === 1,
        JSON.stringify({ error: after401.error, dirtyCount: after401.dirtyCount }),
    );

    // 本地被别处清空（换账号 / 项目在别的窗口被移除的等效）：projects 与 deletedProjects 都没有这个 id
    useCanvasStore.setState({ projects: [], deletedProjects: [] });
    await sync.pushOne("ORPHAN1"); // 孤儿出列路径：无项目、无墓碑 → 不推、只出列
    const afterDrain = sync.canvasProjectSyncSnapshot();
    equal("20 孤儿 dirty 出列后 dirtyCount=0", afterDrain.dirtyCount, 0);
    equal("20 孤儿出列后推送错误被撤掉（横幅文案不留残响）", afterDrain.error, "");
    check(
        "20 横幅显示条件不再成立（error || dirtyCount>0）",
        !(afterDrain.error || afterDrain.dirtyCount > 0),
        JSON.stringify({ error: afterDrain.error, dirtyCount: afterDrain.dirtyCount }),
    );
    // 落盘是 400ms 防抖：等这次 store 清空写进缓存后，dirtyIds 才是最终事实
    await sleep(700);
    equal("20 缓存载荷里的 dirtyIds 也清干净（下次启动不再重推）", Object.keys((await cacheState())?.dirtyIds || {}), []);
    equal("20 出列不是靠「假装推过」：全程零 PUT 成功", PUTS().length, 1);
}

console.warn = warn;
console.log(`=== S3-B2 画布项目同步探针：${passed} passed / ${failures.length} failed ===`);
for (const item of failures) console.log(`FAIL | ${item}`);
console.log(`EXIT_CODE=${failures.length ? 1 : 0}`);
process.exit(failures.length ? 1 : 0);
