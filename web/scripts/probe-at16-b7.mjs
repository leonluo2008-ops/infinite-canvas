/**
 * AT-16 B7 前端探针（免费）：品牌视觉行不再被禁止合并——前后端双守卫移除后的真值 + 源码护栏。
 *
 * 缺陷 D8（所有者定）：品牌资产不得被禁止与产品合并（片尾可能无品牌露出、品牌标签可能就是
 * 产品本身）。本探针用 esbuild 打包**真** merge-mode.ts 断言纯判定，再对源码护栏断言：
 *   ① 品牌视觉行可选（与普通行同权），跨类合并正例
 *   ② 通用守卫不削弱：已确认组=先拆开、按原片=已移除、行不存在（pruneSelection 同源消费）
 *   ③ pruneSelection 保留品牌行（同源消费，不另复制「品牌可选」覆盖）
 *   ④ 前端源码无品牌专用分支；drawer 不另复制品牌覆盖（判定只在 mergeSelectable 一处）
 *   ⑤ 后端 group_rows 品牌专用拒绝已删、通用拒绝四条仍在（同一件事的另一半，源码级对账）
 *
 * 跑法：cd web && node scripts/probe-at16-b7.mjs   （失败 rc=1）
 * 注：离线无 DOM；真机渲染/勾选交互由执行卡真机验收收口。
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const outDir = path.resolve("node_modules/.probe");
mkdirSync(outDir, { recursive: true });
const outFile = path.join(outDir, "at16-b7.mjs");

let pass = 0;
const fails = [];
function check(name, condition, detail = "") {
    if (condition) {
        pass += 1;
        return;
    }
    fails.push(`${name}${detail ? ` — ${detail}` : ""}`);
}

await esbuild.build({
    entryPoints: [path.resolve("src/lib/hub/merge-mode.ts")],
    bundle: true,
    format: "esm",
    platform: "neutral",
    outfile: outFile,
    logLevel: "silent",
    alias: { "@": path.resolve("src") },
});

const merge = await import(pathToFileURL(outFile).href);

// ---------------------------------------------------------------- ① 品牌行可选（跨类合并正例）
const brandPlain = merge.mergeSelectable({ 类别: "品牌视觉（置换位）", 同实体确认: [] });
check("品牌视觉行可选（selectable=true）", brandPlain.selectable === true, JSON.stringify(brandPlain));
check("品牌视觉行 reason 为空（无置灰原因）", brandPlain.reason === "", brandPlain.reason);
check("品牌视觉行 + 主体行都可选 → 可跨类合并",
    merge.mergeSelectable({ 类别: "主体", 同实体确认: [] }).selectable === true &&
    merge.mergeSelectable({ 类别: "品牌视觉", 同实体确认: [] }).selectable === true);
check("品牌行在组里 → 仍先拆开（通用守卫不削弱）",
    merge.mergeSelectable({ 类别: "品牌视觉", 同实体确认: [1, 12] }).selectable === false &&
    merge.mergeSelectable({ 类别: "品牌视觉", 同实体确认: [1, 12] }).reason.includes("先拆开"));
check("品牌行 dismissed → 已移除不可选（通用守卫不削弱）",
    merge.mergeSelectable({ 类别: "品牌视觉", 同实体确认: [], 决策: "dismissed" }).selectable === false);
check("品牌行 keep_origin → 旧数据同样按原片不可选",
    merge.mergeSelectable({ 类别: "品牌视觉", 同实体确认: [], 决策: "keep_origin" }).selectable === false &&
    merge.mergeSelectable({ 类别: "品牌视觉", 同实体确认: [], 决策: "keep_origin" }).reason.includes("按原片"));

// ---------------------------------------------------------------- ② pruneSelection 同源消费
const cards = [
    { n: 1, 类别: "主体", 同实体确认: [] },
    { n: 2, 类别: "人物", 同实体确认: [2, 3] },            // 轮询中被成组
    { n: 12, 类别: "品牌视觉（置换位）", 同实体确认: [] },   // 品牌行（本卡核心）
    { n: 7, 类别: "主体", 同实体确认: [], 决策: "dismissed" },
];
const pruned = merge.pruneSelection(new Set([1, 2, 12, 7, 99]), cards);
check("轮询收口保留品牌行（成组/移除/消失照旧剔出）",
    pruned.size === 2 && pruned.has(1) && pruned.has(12), JSON.stringify([...pruned]));
const stable = new Set([1, 12]);
check("无变化 → 返回原 Set 引用（轮询不抖）", merge.pruneSelection(stable, cards) === stable);

// ---------------------------------------------------------------- ③ 前端源码无品牌专用分支
const mergeSrc = readFileSync(path.resolve("src/lib/hub/merge-mode.ts"), "utf8");
check("merge-mode.ts 无品牌类别判定（无 类别.includes(\"品牌视觉\") 分支）",
    !/类别\s*\.\s*includes\(\s*"品牌/.test(mergeSrc), "仍有品牌类别分支");
check("merge-mode.ts 无「品牌行单独处理」拒绝文案", !mergeSrc.includes("品牌行单独处理"));
check("mergeSelectable 只剩两条拒绝（按原片 + 已确认组）",
    (mergeSrc.match(/return \{ selectable: false/g) || []).length === 2,
    String((mergeSrc.match(/return \{ selectable: false/g) || []).length));

// ---------------------------------------------------------------- ④ drawer 不另复制「品牌可选」覆盖
const drawer = readFileSync(path.resolve("src/pages/hub/task-detail-drawer.tsx"), "utf8");
const selectableMapSlice = drawer.slice(drawer.indexOf("const selectableMap"),
                                        drawer.indexOf("const mergeableSelected"));
check("drawer 可选性只消费 mergeSelectable（不按类别另判）",
    selectableMapSlice.includes("mergeSelectable(card)") && !/品牌/.test(selectableMapSlice),
    selectableMapSlice.slice(0, 120));
check("drawer 轮询收口仍走 pruneSelection（同源，不自己过滤）",
    drawer.includes("pruneSelection(prev, detail.asset_cards)"));
check("drawer 注释与实现一致（不再写「品牌视觉行置灰」）", !drawer.includes("品牌视觉行置灰"));

// ---------------------------------------------------------------- ⑤ 后端 group_rows 双守卫对账
const hubApi = readFileSync(path.resolve("../wanrepro/ui/hub_api.py"), "utf8");
const groupStart = hubApi.indexOf("def group_rows(");
const groupEnd = hubApi.indexOf("def ungroup_rows(");
const groupSrc = groupApiSlice(hubApi, groupStart, groupEnd);
check("后端 group_rows 已删品牌专用拒绝（无 startswith(\"品牌视觉\") 分支）",
    !groupApiSlice(hubApi, groupStart, groupEnd).includes('startswith("品牌视觉")'));
check("后端 group_rows 无「品牌行单独处理」拒绝文案", !groupSrc.includes("品牌行单独处理"));
check("后端通用拒绝四条仍在（行不存在/已按原片/已在组/≥2 行）",
    groupSrc.includes("行号不在清单里") && groupSrc.includes("先恢复再合并") &&
    groupSrc.includes("已在组") && groupSrc.includes("成组至少要 2 行"));

function groupApiSlice(source, start, end) {
    return source.slice(start, end < 0 ? source.length : end);
}

// ---------------------------------------------------------------- 汇总
console.log(`\nprobe-at16-b7: ${pass}/${pass + fails.length} PASS`);
for (const f of fails) console.log(`  [FAIL] ${f}`);
process.exit(fails.length ? 1 : 0);
