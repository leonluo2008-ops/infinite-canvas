/**
 * AT-16 B2 前端探针（本地免费，不联网、不动真实任务）：**自检人话化透传 + 提示词区布局**。
 *
 * 两件事各一组断言：
 *  [A] 读层透传：normalizeSelfCheck（hub-api.ts）把生产者「说明」整块搬过来，缺/畸形 → undefined
 *      （旧结构降级），且**不从「证据」文字派生任何东西**（红线 25：前端不解析证据重判）。
 *  [B] 真渲染：drawer 的 PromptSection 逐字抽出编译（streamdown 打桩，其余走真代码）真渲染 →
 *      · 标题行只放标题 + 操作按钮，自检/门禁详情不在标题行里（结构上不互挤：head < 自检 < 正文）
 *      · 正文默认展开（data-expanded="1"，正文文字在 DOM 里）
 *      · 自检/门禁区域是整宽块级流（w-full，无 absolute/fixed、无旧 w-[min(560px,80vw)] 弹层）
 *      · 新结构（有说明）出「怎么处理」，行号/图号直接来自检查器发现（#12 / 图1）
 *      · 旧结构（auto-0042 真实 自检.json）降级：只有原始证据 + 明说没有处理指引，不假装有指引
 *      · --selfcheck / --legacy 参数：喂**真实生产者产出**与**真实历史文件**，不是手搓形状。
 *
 * 跑法：cd web && node scripts/probe-at16-b2.mjs [--selfcheck <自检.json>] [--legacy <自检.json>]
 * 判据全对才 exit 0；任何一步 FAIL 如实红。
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const WEB = process.cwd();
const CACHE = path.join(WEB, "node_modules", ".cache", "at16-b2");
mkdirSync(CACHE, { recursive: true });
const read = (rel) => readFileSync(path.join(WEB, rel), "utf-8");

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

// ════════════════════════════════════════════════════════════ [A] 读层透传
const apiBundle = path.join(CACHE, "hub-api.bundle.mjs");
const apiBuilt = await esbuild.build({
    entryPoints: [path.join(WEB, "src/pages/hub/hub-api.ts")],
    bundle: true, format: "esm", platform: "node", outfile: apiBundle, logLevel: "silent",
    define: { "import.meta.env": "{}" },
});
check("[A] esbuild 打包 hub-api.ts 成功", apiBuilt.errors.length === 0, apiBuilt.errors.map((e) => e.text).join("; "));
// AT-16 B5a-2：hub-api 参与 UI 模块图（快捷入口复用 gen-settings 的准入校验 → store），
// 纯 node 里 import 前必须先补最小浏览器全局（原先这段在 SSR 打包之后，早于 import 就晚了）。
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
const hubApi = await import(pathToFileURL(apiBundle).href + `?t=${Date.now()}`);

console.log("1) normalizeSelfCheck 透传（说明字段 / 旧结构降级 / 畸形兜底）");
const 说明样本 = {
    结论人话: "发的图和提交的图没对上：2 处——提交的图数和该发的图数不一致",
    原因: "提交快照 3 条 ≠ 发图单位 2 个",
    处理: "点「退回置换」回到待置换区：对点到的行在上传区「重传（覆盖）」重新绑图",
    发现: [{ 文字: "提交第 3 张来自行 12", 行号: 12, 图号: 1 }, { 文字: "只有数字串的行号也收", 行号: "7" }],
};
const kept = hubApi.normalizeSelfCheck([{ 项: "次序对账", 结论: "FAIL", 证据: "原始证据", 说明: 说明样本 }]);
check("[A] 说明整块透传（结论人话/原因/处理都在）",
    kept?.[0]?.说明?.结论人话 === 说明样本.结论人话
    && kept?.[0]?.说明?.原因 === 说明样本.原因
    && kept?.[0]?.说明?.处理 === 说明样本.处理,
    JSON.stringify(kept?.[0]?.说明 ?? null).slice(0, 240));
check("[A] 发现结构化透传：行号/图号是数字、缺的不编（null）",
    kept?.[0]?.说明?.发现?.length === 2
    && kept[0].说明.发现[0].行号 === 12 && kept[0].说明.发现[0].图号 === 1
    && kept[0].说明.发现[1].行号 === 7 && kept[0].说明.发现[1].图号 === null,
    JSON.stringify(kept?.[0]?.说明?.发现 ?? null));

const legacyNorm = hubApi.normalizeSelfCheck([
    { 项: "次序对账", 结论: "FAIL", 证据: "提交快照 3 条 ≠ 发图单位 2 个｜行→图号={1: 1}" },
]);
check("[A] 旧结构（无说明）→ 说明缺省，证据原样保留（降级由展示层做）",
    legacyNorm?.[0]?.说明 === undefined && legacyNorm[0].证据.startsWith("提交快照 3 条"),
    JSON.stringify(legacyNorm?.[0] ?? null).slice(0, 200));
check("[A] 不从证据文字派生说明（红线 25：读层不解析证据重判）",
    legacyNorm?.[0]?.说明 === undefined && !("说明" in legacyNorm[0]));

const junkNorm = hubApi.normalizeSelfCheck([
    { 项: "甲", 结论: "FAIL", 证据: "e", 说明: "这是个字符串不是对象" },
    { 项: "乙", 结论: "FAIL", 证据: "e", 说明: {} },
    { 项: "丙", 结论: "FAIL", 证据: "e", 说明: { 发现: [{ 文字: "", 行号: "x" }, "junk"] } },
]);
check("[A] 畸形说明（字符串/空对象/垃圾发现）→ 一律 undefined，不炸页面也不摆假指引",
    junkNorm?.every((row) => row.说明 === undefined) === true,
    JSON.stringify(junkNorm?.map((row) => row.说明) ?? null));

const partialNorm = hubApi.normalizeSelfCheck([
    { 项: "丁", 结论: "FAIL", 证据: "e", 说明: { 处理: "点「重试」" } },
]);
check("[A] 只有部分字段的说明也保留（有真字段就算有）",
    partialNorm?.[0]?.说明?.处理 === "点「重试」" && partialNorm[0].说明.结论人话 === ""
    && partialNorm[0].说明.发现.length === 0,
    JSON.stringify(partialNorm?.[0]?.说明 ?? null));

check("[A] 无文件/空数组 → null（「未检查」不变）",
    hubApi.normalizeSelfCheck(null) === null && hubApi.normalizeSelfCheck([]) === null);

// ════════════════════════════════════════════════════════════ [B] 真渲染
console.log("2) drawer PromptSection 真渲染（streamdown 打桩，其余真代码）");
const STUB = path.join(CACHE, "streamdown-stub.tsx");
writeFileSync(STUB,
    "export function Streamdown({ children }: { children?: unknown }) {\n" +
    "    return <pre data-stub-streamdown>{children as string}</pre>;\n" +
    "}\n");
const stubPlugin = {
    name: "at16b2-stubs",
    setup(build) {
        build.onResolve({ filter: /^streamdown$/ }, () => ({ path: STUB, namespace: "at16b2-stub" }));
        build.onLoad({ filter: /.*/, namespace: "at16b2-stub" }, () => ({ contents: readFileSync(STUB, "utf-8"), loader: "tsx", resolveDir: WEB }));
        build.onResolve({ filter: /^@\// }, (args) => {
            const base = path.join(WEB, "src", args.path.slice(2));
            for (const candidate of [base, `${base}.ts`, `${base}.tsx`, path.join(base, "index.ts"), path.join(base, "index.tsx")]) {
                try {
                    if (require("node:fs").statSync(candidate).isFile()) return { path: candidate };
                } catch {
                    /* 继续试下一个后缀 */
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
check("[B] PromptSection 真渲染打包成功（react/react-dom/antd 走宿主机，仅 streamdown 打桩）",
    ssrBuilt.errors.length === 0, ssrBuilt.errors.map((e) => `${e.location?.file}:${e.text}`).join("; "));
// 纯 node 里跑真前端模块：补最小的浏览器全局（i18n 在 import 期读 localStorage；antd 走 SSR 不需要真 DOM）
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
const ssr = await import(pathToFileURL(ssrOut).href + `?t=${Date.now()}`);
const PROMPT = "## 画面提示词\n@图片1 手机主体特写；@图片2 香水静物。";
const 新说明 = {
    项: "次序对账",
    结论: "FAIL",
    证据: "提交快照 3 条 ≠ 发图单位 2 个｜行→图号={1: 1, 12: 2}；正文@图片N=[1, 2]；提交次序=[1, 2, 12]",
    说明: {
        结论人话: "发的图和提交的图没对上：2 处——提交的图数和该发的图数不一致、提交次序和行对不上",
        原因: "提交快照 3 条 ≠ 发图单位 2 个；提交第 3 张来自行 12，但发号映射给该行的是图1（应为图3）",
        处理: "点「退回置换」回到待置换区：对点到的行在上传区「重传（覆盖）」重新绑图；不要这张图就点「移除」按原片处理",
        发现: [
            { 行号: null, 图号: null, 文字: "提交快照 3 条 ≠ 发图单位 2 个（组内多行应只提交一条）" },
            { 行号: 12, 图号: 1, 文字: "提交第 3 张来自行 12，但发号映射给该行的是图1（应为图3）" },
        ],
    },
};
const 门禁说明 = {
    项: "拆解门禁",
    结论: "FAIL",
    证据: "状态 标红｜绿项 6/8｜缺项 2｜告警 0｜重拆 2/2\n❌ D2 分镜表缺片尾覆盖\n❌ D5 每行缺画面位置",
    说明: {
        结论人话: "拆解产物的机械门禁没通过：绿项 6/8、缺项 2、告警 0（重拆 2/2）",
        原因: "缺项：D2 分镜表缺片尾覆盖；缺项：D5 每行缺画面位置",
        处理: "点详情底部「重试」重跑拆解与渲染（门禁按重拆结果重算）；重拆轮次用尽仍缺项 → 按缺项补齐素材/意图后重跑",
        发现: [{ 行号: null, 图号: null, 文字: "缺项：D2 分镜表缺片尾覆盖" }, { 行号: null, 图号: null, 文字: "缺项：D5 每行缺画面位置" }],
    },
};
const detailNew = {
    local_id: "auto-9701",
    prompt_md: PROMPT,
    自检: [
        新说明,
        { 项: "绑图行残留", 结论: "PASS", 证据: "零残留｜已传图行词表 2 行", 说明: { 结论人话: "已传图的每一行都没残留原物词", 原因: "", 处理: "无需处理", 发现: [] } },
        门禁说明,
    ],
};
const htmlNew = ssr.renderPrompt(detailNew);

/** 取带某标记的元素（div/details/section 皆可）：从标记处回找起始标签，按同名标签配平取整段。 */
function elementByMarker(html, marker) {
    const at = html.indexOf(marker);
    if (at < 0) return "";
    let start = -1;
    let tag = "";
    for (const name of ["div", "section", "details", "span", "ul", "li"]) {
        const from = html.lastIndexOf(`<${name}`, at);
        if (from >= 0 && (start < 0 || from > start)) {
            start = from;
            tag = name;
        }
    }
    if (start < 0) return "";
    const close = `</${tag}>`;
    let depth = 0;
    let i = start;
    while (i < html.length) {
        if (html.startsWith(`<${tag}`, i)) depth += 1;
        else if (html.startsWith(close, i)) {
            depth -= 1;
            if (depth === 0) return html.slice(start, i + close.length);
        }
        i += 1;
    }
    return html.slice(start);
}
const classOf = (html, marker) => {
    const seg = html.slice(Math.max(0, html.indexOf(marker) - 400), html.indexOf(marker));
    const m = /class="([^"]*)"/g;
    let last = "";
    let hit;
    while ((hit = m.exec(seg))) last = hit[1];
    return last;
};

console.log("3) 布局：标题一行 / 正文默认展开 / 自检异常独立整宽");
const head = elementByMarker(htmlNew, "data-hub-prompt-head");
check("[B] 标题行存在且只有「提示词」+ 操作按钮（一行收住）",
    head.includes("提示词") && head.includes("data-hub-prompt-toggle") && head.includes("收起正文"),
    head.slice(0, 220));
check("[B] 自检/门禁详情**不在**标题行里（不嵌标题行 flex，结构上不互挤）",
    !head.includes("data-hub-selfcheck") && !head.includes("data-hub-gate"),
    head.slice(0, 220));

const bodyStart = htmlNew.indexOf("data-hub-prompt-body");
const selfcheckAt = htmlNew.indexOf("data-hub-selfcheck");
const gateAt = htmlNew.indexOf("data-hub-gate");
check("[B] 三段顺序为 标题 → 自检/门禁 → 正文（正文在整宽区之后，边界不重叠）",
    htmlNew.indexOf("data-hub-prompt-head") < selfcheckAt && selfcheckAt < gateAt && gateAt < bodyStart,
    `head=${htmlNew.indexOf("data-hub-prompt-head")} selfcheck=${selfcheckAt} gate=${gateAt} body=${bodyStart}`);
check("[B] 正文默认展开（data-expanded=1 且正文文字真在 DOM 里、不在 <details> 折叠壳里）",
    bodyStart > 0 && htmlNew.includes('data-expanded="1"') && htmlNew.includes("@图片1 手机主体特写"),
    htmlNew.slice(bodyStart - 60, bodyStart + 220));
check("[B] 标题不再写「默认折叠」文案（正文默认可见，收起才要操作）",
    !htmlNew.includes("默认折叠"));

const selfcheckEl = elementByMarker(htmlNew, "data-hub-selfcheck-detail");
check("[B] 自检异常详情是整宽块（w-full、普通流：无 absolute/fixed、无旧 560px 弹层宽度）",
    selfcheckEl.includes("w-full")
    && !/absolute|fixed|w-\[min\(560px/.test(selfcheckEl),
    selfcheckEl.slice(0, 240));
check("[B] 自检面板外层同样整宽，不靠固定宽度挤在标题旁",
    /data-hub-selfcheck(?![-\w])/.test(htmlNew)
    && classOf(htmlNew, "data-hub-selfcheck>").includes("w-full"),
    classOf(htmlNew, "data-hub-selfcheck>"));
check("[B] 旧的小弹层宽度 w-[min(560px,80vw)] 在提示词段彻底消失",
    !htmlNew.includes("w-[min(560px")
    && !read("src/pages/hub/task-detail-drawer.tsx").includes("w-[min(560px"),
    "drawer 源码仍含固定弹层宽度");

console.log("4) 用户话：结论 + 原因 + 怎么处理 + 检查器自己的行号/图号");
const howEl = elementByMarker(htmlNew, "data-hub-selfcheck-how");
check("[B] FAIL 项出「怎么处理」（文案=生产者处理指引原文）",
    howEl.includes("怎么处理：") && howEl.includes("重传（覆盖）") && howEl.includes("按原片处理"),
    howEl.slice(0, 260));
const textOf = (html) => html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ");
// AT-22 E2 改判：可见区不再有「原因」段（它与发现/证据 100% 重复，是用户看不懂的主因）
check("[B] 可见区不再渲染「原因」段（E2 拔掉最大重复源）",
    elementByMarker(htmlNew, "data-hub-selfcheck-reason") === ""
    && !textOf(elementByMarker(htmlNew, "data-hub-selfcheck-item")
        .replace(elementByMarker(htmlNew, "data-hub-selfcheck-fold"), " ")).includes("提交快照 3 条 ≠ 发图单位 2 个"));
// AT-22 E2 改判：无 v2 槽位（旧/中态载荷）不出「发现逐条」列表——同样内容在折叠区证据里
check("[B] 旧/中态载荷不出发现列表也不丢信息（同一内容收进折叠区证据）",
    !htmlNew.includes("data-hub-selfcheck-findings")
    && elementByMarker(htmlNew, "data-hub-selfcheck-fold").includes("行→图号"));
const gateDetailEl = elementByMarker(htmlNew, "data-hub-gate-detail");
const gateVisibleText = textOf(gateDetailEl.replace(elementByMarker(gateDetailEl, "data-hub-selfcheck-fold"), " "));
check("[B] 门禁 FAIL：整宽红条 + 缺项逐条人话 + 怎么处理（重试）",
    /data-hub-gate-state="fail"/.test(htmlNew)
    && gateVisibleText.includes("分镜表缺片尾覆盖")
    && gateVisibleText.includes("怎么处理：") && gateVisibleText.includes("重试"),
    gateVisibleText.slice(0, 260));
check("[B] 门禁可见区不摆统计/不拆证据串（「状态 标红｜绿项 6/8」只在折叠区）",
    !gateVisibleText.includes("状态 标红") && !gateVisibleText.includes("绿项 6/8")
    && elementByMarker(gateDetailEl, "data-hub-selfcheck-fold").includes("状态 标红"),
    "门禁摘要应来自说明.结论人话，统计进折叠区");

const htmlAllPass = ssr.renderPrompt({
    local_id: "auto-9702",
    prompt_md: PROMPT,
    自检: [{ 项: "次序对账", 结论: "PASS", 证据: "三方映射一致：…", 说明: { 结论人话: "三方对得上", 原因: "", 处理: "无需处理", 发现: [] } }],
});
check("[B] 全绿时只出绿 Tag（不铺无谓的异常区），正文照常默认展开",
    htmlAllPass.includes("自检 ✓ 1 项通过") && !htmlAllPass.includes("data-hub-selfcheck-detail")
    && htmlAllPass.includes("@图片1 手机主体特写"),
    htmlAllPass.slice(0, 200));

const htmlUnchecked = ssr.renderPrompt({ local_id: "auto-9703", prompt_md: "", 自检: null });
check("[B] 无自检文件 → 「未检查」+ 提示词未生成占位（旧行为不破）",
    htmlUnchecked.includes("自检结论：未检查") && htmlUnchecked.includes("提示词尚未生成"),
    htmlUnchecked.slice(0, 220));

// ════════════════════════════════════════════════════════════ [C] 红线静态守卫
console.log("5) 红线 25 静态守卫：drawer 不解析证据文字");
const drawerSrc = read("src/pages/hub/task-detail-drawer.tsx");
const evidenceCode = drawerSrc
    .split("\n")
    .filter((line) => !/^\s*(\*|\/\/|\/\*)/.test(line))          // 注释不算代码
    .filter((line) => line.includes("证据"));
const parsed = evidenceCode.filter((line) => /证据[^;\n]{0,24}\.(split|match|test|replace|exec|includes|indexOf|startsWith|search)\s*\(/.test(line));
check("[C] 没有任何对「证据」做 split/match/test/replace 的代码（不重判、不重解析）",
    parsed.length === 0, parsed.join(" ⏎ ").slice(0, 300));
check("[C] 「证据」只作为展示表达式/文案出现（{item.证据} / 「原始证据」/「展开看证据」）",
    evidenceCode.every((line) => /(\{item\.证据\}|\{item\.证据 \?|原始证据|展开看证据)/.test(line)),
    evidenceCode.join(" ⏎ ").slice(0, 300));

// ════════════════════════════════════════════════════════════ [D] 真实数据端到端
const argv = process.argv.slice(2);
const argVal = (flag) => { const i = argv.indexOf(flag); return i >= 0 ? argv[i + 1] : ""; };
const realSelfcheck = argVal("--selfcheck");
if (realSelfcheck) {
    console.log(`6) 真实生产者产出真渲染：${realSelfcheck}`);
    const items = JSON.parse(readFileSync(realSelfcheck, "utf-8")).map((row) => hubApi.normalizeSelfCheck([row])[0]);
    const html = ssr.renderPrompt({ local_id: "auto-9704", prompt_md: PROMPT, 自检: items });
    const failRows = items.filter((row) => row.结论 !== "PASS");
    check("[D] 真实产出里每条都有「说明」（生产者侧同源）",
        items.every((row) => row.说明 && row.说明.结论人话), JSON.stringify(items.map((row) => [row.项, Boolean(row.说明)])));
    check("[D] 真实产出渲染后每条异常都有「怎么处理」入口",
        failRows.length > 0 && failRows.every((row) => row.说明?.处理)
        && html.includes("怎么处理："), JSON.stringify(failRows.map((row) => [row.项, row.说明?.处理?.slice(0, 40)])));
    check("[D] 渲染出的处理指引与生产者原文逐字一致（读层/展示层没有二次加工）",
        failRows.every((row) => html.includes(row.说明.处理)), "逐条对不上");
    // AT-22 E2 改判：检查器原句（含 #N 写法）不再进可见区，改成「行号 + 短名」进可见区、
// 原句只进折叠区。这条断言跟到最后：动态行号必须在页面上（以人话形态），原句必须可查（折叠区）。
// v2 载荷（带 词/行称呼）→ 行号必须以「第 N 行」形态进可见区；旧载荷（pre-B2 自检.json，
// 没有 v2 槽位）不摆卡也不假装有行号——降级路径的断言在 [D] 前两条。
const v2Findings = failRows.flatMap((row) => row.说明.发现 || []).filter((finding) => finding.词 || finding.行称呼);
check("[D] 真实产出的动态行号进页面（人话「第 N 行」，原句收折叠区）",
    v2Findings.every((finding) => typeof finding.行号 === "number" && html.includes(`第 ${finding.行号} 行`)),
    JSON.stringify(v2Findings.map((f) => [f.行号, f.行称呼])));
}

const realLegacy = argVal("--legacy");
if (realLegacy) {
    console.log(`7) 历史文件只读降级真渲染：${realLegacy}`);
    const rawItems = JSON.parse(readFileSync(realLegacy, "utf-8"));
    const items = hubApi.normalizeSelfCheck(rawItems);
    const html = ssr.renderPrompt({ local_id: "auto-0042", prompt_md: PROMPT, 自检: items });
    const legacyRows = items.filter((row) => !row.说明);
    if (legacyRows.length === items.length) {
        // 全 legacy：连 说明 都没有（AT-21 之前的老文件）
        check("[D] 历史条目一个都没有伪造「说明」",
            legacyRows.length === items.length && items.every((row) => row.说明 === undefined),
            JSON.stringify(items.map((row) => [row.项, Boolean(row.说明)])));
        check("[D] 历史条目降级标记 + 明说没有处理指引（不假装有指引）",
            html.includes('data-hub-selfcheck-legacy="1"')
            && html.includes("没有处理指引") && !html.includes("data-hub-selfcheck-how"),
            html.slice(html.indexOf("data-hub-selfcheck-legacy") - 40, html.indexOf("data-hub-selfcheck-legacy") + 260));
    } else {
        // 中态：有 说明（结论人话/处理）但没有 v2 槽位（词/行称呼/片段）——AT-22 B2 之前的真机文件
        const midReason = rawItems.map((row) => row.说明?.原因).find((text) => text);
        const foldHtml = elementByMarker(html, "data-hub-selfcheck-fold");
        const visible = textOf(html.replace(foldHtml, " "));
        check("[D] 中态历史载荷不摆 v2 卡（没有 v2 槽位就不套新模板）",
            !html.includes("data-hub-selfcheck-card") && legacyRows.length < items.length,
            JSON.stringify(items.map((row) => [row.项, Boolean(row.说明)])));
        check("[D] 中态历史载荷可见区不出「原因：」段（最大重复源已拔，句子仍收折叠区）",
            !visible.includes("原因：") && (!midReason || foldHtml.includes(midReason.slice(0, 20))),
            `${visible.indexOf("原因：")} / ${Boolean(midReason)}`);
    }
    check("[D] 历史条目的原始证据原文完整可读（逐字保留，不截断不加戏）",
        rawItems.filter((row) => row.结论 !== "PASS").every((row) => html.includes(row.证据)),
        rawItems.filter((row) => row.结论 !== "PASS").map((row) => row.证据.slice(0, 40)).join(" / "));
}

console.log(`\nprobe-at16-b2: ${pass} PASS, ${fails.length} FAIL`);
for (const item of fails) console.log(`  FAIL ${item}`);
process.exit(fails.length ? 1 : 0);
