/**
 * AT-22 E4 前端探针（本地免费、不联网、不动真实任务）：**误报豁免白名单**的接线与真渲染验收。
 *
 * 断言三组：
 *  [A] 起态门唯一口径：`hubWhitelistGate` 对后端 `state.STATES` 里**每一个**状态都有明确答复
 *      （可改=只有 awaiting_approval，其余一律不给入口 + 一句人话原因），
 *      且后端拒绝文案点名的出口在详情页源码里真实存在（禁编造按钮）。
 *  [B] 接线（源合同）：自检 finding 上的「保留这个词」不再是 disabled 占位 → WhitelistContext.keep
 *      → `postHubSelfcheckWhitelist({行, 词})`；台账面板 `data-hub-whitelist` 带逐条撤销与全部撤销
 *      → `deleteHubSelfcheckWhitelist`；新增文案零禁用词（路径 / `.md` / 磁盘 / 阻塞式弹窗）。
 *  [C] 真渲染（esbuild + react-dom/server）：
 *      · awaiting_approval + FAIL：每个命中词上都挂着可点（无 disabled）的「保留这个词」；
 *      · awaiting_approval + PASS（带豁免台账）：渲染出「已按你的裁量保留」面板、逐条状态与撤销；
 *      · approved：同一个按钮置灰（已批准不给入口），不静默放行。
 *
 * 跑法：cd web && node scripts/probe-at22-e4.mjs --states <states.json>
 * （states.json 由 automation/probe_at22_e4.py 从 `state.STATES` 直出，不是手抄。）
 */
import { createRequire } from "node:module";
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const WEB = process.cwd();
const CACHE = path.join(WEB, "node_modules", ".cache", "at22-e4");
mkdirSync(CACHE, { recursive: true });
const read = (rel) => readFileSync(path.join(WEB, rel), "utf-8");

const argv = process.argv.slice(2);
const arg = (name) => {
    const at = argv.indexOf(name);
    return at >= 0 ? argv[at + 1] : "";
};
const STATES_PATH = arg("--states");
const STATES = STATES_PATH && existsSync(STATES_PATH) ? JSON.parse(readFileSync(STATES_PATH, "utf-8")) : [];

let pass = 0;
const fails = [];
function check(name, cond, detail = "") {
    if (cond) {
        pass += 1;
        console.log(`  [PASS] ${name}${detail ? ` —— ${String(detail).slice(0, 200)}` : ""}`);
    } else {
        fails.push(name);
        console.log(`  [FAIL] ${name}${detail ? ` —— ${String(detail).slice(0, 400)}` : ""}`);
    }
}

if (!globalThis.localStorage) {
    const store = new Map();
    globalThis.localStorage = {
        getItem: (key) => (store.has(key) ? store.get(key) : null),
        setItem: (key, value) => store.set(key, String(value)),
        removeItem: (key) => store.delete(key),
        clear: () => store.clear(),
    };
}
if (!globalThis.window) globalThis.window = globalThis;

// ════════════════════════════════════════════════ [A] 起态门
const apiBundle = path.join(CACHE, "hub-api.bundle.mjs");
const apiBuilt = await esbuild.build({
    entryPoints: [path.join(WEB, "src/pages/hub/hub-api.ts")],
    bundle: true, format: "esm", platform: "node", outfile: apiBundle, logLevel: "silent",
    define: { "import.meta.env": "{}" },
});
check("[A] esbuild 打包 hub-api.ts 成功", apiBuilt.errors.length === 0, apiBuilt.errors.map((e) => e.text).join("; "));
const hubApi = await import(pathToFileURL(apiBundle).href + `?t=${Date.now()}`);

check("[A] 后端状态表拿到了（--states 由 python 直出 state.STATES）", Array.isArray(STATES) && STATES.length >= 9,
    JSON.stringify(STATES));
check("[A] hubWhitelistGate 是导出的纯函数", typeof hubApi.hubWhitelistGate === "function");
const gateOf = (s) => hubApi.hubWhitelistGate(s);
const unknown = STATES.filter((s) => {
    const g = gateOf(s);
    return !g || typeof g.enabled !== "boolean" || (g.enabled ? s !== "awaiting_approval" : !g.reason);
});
check("[A] 每个后端状态都有明确答复：可改=awaiting_approval，其余给一句人话原因", unknown.length === 0,
    JSON.stringify(unknown.map((s) => [s, gateOf(s)])));
check("[A] 可改态恰是 awaiting_approval（不多不少）",
    STATES.filter((s) => gateOf(s).enabled).join(",") === "awaiting_approval",
    STATES.filter((s) => gateOf(s).enabled).join(","));
