/**
 * S2.5 —— UI 门控收尾探针（免费：不连服务端、不花钱、不起浏览器）。
 *
 * 用 esbuild 打包**真源码**（session-role.ts / config-local-proxy.tsx / app-top-nav.tsx /
 * canvas-top-bar.tsx / agent-panel.tsx）到 node 里，shim 出最小 window/document，
 * 再 react-dom/server 渲染成 HTML 断言。所以下面断言的是真渲染结果，不是源码 grep。
 *   node scripts/probe-s2-b35-ui-gating.mjs
 * 任一断言不成立 → 退出码 1。
 *
 * 覆盖任务书 B1.1-B1.3 / 判据 V1-V6：
 *  1. `/api/auth/me` → 角色四态映射（admin / user / logged-out / single-user），
 *     以及探活失败保持 unknown（单人模式 UI 不因探活失败变化）
 *  2. Agent 入口三态：admin 可见 / 普通登录用户不渲染 / 未登录不渲染 / 单人模式与基线一致
 *     —— 顶栏按钮、画布工具栏按钮、Agent 面板三处各断言一次
 *  3. 本地代理 tab：remote 模式显示接管提示 + 开关 disabled + 命令/地址/测试按钮全部消失；
 *     local 模式与基线一致（无提示、开关可用、命令/地址/测试按钮在）
 *  4. 单人模式基线等价：角色 single-user 与 unknown 渲染结果逐字节相同（V4/V6 对照基线）
 *  5. i18n：zh-CN / en-US 均有 config.proxy.serverManaged
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
const workdir = mkdtempSync(path.join(tmpdir(), "probe-s2-b35-ui-"));

// ---- shim：必须先于目标模块求值（ESM import 按序求值）--------------------------------
writeFileSync(
    path.join(workdir, "shim.mjs"),
    `export const calls = { fetch: [] };
globalThis.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {}, clear: () => {}, key: () => null, get length() { return 0; } };
globalThis.window = globalThis;
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.location = { origin: "http://localhost:3000", href: "http://localhost:3000/" };
let route = async () => ({ ok: false, status: 404, json: async () => ({}) });
export function setRoute(next) { route = next; }
globalThis.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : String(input && input.url);
    calls.fetch.push({ url, credentials: init && init.credentials });
    return route(url, init);
};
globalThis.getComputedStyle = () => ({ tabSize: "4", getPropertyValue: () => "", getPropertyPriority: () => "", length: 0 });
globalThis.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 0);
globalThis.cancelAnimationFrame = (id) => clearTimeout(id);
globalThis.matchMedia = () => ({ matches: false, addListener: () => {}, removeListener: () => {}, addEventListener: () => {}, removeEventListener: () => {} });
const el = () => ({ setAttribute: () => {}, getAttribute: () => null, style: {}, addEventListener: () => {}, removeEventListener: () => {}, appendChild: () => {}, cloneNode() { return this; } });
globalThis.document = {
    createElement: el,
    createTextNode: () => ({ appendChild: () => {} }),
    head: { appendChild: () => {}, insertBefore: () => {}, querySelectorAll: () => [], replaceChild: () => {} },
    body: { appendChild: () => {}, insertBefore: () => {}, querySelectorAll: () => [], replaceChild: () => {}, removeChild: () => {} },
    documentElement: { style: {}, addEventListener: () => {}, removeEventListener: () => {}, appendChild: () => {} },
    querySelector: () => null,
    querySelectorAll: () => [],
    getElementsByTagName: () => [],
    addEventListener: () => {},
    removeEventListener: () => {},
};
`,
);

const rel = (p) => JSON.stringify(path.join(root, p).replace(/\\/g, "/"));
// zustand v5 的 useStore 在 SSR 下取 `api.getInitialState()`（创建期状态），探针改的是 getState()。
// 这里把裸 "zustand" 换成探针版：useStore 的 server snapshot 也读 getState()。
// 只影响探针 bundle，不动源码；zustand/react/shallow 等子路径保持真实实现。
const zustandDir = JSON.stringify(path.join(root, "node_modules/zustand").replace(/\\/g, "/"));
writeFileSync(
    path.join(workdir, "zustand-shim.mjs"),
    `import * as React from "react";
export * from ${zustandDir.slice(0, -1)}/index.js";
import { createStore as vanillaCreateStore } from ${zustandDir.slice(0, -1)}/vanilla.js";
export function useStore(api, selector = (s) => s) {
    return React.useSyncExternalStore(api.subscribe, () => selector(api.getState()), () => selector(api.getState()));
}
const createImpl = (createState) => {
    const api = vanillaCreateStore(createState);
    const useBoundStore = (selector) => useStore(api, selector);
    Object.assign(useBoundStore, api);
    return useBoundStore;
};
export const create = (createState) => (createState ? createImpl(createState) : createImpl);
`,
);
const entry = `
import { setRoute } from "./shim.mjs";
export { calls, setRoute } from "./shim.mjs";
export * as SSR from "react-dom/server";
export * as React from "react";
export * as RR from "react-router-dom";
export * as role from ${rel("src/services/session-role.ts")};
export * as store from ${rel("src/stores/use-config-store.ts")};
export * as agentStore from ${rel("src/stores/use-agent-store.ts")};
export * as proxy from ${rel("src/components/layout/config-local-proxy.tsx")};
export * as nav from ${rel("src/components/layout/app-top-nav.tsx")};
export * as topbar from ${rel("src/components/canvas/canvas-top-bar.tsx")};
export * as panel from ${rel("src/components/agent/agent-panel.tsx")};
`;
writeFileSync(path.join(workdir, "entry.tsx"), entry);

const built = await esbuild.build({
    entryPoints: [path.join(workdir, "entry.tsx")],
    bundle: true,
    format: "esm",
    platform: "browser",
    write: false,
    absWorkingDir: root,
    nodePaths: [path.join(root, "node_modules")],
    alias: {
        "@": path.join(root, "src"),
        // 只有设置弹窗被替换（会拖进 @codemirror 的 DOM 特性探测）；被测组件都在真源码里
        "@/components/layout/app-config-modal": path.join(root, "scripts/probe-stubs/app-config-modal.tsx"),
    },
    define: { "process.env.NODE_ENV": '"test"', "import.meta.env": "{}", __APP_VERSION__: '"0.0.0-probe"', __APP_RELEASES__: "[]" },
    loader: { ".tsx": "tsx", ".ts": "ts", ".css": "empty", ".svg": "empty" },
    logLevel: "silent",
    plugins: [
        {
            // 只精确替换裸 "zustand"（zustand/react/shallow 等子路径保持真实实现）
            name: "zustand-live-store",
            setup(build) {
                build.onResolve({ filter: /^zustand$/ }, () => ({ path: path.join(workdir, "zustand-shim.mjs") }));
            },
        },
        {
            name: "vite-raw",
            setup(build) {
                build.onResolve({ filter: /\?raw$/ }, (args) => ({ path: path.resolve(args.resolveDir, args.path.replace(/\?raw$/, "")), namespace: "raw" }));
                build.onLoad({ filter: /.*/, namespace: "raw" }, (args) => ({ contents: `export default ${JSON.stringify(readFileSync(args.path, "utf8"))};`, loader: "js" }));
            },
        },
    ],
});
const outFile = path.join(workdir, "bundle.mjs");
writeFileSync(outFile, built.outputFiles[0].text);

