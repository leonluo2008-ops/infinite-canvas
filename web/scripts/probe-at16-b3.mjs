/**
 * AT-16 B3 前端探针（本地免费、不联网、不动真实任务）：**Agent 建议行的视觉锚点**。
 *
 * 一件事：建议行（必须换/建议准备/待核对/可沿用 四类同款）挂画面锚点，锚点**复用**
 * FrameWithPosition / GroupFrame（既有框与比例处理，红线 25 不另画一套框），数据源=
 * asset_cards 已透出的代表帧；无对应帧/无对应卡的行诚实占位、不造图；建议行 #n 与资产卡编号同源。
 *
 * 三组断言：
 *  [A] 真渲染（drawer 的 SuggestionSection 逐字抽出编译，真代码 + SSR）四类场景：
 *      · 有帧有框：本卡框 data-hub-box-own=1 且行号=建议行号、框有真实 px 几何（不是空壳）、
 *        他人框淡显；帧图 src = 既有受控出口换算后的 URL（含 local: 引用 → /api/hub/asset-file）
 *      · 有帧无框：state=no-own-box、**没有任何 own 框**（诚实，不搬他人框）、出「未能标注位置」
 *      · 缺帧：虚线占位 + 「无对应帧」、锚点内**零 <img>**（不造图）
 *      · 组成员：走 GroupFrame 单帧单框，框行号=组主行（两行成员锚点同源同框）、带组键
 *      · 编号映射：锚点行号 == 建议行号 == 资产卡行号；无对应卡的行走占位
 *  [B] 红线 25 静态守卫：几何只有一套（stageSize/containRect/boxRect/layoutLegend 只在两个帧件里用），
 *      锚点只是把两个帧件挂上去；置换区 1:1 调用点保持不变（scale 默认 1，不动既有视觉）。
 *  [C] 真渲染的可见性下限（离线能验的部分）：锚点缩放走 data-hub-frame-scale=0.5（缩略图不是另画的小框）；
 *      真实图片「加载成功（naturalWidth>0）」由 ego 真浏览器探针 ego-at16-b3-anchor.mjs 对真实任务验。
 *
 * 跑法：cd web && node scripts/probe-at16-b3.mjs      （失败 rc=1）
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const WEB = process.cwd();
const CACHE = path.join(WEB, "node_modules", ".cache", "at16-b3");
mkdirSync(CACHE, { recursive: true });
const read = (rel) => readFileSync(path.join(WEB, rel), "utf-8");

let pass = 0;
const fails = [];
function check(name, cond, detail = "") {
    if (cond) {
        pass += 1;
        console.log(`  [PASS] ${name}${detail ? ` —— ${String(detail).slice(0, 240)}` : ""}`);
    } else {
        fails.push(name);
        console.log(`  [FAIL] ${name}${detail ? ` —— ${String(detail).slice(0, 400)}` : ""}`);
    }
}

// ════════════════════════════════════════════════════════════ 夹具（数据形状全部按 hub-api 合同）

const box = (行, 本卡, x = 10, y = 20, w = 40, h = 30, 角标 = "必须换") => ({
    行,
    短名: `资产${行}`,
    角标,
    格: 5,
    "x%": x,
    "y%": y,
    "w%": w,
    "h%": h,
    本卡,
});
const frame = (url, 镜次 = 2, 定位 = "精确", 秒 = 3) => ({ url, 秒, 镜次, 定位 });
const cardBase = (n, extra = {}) => ({
    n,
    类别: "道具",
    短名: `资产${n}`,
    建议: "必须换",
    核心: n <= 2,
    出现时段: [],
    帧: [],
    代表帧: frame("/api/hub/frame/auto-9999/shot-2-3.00.png", 2),
    自身框: true,
    帧内框: [box(n, true), box(n + 20, false)],
    已传图: [],
    描述: `第 ${n} 行的资产描述`,
    同实体疑似: [],
    同实体确认: [],
    画面位置: { 格: 5, 镜次: 2 },
    定位: "精确",
    时段来源: "",
    组: null,
    决策: "",
    ...extra,
});

// 1 有帧有框 / 2 有帧无框 / 3 缺帧 / 4+6 同一确认组 / 5 可沿用 / 7 待核对 / 8 local: 引用（受控出口）
const GROUP_FRAME = frame("/api/hub/frame/auto-9999/group-main.png", 4, "精确", 7);
const group = {
    键: "4-6",
    成员: [4, 6],
    主行: 4,
    组名: "同件道具",
    代表帧: GROUP_FRAME,
    单框: box(4, true, 12, 22, 44, 32),
    单框降级: false,
    镜次并集: [2, 4],
};

const cards = [
    cardBase(1),
    cardBase(2, { 自身框: false, 帧内框: [box(22, false)], 建议: "必须换" }),
    cardBase(3, { 代表帧: frame("", null, "未定位", 0), 帧: [], 帧内框: [], 自身框: false }),
    cardBase(4, { 组: group, 同实体确认: [4, 6], 建议: "建议准备" }),
    cardBase(5, { 建议: "可沿用", 代表帧: frame("/api/hub/frame/auto-9999/keep.png", 1), 帧内框: [box(5, true)] }),
    cardBase(6, { 组: group, 同实体确认: [4, 6], 建议: "建议准备", 帧内框: [box(6, true)] }),
    cardBase(7, { 建议: "", 代表帧: frame("/api/hub/frame/auto-9999/unverified.png", 3), 帧内框: [box(7, true)] }),
    cardBase(8, { 代表帧: frame("local:auto-9999/asset-uploads/0123456789abcdef.png", 5), 帧内框: [box(8, true)] }),
];

const suggestions = [
    { n: 1, 档位: "必须换", 理由: "意图点名换主体", 镜次: [2], 待核对: false },
    { n: 2, 档位: "必须换", 理由: "核心产品要换", 镜次: [3, 4], 待核对: false },
    { n: 3, 档位: "必须换", 理由: "缺帧行也要挂占位", 镜次: [1], 待核对: false },
    { n: 4, 档位: "建议准备", 理由: "组内主行", 镜次: [2, 4], 待核对: false },
    { n: 5, 档位: "可沿用", 理由: "与意图无关", 镜次: [1], 待核对: false },
    { n: 6, 档位: "建议准备", 理由: "组内成员", 镜次: [4], 待核对: false },
    { n: 7, 档位: "", 理由: "", 镜次: [], 待核对: true },
    { n: 8, 档位: "必须换", 理由: "local 引用走受控出口", 镜次: [5], 待核对: false },
    { n: 99, 档位: "必须换", 理由: "清单有建议没有资产卡", 镜次: [1], 待核对: false },
];

const detail = { local_id: "auto-9999", asset_cards: cards, suggestions };

// ════════════════════════════════════════════════════════════ [A] 真渲染
console.log("1) SuggestionSection 真渲染（真代码 + SSR，无打桩）");

const entry = path.join(CACHE, "ssr-entry.tsx");
writeFileSync(
    entry,
    'import { renderToStaticMarkup } from "react-dom/server";\n' +
        'import { SuggestionSection } from "../../../src/pages/hub/task-detail-drawer";\n' +
        "export function renderSuggestions(detail: any): string {\n" +
        "    return renderToStaticMarkup(<SuggestionSection detail={detail} />);\n" +
        "}\n",
);
const aliasPlugin = {
    name: "at16b3-alias",
    setup(build) {
        build.onResolve({ filter: /^@\// }, (args) => {
            const base = path.join(WEB, "src", args.path.slice(2));
            for (const candidate of [base, `${base}.ts`, `${base}.tsx`, path.join(base, "index.ts"), path.join(base, "index.tsx")]) {
                try {
                    if (require("node:fs").statSync(candidate).isFile()) return { path: candidate };
                } catch {
                    /* 试下一个后缀 */
                }
            }
            return { path: base };
        });
    },
};
const ssrOut = path.join(CACHE, "ssr.mjs");
const built = await esbuild.build({
    entryPoints: [entry],
    bundle: true,
    platform: "node",
    format: "esm",
    outfile: ssrOut,
    jsx: "automatic",
    packages: "external",
    plugins: [aliasPlugin],
    define: { "import.meta.env": "{}" },
    logLevel: "silent",
});
check("[A] SuggestionSection 真渲染打包成功（真代码，React/antd 走宿主机）", built.errors.length === 0,
    built.errors.map((err) => `${err.location?.file}:${err.text}`).join("; "));

