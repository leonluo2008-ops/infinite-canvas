/**
 * AT-22 E3 前端探针（本地免费、不联网、不动真实任务）：**自检异常行内改描述**的接线与真渲染验收。
 *
 * 断言三组：
 *  [A] 起态门唯一口径：`hubDescEditGate` 对后端 `state.STATES` 里**每一个**状态都有明确答复
 *      （可改=awaiting_assets/awaiting_approval，其余一律不给入口 + 一句人话原因），
 *      且 409 文案点名的出口在详情页源码里真实存在（禁编造按钮）。
 *  [B] 接线（源合同）：自检每条 finding 的「改这一行的描述」→ HubTextEditor（cell）→
 *      postHubAsset({desc, expect_desc}) → 「保存并重渲染」；DescEditContext 行级互斥；
 *      新增文案零禁用词（路径 / `.md` / 磁盘 / 待确认清单 / 退回置换以外的内部词）。
 *  [C] 真渲染（esbuild + react-dom/server）：PromptSection 在 awaiting_approval 下渲染出
 *      「改这一行的描述」**可点**（无 disabled），在 approved 下**置灰**（有 disabled）；
 *      HubTextEditor 渲染出输入框 / 字数 / 取消 / 保存并重渲染。
 *
 * 跑法：cd web && node scripts/probe-at22-e3.mjs --states <states.json>
 * （states.json 由 automation/probe_at22_e3.py 从 `state.STATES` 直出，不是手抄。）
 */
import { createRequire } from "node:module";
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const WEB = process.cwd();
const CACHE = path.join(WEB, "node_modules", ".cache", "at22-e3");
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
const GATE_EDITABLE = ["awaiting_assets", "awaiting_approval"];
const gateOf = (s) => hubApi.hubDescEditGate(s);
const unknown = STATES.filter((s) => {
    const g = gateOf(s);
    return !g || typeof g.enabled !== "boolean" || (g.enabled ? !GATE_EDITABLE.includes(s) : !g.reason);
});
check("[A] 每个后端状态都有明确答复：可改=两态，其余给一句人话原因（无空答复）", unknown.length === 0,
    JSON.stringify(unknown.map((s) => [s, gateOf(s)])));
check("[A] 可改态恰是 awaiting_assets / awaiting_approval（不多不少）",
    STATES.filter((s) => gateOf(s).enabled).sort().join(",") === [...GATE_EDITABLE].sort().join(","),
    STATES.filter((s) => gateOf(s).enabled).join(","));
check("[A] 未知状态不默认放行（enabled=false + 带状态名的原因）",
    gateOf("weird_state").enabled === false && gateOf("weird_state").reason.includes("weird_state"),
    JSON.stringify(gateOf("weird_state")));

// 后端 409 文案里点名的出口，前端源码必须真有（禁编造按钮）
const hubApiSrc = read("src/pages/hub/hub-api.ts");
const pySrc = readFileSync(path.join(WEB, "..", "wanrepro", "ui", "hub_api.py"), "utf-8");
const drawerSrc = read("src/pages/hub/task-detail-drawer.tsx");
const refusalBlock = pySrc.slice(pySrc.indexOf("DESC_EDIT_REFUSALS"), pySrc.indexOf("def _desc_of"));
const named = [...new Set([...refusalBlock.matchAll(/「([^」]+)」/g)].map((m) => m[1]))];
const missing = named.filter((word) => !drawerSrc.includes(word));
check("[A] 后端拒绝文案点名的每个按钮/区域都在详情页源码里存在", named.length >= 3 && missing.length === 0,
    `点名 ${JSON.stringify(named)}，缺 ${JSON.stringify(missing)}`);
check("[A] 起态门是纯函数（同一个输入两次同答，不读全局/时间）",
    JSON.stringify(gateOf("awaiting_approval")) === JSON.stringify(gateOf("awaiting_approval")));

// ════════════════════════════════════════════════ [B] 接线（源合同）
check("[B] 「改这一行的描述」按钮不再是 disabled 占位（E3 已落地）",
    !/disabled data-hub-selfcheck-fix/.test(drawerSrc)
    && /onClick=\{\(\) => descApi\.setEditingRow\(editing \? null : rowNo\)\}/.test(drawerSrc));
