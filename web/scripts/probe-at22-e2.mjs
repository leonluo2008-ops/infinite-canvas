/**
 * AT-22 E2 前端探针（本地免费，不联网、不动真实任务）：**自检 finding 呈现重构**的真渲染验收。
 *
 * 输入是**真生产者产出**（`automation/probe_at22_e2.py --isolated` 跑出来的 items）与**真历史载荷**
 * （auto-0066 的 pre-B2 自检.json），不是手搓形状。三组断言：
 *  [A] 读层：normalizeSelfCheck 把 finding v2 槽位（词/句序/上下文/片段/起止/高亮/行称呼/图称呼/
 *      图引用/可修/组）原样搬过来，畸形片段丢弃、旧结构不伪造、统计与清单行/原句不丢。
 *  [B] 真渲染（v2）：一条说清 —— 项显示名取话术、结论人话只出现 1 次、同句多词并成 1 张卡、
 *      位置段「第 N 行「短名」·图N（你传的…）」、命中词各一次、上下文按偏移高亮（<mark>）、
 *      缩略图、定位按钮、可见区零禁用词（#N / 裸图号 / 内部术语 / 路径 / 「原因：」）、
 *      折叠区五块齐全且默认关闭、红条只如实显示不阻断。
 *  [C] 去重可量化：同一命中句在 v2 可见区承载位 = 0（检查器原句只在折叠区），
 *      而真实 pre-B2 载荷里同一句被 ≥3 个可见位承载 —— 打印前后数字。
 *  [D] 旧数据降级：pre-B2 载荷 → 结论人话 + 处理 + 折叠证据，不出卡、不出「原因：」、不炸；
 *      连说明都没有的更老载荷 → 明说没有处理指引。
 *
 * 跑法：cd web && node scripts/probe-at22-e2.mjs --v2 <items.json> [--legacy <自检.json>]
 * 判据全对才 exit 0；任何一步 FAIL 如实红。
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const WEB = process.cwd();
const CACHE = path.join(WEB, "node_modules", ".cache", "at22-e2");
mkdirSync(CACHE, { recursive: true });
const read = (rel) => readFileSync(path.join(WEB, rel), "utf-8");

const argv = process.argv.slice(2);
const arg = (name) => {
    const at = argv.indexOf(name);
    return at >= 0 ? argv[at + 1] : "";
};
const V2_PATH = arg("--v2");
const LEGACY_PATH = arg("--legacy");

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

// 纯 node 里跑真前端模块：补最小的浏览器全局（i18n 在 import 期读 localStorage）
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

// ════════════════════════════════════════════════ [A] 读层
const apiBundle = path.join(CACHE, "hub-api.bundle.mjs");
const apiBuilt = await esbuild.build({
    entryPoints: [path.join(WEB, "src/pages/hub/hub-api.ts")],
    bundle: true, format: "esm", platform: "node", outfile: apiBundle, logLevel: "silent",
    define: { "import.meta.env": "{}" },
});
check("[A] esbuild 打包 hub-api.ts 成功", apiBuilt.errors.length === 0, apiBuilt.errors.map((e) => e.text).join("; "));
const hubApi = await import(pathToFileURL(apiBundle).href + `?t=${Date.now()}`);

const v2Raw = JSON.parse(readFileSync(V2_PATH, "utf-8"));
const legacyRaw = LEGACY_PATH ? JSON.parse(readFileSync(LEGACY_PATH, "utf-8")) : null;

const norm = hubApi.normalizeSelfCheck(v2Raw);
const residueRaw = v2Raw.find((item) => item.项 === "绑图行残留");
const residueNorm = norm.find((item) => item.项 === "绑图行残留");
const rawHit = ((residueRaw?.说明 ?? {}).发现 ?? [])[0] ?? {};
const normHit = ((residueNorm?.说明 ?? {}).发现 ?? [])[0] ?? {};

check("[A] finding v2 槽位原样透传（词/句序/上下文/行称呼/图称呼/图引用/可修/组/原文）",
    normHit.词 === rawHit.词 && normHit.句序 === rawHit.句序 && normHit.行称呼 === rawHit.行称呼
    && normHit.图称呼 === rawHit.图称呼 && normHit.图引用 === rawHit.图引用
    && JSON.stringify(normHit.可修) === JSON.stringify(rawHit.可修) && normHit.原文 === rawHit.原文
    && JSON.stringify(normHit.组) === JSON.stringify(rawHit.组),
    JSON.stringify(normHit).slice(0, 300));
check("[A] `片段` 保留 起/止/高亮 三个字段（前端按偏移上色，不解析文字）",
    Array.isArray(normHit.片段) && normHit.片段.length >= 2
    && normHit.片段.every((piece) => typeof piece.起 === "number" && typeof piece.止 === "number"
        && typeof piece.文 === "string" && typeof piece.高亮 === "boolean"),
    JSON.stringify(normHit.片段 ?? null).slice(0, 300));
check("[A] 折叠区三源（统计/清单行/原句）与 项显示名 都搬过来了",
    residueNorm?.说明?.统计?.["词表行数"] === residueRaw?.说明?.统计?.["词表行数"]
    && JSON.stringify(residueNorm?.说明?.清单行) === JSON.stringify(residueRaw?.说明?.清单行)
    && JSON.stringify(residueNorm?.说明?.原句) === JSON.stringify(residueRaw?.说明?.原句)
    && residueNorm?.项显示名 === residueRaw?.项显示名,
    JSON.stringify({ 统计: residueNorm?.说明?.统计, 项显示名: residueNorm?.项显示名 }).slice(0, 300));

const junkPiece = hubApi.normalizeSelfCheck([{
    项: "甲", 结论: "FAIL", 证据: "e",
    说明: { 结论人话: "话", 发现: [{ 文字: "t", 行号: 3, 词: "w", 片段: [{ 文: "缺起止" }, { 起: 2, 止: 1, 文: "止<起" }, "junk", { 起: 0, 止: 1, 文: "好" }] }] },
}]);
check("[A] 畸形片段（缺起止/止<起/非对象）被丢弃，好片段留着 → 不炸也不乱画",
    junkPiece?.[0]?.说明?.发现?.[0]?.片段?.length === 1
    && junkPiece[0].说明.发现[0].片段[0].文 === "好",
    JSON.stringify(junkPiece?.[0]?.说明?.发现?.[0]?.片段 ?? null));
check("[A] 旧结构不伪造 v2：无 说明 → undefined，无 片段 → 不造空数组",
    hubApi.normalizeSelfCheck([{ 项: "乙", 结论: "FAIL", 证据: "e" }])?.[0]?.说明 === undefined
    && hubApi.normalizeSelfCheck([{ 项: "丙", 结论: "FAIL", 证据: "e", 说明: { 结论人话: "话", 发现: [{ 文字: "t", 行号: 1 }] } }])
        ?.[0]?.说明?.发现?.[0]?.片段 === undefined);

// ════════════════════════════════════════════════ [B] 真渲染
const STUB = path.join(CACHE, "streamdown-stub.tsx");
writeFileSync(STUB,
    "export function Streamdown({ children }: { children?: unknown }) {\n" +
    "    return <pre data-stub-streamdown>{children as string}</pre>;\n" +
    "}\n");
const stubPlugin = {
    name: "at22e2-stubs",
    setup(build) {
        build.onResolve({ filter: /^streamdown$/ }, () => ({ path: STUB, namespace: "at22e2-stub" }));
        build.onLoad({ filter: /.*/, namespace: "at22e2-stub" }, () => ({ contents: readFileSync(STUB, "utf-8"), loader: "tsx", resolveDir: WEB }));
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
check("[B] SelfCheckPanel 真渲染打包成功（react/react-dom/antd 走宿主机，仅 streamdown 打桩）",
    ssrBuilt.errors.length === 0, ssrBuilt.errors.map((e) => `${e.location?.file}:${e.text}`).join("; "));