// 纯 node 里跑真前端模块：补最小浏览器全局（i18n 在 import 期读 localStorage）
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
const html = ssr.renderSuggestions(detail);

/** 从标记处回找起始标签，按同名标签配平取整段。 */
function elementByMarker(source, marker) {
    const at = source.indexOf(marker);
    if (at < 0) return "";
    let start = -1;
    let tag = "";
    for (const name of ["div", "section", "details", "span", "ul", "li", "a", "img"]) {
        const from = source.lastIndexOf(`<${name}`, at);
        if (from >= 0 && (start < 0 || from > start)) {
            start = from;
            tag = name;
        }
    }
    if (start < 0) return "";
    if (tag === "img") return source.slice(start, source.indexOf(">", start) + 1);
    const close = `</${tag}>`;
    let depth = 0;
    let i = start;
    while (i < source.length) {
        if (source.startsWith(`<${tag}`, i)) depth += 1;
        else if (source.startsWith(close, i)) {
            depth -= 1;
            if (depth === 0) return source.slice(start, i + close.length);
        }
        i += 1;
    }
    return source.slice(start);
}
const rowOf = (n) => elementByMarker(html, `data-hub-suggest-row="${n}"`);
const anchorOf = (n) => elementByMarker(html, `data-hub-suggest-anchor="${n}"`);
const attrOf = (segment, key) => {
    const hit = new RegExp(`${key}="([^"]*)"`).exec(segment || "");
    return hit ? hit[1] : "";
};