const bundle = await import(pathToFileURL(outFile).href);
const { SSR, React } = bundle;
const R = React.createElement;
const role = bundle.role;
const configStore = bundle.store.useConfigStore;
const agentStore = bundle.agentStore.useAgentStore;

// ---- 断言工具 -----------------------------------------------------------------------
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

// 模块 import 期已自发一次探活（404 → single-user）；先把它结算掉，再按用例逐条重置
await new Promise((resolve) => setTimeout(resolve, 10));

const ME_RESPONSES = {
    admin: async () => ({ ok: true, status: 200, json: async () => ({ username: "owner", expiresAt: 4102444800, is_admin: true }) }),
    user: async () => ({ ok: true, status: 200, json: async () => ({ username: "user1", expiresAt: 4102444800, is_admin: false }) }),
    "logged-out": async () => ({ ok: false, status: 401, json: async () => ({ error: "未登录" }) }),
    "single-user": async () => ({ ok: false, status: 404, json: async () => ({}) }),
};

async function applyRole(name) {
    role.resetSessionRole();
    if (name === "unknown") return;
    await role.probeSessionRole(ME_RESPONSES[name]);
}

function setChannelMode(mode) {
    configStore.setState({ config: { ...configStore.getState().config, channelMode: mode, proxyEnabled: true } });
}