check("[B] 保存走既有 postHubAsset 的 desc 支（不新开端点、不新开窗口）",
    /postHubAsset\(localId, \{ n, desc: next, expect_desc: expect \}\)/.test(drawerSrc)
    && /desc\?: string;/.test(hubApiSrc) && /expect_desc\?: string;/.test(hubApiSrc));
check("[B] 保存按钮文案逐字是「保存并重渲染」（自检指引对账的具名）",
    /saveLabel="保存并重渲染"/.test(drawerSrc) && drawerSrc.includes("保存并重渲染"));
check("[B] 自检指引里的两个具名都在详情页源码里（改这一行的描述 / 保存并重渲染）",
    drawerSrc.includes("改这一行的描述") && drawerSrc.includes("保存并重渲染"));
check("[B] 行级互斥：同一任务同时只开一个编辑块（PromptSection 持有 editingRow）",
    /const \[editingRow, setEditingRow\] = useState<number \| null>\(null\)/.test(drawerSrc)
    && /otherEditorOpen/.test(drawerSrc));
check("[B] 保存成功后按服务端回显的这一代自检决定收起/留场（不在前端重判）",
    /normalizeSelfCheck\(lastWrite\.current\?\.自检/.test(drawerSrc));
check("[B] 编辑块复用同一个承载（cell 变体），不是另写一个输入框",
    /variant="cell"/.test(drawerSrc) && /data-hub-text-editor=\{variant\}/.test(read("src/pages/hub/hub-text-editor.tsx")));

const editorSrc = read("src/pages/hub/hub-text-editor.tsx");
const newCopy = [drawerSrc, editorSrc].join("\n");
const BANNED = [
    [/待确认清单\.md|analysis\/reverse/, "磁盘路径"],
    [/\u78c1盘|落盘文件|本地文件路径/, "磁盘操作话术"],
    [/window\.confirm\(|window\.alert\(/, "阻塞式弹窗（会掐断自动化与键盘流）"],
    [/alert\(/, "alert"],
];
const bannedHit = BANNED.filter(([re]) => re.test(newCopy)).map(([, why]) => why);
check("[B] 新增文案零禁用词（无路径 / 无磁盘操作 / 无阻塞弹窗）", bannedHit.length === 0, JSON.stringify(bannedHit));
check("[B] 失败态不丢草稿且可原地重试（「再试一次」在状态条里）",
    /data-hub-desc-retry/.test(editorSrc) && /再试一次/.test(editorSrc));
check("[B] Esc 两段式放弃（第一次只提示，不弹阻塞框）",
    /data-hub-desc-esc-warn/.test(editorSrc) && /再按一次 Esc 放弃/.test(editorSrc));

// ════════════════════════════════════════════════ [C] 真渲染
const STUB = path.join(CACHE, "streamdown-stub.tsx");
writeFileSync(STUB,
    "export function Streamdown({ children }: { children?: unknown }) {\n" +
    "    return <pre data-stub-streamdown>{children as string}</pre>;\n" +
    "}\n");
const stubPlugin = {
    name: "at22e3-stubs",
    setup(build) {
        build.onResolve({ filter: /^streamdown$/ }, () => ({ path: STUB, namespace: "at22e3-stub" }));
        build.onLoad({ filter: /.*/, namespace: "at22e3-stub" }, () => ({
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
    'import { HubTextEditor } from "../../../src/pages/hub/hub-text-editor";\n' +
    "export function renderPrompt(detail: any): string {\n" +
    "    return renderToStaticMarkup(<PromptSection detail={detail} />);\n" +
    "}\n" +
    "export function renderEditor(props: any): string {\n" +
    "    return renderToStaticMarkup(<HubTextEditor {...props} />);\n" +
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

const ITEMS = [{
    项: "绑图行残留", 项显示名: "提示词里的老词", 结论: "FAIL", 证据: "第 1 行残留：细长圆柱体",
    说明: {
        结论人话: "第 1 行「细长圆柱体」的原物词还在提示词里（1 处在正文）",
        发现: [{
            类型: "残留", 文字: "第 1 行残留：细长圆柱体", 行号: 1, 图号: 1,
            行称呼: "细长圆柱体", 图称呼: "产品图", 图引用: "@图片1", 词: "细长圆柱体",
            句序: 1, 原文: "第 1 行残留：细长圆柱体", 可修: true,
            上下文: "0-2秒，中景，细长圆柱体紫色表面静置桌面。",
            片段: [{ 起: 8, 止: 13, 文: "细长圆柱体", 高亮: true }],
        }],
        处理: "在下面这条发现上点「改这一行的描述」，写完点「保存并重渲染」",
    },
}];
const DETAIL = (state) => ({
    local_id: "auto-9801", state,
    prompt_md: "## 画面提示词\n0-2秒，中景，细长圆柱体紫色表面静置桌面。",
    asset_cards: [{ n: 1, 类别: "主体", 描述: "细长圆柱体紫色表面", 我的描述: "", 已传图: [], 命名: "", 同实体疑似: [] }],
    自检: ITEMS,
});

const htmlApproval = ssr.renderPrompt(DETAIL("awaiting_approval"));
const htmlApproved = ssr.renderPrompt(DETAIL("approved"));
const buttonOf = (html) => {
    const at = html.indexOf("data-hub-selfcheck-fix");
    if (at < 0) return "";
    const start = html.lastIndexOf("<button", at);
    const end = html.indexOf(">", at);
    return start >= 0 && end > start ? html.slice(start, end + 1) : "";
};
const btnApproval = buttonOf(htmlApproval);
const btnApproved = buttonOf(htmlApproved);
check("[C] 待审批下每条 finding 上都挂着「改这一行的描述」", btnApproval !== "" && htmlApproval.includes("改这一行的描述"),
    btnApproval.slice(0, 200));
check("[C] 待审批下该按钮可点（无 disabled）", btnApproval !== "" && !btnApproval.includes("disabled"),
    btnApproval.slice(0, 200));
check("[C] 已批准下同一按钮置灰（不给编辑入口）", btnApproved !== "" && btnApproved.includes("disabled"),
    btnApproved.slice(0, 200));
check("[C] 初始态不预展开编辑器（点了才展开：SSR 里没有输入框与保存按钮）",
    !htmlApproval.includes("data-hub-desc-input") && !htmlApproval.includes("data-hub-desc-save"));

const htmlEditor = ssr.renderEditor({
    variant: "cell", value: "客户款 双层玻璃香水瓶", baseline: "客户款 双层玻璃香水瓶",
    placeholder: "留空＝沿用原物描述（原物词会被带回正文）",
    hint: "写完点「保存并重渲染」——提示词按新描述重出、自检自动重跑。",
    before: { label: "原物描述（只读）", text: "细长圆柱体紫色表面" },
    saveLabel: "保存并重渲染", maxLength: 500, softMax: 200,
    onSave: async () => ({ unchanged: false }), onCancel: () => undefined,
});
check("[C] HubTextEditor 渲染出输入框 + 只读原物描述", htmlEditor.includes("data-hub-desc-input")
    && htmlEditor.includes("原物描述（只读）") && htmlEditor.includes("细长圆柱体紫色表面"));
check("[C] HubTextEditor 带字数上限提示与两个按钮（取消 / 保存并重渲染）",
    htmlEditor.includes("data-hub-desc-counter") && htmlEditor.includes("/500")
    && htmlEditor.includes("data-hub-desc-cancel") && htmlEditor.includes("保存并重渲染"),
    htmlEditor.match(/data-hub-desc-counter[^<]*<\/span>/)?.[0] ?? "");
check("[C] 保存按钮带 data-hub-desc-save（探针/自动化可点）", htmlEditor.includes("data-hub-desc-save"));

const passed = pass;
console.log(`\n[probe-at22-e3.mjs] ${passed}/${passed + fails.length} PASS`);
if (fails.length) {
    console.log("FAIL: " + fails.join(" | "));
    process.exit(1);
}
process.exit(0);