check("[A] 建议区渲染出全部 9 条建议行",
    detail.suggestions.every((item) => rowOf(item.n)),
    detail.suggestions.filter((item) => !rowOf(item.n)).map((item) => `缺行 #${item.n}`).join(" "));
check("[A] 每行都有视觉锚点（四类行同款 data-hub-suggest-anchor）",
    detail.suggestions.every((item) => anchorOf(item.n)),
    detail.suggestions.filter((item) => !anchorOf(item.n)).map((item) => `缺锚点 #${item.n}`).join(" "));
const sources = detail.suggestions.map((item) => `${item.n}:${attrOf(anchorOf(item.n), "data-hub-suggest-anchor-source")}`);
check("[A] 锚点来源已标注（frame / group / none 三态，无第四种）",
    detail.suggestions.every((item) => ["frame", "group", "none"].includes(attrOf(anchorOf(item.n), "data-hub-suggest-anchor-source"))),
    sources.join(" "));

console.log("2) 有帧有框：本卡框真实存在（行号=建议行号、有 px 几何）");
const a1 = anchorOf(1);
check("[A] #1 走既有 FrameWithPosition（state=precise + 缩略缩放 0.5）",
    attrOf(a1, "data-hub-frame-state") === "precise" && attrOf(a1, "data-hub-frame-scale") === "0.5",
    a1.slice(0, 200));
check("[A] #1 恰一枚 own 框且行号=建议行号（框归属正确）",
    a1.includes('data-hub-box-row="1"') && a1.includes('data-hub-box-own="1"'),
    a1.slice(0, 240));
const ownBox1 = elementByMarker(a1, 'data-hub-box-own="1"');
const boxGeom = /left:\s*([\d.]+)px;\s*top:\s*([\d.]+)px;\s*width:\s*([\d.]+)px;\s*height:\s*([\d.]+)px/.exec(ownBox1);
check("[A] #1 own 框有真实 px 几何（不是只挂了个空的框 div）",
    Boolean(boxGeom) && Number(boxGeom[3]) > 20 && Number(boxGeom[4]) > 20,
    boxGeom ? boxGeom[0] : ownBox1.slice(0, 200));
check("[A] #1 同帧他人框淡显（own=0，行号≠建议行号）",
    a1.includes('data-hub-box-own="0"') && a1.includes('data-hub-box-row="21"'), a1.slice(0, 240));
check("[A] #1 帧图 src 走既有受控出口（/api/hub/frame/... 原样）",
    a1.includes('src="/api/hub/frame/auto-9999/shot-2-3.00.png"'), a1.slice(0, 200));
check("[A] #1 缩略图关掉图例 rail（缩略图里不可读，框+角标照画）", !a1.includes("data-hub-frame-legend"));

console.log("3) 有帧无框：诚实降级，不搬他人框");
const a2 = anchorOf(2);
check("[A] #2 有帧但无自身框 → state=no-own-box", attrOf(a2, "data-hub-frame-state") === "no-own-box",
    a2.slice(0, 220));
check("[A] #2 画面里一个有主框都没有（绝不用他人框充本行）", !a2.includes('data-hub-box-own="1"'),
    a2.slice(0, 240));