const renderNav = () => SSR.renderToStaticMarkup(R(bundle.RR.MemoryRouter, null, R(bundle.nav.AppTopNav)));
const renderTopBar = () =>
    SSR.renderToStaticMarkup(
        R(bundle.topbar.CanvasTopBar, {
            title: "probe",
            titleDraft: "probe",
            isTitleEditing: false,
            onTitleDraftChange: () => {},
            onStartTitleEditing: () => {},
            onFinishTitleEditing: () => {},
            onCancelTitleEditing: () => {},
            canUndo: false,
            canRedo: false,
            onHome: () => {},
            onProjects: () => {},
            onCreateProject: () => {},
            onDeleteProject: () => {},
            onExportProject: () => {},
            onImportImage: () => {},
            onOpenPlugins: () => {},
            onUndo: () => {},
            onRedo: () => {},
            agentOpen: false,
            compactAgentStatus: { connected: true, enabled: true, activity: "probe" },
            onToggleAgent: () => {},
        }),
    );
const renderPanel = () => {
    agentStore.setState({ panelMounted: true, panelOpen: true });
    return SSR.renderToStaticMarkup(R(bundle.RR.MemoryRouter, null, R(bundle.panel.AgentPanel)));
};
const renderProxyTab = () => SSR.renderToStaticMarkup(R(bundle.proxy.ConfigLocalProxy));

// Agent 入口标记（真 DOM 结构，不是源码字符串）
const NAV_AGENT_BUTTON = /aria-label="(打开 Agent|收起 Agent)"/;
const TOPBAR_AGENT_BUTTON = /<span>Agent<\/span>/;
const PROXY_NOTICE = "已登录用户由服务器代理接管网络请求，无需本地代理。";

// === 1. /api/auth/me → 角色四态（纯映射）===============================================
equal("roleFromMe: is_admin=true → admin", role.roleFromMe(200, { username: "owner", is_admin: true }), "admin");
equal("roleFromMe: is_admin=false → user", role.roleFromMe(200, { username: "u", is_admin: false }), "user");
equal("roleFromMe: 缺 is_admin 字段 → user（不默认放行）", role.roleFromMe(200, { username: "u" }), "user");
equal("roleFromMe: 401 → logged-out", role.roleFromMe(401, { error: "未登录" }), "logged-out");
equal("roleFromMe: 404（MULTI_USER=0）→ single-user", role.roleFromMe(404, {}), "single-user");
check("roleFromMe: 2xx 但无 username 不算登录态", role.roleFromMe(200, {}) === "single-user");

// === 2. probeSessionRole：请求契约 + 快照写入 ==========================================
role.resetSessionRole();
bundle.setRoute(ME_RESPONSES.admin);
bundle.calls.fetch.length = 0;
let snapshot = await role.probeSessionRole();
const probedCall = bundle.calls.fetch[0];
check("探活请求打到同源 /api/auth/me", probedCall && probedCall.url === "/api/auth/me", JSON.stringify(probedCall));
equal("探活带 same-origin 凭据", probedCall && probedCall.credentials, "same-origin");
equal("admin 会话 → 快照 role=admin", snapshot.role, "admin");
equal("admin 会话 → 快照 username", snapshot.username, "owner");
equal("快照（模块级状态）与返回值一致", role.getSessionRoleSnapshot().role, "admin");

role.resetSessionRole();
check("重置后回到 unknown", role.getSessionRoleSnapshot().role === "unknown");
await new Promise((resolve) => setTimeout(resolve, 5));

role.resetSessionRole();
await role.probeSessionRole(ME_RESPONSES["logged-out"]);
equal("401 会话 → logged-out", role.getSessionRoleSnapshot().role, "logged-out");

role.resetSessionRole();
await role.probeSessionRole(ME_RESPONSES["single-user"]);
equal("404 → single-user", role.getSessionRoleSnapshot().role, "single-user");