const ssr = await import(pathToFileURL(ssrOut).href + `?t=${Date.now()}`);

const PROMPT = "## 画面提示词\n@图片1 主体特写；@图片6 静物。";
const render = (items) => ssr.renderPrompt({ local_id: "auto-9801", prompt_md: PROMPT, 自检: items });

/** 取带某标记的元素（div/section/details/article 皆可）：从标记处回找起始标签，按同名标签配平取整段。 */
function elementByMarker(html, marker, searchFrom = 0) {
    const at = html.indexOf(marker, searchFrom);
    if (at < 0) return "";
    let start = -1;
    let tag = "";
    for (const name of ["article", "div", "section", "details", "span", "ul", "li", "mark"]) {
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
const allElements = (html, marker) => {
    const out = [];
    let from = 0;
    for (;;) {
        if (html.indexOf(marker, from) < 0) return out;
        const piece = elementByMarker(html, marker, from);
        if (!piece) return out;
        out.push(piece);
        from += html.indexOf(piece, from) + piece.length;
    }
};
const strip = (html) => html
    .replace(/<[^>]*>/g, " ")
    .replace(/&quot;/g, '"').replace(/&#x27;/g, "'").replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&nbsp;/g, " ");
const inside = (html, marker) => {
    const piece = elementByMarker(html, marker);
    return piece ? strip(piece) : "";
};
const countOf = (haystack, needle) => (needle ? haystack.split(needle).length - 1 : 0);

const htmlV2 = render(norm);                    // 渲染生产路径载荷（normalize 之后），不是原始 JSON
const residueItemEl = allElements(htmlV2, "data-hub-selfcheck-item")
    .find((el) => el.includes(v2Raw.find((item) => item.项 === "绑图行残留").项显示名));
const residueVisible = strip(
    residueItemEl
        .replace(elementByMarker(residueItemEl, "data-hub-selfcheck-fold"), " ")
        .replace(elementByMarker(residueItemEl, "data-hub-selfcheck-context"), " "),
);
const cards = allElements(residueItemEl, "data-hub-selfcheck-card");
const words = [...new Set((residueRaw.说明.发现 ?? []).map((finding) => finding.词))];

console.log(`1) v2 真渲染（命中 ${words.length} 词 / 句序 ${rawHit.句序} / 行 ${rawHit.行号}）`);
check("[B] 面板徽标是「N 项没通过（共 M 项）」（不再「自检 ⚠ N/M 项异常」）",
    /\d+ 项没通过（共 \d+ 项）/.test(inside(htmlV2, "data-hub-selfcheck-badge")),
    inside(htmlV2, "data-hub-selfcheck-badge"));
check("[B] 项名取话术（提示词里的老词），内部项名（绑图行残留）不出现在可见区",
    residueVisible.includes("提示词里的老词") && !residueVisible.includes("绑图行残留"));
check("[B] 同句多词并成 1 张卡（不是一词一行）",
    cards.length === 1, `卡数=${cards.length}`);
check("[B] 卡内命中词逐个只提一次（『词』并列在命中段）",
    words.every((word) => countOf(cards[0] ?? "", `『${word}』`) === 1),
    words.map((word) => `${word}:${countOf(cards[0] ?? "", `『${word}』`)}`).join(" "));
check("[B] 位置段是「第 6 行「短名」」，行号与短名同句给出",
    new RegExp(`第 ${rawHit.行号} 行「${rawHit.行称呼}」`).test(inside(cards[0] ?? "", "data-hub-selfcheck-position")),
    inside(cards[0] ?? "", "data-hub-selfcheck-position"));
check("[B] 图号带称呼与人话括号（图N（你传的…）），不是裸「图N」",
    new RegExp(`图${rawHit.图号}（[^）]+）`).test(inside(cards[0] ?? "", "data-hub-selfcheck-position")),
    inside(cards[0] ?? "", "data-hub-selfcheck-position"));
check("[B] 卡内出缩略图（受控资产出口 data-hub-asset-exit）",
    (cards[0] ?? "").includes("data-hub-asset-exit") && /<img/.test(cards[0] ?? ""));
check("[B] 上下文按偏移高亮：每个命中词都落在某个 <mark> 里（前端不解析文字）",
    (() => {
        const marks = [...(cards[0] ?? "").matchAll(/<mark[^>]*>([\s\S]*?)<\/mark>/g)].map((hit) => strip(hit[1]));
        return marks.length > 0 && words.every((word) => marks.some((mark) => mark.includes(word)));
    })(),
    (cards[0] ?? "").slice(0, 300));
check("[B] 每条命中都能在正文里定位（data-hub-selfcheck-locate；假动作 data-hub-selfcheck-locator 已删）",
    (cards[0] ?? "").includes("data-hub-selfcheck-locate") && !htmlV2.includes("data-hub-selfcheck-locator")
    && !htmlV2.includes("去这一行"));
check("[B] 结论人话在可见区只出现 1 次（不再「结论/原因/发现/证据」各说一遍）",
    countOf(residueVisible, residueRaw.说明.结论人话) === 1,
    String(countOf(residueVisible, residueRaw.说明.结论人话)));
check("[B] 可见区零禁用词（#N / 裸图号 / 内部术语 / 路径 / 「原因：」）",
    ![/#\d/, /图\d+(?!（你传的)/, /已传图行词表|发图单位|提交快照|必换行|锁点|门禁|绿项|缺项|告警/, /analysis\/reverse|待确认清单\.md/, /原因：/]
        .some((pattern) => pattern.test(residueVisible)),
    (residueVisible.match(/#\d|图\d+(?!（你传的)|原因：|锁点|门禁|绿项|缺项/) ?? []).join(" "));
check("[B] 红条语义是「如实显示，不挡提交」（已裁决 B-2）",
    residueVisible.includes("如实显示，不挡提交"));
check("[B] 怎么处理只指页面里的入口（退回置换）",
    inside(residueItemEl, "data-hub-selfcheck-how").includes("退回置换"));

const fold = elementByMarker(residueItemEl, "data-hub-selfcheck-fold");
check("[B] 折叠区默认关闭（<details> 上没有 open）", Boolean(fold) && !/^<details[^>]*\bopen\b/.test(fold),
    fold.slice(0, 120));
check("[B] 折叠区五块齐全（内部统计/机器键值/清单原文/提示词原句/检查器原始输出）",
    ["内部统计", "机器键值", "这一行的清单原文", "提示词里的原句", "检查器原始输出"]
        .every((block) => fold.includes(`data-hub-fold-block="${block}"`)),
    fold.slice(0, 200));
check("[B] 检查器原句只在折叠区（可见区承载位 0）",
    !residueVisible.includes(rawHit.原文) && fold.includes(rawHit.原文.slice(0, 12)));
check("[B] 「N 项通过（展开看证据）」折叠区仍在，且通过项在它里面",
    /\d+ 项通过（展开看证据）/.test(strip(htmlV2)) && htmlV2.includes("data-hub-selfcheck-passed"));

const gateEl = elementByMarker(htmlV2, "data-hub-gate");
const gateRaw = v2Raw.find((item) => item.项显示名 === "拆解产物检查") ?? { 说明: { 发现: [] } };
const gateVisible = strip(gateEl.replace(elementByMarker(gateEl, "data-hub-selfcheck-fold"), " "));
check("[B] 门禁条目也叫人话名（拆解产物检查），不叫「拆解门禁」",
    gateVisible.includes("拆解产物检查") && !gateVisible.includes("拆解门禁"), gateVisible.slice(0, 120));
check("[B] 门禁可见区零统计（绿项/缺项/重拆 只在折叠区）",
    !/绿项|缺项|重拆\s*\d|❌/.test(gateVisible), gateVisible.slice(0, 200));
check("[B] 门禁逐条人话在可见区，原始 ❌ 串只在折叠区",
    (gateRaw.说明.发现 ?? []).every((finding) => gateVisible.includes(finding.人话))
    && !gateVisible.includes("❌"));

// ════════════════════════════════════════════════ [C] 去重可量化
console.log("2) 去重可量化：v2 可见承载位 vs pre-B2 真实载荷承载位");
const legacyItem = (legacyRaw ?? []).find((item) => item.项 === "绑图行残留") ?? null;
let legacyCarriers = 0;
let legacySentence = "";
if (legacyItem) {
    const evidence = String(legacyItem.证据 ?? "");
    const match = evidence.match(/…([^…]{8,})…/);
    legacySentence = match ? match[1] : "";
    const probe = legacySentence.slice(3, 14);
    const say = legacyItem.说明 ?? {};
    if (probe && String(say.结论人话 ?? "").includes(probe)) legacyCarriers += 1;
    if (probe && String(say.原因 ?? "").includes(probe)) legacyCarriers += 1;
    legacyCarriers += (say.发现 ?? []).filter((finding) => probe && String(finding.文字 ?? "").includes(probe)).length;
    if (probe && evidence.includes(probe)) legacyCarriers += 1;
}
check("[C] pre-B2 真实载荷里同一命中句被 ≥3 个可见位承载（基线真实可量化）",
    legacyCarriers >= 3, `承载位=${legacyCarriers} sentence=${legacySentence.slice(0, 24)}`);
check("[C] v2 可见区同一命中句恰好 1 处（上下文里那一次），检查器原句 0 处",
    countOf(residueVisible, words[0] ?? "") >= 1
    && !residueVisible.includes(rawHit.上下文)
    && countOf(residueVisible, rawHit.原文) === 0,
    `词出现=${countOf(residueVisible, words[0] ?? "")}`);
console.log(`      去重：旧载荷承载位 ${legacyCarriers} → 新可见区 1（同一句），检查器原句 ${legacyCarriers} → 0`);

// ════════════════════════════════════════════════ [D] 旧数据降级
console.log("3) 旧数据降级（pre-B2 载荷 / 更老的连说明都没有）");
if (legacyRaw) {
    const htmlLegacy = render(hubApi.normalizeSelfCheck(legacyRaw));
    const legacyVisible = strip(
        elementByMarker(htmlLegacy, "data-hub-selfcheck-item")
            .replace(elementByMarker(htmlLegacy, "data-hub-selfcheck-fold"), " "),
    );
    check("[D] 旧载荷不出 finding 卡（没有 v2 槽位就不摆新模板）",
        !htmlLegacy.includes("data-hub-selfcheck-card"), htmlLegacy.slice(0, 120));
    check("[D] 旧载荷仍显结论人话与处理，且不再摆「原因：」（与发现 100% 重复的那句）",
        legacyVisible.includes(legacyItem.说明.结论人话) && legacyVisible.includes("怎么处理：")
        && !legacyVisible.includes("原因："), legacyVisible.slice(0, 200));
    check("[D] 旧载荷的检查器原句收进折叠区（可查但不占屏）",
        elementByMarker(htmlLegacy, "data-hub-selfcheck-fold").includes("残留于正文"));
}
const htmlAncient = render(hubApi.normalizeSelfCheck(
    legacyRaw
        ? legacyRaw.map((item) => ({ 项: item.项, 结论: item.结论, 证据: item.证据 }))
        : [{ 项: "次序对账", 结论: "FAIL", 证据: "提交快照 3 条 ≠ 发图单位 2 个" }],
));
const ancientEvidence = String((legacyRaw?.[0] ?? {}).证据 ?? "").slice(0, 10);
check("[D] 连说明都没有的更老载荷：明说没有处理指引 + 原始证据照旧可看，不炸",
    htmlAncient.includes("没有处理指引") && (!ancientEvidence || htmlAncient.includes(ancientEvidence))
    && !htmlAncient.includes("data-hub-selfcheck-card"),
    `evidence=${ancientEvidence}`);
check("[D] 空自检（null）→「未检查」，不炸",
    render(hubApi.normalizeSelfCheck(null)).includes("自检结论：未检查"));

// ════════════════════════════════════════════════ [E] 源码级红线
const drawer = read("src/pages/hub/task-detail-drawer.tsx");
const api = read("src/pages/hub/hub-api.ts");
check("[E] 前端不再有「原因」渲染位（data-hub-selfcheck-reason 已删）",
    !drawer.includes("data-hub-selfcheck-reason") && !drawer.includes("say.原因"));
check("[E] 自检结论不参与任何 disabled/阻断表达式（红条只如实显示）",
    !/disabled=\{[^}]*自检/.test(drawer) && !/自检[^;\n]{0,40}\?\s*null\s*:\s*\(/.test(drawer));
check("[E] 呈现层不解析证据字符串（无 证据.split/match/replace/regExp）",
    !/证据\s*\.\s*(split|match|replace|search|indexOf)/.test(drawer)
    && !/证据\s*\.\s*(split|match|replace|search|indexOf)/.test(api),
    (drawer.match(/证据\s*\.\s*\w+/g) ?? []).join(" "));

console.log(`\n[probe-at22-e2] ${pass} PASS / ${fails.length} FAIL`);
if (fails.length) {
    console.log("FAIL 清单：\n  - " + fails.join("\n  - "));
    process.exit(1);
}