check("[A] #2 他人框照画（own=0 行 22 在场）", a2.includes('data-hub-box-row="22"') && a2.includes('data-hub-box-own="0"'));
check("[A] #2 明说「未能标注位置」", a2.includes("未能标注位置"), a2.slice(-260));

console.log("4) 缺帧：诚实占位不造图");
const a3 = anchorOf(3);
check("[A] #3 无对应帧 → 占位（source=none / no-frame）",
    attrOf(a3, "data-hub-suggest-anchor-source") === "none" && attrOf(a3, "data-hub-suggest-no-frame") === "no-frame",
    a3.slice(0, 200));
check("[A] #3 占位写明「无对应帧」且没有伪造帧图", a3.includes("data-hub-suggest-placeholder") && a3.includes("无对应帧") && !a3.includes("<img"),
    a3.slice(0, 240));
check("[A] #3 占位里没有帧舞台（不假装有帧）", !a3.includes("data-hub-frame-stage"));

console.log("5) 组成员：GroupFrame 单帧单框（框=组主行），成员两行同源");
const a4 = anchorOf(4);
const a6 = anchorOf(6);
check("[A] #4/#6 都走组卡锚点（source=group + GroupFrame）",
    attrOf(a4, "data-hub-suggest-anchor-source") === "group" && attrOf(a6, "data-hub-suggest-anchor-source") === "group"
    && a4.includes("data-hub-group-frame") && a6.includes("data-hub-group-frame"),
    `${a4.slice(0, 120)} | ${a6.slice(0, 120)}`);
check("[A] #4 组卡恰一枚框且=主行 4", a4.includes('data-hub-box-row="4"') && (a4.match(/data-hub-box-row=/g) || []).length === 1,
    a4.slice(0, 240));
check("[A] #6（成员，非主行）的框行号=主行 4，不按成员行号画", a6.includes('data-hub-box-row="4"') && !a6.includes('data-hub-box-row="6"'),
    a6.slice(0, 240));
check("[A] #4/#6 锚点带同一组键（归属可核对）",
    attrOf(a4, "data-hub-suggest-group-key") === "4-6" && attrOf(a6, "data-hub-suggest-group-key") === "4-6");
check("[A] #4/#6 帧图同源（组卡=一张代表帧）",
    a4.includes('src="/api/hub/frame/auto-9999/group-main.png"') && a6.includes('src="/api/hub/frame/auto-9999/group-main.png"'));

console.log("6) 编号映射：建议行 #n = 资产卡行号（同源，不二次编号）");
const numRows = [];
for (const item of detail.suggestions) {
    const row = rowOf(item.n);
    if (!row.includes(`>#${item.n}<`)) numRows.push(item.n);
}
check("[A] 每行文本里的 #编号 = 建议行号", numRows.length === 0, `行 ${numRows.join(",")}`);
const ownRowMismatch = [1, 8].filter((n) => !anchorOf(n).includes(`data-hub-box-row="${n}"`));
check("[A] 有自身框的行：锚点 own 框行号 = 该行行号（#1、#8）", ownRowMismatch.length === 0, ownRowMismatch.join(","));
check("[A] 行名来自同一资产卡（#1 短名 资产1）", rowOf(1).includes("资产1") && cardOfNameCheck(), "");
function cardOfNameCheck() {
    return cards.filter((c) => c.短名).every((c) => rowOf(c.n).includes(c.短名));
}

console.log("7) local: 引用经既有受控出口换算（锚点没绕过 hubAssetDisplayUrl）");
const a8 = anchorOf(8);
check("[A] #8 local: 引用 → /api/hub/asset-file/<任务>/<文件>",
    a8.includes('src="/api/hub/asset-file/auto-9999/0123456789abcdef.png"') && !a8.includes('src="local:'),
    a8.slice(0, 220));

console.log("8) 无对应资产卡 / 待核对行：都挂占位不崩");
const a99 = anchorOf(99);
check("[A] #99（有建议没资产卡）→ 占位「无对应资产卡」、零 <img>",
    attrOf(a99, "data-hub-suggest-anchor-source") === "none" && a99.includes("无对应资产卡") && !a99.includes("<img"),
    a99.slice(0, 240));
const a7 = anchorOf(7);
check("[A] #7 待核对行也有同款锚点（不因缺建议少挂锚点）",
    attrOf(a7, "data-hub-suggest-anchor-source") === "frame" && a7.includes("<img"), a7.slice(0, 160));
