/**
 * AT-16 B1a 前端探针（本地免费，不联网、不动真实任务）：**受控资产出口 + 可失败的防再漏检查**。
 *
 * 背景缺陷：hub 任务详情上传资产后图不显示（灰底 + alt「#N 换图」）——drawer 渲染点
 * （原 UploadedThumb：`url` 经 props 间接传入，组件内 `href={url}` / `src={url}`）没过
 * `hubAssetDisplayUrl`（显示层唯一换算点），于是 `local:任务号/asset-uploads/xx.png` 被直接当
 * 图片 URL 用 → 浏览器取不到 → 灰底。服务端/数据/vite 代理全绿，纯前端显示层缺陷。
 *
 * 本探针三件事：
 *   [1] 防再漏静态检查器 **可失败性自证**：冻结坏例必红、修复夹具必绿；
 *       并复核实证「旧字符串检查（probe-at14-b3.mjs:98-99）会放过该坏代码」。
 *   [2] 真文件检查：drawer 全文扫描 0 泄漏 + 白名单标记区结构守卫 + 接线断言。
 *   [3] 真渲染：把 drawer 里 hub-asset-exit 标记区的**真实代码逐字抽出**编译，用
 *       react-dom/server 真渲染 → 断言 `<img src>` 与 `<a href>` 都是 /api/hub/asset-file/...
 *       换算 URL、健康态三态渲染不变（local/ok 出图、expired/unknown 出占位不出图）。
 *
 * 跑法：cd web && node scripts/probe-at16-b1a.mjs [--ref <真实local引用> --expect <换算URL>]
 *   （端到端模式由 wanrepro/automation/probe_at16_b1a.py 传真实上传得到的引用进来）
 * 判据全对才 exit 0；任何一步 FAIL 如实红（禁子串判通过、禁恒真检查）。
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const WEB = process.cwd();
const CACHE = path.join(WEB, "node_modules", ".cache", "at16-b1a");
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

// ════════════════════════════════════════════════════════════ [1] 静态检查器本体
/** 把注释/字符串**内容**替换成空格（长度与换行保持不变）→ 结构与原文下标一一对应。
 *  目的：`<a title="a>b">` 里的 `>` 或注释里的 `src=` 不会干扰标签/属性扫描。 */
function maskSource(src) {
    const out = [];
    let i = 0;
    const n = src.length;
    const blank = (ch) => (ch === "\n" || ch === "\r" ? ch : " ");
    while (i < n) {
        const c = src[i];
        const d = src[i + 1];
        if (c === "/" && d === "/") {
            while (i < n && src[i] !== "\n") { out.push(" "); i += 1; }
            continue;
        }
        if (c === "/" && d === "*") {
            out.push(" ", " ");
            i += 2;
            while (i < n && !(src[i] === "*" && src[i + 1] === "/")) { out.push(blank(src[i])); i += 1; }
            if (i < n) { out.push(" ", " "); i += 2; }
            continue;
        }
        if (c === '"' || c === "'" || c === "`") {
            const quote = c;
            out.push(quote);
            i += 1;
            while (i < n && src[i] !== quote) {
                if (src[i] === "\\") { out.push(" ", " "); i += 2; continue; }
                out.push(blank(src[i]));
                i += 1;
            }
            if (i < n) { out.push(quote); i += 1; }
            continue;
        }
        out.push(c);
        i += 1;
    }
    return out.join("");
}

const lineAt = (src, index) => src.slice(0, index).split("\n").length;

/** 判红判据中的「资产来源标识」：以 url/ref/image/thumb/preview 结尾的标识（含 `.url` 成员形态），
 *  如 `url` / `uploadedRef` / `card.代表帧.url` / `imageUrl`。
 *  `hubOutputUrl` 是成片输出路由（/api/hub/file/<id>，非任务资产引用），进 SAFE_EXPR_ALLOW 显式豁免——
 *  豁免必须具名，不许用「带 Url 就放过」这类模糊规则。 */
