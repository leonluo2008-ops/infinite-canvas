/**
 * AT-11 M3 前端自检（免费探针）：esbuild 打包 web/src/lib/hub/row-decision.ts 后直接断言真代码
 * + 源码接线护栏（task-detail-drawer.tsx 的单行降级菜单/徽标/锁点豁免/竞态收尾 DOM 合同必须落在真 JSX 上）。
 *
 * 覆盖（对应执行卡质量门）：
 *   ① 降级入口显隐规则（必换/建议准备 → keep_origin；可沿用/待核对 → 无入口；组内行 → 隐藏）
 *   ② restore 恢复（已 keep_origin 行 → 「恢复替换」）
 *   ③ keep_origin 锁点豁免（有覆盖的必换行不进锁点列表，与后端 lock_remaining 同口径）
 *   ④ 徽标文字非空（「按原片」）+ 操作反馈点名行号
 *   ⑤ 报错文案如实（撞组点名「先拆开」，其余原文透传）
 *   ⑥ 竞态收尾接线（busy="decision" 在途防重复、轮询读 sidecar 覆盖透传字段不闪回、taskId 清理）
 *
 * 跑法：cd web && node scripts/probe-at11-m3.mjs   （失败 rc=1）
 * 注：离线无 DOM runtime，渲染级 rect/真机点选由 M4 真机 ego 收口；本探针管逻辑真值 + JSX 接线护栏。
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const outDir = path.resolve("node_modules/.probe");
mkdirSync(outDir, { recursive: true });
const outFile = path.join(outDir, "at11-m3.mjs");

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
    entryPoints: [path.resolve("src/lib/hub/row-decision.ts")],
    bundle: true,
    format: "esm",
    platform: "neutral",
    outfile: outFile,
    logLevel: "silent",
    alias: { "@": path.resolve("src") },
});

const mod = await import(pathToFileURL(outFile).href);

// ---------------------------------------------------------------- ① 降级入口显隐规则
const mustRow = { 建议: "必须换", 决策: "", 同实体确认: [] };
const prepareRow = { 建议: "建议准备", 决策: "", 同实体确认: [] };
const keepRow = { 建议: "可沿用", 决策: "", 同实体确认: [] };
const unverifiedRow = { 建议: "", 决策: "", 同实体确认: [] };

const a1 = mod.rowDecisionAction(mustRow);
check("必换行 → dismissed「移除（不需要替换，按原片处理）」", a1 !== null && a1.action === "dismissed" && a1.label === "移除（不需要替换，按原片处理）", JSON.stringify(a1));
const a2 = mod.rowDecisionAction(prepareRow);
check("建议准备行 → dismissed 入口", a2 !== null && a2.action === "dismissed");
check("可沿用行 → 也有移除入口（AT-12 N2 修订）", mod.rowDecisionAction(keepRow)?.action === "dismissed");
check("待核对行（建议空）→ 也有移除入口（AT-12 N2 修订）", mod.rowDecisionAction(unverifiedRow)?.action === "dismissed");
const groupedMust = { 建议: "必须换", 决策: "", 同实体确认: [1, 5] };
check("组内行 → 入口隐藏（null，组=原子单位）", mod.rowDecisionAction(groupedMust) === null);

// ---------------------------------------------------------------- ② restore 恢复
const degradedRow = { 建议: "必须换", 决策: "keep_origin", 同实体确认: [] };
const a3 = mod.rowDecisionAction(degradedRow);
check("已 keep_origin 行 → restore「恢复替换」", a3 !== null && a3.action === "restore" && a3.label === "恢复替换", JSON.stringify(a3));
check("已 keep_origin 行即使进了组（竞态）也不给入口（组内先拆开）", mod.rowDecisionAction({ 建议: "必须换", 决策: "keep_origin", 同实体确认: [1, 5] }) === null);

// ---------------------------------------------------------------- ③ keep_origin 锁点豁免
const cards = [
    { n: 2, 建议: "必须换", 已传图: ["https://x/a.png"], 决策: "" },
    { n: 4, 建议: "必须换", 已传图: [], 决策: "" },
    { n: 7, 建议: "必须换", 已传图: [], 决策: "keep_origin" },
    { n: 9, 建议: "建议准备", 已传图: [], 决策: "" },
    { n: 11, 建议: "必须换", 已传图: [], 决策: "" },
];
check("锁点列表=未换必换行−keep_origin（升序）", JSON.stringify(mod.lockRows(cards)) === JSON.stringify([4, 11]), JSON.stringify(mod.lockRows(cards)));
check("降级 #7 后锁点即时重算：#7 不进锁点列表", !mod.lockRows(cards).includes(7));
const restored = cards.map((card) => (card.n === 7 ? { ...card, 决策: "" } : card));
check("恢复 #7 后重新进锁点列表", mod.lockRows(restored).includes(7) && JSON.stringify(mod.lockRows(restored)) === JSON.stringify([4, 7, 11]));
const counters = mod.lockCounters(cards);
check("计数器：必换 4=已换 1+未换 2+按原片 1", counters.must === 4 && counters.replaced === 1 && counters.unfilled === 2 && counters.keptOrigin === 1, JSON.stringify(counters));
const allKept = mod.lockCounters([{ 建议: "必须换", 已传图: [], 决策: "keep_origin" }]);
check("唯一必换行降级 → 未换 0（锁点全豁免）", allKept.unfilled === 0 && allKept.keptOrigin === 1, JSON.stringify(allKept));

// ---------------------------------------------------------------- ④ 徽标 + 反馈点名行号
check("降级行徽标文字=「按原片」非空", mod.decisionBadgeText({ 决策: "keep_origin" }) === "按原片");
check("无覆盖行徽标为空（不显示）", mod.decisionBadgeText({ 决策: "" }) === "");
check("keep 反馈点名行号", mod.decisionFeedback(7, "keep_origin").includes("#7") && mod.decisionFeedback(7, "keep_origin").includes("按原片"));
check("restore 反馈点名行号", mod.decisionFeedback(7, "restore").includes("#7") && mod.decisionFeedback(7, "restore").includes("恢复替换"));

// ---------------------------------------------------------------- ⑤ 报错文案如实（I3）
check("撞组 → 点名「先拆开再单独处理」且带后端原文", mod.decisionErrorText("第 3 行在确认组 [1, 3] 里（组=原子单位，不可单行降级，先拆开合并）").includes("先拆开再单独处理"));
check("非 awaiting_assets 等其余报错原文透传不吞", mod.decisionErrorText("行决策需 awaiting_assets（确认后覆盖表锁定不可改），当前 rendering").includes("awaiting_assets"));
check("普通错误原样返回", mod.decisionErrorText("任务不存在") === "任务不存在");

// ---------------------------------------------------------------- 源码接线护栏（渲染层 DOM 合同必须落在真 JSX）
const drawer = readFileSync(path.resolve("src/pages/hub/task-detail-drawer.tsx"), "utf8");
const rowDecisionSrc = readFileSync(path.resolve("src/lib/hub/row-decision.ts"), "utf8");

check("降级走 postHubAsset decision 参数（M1 set_asset 合同）", /postHubAsset\(taskId, \{ n: card\.n, decision \}\)/.test(drawer));
check("busy=\"decision\" 在途防重复点击（BusyKind + doDecide 守卫）", drawer.includes('"decision"') && /const doDecide = async[\s\S]*?if \(busy\) return;[\s\S]*?setBusy\("decision"\)/.test(drawer));
check("「…」菜单入口 data-hub-decision-menu（非组卡）", drawer.includes("data-hub-decision-menu"));
check("菜单项走 rowDecisionAction 纯判定（label 真源在 lib，接线落 decision.label）", drawer.includes("rowDecisionAction(card)") && drawer.includes("label: decision.label") && rowDecisionSrc.includes("移除（不需要替换，按原片处理）"));
check("勾选态下菜单常驻（AT-12 无模式切换，不再有 mergeMode 隐藏）", /decision && onDecide/.test(drawer) && !drawer.includes("mergeMode"));
check("降级徽标「按原片」灰·虚线 data-hub-decision-badge", drawer.includes("data-hub-decision-badge") && drawer.includes("decisionBadgeText(card)") && /dashed/.test(drawer));
check("锁点计算接线 lockRows（按原片豁免落在真派生）", drawer.includes("lockRows(allCards)") && drawer.includes("lockCounters(allCards)"));
check("计数行带「按原片 N」与 data-hub-lock-counters", drawer.includes("data-hub-lock-counters") && drawer.includes("按原片 ${counters.keptOrigin}"));
check("成功反馈走 decisionFeedback（点名行号）", drawer.includes("decisionFeedback(card.n, decision)"));
check("报错走 decisionErrorText（如实核对）", drawer.includes("decisionErrorText(errorText(error))"));
check("成功后 onWritten() 回刷（徽标/锁点派生自服务端 detail，轮询不闪回）", /await postHubAsset\(taskId, \{ n: card\.n, decision \}\);[\s\S]*?message\.success\(decisionFeedback[\s\S]*?onWritten\(\);/.test(drawer));
check("无本地决策影子状态（徽标/菜单读 card.决策 服务端透传字段，无 decision 专用 useState）", !/(const|let)\s*\[[^\]]*(决策|Decision)[^\]]*\]\s*=\s*useState/.test(drawer));
check("菜单/徽标按钮 busy!=='null' 全局在途禁用", /disabled: busy !== null/.test(drawer) && /disabled=\{busy !== null\}/.test(drawer));
check("拆开合并成功文案=已解除联动保留当前传图（I1）", drawer.includes("已解除联动，各行保留当前传图"));
check("M2 多选态 taskId 清理仍在（I2 不断链）", /useEffect\(\(\) => \{[\s\S]*?setSelectedRows\(new Set\(\)\);[\s\S]*?\}, \[taskId\]\)/.test(drawer));

// ---------------------------------------------------------------- 结果
console.log(`probe-at11-m3: ${pass} PASS, ${fails.length} FAIL`);
for (const item of fails) console.log(`  FAIL ${item}`);
process.exit(fails.length ? 1 : 0);