check("[A] 待核对行文案不变（尚无建议·待核对）", rowOf(7).includes("尚无建议·待核对"));

console.log("9) 四类行锚点规格一致（同一套缩略参数，不按档位另做）");
const toneOfRow = (n) => attrOf(rowOf(n), "data-hub-suggest-tone");
check("[A] 四类行各自都有行（must/prepare/unverified/keep）",
    ["must", "prepare", "unverified", "keep"].every((tone) => detail.suggestions.some((item) => toneOfRow(item.n) === tone)),
    detail.suggestions.map((item) => `${item.n}:${toneOfRow(item.n)}`).join(" "));
const scaledFrames = detail.suggestions
    .filter((item) => attrOf(anchorOf(item.n), "data-hub-suggest-anchor-source") !== "none")
    .map((item) => attrOf(anchorOf(item.n), "data-hub-frame-scale"));
check("[A] 全部有帧锚点都是同一缩放（0.5），没有按行另造尺寸",
    scaledFrames.length > 0 && scaledFrames.every((value) => value === "0.5"), scaledFrames.join(","));
check("[A] 可沿用行（折叠区）同样挂锚点——折叠不改变渲染", toneOfRow(5) === "keep" && anchorOf(5).includes("<img"));
const placeholders = (html.match(/data-hub-suggest-placeholder/g) || []).length;
check("[A] 占位只出现在真缺帧/缺卡的行（不多不少 2 处）", placeholders === 2, String(placeholders));

// ════════════════════════════════════════════════════════════ [B] 红线 25 静态守卫
console.log("10) 红线 25：几何只有一套，锚点只挂既有件");
const src = read("src/pages/hub/task-detail-drawer.tsx");
check("[B] 帧件各自只有一个实现（不另建第二个 Frame 组件）",
    (src.match(/function FrameWithPosition\(/g) || []).length === 1
    && (src.match(/function GroupFrame\(/g) || []).length === 1,
    `FrameWithPosition=${(src.match(/function FrameWithPosition\(/g) || []).length} GroupFrame=${(src.match(/function GroupFrame\(/g) || []).length}`);
const between = (text, startMark, endMark) => {
    const from = text.indexOf(startMark);
    const to = text.indexOf(endMark, from + startMark.length);
    return from >= 0 && to > from ? text.slice(from, to) : "";
};
const anchorBody = between(src, "function SuggestionAnchor(", "function NoFrameBox(");
check("[B] 锚点体里只调既有帧件（FrameWithPosition + GroupFrame），无几何重算",
    anchorBody.includes("<FrameWithPosition") && anchorBody.includes("<GroupFrame")
    && !/stageSize\(|containRect\(|boxRect\(|layoutLegend\(/.test(anchorBody),
    anchorBody.slice(0, 200).replace(/\s+/g, " "));
check("[B] 帧取图口径单一（frameUrlOf 被帧件与锚点共用，禁第二套解析）",
    (src.match(/frameUrlOf\(/g) || []).length >= 3
    && between(src, "function FrameWithPosition(", "const [ratio").includes("frameUrlOf(card)")
    && anchorBody.includes("frameUrlOf(card)"),
    `frameUrlOf 调用 ${(src.match(/frameUrlOf\(/g) || []).length} 处`);
check("[B] 置换区调用点保持 1:1（scale 默认 1，既有视觉零变化）",
    src.includes('<FrameWithPosition card={card} className="shrink-0" />')
    && src.includes("<GroupFrame group={group} className=\"shrink-0\" />"));
check("[B] 锚点没有绕过受控出口直接铺 <img src>（不新开取图路径）", !/anchorBody[\s\S]*<img/.test(anchorBody) && !anchorBody.includes("src={"));
check("[B] 占位是虚线空框 + 明说取不到帧（不造图、不搬别的行帧）",
    /data-hub-suggest-placeholder/.test(src) && src.includes("该行没有可定位的画面帧（不造图）")
    && /border-dashed/.test(between(src, "function NoFrameBox(", "function SuggestionRow(")));

// ════════════════════════════════════════════════════════════ 汇总
console.log(`\nprobe-at16-b3: ${pass} PASS, ${fails.length} FAIL`);
for (const item of fails) console.log(`  FAIL ${item}`);
process.exit(fails.length ? 1 : 0);
