/**
 * AT-12 N1 前端自检（免费探针）：esbuild 打包 web/src/lib/hub/row-decision.ts + merge-mode.ts 后
 * 直接断言真代码 + 源码接线护栏（task-detail-drawer.tsx 的勾选式合并/资产移除折叠 DOM 合同）。
 *
 * 覆盖（对应执行卡质量门）：
 *   ① 勾选 Set 纯切换 / 跨轮询保留（pruneSelection 无变化返回原引用）/ 清理（被成组·被移除·行消失）
 *   ② dismissed 三处读点：渲染文字（徽标/折叠条）、锁点豁免、前端过滤（activeCards）
 *   ③ restore 恢复（dismissed / 旧 keep_origin 都给「恢复替换」）
 *   ④ 旧 keep_origin 归折叠不炸（与 dismissed 同义）
 *   ⑤ 反例：组内行移除拒（菜单 null）/ dismissed 行不可参与合并（对齐先降后合规则）
 *   ⑥ JSX 接线：常驻 checkbox、勾≥2 浮条、成功清空失败保留、折叠条+恢复、无旧模式按钮
 *
 * 跑法：cd web && node scripts/probe-at12-n1.mjs   （失败 rc=1）
 * 注：离线无 DOM runtime，渲染级可见性/真机点选由 N2 真机收口；本探针管逻辑真值 + JSX 接线护栏。
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const outDir = path.resolve("node_modules/.probe");
mkdirSync(outDir, { recursive: true });

let pass = 0;
const fails = [];
function check(name, condition, detail = "") {
    if (condition) {
        pass += 1;
        return;
    }
    fails.push(`${name}${detail ? ` — ${detail}` : ""}`);
}

async function bundle(entry, outName) {
    const outFile = path.join(outDir, outName);
    await esbuild.build({
        entryPoints: [path.resolve(entry)],
        bundle: true,
        format: "esm",
        platform: "neutral",
        outfile: outFile,
        logLevel: "silent",
        alias: { "@": path.resolve("src") },
    });
    return import(pathToFileURL(outFile).href);
}

const row = await bundle("src/lib/hub/row-decision.ts", "at12-row-decision.mjs");
const merge = await bundle("src/lib/hub/merge-mode.ts", "at12-merge-mode.mjs");

// ---------------------------------------------------------------- ① 勾选 Set：纯切换 + 轮询收口
const sel = new Set();
const sel1 = merge.toggleMergeRow(sel, 1);
check("切换返回新 Set（不改原集合）", sel1 !== sel && sel.size === 0 && sel1.has(1));
const sel2 = merge.toggleMergeRow(sel1, 5);
check("加第二行", sel2.size === 2 && sel2.has(1) && sel2.has(5));
const sel3 = merge.toggleMergeRow(sel2, 1);
check("再点同行=取消勾选", sel3.size === 1 && !sel3.has(1) && sel3.has(5));
check("Set 里只有行号（number），无卡对象引用", [...sel2].every((x) => typeof x === "number"));

const cards = [
    { n: 1, 类别: "主体", 同实体确认: [] },
    { n: 2, 类别: "场景", 同实体确认: [] },
    { n: 3, 类别: "道具", 同实体确认: [2, 3] },      // 被成组（轮询中变化）
    { n: 5, 类别: "品牌视觉", 同实体确认: [] },       // 品牌行
    { n: 7, 类别: "主体", 同实体确认: [], 决策: "dismissed" }, // 被移除
];
const keep = merge.pruneSelection(new Set([1, 2, 3, 5, 7, 9]), cards);
check("轮询收口：只留仍存在且可选的行（成组/品牌/移除/消失剔除）",
    keep.size === 2 && keep.has(1) && keep.has(2), JSON.stringify([...keep]));
const sameRef = merge.pruneSelection(new Set([1, 2]), cards);
check("轮询无变化 → 返回原 Set 引用（不触发重渲染）", sameRef instanceof Set);
const stable = new Set([1, 2]);
check("轮询无变化 → 引用相等", merge.pruneSelection(stable, cards) === stable);
check("浮条计数文案=「合并勾选项(N)」", merge.mergeBarText(3) === "合并勾选项(3)");
check("品牌行置灰=品牌行单独处理",
    merge.mergeSelectable({ 类别: "品牌视觉", 同实体确认: [] }).selectable === false &&
    merge.mergeSelectable({ 类别: "品牌视觉", 同实体确认: [] }).reason.includes("品牌行单独处理"));
check("已确认组卡不参与合并（先拆开）",
    merge.mergeSelectable({ 类别: "主体", 同实体确认: [1, 5] }).selectable === false &&
    merge.mergeSelectable({ 类别: "主体", 同实体确认: [1, 5] }).reason.includes("先拆开"));
check("dismissed 行不可参与合并（已折叠）",
    merge.mergeSelectable({ 类别: "主体", 同实体确认: [], 决策: "dismissed" }).selectable === false);

// ---------------------------------------------------------------- ② dismissed 三处读点
const mustDismissed = { 建议: "必须换", 决策: "dismissed", 同实体确认: [] };
const mustKeep = { 建议: "必须换", 决策: "keep_origin", 同实体确认: [] };
const mustPlain = { 建议: "必须换", 决策: "", 同实体确认: [] };

check("渲染文字：dismissed 徽标=「已移除·按原片」", row.decisionBadgeText(mustDismissed) === "已移除·按原片");
check("渲染文字：旧 keep_origin 徽标=「按原片」（不炸旧数据）", row.decisionBadgeText(mustKeep) === "按原片");
check("渲染文字：无覆盖 → 空", row.decisionBadgeText(mustPlain) === "");
check("折叠条文案=「已移除 · 按原片 N 项」", row.originStripLabel(2) === "已移除 · 按原片 2 项");

const mixed = [
    { n: 1, 建议: "必须换", 已传图: ["https://x/a.png"], 决策: "" },
    { n: 2, 建议: "必须换", 已传图: [], 决策: "" },
    { n: 3, 建议: "必须换", 已传图: [], 决策: "keep_origin" },
    { n: 5, 建议: "必须换", 已传图: [], 决策: "dismissed" },
    { n: 7, 建议: "建议准备", 已传图: [], 决策: "dismissed" },
];
check("锁点豁免：dismissed 与 keep_origin 都不进锁点列表",
    JSON.stringify(row.lockRows(mixed)) === JSON.stringify([2]), JSON.stringify(row.lockRows(mixed)));
const counters = row.lockCounters(mixed);
check("计数器：必换 4=已换 1+未换 1+按原片 2（含 dismissed 1）",
    counters.must === 4 && counters.replaced === 1 && counters.unfilled === 1 &&
    counters.keptOrigin === 2 && counters.dismissed === 1, JSON.stringify(counters));
check("前端过滤：按原片行号（keep_origin∪dismissed，升序）",
    JSON.stringify(row.originRowNumbers(mixed)) === JSON.stringify([3, 5, 7]), JSON.stringify(row.originRowNumbers(mixed)));
check("前端过滤：置换区在列卡剔除按原片行",
    JSON.stringify(row.activeCards(mixed).map((card) => card.n)) === JSON.stringify([1, 2]),
    JSON.stringify(row.activeCards(mixed).map((card) => card.n)));
check("空卡不炸：两集合/过滤都空", row.originRowNumbers([]).length === 0 && row.activeCards([]).length === 0);

// ---------------------------------------------------------------- ③ restore / ⑤ 反例
const a1 = row.rowDecisionAction(mustPlain);
check("必换行 → 移除菜单（按原片处理）",
    a1 !== null && a1.action === "dismissed" && a1.label === "移除（不需要替换，按原片处理）", JSON.stringify(a1));
const a2 = row.rowDecisionAction(mustDismissed);
check("dismissed 行 → 恢复替换（restore）", a2 !== null && a2.action === "restore" && a2.label === "恢复替换", JSON.stringify(a2));
const a3 = row.rowDecisionAction(mustKeep);
check("旧 keep_origin 行 → 恢复替换（旧数据可恢复）", a3 !== null && a3.action === "restore");
check("反例：组内行不给移除入口（组=原子单位，先拆开）",
    row.rowDecisionAction({ 建议: "必须换", 决策: "", 同实体确认: [1, 5] }) === null);
check("可沿用行无移除入口（不需要替换）", row.rowDecisionAction({ 建议: "可沿用", 决策: "", 同实体确认: [] }) === null);
check("dismissed 反馈点名行号+恢复指引",
    row.decisionFeedback(5, "dismissed").includes("#5") && row.decisionFeedback(5, "dismissed").includes("恢复"));
check("restore 反馈点名行号", row.decisionFeedback(5, "restore").includes("#5") && row.decisionFeedback(5, "restore").includes("恢复替换"));
check("旧 keep_origin 值仍判为按原片（isOriginDecision）",
    row.isOriginDecision("keep_origin") && row.isOriginDecision("dismissed") && !row.isOriginDecision(""));

// ---------------------------------------------------------------- ⑥ JSX 接线护栏
const drawer = readFileSync(path.resolve("src/pages/hub/task-detail-drawer.tsx"), "utf8");
const rowSrc = readFileSync(path.resolve("src/lib/hub/row-decision.ts"), "utf8");

check("删除旧「合并资产」模式入口与退出逻辑（无 data-hub-merge-enter/exit）",
    !drawer.includes("data-hub-merge-enter") && !drawer.includes("data-hub-merge-exit") && !drawer.includes("mergeMode"));
check("常驻 checkbox：data-hub-check-row + 勾选态 data-hub-check-state",
    drawer.includes("data-hub-check-row={card.n}") && drawer.includes("data-hub-check-state"));
check("checkbox 置灰原因（品牌行）经 Tooltip 透出", drawer.includes("checkDisabledReason") && drawer.includes("<Tooltip title={checkDisabledReason}>"));
check("勾选态存行号 Set（useState<Set<number>>）", drawer.includes("useState<Set<number>>(new Set())"));
check("taskId 切换清空勾选（I2）",
    /useEffect\(\(\) => \{[\s\S]*?setSelectedRows\(new Set\(\)\);[\s\S]*?\}, \[taskId\]\)/.test(drawer));
check("轮询收口接 pruneSelection（无变化返回原引用）",
    drawer.includes("pruneSelection(prev, detail.asset_cards)") && drawer.includes("}, [detail.asset_cards]);"));
check("浮条勾≥2 才出现 + 文案走 mergeBarText + [合并][清空]",
    drawer.includes("mergeableSelected.length >= 2") && drawer.includes("{mergeBarText(mergeableSelected.length)}") &&
    drawer.includes("data-hub-merge-bar") && drawer.includes("data-hub-merge-submit") && drawer.includes("data-hub-merge-clear"));
check("合并成功清空勾选、失败保留（catch 不动 selectedRows）",
    /message\.success\(`已合并[\s\S]*?setSelectedRows\(new Set\(\)\);/.test(drawer) &&
    /catch \(error\) \{[\s\S]*?其中一行已在他组[\s\S]*?return false;/.test(drawer));
check("确认小窗仍走 groupHubRows + 成员复核（组元数据全链不动）",
    drawer.includes("mergeMembershipChanged(opts.rows, detail.asset_cards)") &&
    drawer.includes("groupHubRows(taskId, opts.rows, { primary: opts.primary, name: opts.name })"));
check("按原片折叠：置换区过滤走 activeCards(allCards)",
    drawer.includes("const cards = activeCards(allCards);") && drawer.includes("const originRows = allCards.filter"));
check("折叠条 DOM：data-hub-origin-strip / toggle / row / restore",
    drawer.includes("data-hub-origin-strip") && drawer.includes("data-hub-origin-toggle") &&
    drawer.includes("data-hub-origin-row={card.n}") && drawer.includes("data-hub-origin-restore"));
check("折叠条标题走 originStripLabel（N=按原片行数）", drawer.includes("{originStripLabel(originRows.length)}"));
check("恢复按钮调 set_asset decision=restore", /doDecide\(card, "restore"\)/.test(drawer));
check("组内行菜单不显示移除（入口判定走 rowDecisionAction 纯判定）",
    drawer.includes("rowDecisionAction(card)") && rowSrc.includes("card.同实体确认.length >= 2"));
check("菜单文案真源在 lib（移除·不需要替换·按原片处理）", rowSrc.includes("移除（不需要替换，按原片处理）") && drawer.includes("label: decision.label"));
check("锁点计算接线 lockRows/lockCounters（dismissed 同豁免）", drawer.includes("lockRows(allCards)") && drawer.includes("lockCounters(allCards)"));
check("组卡不挂 checkbox（GroupCard 体内无 data-hub-check-row）", (() => {
    const start = drawer.indexOf("function GroupCard(");
    const end = drawer.indexOf("function GroupFrame(");
    return start > 0 && end > start && !drawer.slice(start, end).includes("data-hub-check-row");
})());
check("无本地决策影子状态（dismissed 状态全部派生自服务端 card.决策）",
    !/(const|let)\s*\[[^\]]*(决策|Decision)[^\]]*\]\s*=\s*useState/.test(drawer));

// ---------------------------------------------------------------- 结果
console.log(`probe-at12-n1: ${pass} PASS, ${fails.length} FAIL`);
for (const item of fails) console.log(`  FAIL ${item}`);
process.exit(fails.length ? 1 : 0);