check("[A] 未知状态不默认放行（enabled=false + 带状态名的原因）",
    gateOf("weird_state").enabled === false && gateOf("weird_state").reason.includes("weird_state"),
    JSON.stringify(gateOf("weird_state")));
check("[A] 待置换区也不能改豁免（自检还没跑过）", gateOf("awaiting_assets").enabled === false,
    JSON.stringify(gateOf("awaiting_assets")));

// 后端 409 文案里点名的按钮/区域，前端源码必须真有（禁编造）
const hubApiSrc = read("src/pages/hub/hub-api.ts");
const pySrc = readFileSync(path.join(WEB, "..", "wanrepro", "ui", "hub_api.py"), "utf-8");
const drawerSrc = read("src/pages/hub/task-detail-drawer.tsx");
const refusalBlock = pySrc.slice(pySrc.indexOf("WL_REFUSALS"), pySrc.indexOf("def whitelist_rows"));
const named = [...new Set([...refusalBlock.matchAll(/「([^」]+)」/g)].map((m) => m[1]))];
const missing = named.filter((word) => !drawerSrc.includes(word));
check("[A] 后端拒绝文案点名的每个出口都在详情页源码里存在", named.length >= 2 && missing.length === 0,
    `点名 ${JSON.stringify(named)}，缺 ${JSON.stringify(missing)}`);

// ════════════════════════════════════════════════ [B] 接线（源合同）
check("[B] 「保留这个词」不再是 disabled 占位（E4 已落地）",
    !/disabled data-hub-selfcheck-keep/.test(drawerSrc)
    && /data-hub-selfcheck-keep=\{word\}/.test(drawerSrc));
check("[B] 一键按钮只发 API（走 WhitelistContext.keep，不在前端判该不该豁免）",
    /whitelist\.keep\(rows, word\)/.test(drawerSrc)
    && /postHubSelfcheckWhitelist\(localId, \{ 行: rows, 词: word \}\)/.test(drawerSrc));
check("[B] 撤销走同一路由的 DELETE 支（逐条 / 全部）",
    /deleteHubSelfcheckWhitelist\(localId, \{ 行: \[row\], 词: word \}\)/.test(drawerSrc)
    && /deleteHubSelfcheckWhitelist\(localId, \{ 全部: true \}\)/.test(drawerSrc));
check("[B] 台账面板存在：逐条状态 + 单条撤销 + 全部撤销（≥2 条才显示）",
    /data-hub-whitelist\b/.test(drawerSrc) && /data-hub-whitelist-row=/.test(drawerSrc)
    && /data-hub-whitelist-remove=/.test(drawerSrc) && /data-hub-whitelist-remove-all/.test(drawerSrc));
check("[B] 同一个词在多行命中时先问「哪几行」，存储仍是行级多条",
    /data-hub-selfcheck-keep-one/.test(drawerSrc) && /data-hub-selfcheck-keep-rows/.test(drawerSrc)
    && /whitelist\.keep\(rows, word\)/.test(hubApiSrc + drawerSrc));
check("[B] 台账优先取自检②的「豁免」，回落任务.json 的 selfcheck_whitelist",
    /residueItem\?\.豁免\?\.length/.test(drawerSrc) && /detail\.selfcheck_whitelist\?\.条目/.test(drawerSrc));
check("[B] 前端不重算条目状态（normalizeWhitelistEntries 只搬真值）",
    /export function normalizeWhitelistEntries/.test(hubApiSrc)
    && /if \(status\) entry\.状态 = status;/.test(hubApiSrc));