role.resetSessionRole();
await role.probeSessionRole(async () => {
    throw new Error("network down");
});
equal("探活异常 → 保持 unknown（单人模式 UI 不因探活失败变化）", role.getSessionRoleSnapshot().role, "unknown");

// 请求去重：并行调用只发一次 /api/auth/me
role.resetSessionRole();
bundle.calls.fetch.length = 0;
bundle.setRoute(ME_RESPONSES.admin);
await Promise.all([role.probeSessionRole(), role.probeSessionRole(), role.probeSessionRole()]);
equal("并发探活只发一次请求", bundle.calls.fetch.length, 1);
bundle.setRoute(ME_RESPONSES["single-user"]);

// === 3. 门控纯函数表 ==================================================================
const agentTable = [
    ["admin", true, true],
    ["admin", false, true],
    ["user", true, false],
    ["user", false, false],
    ["logged-out", true, false],
    ["logged-out", false, false],
    ["single-user", false, true],
    ["unknown", false, true],
    ["unknown", true, false],
];
for (const [r, remote, expected] of agentTable) {
    equal(`agentEntryVisible(${r}, remote=${remote})`, role.agentEntryVisible(r, remote), expected);
}
const proxyTable = [
    [true, "unknown", true],
    [true, "single-user", true],
    [true, "user", true],
    [true, "admin", true],
    [false, "unknown", false],
    [false, "single-user", false],
    [false, "user", true],
    [false, "admin", true],
];
for (const [remote, r, expected] of proxyTable) {
    equal(`localProxyLocked(remote=${remote}, ${r})`, role.localProxyLocked(remote, r), expected);
}

// === 4. Agent 入口三态（顶栏按钮 / 画布工具栏按钮 / 面板 —— 真渲染）====================
await applyRole("admin");
setChannelMode("remote");
const navAdmin = renderNav();
const topBarAdmin = renderTopBar();
const panelAdmin = renderPanel();
check("V1 顶栏：admin 登录可见 Agent 按钮", NAV_AGENT_BUTTON.test(navAdmin), navAdmin.slice(0, 200));
check("V1 画布工具栏：admin 登录可见 Agent 按钮", TOPBAR_AGENT_BUTTON.test(topBarAdmin), topBarAdmin.slice(0, 200));
check("V1 面板：admin 登录渲染出 Agent 面板", panelAdmin.length > 1000, `len=${panelAdmin.length}`);

await applyRole("user");
setChannelMode("remote");
const navUser = renderNav();
const topBarUser = renderTopBar();
const panelUser = renderPanel();
check("V2 顶栏：普通登录用户 DOM 无 Agent 按钮", !NAV_AGENT_BUTTON.test(navUser) && !/lucide-bot/.test(navUser), `len=${navUser.length}`);
check("V2 画布工具栏：普通登录用户 DOM 无 Agent 按钮", !TOPBAR_AGENT_BUTTON.test(topBarUser), `len=${topBarUser.length}`);
check("V2 面板：普通登录用户不渲染 Agent 面板", panelUser === "", `len=${panelUser.length}`);
check("V2 对照：admin 与普通用户的顶栏确实不同（不是断言空转）", navAdmin !== navUser);

await applyRole("logged-out");
setChannelMode("local");
check("V3 未登录：顶栏无 Agent 按钮", !NAV_AGENT_BUTTON.test(renderNav()));
check("V3 未登录：画布工具栏无 Agent 按钮", !TOPBAR_AGENT_BUTTON.test(renderTopBar()));
check("V3 未登录：面板不渲染", renderPanel() === "");

await applyRole("single-user");
setChannelMode("local");
const navSingle = renderNav();
const topBarSingle = renderTopBar();
const panelSingle = renderPanel();
check("V4 单人模式：顶栏 Agent 按钮仍在（基线）", NAV_AGENT_BUTTON.test(navSingle));
check("V4 单人模式：画布工具栏 Agent 按钮仍在（基线）", TOPBAR_AGENT_BUTTON.test(topBarSingle));
check("V4 单人模式：面板照常渲染（基线）", panelSingle.length > 1000);
check("V4 对照：单人模式顶栏与 admin 顶栏结构一致（同含按钮）", NAV_AGENT_BUTTON.test(navSingle) && NAV_AGENT_BUTTON.test(navAdmin));

