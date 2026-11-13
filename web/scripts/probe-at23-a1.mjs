/**
 * 质检面板就地处理闭环 · 第 1 批前端探针（本地免费、不联网、不动真实任务）。
 *
 * 第 2 批（字符级常驻高亮）起：`[A]` 里「临时高亮 2 秒」那几条断言已按新合同换成常驻高亮的
 * 纯逻辑断言（`buildPromptHighlightPlan` / `promptHighlightSpans`），`[B]` 的接线断言换成
 * `scrollToPromptHighlight` + `PromptMarkdown`；其余断言逐条不动。
 *
 * 断言四组：
 *  [A] 纯逻辑（esbuild 打包 src/lib/hub/toast.ts + prompt-anchor.ts + prompt-highlight.ts 后直接 import）
 *      · `claimToast`：同文案 1s 内只放行第一条；被吞的点击**不续期**（连点不会无限延长静默期）；
 *        超过 1s 可再弹；不同文案互不影响；非文本载荷（ReactNode）不拦；判重表按窗口自清（有界）。
 *      · `promptHitRange`：整句命中给准确区间；句首被 markdown 切开时退到最长前缀；找不到如实 null。
 *      · 常驻高亮的两层纯运算：计划只收结构化槽位（上下文优先 / 词兜底）；`片段` 的 `起/止` 当区间用；
 *        重叠与相邻区间取并集（不重复渲染）；整句落空退最长前缀；短词不拿前缀猜。
 *  [B] 静态合同（源码）
 *      · 「去这一行」假动作整条路径已删：`data-hub-selfcheck-locator` 0 命中、按行号查资产行 DOM 的
 *        querySelector 0 命中、那句「先点「退回置换」…」Toast 0 命中（**全 web/src 一起扫**，不只本文件）。
 *      · 动作位收敛：只剩「改这一行的描述」（置灰判据与 tooltip 都读 `editGate`＝`hubDescEditGate`
 *        单一口径）＋ 不可编辑态的「看正文里的这一句」（`canLocate && !editGate.enabled`）。
 *      · 只读定位锚正文容器（`ref={promptBodyRef}` 与 `data-hub-prompt-body` 同一元素），不锚资产行；
 *        正文渲染件 = Streamdown 原样输出 + 渲染后包常驻 mark（**不切 markdown 源**），
 *        点击定位＝滚到已存在的高亮段（不再有 2 秒定时摘除）。
 *      · Toast 薄封装：hub 页不再解构 `App.useApp().message`，全部走 `useHubMessage`。
 *  [C] 真渲染（esbuild + react-dom/server，streamdown 打桩）
 *      · 状态矩阵：可编辑两态 → 「改这一行的描述」可点、**没有**定位按钮（动作位收敛）；
 *        其余每态（由 automation/state.py 的 STATES 直出，不手抄）→ 置灰 + 定位按钮可点、无 locator。
 *      · done 态：tooltip 的真实原因＝`hubDescEditGate("done").reason`（含「已完成」），不是自造文案。
 *  [D] 验收口径自查：连点 5 次的开关语义由「单一 editingRow + toggle」结构保证（SSR 只能证结构，
 *      真机行为由 ego 探针 ego-at23-a1.mjs 断言）。
 *
 * 跑法：cd web && node scripts/probe-at23-a1.mjs
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const WEB = process.cwd();
const CACHE = path.join(WEB, "node_modules", ".cache", "at23-a1");
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

// ════════════════════════════════════════════════ [A] 纯逻辑
const pureOut = path.join(CACHE, "pure.mjs");
const pureBuilt = await esbuild.build({
    stdin: {
        contents:
            'export * from "../../../src/lib/hub/toast";\n' +
            'export * from "../../../src/lib/hub/prompt-anchor";\n' +
            'export * from "../../../src/lib/hub/prompt-highlight";\n',
        resolveDir: CACHE,
        loader: "ts",
    },
    bundle: true, format: "esm", platform: "node", outfile: pureOut, logLevel: "silent",
});
check("[A] 纯逻辑模块打包成功（toast / prompt-anchor / prompt-highlight）", pureBuilt.errors.length === 0,
    pureBuilt.errors.map((e) => e.text).join("; "));
const pure = await import(pathToFileURL(pureOut).href + `?t=${Date.now()}`);

console.log("1) claimToast：同文案 1s 内只出一条，且被吞的点击不续期");
{
    const log = new Map();
    const shots = [0, 100, 200, 300, 400].map((t) => pure.claimToast(log, "同一句提示", t));
    check("[A] 连点 5 次（间隔 100ms）→ 只有第 1 条放行", shots.filter(Boolean).length === 1 && shots[0] === true,
        JSON.stringify(shots));
    check("[A] 距第 1 条 1050ms（>1s）再点 → 允许再弹 1 条", pure.claimToast(log, "同一句提示", 1050) === true);
    const logN = new Map();
    const r1 = pure.claimToast(logN, "y", 0);
    const r2 = pure.claimToast(logN, "y", 900);
    const r3 = pure.claimToast(logN, "y", 999);
    const r4 = pure.claimToast(logN, "y", 1000);
    check("[A] 被吞的点击不续期（900/999ms 都被吞，1000ms 才放行——续期实现会在 1000ms 仍吞）",
        r1 === true && r2 === false && r3 === false && r4 === true, JSON.stringify([r1, r2, r3, r4]));
    check("[A] 不同文案互不影响（各自独立窗口）",
        pure.claimToast(new Map([["A", 0]]), "B", 100) === true);
    check("[A] 非文本载荷（ReactNode → 空串）不拦，原样交给 antd",
        pure.claimToast(new Map([["", 0]]), "", 100) === true);
    const many = new Map();
    for (let i = 0; i < 300; i += 1) many.set(`t${i}`, i);
    pure.claimToast(many, "新文案", 10_000);
    check("[A] 判重表有界（满 200 条后清过期项，不会无限增长）", many.size <= 201, `size=${many.size}`);
    check("[A] 去重窗口常量＝1000ms（设计 §4）", pure.HUB_TOAST_DEDUPE_MS === 1000, String(pure.HUB_TOAST_DEDUPE_MS));
}

console.log("2) promptHitRange：整句 → 最长前缀 → 如实找不到");
{
    const hay = "0-2秒，中景，全程使用参考图中的 裹亮橙色标签款静置桌面。";
    const whole = pure.promptHitRange(hay, "全程使用参考图中的 裹亮橙色标签款");
    check("[A] 整句子串命中：区间两端正好切出这一句",
        Boolean(whole) && hay.slice(whole.start, whole.end) === "全程使用参考图中的 裹亮橙色标签款",
        JSON.stringify(whole));
    const split = pure.promptHitRange(hay, "全程使用参考图中的@图片1 裹亮橙色标签款。");
    check("[A] markdown 把原句切碎时退到最长前缀（落在句首，不漂到别处）",
        Boolean(split) && hay.slice(split.start, split.end) === "全程使用参考图中的",
        split ? hay.slice(split.start, split.end) : "null");
    check("[A] 正文里确实没有这一句 → null（不瞎指位置）",
        pure.promptHitRange(hay, "这句话根本不在这一版正文里") === null);
    check("[A] 太短的串（<2 字）不猜", pure.promptHitRange(hay, "桌") === null);
    check("[A] 正文定位只留纯下标运算：临时高亮路径已删（无 flashPromptHit / 无 2 秒定时摘除）",
        typeof pure.flashPromptHit === "undefined"
        && !/setTimeout/.test(read("src/lib/hub/prompt-anchor.ts")));
    check("[A] 常驻高亮的锚点沿用 data-hub-prompt-hit，值＝persistent（与第 1 批临时高亮可区分）",
        pure.PROMPT_HL_ATTR === "data-hub-prompt-hit" && pure.PROMPT_HL_STATE === "persistent",
        `${pure.PROMPT_HL_ATTR}=${pure.PROMPT_HL_STATE}`);
}

console.log("3) 常驻高亮纯运算：计划收口结构 → 片段区间 → 并集合并 → 前缀回退");
const SENTENCE = "全程使用参考图中的@图片1 裹亮橙色标签款。";
const HL_ITEMS = [{
    说明: {
        发现: [
            { 上下文: SENTENCE, 词: "裹亮橙色标签款", 片段: [
                { 起: 0, 止: 14, 文: "全程使用参考图中的@图片1 ", 高亮: false },
                { 起: 14, 止: 21, 文: "裹亮橙色标签款", 高亮: true },
                { 起: 21, 止: 22, 文: "。", 高亮: false },
            ] },
            { 上下文: SENTENCE, 词: "亮橙色", 片段: [{ 起: 15, 止: 18, 文: "亮橙色", 高亮: true }] },
            { 上下文: SENTENCE, 词: "标签款", 片段: [{ 起: 18, 止: 21, 文: "标签款", 高亮: true }] },
            { 上下文: SENTENCE, 词: "裹亮橙色标签款", 片段: [] },
        ],
    },
}];
const PLAN = pure.buildPromptHighlightPlan(HL_ITEMS);
check("[A] 计划：同上下文+同词去重（4 条 finding → 3 条计划项）", PLAN.length === 3, String(PLAN.length));
check("[A] 计划只用结构化槽位：非高亮片段丢弃，命中片段原样保留（相对上下文下标）",
    PLAN[0] && PLAN[0].片段.length === 1 && PLAN[0].片段[0].起 === 14 && PLAN[0].片段[0].止 === 21,
    JSON.stringify(PLAN[0] && PLAN[0].片段));
const body = `0-3秒，中景，人物与@图片1，微幅晃动。${SENTENCE}8-10秒，中景，保持静止。`;
const spans = pure.promptHighlightSpans(body, PLAN);
const hitText = spans.map((s) => body.slice(s.起, s.止));
check("[A] 三个滑窗切片（7 字 / 3 字 / 3 字）取并集后只剩一段：命中词本身",
    spans.length === 1 && hitText[0] === "裹亮橙色标签款", JSON.stringify(hitText));
check("[A] 并集不重复渲染：区间互不重叠、也不相邻",
    spans.every((s, i) => i === 0 || s.起 > spans[i - 1].止), JSON.stringify(spans));
check("[A] 只上色真正命中的那段字（句首非命中部分不跟着上色）",
    !hitText[0].includes("全程使用"), hitText[0]);
const noPiecePlan = pure.buildPromptHighlightPlan([{ 说明: { 发现: [{ 上下文: SENTENCE, 词: "裹亮橙色标签款" }] } }]);
const wholeSpans = pure.promptHighlightSpans(body, noPiecePlan);
check("[A] 没有片段（旧数据）→ 整句上色，区间正好是上下文整句",
    wholeSpans.length === 1 && body.slice(wholeSpans[0].起, wholeSpans[0].止) === SENTENCE, JSON.stringify(wholeSpans));
const cutBody = "0-3秒。全程使用参考图中的图片 裹亮橙色标签款。";
const cutSpans = pure.promptHighlightSpans(cutBody, noPiecePlan);
check("[A] 整句落空（markdown 把 @图片1 拆走）→ 退最长前缀，仍落在这一句开头",
    cutSpans.length === 1 && cutBody.slice(cutSpans[0].起, cutSpans[0].止) === "全程使用参考图中的",
    JSON.stringify(cutSpans.map((s) => cutBody.slice(s.起, s.止))));
const wordPlan = pure.buildPromptHighlightPlan([{ 说明: { 发现: [{ 词: "裹亮橙色标签款" }] } }]);
const wordSpans = pure.promptHighlightSpans(body, wordPlan);
check("[A] 旧数据没有上下文 → 词兜底（精确匹配，不拿两字前缀瞎猜）",
    wordSpans.length === 1 && body.slice(wordSpans[0].起, wordSpans[0].止) === "裹亮橙色标签款", JSON.stringify(wordSpans));
const absentPlan = pure.buildPromptHighlightPlan([{ 说明: { 发现: [{ 上下文: "这句话根本不在这一版正文里" }] } }]);
check("[A] 整句与前缀都找不到 → 一段都不上色（不瞎指位置）",
    pure.promptHighlightSpans(body, absentPlan).length === 0);
check("[A] 空自检束／空输入 → 空计划（正文照常渲染，不上任何色）",
    pure.buildPromptHighlightPlan([]).length === 0 && pure.buildPromptHighlightPlan(undefined).length === 0);

// ════════════════════════════════════════════════ [B] 静态合同
console.log("4) 假动作整条路径已删（全 web/src 一起扫）");
const drawerSrc = read("src/pages/hub/task-detail-drawer.tsx");
const DEAD_COPY = "先点「退回置换」回到资产置换区，再点这里";
const walk = (dir, out = []) => {
    for (const entry of readdirSync(dir)) {
        const full = path.join(dir, entry);
        if (statSync(full).isDirectory()) walk(full, out);
        else if (/\.(ts|tsx|mjs|js)$/.test(entry)) out.push(full);
    }
    return out;
};
const srcFiles = walk(path.join(WEB, "src"));
const deadHits = srcFiles.filter((file) => readFileSync(file, "utf-8").includes(DEAD_COPY));
check("[B] 验收 2：全 web/src 里那句「先点「退回置换」…」0 命中", deadHits.length === 0,
    deadHits.map((file) => path.relative(WEB, file)).join(" "));
check("[B] `data-hub-selfcheck-locator` 元素已不存在（源码 0 命中）",
    !drawerSrc.includes("data-hub-selfcheck-locator"));
check("[B] 不再按行号去查资产置换区的行 DOM（假定位的根因已删）",
    !drawerSrc.includes("querySelector(`[data-hub-asset-row=")
    && !drawerSrc.includes('querySelectorAll("[data-hub-asset-rows]")'));
check("[B] 「去这一行」这个按钮文案已不存在", !/去这一行<\/Button>/.test(drawerSrc) && !drawerSrc.includes(">去这一行"));

console.log("5) 动作位收敛：一个改入口（置灰讲真原因）+ 不可编辑态的只读定位");
check("[B] 质检卡动作位仍有唯一的改入口（生产者指引点名的「改这一行的描述」逐字未变）",
    drawerSrc.includes("改这一行的描述") && /data-hub-selfcheck-fix\b/.test(drawerSrc));
check("[B] 置灰判据与 tooltip 同源（都读 editGate＝hubDescEditGate 单一口径，不自造第二份状态表）",
    /disabled=\{!editGate\.enabled \|\| descApi\.busy \|\| otherEditorOpen\}/.test(drawerSrc)
    && /: editGate\.reason\}/.test(drawerSrc)
    && /const editGate = descApi\?\.gate \?\? \{ enabled: false, reason: "这条详情还没接上自检面板" \};/.test(drawerSrc));
check("[B] 新增只读定位按钮：仅有它、且只在**不可编辑**态出现",
    /data-hub-selfcheck-locate\b/.test(drawerSrc)
    && /canLocate && !editGate\.enabled \? \(/.test(drawerSrc)
    && drawerSrc.includes("看正文里的这一句"));
check("[B] 定位锚正文容器（恒渲染），不锚资产行：ref 与 data-hub-prompt-body 同一元素",
    /ref=\{promptBodyRef\}\s+className="agent-streamdown[^"]*"\s+data-hub-prompt-body/.test(drawerSrc)
    && /scrollToPromptHighlight\(promptBodyRef\.current, wants\)/.test(drawerSrc));
check("[B] 正文渲染件＝Streamdown 原样输出 + 渲染后包常驻 mark（不按字符切 markdown 源）",
    /<PromptMarkdown markdown=\{prompt\} plan=\{promptHighlightPlan\} \/>/.test(drawerSrc)
    && /<Streamdown>\{markdown\}<\/Streamdown>/.test(drawerSrc)
    && !/Streamdown>\{prompt\}/.test(drawerSrc));
check("[B] 高亮计划一次算好（useMemo 按自检束），不随每次 render 重算",
    /const promptHighlightPlan = useMemo\(\s*\(\) => buildPromptHighlightPlan\(detail\.自检\),\s*\[detail\.自检\],\s*\);/.test(drawerSrc));
check("[B] 常驻高亮的生命周期：包在 layout effect 里、清理时展开回去（先拆再让 React 改 DOM）",
    /useIsoLayoutEffect\(\(\) => \{\s*const marks = applyPromptHighlights\(hostRef\.current, plan\);\s*return \(\) => unwrapPromptHighlights\(marks\);/.test(drawerSrc));
check("[B] 常驻高亮不再有 2 秒定时摘除（临时高亮那条路径整条已删）",
    !/unwrapPromptHit\b|flashPromptHit\b|PROMPT_HIT_MS/.test(drawerSrc) && !/setTimeout[^\n]*unwrap/.test(drawerSrc));
check("[B] 正文可能在收起态：定位前先展开（不是让用户自己去找）",
    /setCollapsed\(false\);\s+\/\/ 正文可能处于收起态/.test(drawerSrc));
check("[B] 定位路径永不要求退状态（没有「退回置换」指路文案）",
    !/先点「退回置换」/.test(drawerSrc));
check("[B] 开关语义：单一 editingRow + toggle（结构上不可能出现多份编辑器）",
    /const \[editingRow, setEditingRow\] = useState<number \| null>\(null\)/.test(drawerSrc)
    && /onClick=\{\(\) => descApi\.setEditingRow\(editing \? null : rowNo\)\}/.test(drawerSrc)
    && /const editing = rowNo !== null && descApi\?\.editingRow === rowNo;/.test(drawerSrc));

console.log("6) Toast 薄封装接线（同文案 1s 去重）");
check("[B] 详情页不再解构 App.useApp().message（全部走 useHubMessage）",
    !/const \{ message[^}]*\} = App\.useApp\(\);/.test(drawerSrc)
    && (drawerSrc.match(/const message = useHubMessage\(\);/g) || []).length >= 10,
    String((drawerSrc.match(/const message = useHubMessage\(\);/g) || []).length));
const hubFiles = readdirSync(path.join(WEB, "src/pages/hub")).filter((f) => f.endsWith(".tsx"));
const legacyHooks = hubFiles.filter((f) => /const \{ message[^}]*\} = App\.useApp\(\);/.test(read(`src/pages/hub/${f}`)));
check("[B] hub 页（含列表/新建/删除）全部换用封装，零残留", legacyHooks.length === 0, legacyHooks.join(" "));
const hookSrc = read("src/pages/hub/use-hub-message.ts");
check("[B] 封装只拦四个提示级别、其余原样透传（不改文案与语义）",
    /const LEVELS = \["info", "success", "warning", "error"\] as const;/.test(hookSrc)
    && /const wrapped = \{ \.\.\.message \} as MessageInstance;/.test(hookSrc)
    && /claimToast\(TOAST_LOG, text, clock\(\)\)/.test(hookSrc));
check("[B] Hook 只是包装层的薄壳（真机探针可拿假 api 打同一份代码）",
    /export function withToastDedupe\(message: MessageInstance, clock: \(\) => number = \(\) => Date\.now\(\)\): MessageInstance/.test(hookSrc)
    && /return useMemo\(\(\) => withToastDedupe\(message\), \[message\]\);/.test(hookSrc));
check("[B] 判重表是模块级的（跨组件/跨按钮共享，不是每个组件各一套）",
    /^const TOAST_LOG = new Map<string, number>\(\);/m.test(hookSrc));

// ════════════════════════════════════════════════ [C] 真渲染（状态矩阵）
const STATES_SRC = readFileSync(path.join(WEB, "..", "wanrepro", "automation", "state.py"), "utf-8");
const STATES_BLOCK = STATES_SRC.slice(STATES_SRC.indexOf("STATES = ["), STATES_SRC.indexOf("_ORDER"));
const STATES = [...STATES_BLOCK.matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
check("[C] 后端状态表拿到了（直读 automation/state.py 的 STATES，不手抄）", STATES.length >= 9, JSON.stringify(STATES));

const STUB = path.join(CACHE, "streamdown-stub.tsx");
writeFileSync(STUB,
    "export function Streamdown({ children }: { children?: unknown }) {\n" +
    "    return <pre data-stub-streamdown>{children as string}</pre>;\n" +
    "}\n");
const stubPlugin = {
    name: "at23a1-stubs",
    setup(build) {
        build.onResolve({ filter: /^streamdown$/ }, () => ({ path: STUB, namespace: "at23a1-stub" }));
        build.onLoad({ filter: /.*/, namespace: "at23a1-stub" }, () => ({
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
check("[C] SSR 模块可加载", typeof ssr.renderPrompt === "function");

const CONTEXT = "0-2秒，中景，全程使用参考图中的 裹亮橙色标签款静置桌面。";
const ITEMS = [{
    项: "绑图行残留", 项显示名: "提示词里的老词", 结论: "FAIL", 证据: "第 1 行残留：裹亮橙色标签款",
    说明: {
        结论人话: "第 1 行「本品主体」的原物描述词还在提示词里（1 处在正文）",
        发现: [{
            类型: "残留", 文字: "第 1 行残留：裹亮橙色标签款", 行号: 1, 图号: 1,
            行称呼: "本品主体", 图称呼: "产品图", 图引用: "@图片1", 词: "裹亮橙色标签款",
            句序: 1, 原文: "第 1 行残留：裹亮橙色标签款", 可修: true,
            上下文: CONTEXT,
            片段: [{ 起: 20, 止: 28, 文: "裹亮橙色标签款", 高亮: true }],
        }],
        处理: "在下面这条发现上点「改这一行的描述」，写完点「保存并重渲染」",
    },
}];
const DETAIL = (state) => ({
    local_id: "auto-9801", state,
    prompt_md: `## 画面提示词\n${CONTEXT}`,
    asset_cards: [{ n: 1, 类别: "主体", 描述: "裹亮橙色标签款", 我的描述: "", 已传图: [], 命名: "", 同实体疑似: [] }],
    自检: ITEMS,
});
const buttonOf = (html, marker) => {
    const at = html.indexOf(marker);
    if (at < 0) return "";
    const start = html.lastIndexOf("<button", at);
    const end = html.indexOf(">", at);
    return start >= 0 && end > start ? html.slice(start, end + 1) : "";
};

console.log("7) 状态矩阵：可编辑两态＝就地改（无定位按钮）；其余每态＝置灰 + 只读定位");
const EDITABLE = ["awaiting_assets", "awaiting_approval"];
for (const state of STATES) {
    const html = ssr.renderPrompt(DETAIL(state));
    const fix = buttonOf(html, "data-hub-selfcheck-fix");
    const hasLocate = html.includes("data-hub-selfcheck-locate");
    const hasLocator = html.includes("data-hub-selfcheck-locator");
    if (EDITABLE.includes(state)) {
        check(`[C] ${state}：改入口可点（无 disabled）、动作位收敛到它（不出定位按钮）、无 locator 元素`,
            fix !== "" && !fix.includes("disabled") && !hasLocate && !hasLocator,
            `${fix.slice(0, 120)} locate=${hasLocate}`);
    } else {
        check(`[C] ${state}：改入口置灰 + 只读「看正文里的这一句」可点、无 locator 元素`,
            fix !== "" && fix.includes("disabled") && hasLocate && !hasLocator
            && !buttonOf(html, "data-hub-selfcheck-locate").includes("disabled"),
            `${fix.slice(0, 120)} locate=${hasLocate} ${buttonOf(html, "data-hub-selfcheck-locate").slice(0, 120)}`);
    }
}
const unknownHtml = ssr.renderPrompt(DETAIL(""));
check("[C] 未知状态不默认放行（置灰 + 定位可用）",
    buttonOf(unknownHtml, "data-hub-selfcheck-fix").includes("disabled")
    && unknownHtml.includes("data-hub-selfcheck-locate"));

console.log("8) 不可编辑态的解释来自起态门（真实原因，不是自造文案）");
const apiBundle = path.join(CACHE, "hub-api.bundle.mjs");
await esbuild.build({
    entryPoints: [path.join(WEB, "src/pages/hub/hub-api.ts")], bundle: true, format: "esm",
    platform: "node", outfile: apiBundle, logLevel: "silent", define: { "import.meta.env": "{}" },
});
const hubApi = await import(pathToFileURL(apiBundle).href + `?t=${Date.now()}`);
const doneGate = hubApi.hubDescEditGate("done");
check("[C] done 的原因文案＝「已完成：成片已出，这条不能再改描述」（与设计 §2.4 一致）",
    doneGate.enabled === false && doneGate.reason.includes("已完成") && doneGate.reason.includes("不能再改描述"),
    JSON.stringify(doneGate));
const reasonsMissing = STATES.filter((s) => !hubApi.hubDescEditGate(s).enabled && !hubApi.hubDescEditGate(s).reason);
check("[C] 每个不可编辑态都有人话原因（置灰不是哑巴）", reasonsMissing.length === 0, JSON.stringify(reasonsMissing));

console.log("9) 第 3 批（切片词折叠）：一句 1 个完整原词、1 个「保留这个词」、折叠区列 5 种切法");
{
    // 生产者在同一「行×句」按包含关系折叠后的载荷：1 条 finding + `切片`（证据）+ 判定词数留痕。
    const FOLDED = [{
        项: "绑图行残留", 项显示名: "提示词里的老词", 结论: "FAIL", 证据: "#1 的「裹亮橙色标签款」残留于正文…",
        说明: {
            结论人话: "提示词里还留着 1 个要换掉的原物词，都在第 1 行「本品主体」里",
            发现: [{
                类型: "残留", 文字: "#1 的「裹亮橙色标签款」残留于正文", 行号: 1, 图号: 1,
                行称呼: "本品主体", 图称呼: "产品图", 图引用: "@图片1", 词: "裹亮橙色标签款",
                句序: 1, 原文: "#1 的「裹亮橙色标签款」残留于正文", 可修: true,
                上下文: CONTEXT,
                片段: [{ 起: 20, 止: 28, 文: "裹亮橙色标签款", 高亮: true }],
                切片: ["裹亮橙", "亮橙色", "橙色标", "色标签", "标签款"],
            }],
            处理: "在下面这条发现上点「改这一行的描述」，写完点「保存并重渲染」",
            统计: { 词表行数: 1, 正文字数: 448, 词数: 1, 判定词数: 6, 句数: 1 },
        },
    }];
    const html = ssr.renderPrompt({ ...DETAIL("awaiting_approval"), 自检: FOLDED });
    const hitLine = (html.match(/data-hub-selfcheck-words="true">([\s\S]*?)<\/div>/) || ["", ""])[1];
    const quoted = [...hitLine.matchAll(/『([^』]+)』/g)].map((m) => m[1]);
    check("[C] 红字一句只出现 1 个『词』，且＝完整原词（切片不上红字行）",
        quoted.length === 1 && quoted[0] === "裹亮橙色标签款", JSON.stringify(quoted));
    check("[C] 红字一句是设计 §3.4 的措辞（「这一行的原物描述词『…』还在提示词里」）",
        html.includes("这一行的原物描述词『裹亮橙色标签款』还在提示词里"));
    check("[C] 「保留这个词」只剩 1 个，且 data-hub-selfcheck-keep＝完整原词",
        (html.match(/data-hub-selfcheck-keep=/g) || []).length === 1
        && html.includes('data-hub-selfcheck-keep="裹亮橙色标签款"'),
        String((html.match(/data-hub-selfcheck-keep=/g) || []).length));
    const foldBlock = (html.match(/data-hub-fold-block="同一个词的 6 种切法"[\s\S]*?<\/div><\/div>/) || [""])[0];
    check("[C] 折叠区新增「同一个词的 6 种切法」，5 个切片逐字列出",
        Boolean(foldBlock) && ["裹亮橙", "亮橙色", "橙色标", "色标签", "标签款"].every((word) => foldBlock.includes(word)),
        foldBlock.slice(0, 200));
    const visible = html.replace(/<details[\s\S]*?<\/details>/g, "");
    check("[C] 验收 5：可见区（去掉折叠区）没有任何 3 字切片被当词呈现",
        ["裹亮橙", "亮橙色", "橙色标", "色标签", "标签款"].every((word) => !visible.includes(`『${word}』`)));
    const foldArea = (html.match(/<details[^>]*data-hub-selfcheck-fold[\s\S]*?<\/details>/) || [""])[0];
    check("[C] 判定口径在折叠区留痕（内部统计写「判定命中 6 个词」）",
        foldArea.includes("判定命中 6 个词") && !visible.includes("判定命中 6 个词"), foldArea.slice(0, 120));
}

console.log("");
console.log(`[probe-at23-a1.mjs] ${pass}/${pass + fails.length} PASS`);
if (fails.length) {
    console.log("FAIL: " + fails.join(" | "));
    process.exit(1);
}
process.exit(0);