const newCopy = [drawerSrc, read("src/pages/hub/hub-api.ts")].join("\n");
const BANNED = [
    [/待确认清单\.md|analysis\/reverse/, "磁盘路径"],
    [/\u78c1盘|落盘文件|本地文件路径/, "磁盘操作话术"],
    [/window\.confirm\(|window\.alert\(/, "阻塞式弹窗（会掐断自动化与键盘流）"],
    [/\balert\(/, "alert"],
];
const bannedHit = BANNED.filter(([re]) => re.test(newCopy)).map(([why]) => why);
check("[B] 新增文案零禁用词（无路径 / 无磁盘操作 / 无阻塞弹窗）", bannedHit.length === 0, JSON.stringify(bannedHit));

// ════════════════════════════════════════════════ [C] 真渲染
const STUB = path.join(CACHE, "streamdown-stub.tsx");
writeFileSync(STUB,
    "export function Streamdown({ children }: { children?: unknown }) {\n" +
    "    return <pre data-stub-streamdown>{children as string}</pre>;\n" +
    "}\n");
const stubPlugin = {
    name: "at22e4-stubs",
    setup(build) {
        build.onResolve({ filter: /^streamdown$/ }, () => ({ path: STUB, namespace: "at22e4-stub" }));
        build.onLoad({ filter: /.*/, namespace: "at22e4-stub" }, () => ({
            contents: readFileSync(STUB, "utf-8"), loader: "tsx", resolveDir: WEB,
        }));
        build.onResolve({ filter: /^@\// }, (args) => {
            const base = path.join(WEB, "src", args.path.slice(2));
            for (const candidate of [base, `${base}.ts`, `${base}.tsx`, path.join(base, "index.ts"), path.join(base, "index.tsx")]) {
                try {
                    if (statSync(candidate).isFile()) return { path: candidate };
                } catch {
                    /* 试下一个后缀 */
                }
            }
            return { path: base };
        });
    },
};

const entry = path.join(CACHE, "ssr-entry.tsx");
writeFileSync(entry,
    'import { renderToStaticMarkup } from "react-dom/server";\n' +
    'import { PromptSection } from "../../../src/pages/hub/task-detail-drawer";\n' +
    "export function renderPrompt(detail: any): string {\n" +
    "    return renderToStaticMarkup(<PromptSection detail={detail} />);\n" +
    "}\n");
const ssrOut = path.join(CACHE, "ssr.mjs");
const ssrBuilt = await esbuild.build({
    entryPoints: [entry], bundle: true, platform: "node", format: "esm", outfile: ssrOut,
    jsx: "automatic", packages: "external", plugins: [stubPlugin],
    define: { "import.meta.env": "{}" }, logLevel: "silent",
});
check("[C] 真渲染打包成功（react/react-dom/antd 走宿主，仅 streamdown 打桩）",
    ssrBuilt.errors.length === 0, ssrBuilt.errors.map((e) => `${e.location?.file}:${e.text}`).join("; "));
const ssr = await import(pathToFileURL(ssrOut).href + `?t=${Date.now()}`);

const context = "0-2秒，中景，桌上放着香水瓶，镜头缓慢推近。";
const FAIL_ITEMS = [{
    项: "绑图行残留", 项显示名: "提示词里的老词", 结论: "FAIL", 证据: "第 6 行残留：香水瓶",
    说明: {
        结论人话: "提示词里还留着 2 个要换掉的原物词，分别在第 6、8 行",
        发现: [6, 8].map((row) => ({
            类型: "残留", 文字: `第 ${row} 行残留：香水瓶`, 行号: row, 图号: row === 6 ? 1 : 2,
            行称呼: "香水瓶", 图称呼: "产品图", 图引用: `@图片${row === 6 ? 1 : 2}`, 词: "香水瓶",
            句序: 1, 原文: `第 ${row} 行残留：香水瓶`, 可修: { 类型: "改描述" },
            上下文: context, 片段: [{ 起: 8, 止: 11, 文: "香水瓶", 高亮: true }],
        })),
        处理: "在下面这条发现上点「改这一行的描述」，写完点「保存并重渲染」",
    },
}];
const PASS_ITEMS = [
    // 与 E3 探针同一条真实边界：PASS 项只在「有其它项没通过」时进「N 项通过（展开看证据）」折叠区，
    // 所以夹具里必须同时有一条 FAIL（锁点），否则 PASS 项根本不渲染，测不到豁免标记。
    {
        项: "锁点对账", 项显示名: "该换的没换", 结论: "FAIL", 证据: "必换未换行 [12]",
        说明: {
            结论人话: "第 12 行该换的没换",
            处理: "点「退回置换」回到待置换区",
            发现: [{ 类型: "未换", 文字: "必换未换行 [12]", 行号: 12, 图号: null, 人话: "第 12 行还没换" }],
        },
    },
    {
    项: "绑图行残留", 项显示名: "提示词里的老词", 结论: "PASS",
    证据: "零残留｜已传图行词表 1 行（#6→图1(0词)），正文 20 字；已按你的裁量保留 1 个词：#6「香水瓶」",
    豁免: [
        { 行: 6, 词: "香水瓶", 词键: "香水瓶", 来源: "core", 图号: 1, 状态: "生效", 原因: "" },
        { 行: 8, 词: "香水瓶", 词键: "香水瓶", 来源: "core", 图号: 2, 状态: "失效-正文无此词",
          原因: "正文里已经没有这个词（改过描述后会失效）" },
    ],
    说明: {
        结论人话: "已传图的每一行，原物的名字和特征词都没残留在提示词正文里（其中 1 个词按你的裁量保留）",
        处理: "无需处理",
    },
}];
const DETAIL = (state, items) => ({
    local_id: "auto-9901", state,
    prompt_md: `## 画面提示词\n${context}`,
    asset_cards: [{ n: 6, 类别: "道具", 描述: "香水瓶", 我的描述: "", 已传图: [], 命名: "", 同实体疑似: [] }],
    自检: items,
    selfcheck_whitelist: { 版本: 1, 条目: [{ 行: 6, 词: "香水瓶", 词键: "香水瓶" }] },
});

const htmlFail = ssr.renderPrompt(DETAIL("awaiting_approval", FAIL_ITEMS));
const htmlApproved = ssr.renderPrompt(DETAIL("approved", FAIL_ITEMS));
const htmlPass = ssr.renderPrompt(DETAIL("awaiting_approval", PASS_ITEMS));

const tagOf = (html, marker) => {
    const at = html.indexOf(marker);
    if (at < 0) return "";
    const start = html.lastIndexOf("<button", at);
    const end = html.indexOf(">", at);
    return start >= 0 && end > start ? html.slice(start, end + 1) : "";
};
const keepTags = (html) => [...html.matchAll(/data-hub-selfcheck-keep="([^"]*)"/g)].map((m) => m[1]);
const keepTagOf = (html, word) => tagOf(html, `data-hub-selfcheck-keep="${word}"`);

check("[C] 待审批下每个命中词都挂着「保留这个词」（每个词一个按钮）",
    keepTags(htmlFail).length >= 1 && htmlFail.includes("保留这个词"), JSON.stringify(keepTags(htmlFail)));
check("[C] 待审批下该按钮可点（无 disabled）",
    keepTagOf(htmlFail, "香水瓶") !== "" && !keepTagOf(htmlFail, "香水瓶").includes("disabled"),
    keepTagOf(htmlFail, "香水瓶").slice(0, 200));
check("[C] 已批准下同一个按钮置灰（不给编辑入口）",
    keepTagOf(htmlApproved, "香水瓶") !== "" && keepTagOf(htmlApproved, "香水瓶").includes("disabled"),
    keepTagOf(htmlApproved, "香水瓶").slice(0, 200));
check("[C] 待审批下「改这一行的描述」仍在前（左主右次，豁免是次级路径）",
    htmlFail.indexOf("改这一行的描述") >= 0 && htmlFail.indexOf("改这一行的描述") < htmlFail.indexOf("保留这个词"));
check("[C] 点按钮不弹阻塞框（同词多行走自绘确认面板，初始不出）",
    !htmlFail.includes("data-hub-selfcheck-keep-one") && !/window\.confirm/.test(htmlFail));

check("[C] 带豁免台账的 PASS 项渲染出「已按你的裁量保留」面板",
    htmlPass.includes("data-hub-whitelist") && htmlPass.includes("已按你的裁量保留 2 个词"),
    htmlPass.match(/<summary[^>]*>[^<]*已按你的裁量保留[^<]*/)?.[0] ?? "");
check("[C] 台账逐条列出行号/词/状态（含失效原因）",
    htmlPass.includes("data-hub-whitelist-row=\"6:香水瓶\"")
    && htmlPass.includes("data-hub-whitelist-row=\"8:香水瓶\"")
    && htmlPass.includes("失效-正文无此词") && htmlPass.includes("正文里已经没有这个词"));
check("[C] 台账每条都有撤销按钮，且 ≥2 条时有「全部撤销」",
    htmlPass.includes("data-hub-whitelist-remove=\"6:香水瓶\"")
    && htmlPass.includes("data-hub-whitelist-remove=\"8:香水瓶\"")
    && htmlPass.includes("data-hub-whitelist-remove-all"));
check("[C] 待审批下撤销按钮可点（无 disabled）",
    !tagOf(htmlPass, "data-hub-whitelist-remove=\"6:香水瓶\"").includes("disabled"),
    tagOf(htmlPass, "data-hub-whitelist-remove=\"6:香水瓶\"").slice(0, 200));
check("[C] PASS 项额外说明「其中 N 个词是按你的裁量保留的」",
    htmlPass.includes("data-hub-selfcheck-exempt") && htmlPass.includes("按你的裁量保留"),
    htmlPass.match(/data-hub-selfcheck-exempt[^>]*>([^<]*)/)?.[1] ?? "");

const passed = pass;
console.log(`\n[probe-at22-e4.mjs] ${passed}/${passed + fails.length} PASS`);
if (fails.length) {
    console.log("FAIL: " + fails.join(" | "));
    process.exit(1);
}
process.exit(0);
