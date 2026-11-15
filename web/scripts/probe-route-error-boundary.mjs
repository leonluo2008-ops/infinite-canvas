/**
 * 路由全局兜底页探针（node 层，esbuild 打包生产模块，零网络、零浏览器）。
 *
 * 跑法：cd web && node scripts/probe-route-error-boundary.mjs
 *
 * 被测对象 = 生产同一份实现：
 *   - `web/src/lib/route-error.ts`（异常归因：stale / notFound / forbidden / unknown）
 *   - `web/src/i18n/locales/zh-CN.ts` / `en-US.ts`（人话文案）
 *   - `web/src/components/layout/route-error-page.tsx`（兜底页：只出人话，不吐原始异常）
 *
 * 分段：
 *   A 归因矩阵：chunk 失效的各种真实措辞（含被 cause 包一层、命名 Error）→ stale；路由 404/403 优先于
 *     文案匹配；乱入值（null/数字/空对象）不炸、归 unknown
 *   B 人话文案：中英四类齐全、中文里不出现英文开发术语、不留占位符、stale 明确引导「刷新」
 *   C 兜底页不外泄原始异常：静态检查组件不读 error.message/stack，只经 t() + routeErrorKind 取文案
 *   D 路由挂载：生产 router.tsx 的顶层布局路由与通配路由都挂 RouteErrorPage（DOM 层另有真页面探针复核）
 *
 * 失败 rc=1（禁降级、禁跳过）。
 */
import { mkdirSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const bundleDir = path.resolve("node_modules/.probe-route-error");
mkdirSync(bundleDir, { recursive: true });

let pass = 0;
const fails = [];
const results = [];
let section = "A";
const setSection = (next) => {
    section = next;
};
function check(name, condition, detail = "") {
    const ok = Boolean(condition);
    results.push({ section, name, ok, detail: ok ? "" : String(detail) });
    if (ok) pass += 1;
    else fails.push(`[${section}] ${name}${detail ? ` — ${detail}` : ""}`);
    console.log(`${ok ? "PASS" : "FAIL"} [${section}] ${name}${ok ? "" : ` — ${detail}`}`);
}

async function bundle(entry, outName) {
    const outFile = path.join(bundleDir, outName);
    await esbuild.build({ entryPoints: [path.resolve(entry)], outfile: outFile, bundle: true, platform: "node", format: "cjs", logLevel: "silent" });
    return import(`file://${outFile.replace(/\\/g, "/")}`);
}

const routeError = await bundle("src/lib/route-error.ts", "route-error.cjs");
/** esbuild 把「只有 default 导出」的 ESM 打成 CJS 后，再 import 回来会多一层 default（node 的 CJS 互操作）。 */
const defaultDict = (mod) => mod.default?.default ?? mod.default ?? mod;
const zh = defaultDict(await bundle("src/i18n/locales/zh-CN.ts", "zh.cjs"));
const en = defaultDict(await bundle("src/i18n/locales/en-US.ts", "en.cjs"));

// ---------------------------------------------------------------- A 归因矩阵
setSection("A");
const { routeErrorKind, isStaleModuleError } = routeError;

const chunkSamples = [
    ["Chromium 动态 import 取不到（所有者撞到的原文）", new TypeError("Failed to fetch dynamically imported module: http://127.0.0.1:3000/node_modules/.vite/deps/highlighted-body-OFNGDK62-HR3YGE2B.js?v=7fc1650b")],
    ["Firefox/Safari 措辞", new TypeError("error loading dynamically imported module")],
    ["Safari 模块脚本失败", new TypeError("Importing a module script failed.")],
    ["webpack 命名错误", Object.assign(new Error("Loading chunk 12 failed."), { name: "ChunkLoadError" })],
    ["CSS 预加载失败", new Error("Unable to preload CSS for /assets/index-abc.css")],
    ["被 cause 包一层", new Error("页面加载失败", { cause: new TypeError("Failed to fetch dynamically imported module: /x/highlighted-body-1.js") })],
    ["字符串形态", "Failed to fetch dynamically imported module: /x/highlighted-body-2.js"],
    ["大小写混杂", new Error("FAILED TO FETCH DYNAMICALLY IMPORTED MODULE: /x/y.js")],
];
for (const [label, sample] of chunkSamples) {
    check(`A chunk 失效 → stale：${label}`, routeErrorKind(sample) === "stale", `got ${routeErrorKind(sample)}`);
}

check("A 路由 404（ErrorResponse 形态）→ notFound", routeErrorKind({ status: 404, statusText: "Not Found", data: "路由不存在" }) === "notFound");
check("A 路由 403/401 → forbidden", routeErrorKind({ status: 403, statusText: "Forbidden", data: null }) === "forbidden" && routeErrorKind({ status: 401, statusText: "Unauthorized", data: null }) === "forbidden");
check(
    "A 404 优先于 chunk 文案（HTTP 状态先判，不被文案带偏）",
    routeErrorKind({ status: 404, statusText: "Not Found", data: new Error("Failed to fetch dynamically imported module: /x.js") }) === "notFound",
);
check("A 普通渲染异常 → unknown（不冒充「版本过期」）", routeErrorKind(new Error("Cannot read properties of undefined (reading 'map')")) === "unknown");
check(
    "A 乱入值不炸：null / undefined / 数字 / 空对象 / 空字符串 → unknown",
    [null, undefined, 0, 42, {}, "", []].every((value) => {
        try {
            return routeErrorKind(value) === "unknown";
        } catch {
            return false;
        }
    }),
);
check("A 自指 cause 不死循环（cause === error）", (() => {
    const error = new Error("普通错误");
    error.cause = error;
    return routeErrorKind(error) === "unknown";
})());
check("A isStaleModuleError 单独可用（组件外复用时行为一致）", isStaleModuleError(chunkSamples[0][1]) === true && isStaleModuleError(new Error("boom")) === false);

// ---------------------------------------------------------------- B 人话文案
setSection("B");
const kinds = ["stale", "notFound", "forbidden", "unknown"];
const copyComplete = (dict) =>
    kinds.every((kind) => {
        const entry = dict.routeError?.[kind];
        return entry && typeof entry.title === "string" && entry.title.trim() && typeof entry.description === "string" && entry.description.trim() && typeof entry.hint === "string";
    }) && typeof dict.routeError?.refresh === "string" && dict.routeError.refresh.trim() && typeof dict.routeError?.home === "string" && dict.routeError.home.trim();

check("B 中文四类文案齐全（title/description/hint + refresh/home）", copyComplete(zh), JSON.stringify(Object.keys(zh.routeError || {})));
check("B 英文四类文案齐全（i18n 不留洞）", copyComplete(en), JSON.stringify(Object.keys(en.routeError || {})));
check("B 中文 stale 标题明确引导「刷新」", /刷新/.test(zh.routeError.stale.title) && /刷新/.test(zh.routeError.stale.description), JSON.stringify(zh.routeError.stale));
check("B 中文刷新按钮文案含「刷新」", /刷新/.test(zh.routeError.refresh), zh.routeError.refresh);

const devJargon = /Failed to fetch|ChunkLoad|dynamically imported|undefined|Error:|stack|Promise|module/i;
const zhFlat = JSON.stringify(zh.routeError);
check("B 中文文案里没有英文开发术语（不给用户看堆栈/报错原文）", devJargon.test(zhFlat) === false, zhFlat.match(devJargon)?.[0] || "");
check("B 文案里没有未替换的占位符", /\{\{/.test(JSON.stringify(zh.routeError)) === false && /\{\{/.test(JSON.stringify(en.routeError)) === false);
check(
    "B 四类中文文案互不相同（不拿一句话糊所有情况）",
    new Set(kinds.map((kind) => zh.routeError[kind].title)).size === kinds.length && new Set(kinds.map((kind) => zh.routeError[kind].description)).size === kinds.length,
    JSON.stringify(kinds.map((kind) => zh.routeError[kind].title)),
);

// ---------------------------------------------------------------- C 兜底页不外泄原始异常
setSection("C");
const componentSource = readFileSync(path.resolve("src/components/layout/route-error-page.tsx"), "utf8");
check("C 组件不读 error.message / error.stack / String(error)（原始异常不上屏）", !/\.message\b|\.stack\b|String\(\s*error\s*\)|JSON\.stringify\(\s*error\s*\)/.test(componentSource), (componentSource.match(/\.message\b|\.stack\b|String\(\s*error\s*\)/) || [])[0] || "");
check("C 组件文案全部走 i18n 的 routeError.*（没有硬编码第二份话术）", /routeError\.\$\{kind\}\.title/.test(componentSource) && /routeError\.refresh/.test(componentSource));
const imports = [...componentSource.matchAll(/from "([^"]+)"/g)].map((match) => match[1]);
const allowed = new Set(["lucide-react", "react-i18next", "react-router-dom", "@/lib/route-error"]);
check("C 组件只依赖 4 个白名单模块（不拖业务组件/状态进兜底页）", imports.every((name) => allowed.has(name)), JSON.stringify(imports));
check("C 组件归因只经 routeErrorKind（不自己再写一套 pattern）", /routeErrorKind\(error\)/.test(componentSource) && !/dynamically imported/i.test(componentSource));

// ---------------------------------------------------------------- D 路由挂载（源码级；真页面复核在 ego 探针）
setSection("D");
const routerSource = readFileSync(path.resolve("src/router.tsx"), "utf8");
const errorElementCount = (routerSource.match(/errorElement:\s*<RouteErrorPage \/>/g) || []).length;
check("D 生产 router 挂了两处 errorElement（顶层布局路由 + 通配路由）", errorElementCount === 2, `count=${errorElementCount}`);
check("D 挂的是 RouteErrorPage（不是内联匿名页）", /import RouteErrorPage from "@\/components\/layout\/route-error-page"/.test(routerSource));
check(
    "D 路由表零结构改动：9 条业务路径原样在位",
    ["/", "/image", "/video", "/hub", "/assets", "/prompts", "/canvas", "/canvas/:id", "/config"].every((routePath) => routerSource.includes(`path: "${routePath}"`)),
);

// ---------------------------------------------------------------- 汇总
const summary = { at: new Date().toISOString(), pass, fail: fails.length, fails, results };
const outJson = path.resolve("C:/Users/leon3/AppData/Local/hermes/cache/scratch/route-error-probe/探针结果-node层.json");
mkdirSync(path.dirname(outJson), { recursive: true });
const { writeFileSync } = await import("node:fs");
writeFileSync(outJson, JSON.stringify(summary, null, 2), "utf8");

console.log(`\n路由兜底 node 探针：${pass} 通过 / ${fails.length} 失败（证据 ${outJson}）`);
if (fails.length) {
    console.log("失败明细：");
    for (const line of fails) console.log(`  - ${line}`);
}
process.exit(fails.length ? 1 : 0);