const ASSET_IDENT_RE = /(?<![\w$])[A-Za-z0-9_]*(?:url|ref|image|thumb|preview)\b/i;
/** 已知非资产引用的具名豁免（表达式里出现即视为安全）。 */
const SAFE_EXPR_ALLOW = [/hubOutputUrl\s*\(/];
/** 合规换算调用（= 受控出口内部允许的取值面）。 */
const CONVERTER_RES = [/hubAssetDisplayUrl\s*\(/, /hubAssetRefView\s*\(/];

/** 扫一个 TSX/源码里的图/链节点与 src/href 赋值。
 *  节点范围：原生 `<img>` / `<a>` + 组件形式的图片件（`<AntdImage>` / `<Image>` / `<Img>`）——
 *  antd `Image` 才是本仓库真正渲染 `<img>` 的件（drawer 帧图就是它），只扫原生 `<img>` 会留洞。 */
function scanTags(masked, raw) {
    const tags = [];
    const TAG_RE = /<(img|a|[A-Za-z]*(?:Image|Img))\b/g;
    let m;
    while ((m = TAG_RE.exec(masked))) {
        const start = m.index;
        let i = start + 1 + m[1].length;
        let depth = 0;
        for (; i < masked.length; i += 1) {
            const c = masked[i];
            if (c === "{") depth += 1;
            else if (c === "}") depth -= 1;
            else if (c === ">" && depth === 0) break;
        }
        const end = i;
        const attrs = [];
        let d = 0;
        for (let j = start; j < end; j += 1) {
            const c = masked[j];
            if (c === "{") { d += 1; continue; }
            if (c === "}") { d -= 1; continue; }
            if (d !== 0) continue;
            for (const attr of ["src", "href"]) {
                if (!masked.startsWith(attr, j)) continue;
                if (/[\w$-]/.test(masked[j - 1] || "")) continue;      // data-src / xhref 不算
                let k = j + attr.length;
                while (/\s/.test(masked[k] || "")) k += 1;
                if (masked[k] !== "=") continue;
                k += 1;
                while (/\s/.test(masked[k] || "")) k += 1;
                if (masked[k] === "{") {
                    let dd = 0;
                    let q = k;
                    for (; q < masked.length; q += 1) {
                        if (masked[q] === "{") dd += 1;
                        else if (masked[q] === "}") { dd -= 1; if (dd === 0) break; }
                    }
                    attrs.push({ attr, expr: raw.slice(k + 1, q), line: lineAt(raw, j) });
                } else {
                    attrs.push({ attr, literal: true, line: lineAt(raw, j) });
                }
            }
        }
        tags.push({ kind: m[1], start, end: end + 1, line: lineAt(raw, start), attrs });
        TAG_RE.lastIndex = end + 1;
    }
    return tags;
}

/**
 * 防再漏检查器（可失败的静态规则）：
 *   白名单标记区（`hub-asset-exit:begin/end`）之外的 `<img src>` / `<a href>`，
 *   禁止把「资产来源表达式」直接赋给 src=/href=；唯一合规形态是过换算函数
 *   （hubAssetDisplayUrl / hubAssetRefView）。
 *   覆盖形态：① props 间接传值（`{ url }` 解构 → `src={url}`）② 成员访问（`src={card.代表帧.url}`）。
 * @returns {{leaks: Array, tags: number, region: null|{from:number,to:number}, guards: Array<string>}}
 */
function scanAssetUrlLeaks(source, { name = "target", requireMarkers = false, strict = false } = {}) {
    const masked = maskSource(source);
    const guards = [];
    const begins = [];
    const ends = [];
    for (let idx = source.indexOf("hub-asset-exit:begin"); idx !== -1; idx = source.indexOf("hub-asset-exit:begin", idx + 1)) begins.push(idx);
    for (let idx = source.indexOf("hub-asset-exit:end"); idx !== -1; idx = source.indexOf("hub-asset-exit:end", idx + 1)) ends.push(idx);

    let region = null;
    if (requireMarkers && (begins.length !== 1 || ends.length !== 1)) {
        guards.push(`${name}: 受控出口标记必须恰一对（begin=${begins.length}, end=${ends.length}）`);
    } else if (begins.length === 1 && ends.length === 1 && begins[0] < ends[0]) {
        // 标记区 = begin 所在注释块起点 … end 所在注释块终点
        const from = source.lastIndexOf("/*", begins[0]);
        const to = source.indexOf("*/", ends[0]) + 2;
        region = { from, to };
    } else if (begins.length || ends.length) {
        guards.push(`${name}: 标记不成对/顺序颠倒（begin=${begins.length}, end=${ends.length}）`);
    }

    const tags = scanTags(masked, source);
    const leaks = [];
    for (const tag of tags) {
        const inRegion = region && tag.start >= region.from && tag.start < region.to;
        if (inRegion) continue;
        for (const a of tag.attrs) {
            if (a.literal || typeof a.expr !== "string") continue;
            const code = maskSource(a.expr);                 // 去注释/字符串后再判标识
            if (CONVERTER_RES.some((re) => re.test(code))) continue;   // 已过换算点 → 合规
            if (SAFE_EXPR_ALLOW.some((re) => re.test(code))) continue; // 具名豁免（成片输出路由等）
            const hit = ASSET_IDENT_RE.exec(code);
            if (!hit) continue;
            leaks.push({ file: name, line: a.line, attr: a.attr, expr: a.expr.trim().replace(/\s+/g, " "), ident: hit[0] });
        }
    }

    if (strict) {
        if (region) {
            const span = lineAt(source, region.to) - lineAt(source, region.from) + 1;
            if (span > 60) guards.push(`${name}: 受控出口标记区过大（${span} 行 > 60 行）——白名单不许吞文件`);
            const inside = tags.filter((t) => t.start >= region.from && t.start < region.to);
            if (!inside.some((t) => t.kind === "img")) guards.push(`${name}: 标记区内没有 <img>（受控出口形同虚设）`);
        }
        const outside = tags.filter((t) => !(region && t.start >= region.from && t.start < region.to));
        if (outside.length < 1) guards.push(`${name}: 标记区把整个文件的标签都吞了（区外 0 个）——检查将失去意义`);
    }
    return { leaks, tags: tags.length, region, guards };
}

// ════════════════════════════════════════════════════════════ [2] 自证：坏例红 / 修后绿
console.log("1) 检查器可失败性自证（冻结坏例必红、修复形态必绿）");
const BAD_PROP = "scripts/fixtures/at16-b1a/frozen-bad-uploaded-thumb.tsx";
const BAD_MEMBER = "scripts/fixtures/at16-b1a/frozen-bad-member-url.tsx";
const FIXED = "scripts/fixtures/at16-b1a/fixed-hub-asset-thumb.tsx";

const badProp = scanAssetUrlLeaks(read(BAD_PROP), { name: "bad-props" });
check("[1] 冻结坏例（props 间接传值 url → src/href）判红", badProp.leaks.length >= 2,
    JSON.stringify(badProp.leaks));
check("[1] 坏例两处裸赋值都被点名（href + src）",
    badProp.leaks.some((l) => l.attr === "href") && badProp.leaks.some((l) => l.attr === "src"),
    JSON.stringify(badProp.leaks.map((l) => `${l.attr}@${l.line}`)));
check("[1] 坏例点名到「props 间接传入的 url 标识」", badProp.leaks.every((l) => /^url$/.test(l.ident)),
    JSON.stringify(badProp.leaks.map((l) => l.ident)));

const badMember = scanAssetUrlLeaks(read(BAD_MEMBER), { name: "bad-member" });
check("[1] 冻结坏例②（成员形态 card.代表帧.url → src）判红", badMember.leaks.length === 1,
    JSON.stringify(badMember.leaks));

const fixedScan = scanAssetUrlLeaks(read(FIXED), { name: "fixed" });
check("[1] 修复形态（受控出口 + 换算调用）判绿", fixedScan.leaks.length === 0, JSON.stringify(fixedScan.leaks));
check("[1] 修复形态确实含标记区（不是靠「没标签」蒙绿）",
    Boolean(fixedScan.region) && fixedScan.tags >= 3, `tags=${fixedScan.tags}`);

// 复核实证：旧检查（probe-at14-b3.mjs:98-99 的字符串比对）会放过坏代码
const legacyShape = read(BAD_PROP) + '\nconst usage = uploaded.map((url) => <UploadedThumb key={url} url={url} state={states[url] || ""} n={n} />);\n';
const legacyOldCheckPasses = legacyShape.includes("<UploadedThumb") && !legacyShape.includes('<a key={url} href={url}');
check("[1] 实证：旧字符串检查（含 <UploadedThumb 且不含 '<a key={url} href={url}'）**放过了**坏代码",
    legacyOldCheckPasses === true, "若为 false 说明旧检查已改，本条需重新取证");
check("[1] 同一份坏代码新检查判红（旧检查的漏网被堵住）",
    scanAssetUrlLeaks(legacyShape, { name: "legacy-shape" }).leaks.length >= 2,
    JSON.stringify(scanAssetUrlLeaks(legacyShape, { name: "legacy-shape" }).leaks));

// ════════════════════════════════════════════════════════════ [3] 真文件：drawer 零泄漏 + 接线
console.log("2) 真文件扫描：web/src/pages/hub/task-detail-drawer.tsx");
const DRAWER = "src/pages/hub/task-detail-drawer.tsx";
const drawerSrc = read(DRAWER);
const drawerScan = scanAssetUrlLeaks(drawerSrc, { name: "drawer", requireMarkers: true, strict: true });
check("[2] drawer 白名单区外零泄漏（所有 assets url 渲染走受控出口/换算点）",
    drawerScan.leaks.length === 0, JSON.stringify(drawerScan.leaks));
check("[2] 白名单结构守卫全过（成对标记、区域≤60 行、区内有 img、区外仍有标签）",
    drawerScan.guards.length === 0, JSON.stringify(drawerScan.guards));
check("[2] 受控出口组件已导出（export function UploadedThumb，供探针真渲染同一份代码）",
    /export function UploadedThumb\s*\(/.test(drawerSrc));
check("[2] 已传图缩略走受控出口（<UploadedThumb assetRef= …，AT-14 B3 既有接线不断）",
    drawerSrc.includes("<UploadedThumb") && /<UploadedThumb\s+key=\{assetRef\}/.test(drawerSrc));
check("[2] 健康态按 **ref 原串** 查键（states[assetRef]，不是换算后 URL）",
    /states\[\s*assetRef\s*\]/.test(drawerSrc) && !/states\[\s*hubAssetDisplayUrl/.test(drawerSrc));
check("[2] 帧图（代表帧）同样过换算点（AntdImage src=hubAssetDisplayUrl）",
    (drawerSrc.match(/src=\{hubAssetDisplayUrl\(url\)\}/g) || []).length >= 2);

// 牙口自证（真文件变异）：把一处换算改回裸 url → 必红（防「检查器恒绿」）
const mutatedDrawer = drawerSrc.replace("src={hubAssetDisplayUrl(url)}", "src={url}");
check("[2] 检查器对真文件有牙：一处换算回退成裸 src={url} → 必判红",
    mutatedDrawer !== drawerSrc
    && scanAssetUrlLeaks(mutatedDrawer, { name: "drawer-mutated", requireMarkers: true, strict: true }).leaks.length >= 1,
    JSON.stringify(scanAssetUrlLeaks(mutatedDrawer, { name: "drawer-mutated", requireMarkers: true, strict: true }).leaks));

// ════════════════════════════════════════════════════════════ [4] 真函数 + 真渲染
console.log("3) 真函数与真渲染（hub-api 换算 + hub-asset-exit 标记区逐字抽出编译渲染）");
const apiBundle = path.join(CACHE, "hub-api.bundle.mjs");
const apiBuilt = await esbuild.build({
    entryPoints: [path.join(WEB, "src/pages/hub/hub-api.ts")],
    bundle: true, format: "esm", platform: "node", outfile: apiBundle, logLevel: "silent",
    define: { "import.meta.env": "{}" },
});
check("[3] esbuild 打包 hub-api.ts 成功", apiBuilt.errors.length === 0, apiBuilt.errors.map((e) => e.text).join("; "));
// AT-16 B5a-2：hub-api 参与 UI 模块图（快捷入口复用 gen-settings 的准入校验 → store），
// 纯 node 里 import 前必须先补最小浏览器全局（store / i18n 在 import 期读 localStorage）。
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
const REF = "local:auto-9501/asset-uploads/e955c2ea7800b90c.png";
const EXPECT_LOCAL = "/api/hub/asset-file/auto-9501/e955c2ea7800b90c.png";
const OLD_URL = "https://h.uguu.se/WYrMMuFR.png";
check("[3] hubAssetRefView(local 引用) → src/href 均为换算 URL、ref 原串带回",
    (() => { const v = hubApi.hubAssetRefView(REF); return v.src === EXPECT_LOCAL && v.href === EXPECT_LOCAL && v.ref === REF; })(),
    JSON.stringify(hubApi.hubAssetRefView(REF)));
check("[3] hubAssetRefView(旧公网直链) → 原样透传（老任务不吃亏）",
    (() => { const v = hubApi.hubAssetRefView(OLD_URL); return v.src === OLD_URL && v.href === OLD_URL; })());

// 真实代码逐字抽出（标记区 = 唯一受控出口的源码本体）
const bIdx = drawerSrc.indexOf("hub-asset-exit:begin");
const eIdx = drawerSrc.indexOf("hub-asset-exit:end");
const regionSrc = drawerSrc.slice(drawerSrc.lastIndexOf("/*", bIdx), drawerSrc.indexOf("*/", eIdx) + 2);
check("[3] 从 drawer 真源码抽出受控出口代码（含标记与组件本体）",
    regionSrc.includes("hub-asset-exit:begin") && regionSrc.includes("export function UploadedThumb") && regionSrc.includes("hubAssetRefView"),
    `${regionSrc.length} 字节`);

const entry = path.join(CACHE, "ssr-entry.tsx");
// 相对路径导入：node_modules/.cache/at16-b1a → web/（三级上跳），不写绝对路径（Windows 盘符进模板串易踩语法坑）
writeFileSync(entry,
    'import { renderToStaticMarkup } from "react-dom/server";\n' +
    'import { hubAssetRefView } from "../../../src/pages/hub/hub-api";\n' +
    regionSrc + "\n" +
    "export function renderThumb(p: { assetRef: string; state?: string; alt: string }): string {\n" +
    "    return renderToStaticMarkup(<UploadedThumb {...p} />);\n" +
    "}\n");
const ssrOut = path.join(CACHE, "ssr.mjs");
const ssrBuilt = await esbuild.build({
    entryPoints: [entry], bundle: true, platform: "node", format: "esm", outfile: ssrOut,
    jsx: "automatic", packages: "external", define: { "import.meta.env": "{}" }, logLevel: "silent",
});
check("[3] 受控出口真渲染打包成功（react/react-dom 走宿主机，不重复打一份）",
    ssrBuilt.errors.length === 0, ssrBuilt.errors.map((e) => e.text).join("; "));
const ssr = await import(pathToFileURL(ssrOut).href + `?t=${Date.now()}`);

const htmlLocal = ssr.renderThumb({ assetRef: REF, state: "local", alt: "#1 换图" });
/** 取指定标签上的属性值（SSR 输出里有 React 注入的 `<link rel="preload">`，不能用全文首个同名属性抓）。 */
const attrOf = (html, tag, attr) => {
    const m = new RegExp(`<${tag}\\b[^>]*\\b${attr}="([^"]*)"`).exec(html);
    return m ? m[1] : "";
};
check("[3] 真渲染：img src = 换算 URL（缩略图即显，不是 local: 原串）",
    attrOf(htmlLocal, "img", "src") === EXPECT_LOCAL, htmlLocal.slice(0, 260));
check("[3] 真渲染：点击开大图 a href = 换算 URL",
    attrOf(htmlLocal, "a", "href") === EXPECT_LOCAL, htmlLocal.slice(0, 260));
check("[3] 真渲染：缩略块带受控出口标记属性（DOM 可断言）",
    htmlLocal.includes('data-hub-asset-exit="img"') && htmlLocal.includes('data-hub-asset-exit="link"'));
check("[3] 真渲染：不再把 local: 原串写进 src/href",
    !htmlLocal.includes("local:") , htmlLocal.slice(0, 200));

const htmlHttp = ssr.renderThumb({ assetRef: OLD_URL, state: "ok", alt: "#2 换图" });
check("[3] 真渲染：旧公网直链原样出图（三态 ok 不误伤）",
    attrOf(htmlHttp, "img", "src") === OLD_URL && attrOf(htmlHttp, "a", "href") === OLD_URL, htmlHttp.slice(0, 200));

for (const [state, text] of [["expired", "图已过期，请重传"], ["unknown", "图片不可用，请重试"]]) {
    const html = ssr.renderThumb({ assetRef: REF, state, alt: "#1 换图" });
    check(`[3] 真渲染：${state} 态出占位（${text}）且不出 <img>`,
        html.includes(`data-hub-asset-state="${state}"`) && html.includes(text) && !html.includes("<img"),
        html.slice(0, 240));
}
const htmlNoState = ssr.renderThumb({ assetRef: REF, alt: "#1 换图" });
check("[3] 真渲染：缺键（未探测）照常出图，不误判失效",
    attrOf(htmlNoState, "img", "src") === EXPECT_LOCAL && !htmlNoState.includes("data-hub-asset-state"), htmlNoState.slice(0, 200));

// 白名单不是免死金牌：受控出口**本体**被改坏（src={assetRef}）时静态检查按标记放行，
// 但真渲染这一层必须红——两层互补，标记区内的正确性只由渲染断言守。
const brokenRegion = regionSrc.split("src={view.src}").join("src={assetRef}");
if (brokenRegion !== regionSrc) {
    const entryBroken = path.join(CACHE, "ssr-entry-broken.tsx");
    writeFileSync(entryBroken,
        'import { renderToStaticMarkup } from "react-dom/server";\n' +
        'import { hubAssetRefView } from "../../../src/pages/hub/hub-api";\n' +
        brokenRegion + "\n" +
        "export function renderThumb(p: { assetRef: string; state?: string; alt: string }): string {\n" +
        "    return renderToStaticMarkup(<UploadedThumb {...p} />);\n" +
        "}\n");
    const ssrBrokenOut = path.join(CACHE, "ssr-broken.mjs");
    await esbuild.build({
        entryPoints: [entryBroken], bundle: true, platform: "node", format: "esm", outfile: ssrBrokenOut,
        jsx: "automatic", packages: "external", define: { "import.meta.env": "{}" }, logLevel: "silent",
    });
    const ssrBroken = await import(pathToFileURL(ssrBrokenOut).href + `?t=${Date.now()}`);
    const htmlBroken = ssrBroken.renderThumb({ assetRef: REF, state: "local", alt: "#1 换图" });
    check("[3] 受控出口本体被改坏（src={assetRef}）→ 真渲染必红（白名单不免死，两层互补）",
        attrOf(htmlBroken, "img", "src") === REF && htmlBroken.includes("local:"), htmlBroken.slice(0, 220));
} else {
    check("[3] 受控出口本体的渲染断言夹具可造（region 里 src={view.src} 存在）", false, "夹具变异点找不到，渲染兜底层失效");
}

// ── 端到端模式：探针把「真实上传得到的 local 引用」传进来，断言渲染与换算一致
const argv = process.argv.slice(2);
const argVal = (flag) => { const i = argv.indexOf(flag); return i >= 0 ? argv[i + 1] : ""; };
const realRef = argVal("--ref");
if (realRef) {
    const expect = argVal("--expect");
    const html = ssr.renderThumb({ assetRef: realRef, state: "local", alt: "#1 换图" });
    check(`[4] 端到端：真实上传引用 ${realRef} 渲染出的 img src == 换算 URL`,
        Boolean(expect) && attrOf(html, "img", "src") === expect, `expect=${expect} got=${attrOf(html, "img", "src")}`);
    check("[4] 端到端：同一引用的大图 href 与 img src 同源",
        attrOf(html, "a", "href") === attrOf(html, "img", "src"), html.slice(0, 200));
}

console.log(`\nprobe-at16-b1a: ${pass} PASS, ${fails.length} FAIL`);
for (const item of fails) console.log(`  FAIL ${item}`);
process.exit(fails.length ? 1 : 0);