// 单人模式「逐字节」等价：角色 unknown（探活未完成/失败）不得改变渲染结果
await applyRole("unknown");
setChannelMode("local");
check("V4 基线等价：single-user 与 unknown 的顶栏 HTML 逐字节相同", renderNav() === navSingle);
check("V4 基线等价：single-user 与 unknown 的画布工具栏 HTML 逐字节相同", renderTopBar() === topBarSingle);
check("V4 基线等价：single-user 与 unknown 的面板 HTML 逐字节相同", renderPanel() === panelSingle);

// === 5. 本地代理 tab 门控 =============================================================
await applyRole("user");
setChannelMode("remote");
const proxyRemote = renderProxyTab();
check("V5 remote：接管提示文案存在", proxyRemote.includes(PROXY_NOTICE), proxyRemote.slice(0, 300));
check("V5 remote：开关渲染为 disabled", /ant-switch-disabled/.test(proxyRemote), (proxyRemote.match(/class="[^"]*ant-switch[^"]*"/) || [])[0]);
check("V5 remote：开关不是 checked 可操作态", /role="switch"[^>]*aria-checked="false"/.test(proxyRemote));
check("V5 remote：启动命令不再出现（整块配置隐藏）", !proxyRemote.includes("npx"), proxyRemote.slice(0, 300));
check("V5 remote：代理地址输入框不再出现", !/ant-input/.test(proxyRemote));
check("V5 remote：测试连接按钮不再出现", !proxyRemote.includes("测试连接"));
check("V5 remote：带 data-remote-locked 标记（可被 DOM 探针定位）", proxyRemote.includes('data-remote-locked="1"'));

await applyRole("admin");
setChannelMode("remote");
check("V5 admin（同属 remote）：本地代理 tab 同样锁定", renderProxyTab().includes(PROXY_NOTICE));

// channelMode 单独驱动（不靠 role 兜底）：角色还没探活出来时，remote 也必须锁定
await applyRole("unknown");
setChannelMode("remote");
check("V5 remote 由 channelMode 驱动（role 未知也锁定）", renderProxyTab().includes(PROXY_NOTICE));
check("V2 remote + role 未知：Agent 入口先隐藏（避免先露后藏）", !NAV_AGENT_BUTTON.test(renderNav()));

await applyRole("single-user");
setChannelMode("local");
const proxySingle = renderProxyTab();
check("V6 local：无接管提示", !proxySingle.includes(PROXY_NOTICE));
check("V6 local：开关可用（非 disabled）", !/ant-switch-disabled/.test(proxySingle));
check("V6 local：启动命令仍在（开关打开态）", proxySingle.includes("npx"));
check("V6 local：代理地址输入框仍在", /ant-input/.test(proxySingle));
check("V6 local：无 data-remote-locked 标记", !proxySingle.includes("data-remote-locked"));

await applyRole("unknown");
setChannelMode("local");
check("V6 基线等价：single-user 与 unknown 的本地代理 tab HTML 逐字节相同", renderProxyTab() === proxySingle);

// === 6. i18n 文案 =====================================================================
for (const [name, file] of [
    ["zh-CN", "src/i18n/locales/zh-CN.ts"],
    ["en-US", "src/i18n/locales/en-US.ts"],
]) {
    const text = readFileSync(path.join(root, file), "utf8");
    check(`i18n ${name}：有 config.proxy.serverManaged`, /serverManaged:\s*"/.test(text), file);
}
const zhText = readFileSync(path.join(root, "src/i18n/locales/zh-CN.ts"), "utf8");
const enText = readFileSync(path.join(root, "src/i18n/locales/en-US.ts"), "utf8");
check("i18n zh-CN：文案说明「服务器代理接管」", zhText.includes(PROXY_NOTICE));
check("i18n en-US：文案说明 server proxy", /serverManaged:\s*"Logged-in users are proxied by the server/.test(enText));

// ---- 汇总 ---------------------------------------------------------------------------
console.log(`SUMMARY: PASS ${passed}/${passed + failures.length}`);
for (const item of failures) console.log(`FAIL | ${item}`);
process.exit(failures.length ? 1 : 0);
