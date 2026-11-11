/**
 * AT-11 M2 前端自检（免费探针）：esbuild 打包 web/src/lib/hub/merge-mode.ts 后直接断言真代码
 * + 源码接线护栏（task-detail-drawer.tsx 的多选态/确认小窗/合并卡 DOM 合同必须落在真 JSX 上）。
 *
 * 覆盖（对应执行卡质量门）：
 *   ① 多选态进入退出（行号 Set 纯切换、退出清空、taskId 清理）
 *   ② 选中跨轮询保留（Set 不挂 detail 对象引用；成功清空失败保留的接线在源码护栏）
 *   ③ 组卡置灰（已确认组=先拆开；品牌视觉行与普通行同权可选——AT-16 B7 取消品牌例外）
 *   ④ 确认窗三分支（零图 / 一张唯一图 / 多张不同图点名保留）
 *   ⑤ 单帧单框（恰一枚框且=主行；主行无框 → null 诚实降级）+ 徽标文字非空
 *
 * 跑法：cd web && node scripts/probe-at11-m2.mjs   （失败 rc=1）
 * 注：离线无 DOM runtime，渲染级 rect 量取由 M4 真机 ego 收口；本探针管逻辑真值 + JSX 接线护栏。
 */
import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const outDir = path.resolve("node_modules/.probe");
mkdirSync(outDir, { recursive: true });
const outFile = path.join(outDir, "at11-m2.mjs");

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

const mod = await import(pathToFileURL(outFile).href);

// ---------------------------------------------------------------- ① 多选态：行号 Set 纯切换
const sel = new Set();
const sel1 = mod.toggleMergeRow(sel, 1);
check("切换返回新 Set（不改原集合）", sel1 !== sel && sel.size === 0 && sel1.has(1));
const sel2 = mod.toggleMergeRow(sel1, 5);
check("加第二行", sel2.size === 2 && sel2.has(1) && sel2.has(5));
const sel3 = mod.toggleMergeRow(sel2, 1);
check("再点同行=取消选中", sel3.size === 1 && !sel3.has(1) && sel3.has(5));
check("Set 里只有行号（number），无卡对象引用", [...sel2].every((x) => typeof x === "number"));

// ---------------------------------------------------------------- ③ 组卡置灰（可选性判定）
check("已确认组卡置灰=先拆开", mod.mergeSelectable({ 类别: "主产品", 同实体确认: [1, 5] }).selectable === false && mod.mergeSelectable({ 类别: "主产品", 同实体确认: [1, 5] }).reason.includes("先拆开"));
check("品牌视觉行与普通行同权可选（AT-16 B7：片尾可能无品牌露出/品牌标签可能就是产品本身）", mod.mergeSelectable({ 类别: "品牌视觉", 同实体确认: [] }).selectable === true && mod.mergeSelectable({ 类别: "品牌视觉", 同实体确认: [] }).reason === "");
check("普通行可选（reason 空）", mod.mergeSelectable({ 类别: "主产品特写", 同实体确认: [] }).selectable === true && mod.mergeSelectable({ 类别: "主产品特写", 同实体确认: [] }).reason === "");
check("疑似（未确认）不拦可选", mod.mergeSelectable({ 类别: "主产品", 同实体确认: [] }).selectable === true);

// ---------------------------------------------------------------- ④ 确认窗三分支（B3）
const empty = mod.mergeImageBranch([
    { n: 1, 已传图: [] },
    { n: 5, 已传图: [] },
]);
check("零图 → empty", empty.branch === "empty");
const single = mod.mergeImageBranch([
    { n: 5, 已传图: [] },
    { n: 1, 已传图: ["https://x/a.png"] },
]);
check("一张唯一图 → single 且 keepN=持图行（pop-4 实况 #1 有图 #5 无图）", single.branch === "single" && single.keepN === 1 && single.url === "https://x/a.png", JSON.stringify(single));
const multi = mod.mergeImageBranch([
    { n: 3, 已传图: ["https://x/b.png"] },
    { n: 1, 已传图: ["https://x/a.png"] },
]);
check("多张不同图 → multi 列出全部候选（行升序）", multi.branch === "multi" && JSON.stringify(multi.candidates) === JSON.stringify([{ n: 1, url: "https://x/a.png" }, { n: 3, url: "https://x/b.png" }]), JSON.stringify(multi));
const sameUrl = mod.mergeImageBranch([
    { n: 1, 已传图: ["https://x/a.png"] },
    { n: 5, 已传图: ["https://x/a.png"] },
]);
check("同 URL 两行仍算一张唯一图 → single", sameUrl.branch === "single" && sameUrl.keepN === 1);

// ---------------------------------------------------------------- ⑤ 合并卡：徽标 + 单帧单框
check("徽标=「#1+#3+#5 已合并」且非空", mod.mergeBadge([3, 1, 5]) === "#1+#3+#5 已合并");
check("徽标成员乱序也升序", mod.mergeBadge([5, 1]) === "#1+#5 已合并");
check("空成员 → 空徽标（不显示）", mod.mergeBadge([]) === "");

const mkGroup = (over = {}) => ({
    键: "1-5",
    成员: [1, 5],
    主行: 1,
    组名: "紫色管状产品",
    代表帧: { url: "https://x/f.jpg", 秒: 5.35, 镜次: 2, 定位: "精确" },
    单框: { 行: 1, 短名: "紫管", 角标: "必须换", 格: 5, "x%": 33.3, "y%": 33.3, "w%": 33.3, "h%": 33.3, 本卡: true },
    单框降级: false,
    镜次并集: [1, 2, 3],
    ...over,
});
const box = mod.groupBoxRow(mkGroup());
check("有主行自身框 → 恰一枚框且行号=主行", box !== null && box.row === 1, JSON.stringify(box));
const degraded = mod.groupBoxRow(mkGroup({ 单框: null, 单框降级: true }));
check("主行无自身框 → null（诚实降级，绝不搬他人框）", degraded === null);
check("无组投影 → null（回退旧渲染）", mod.groupBoxRow(null) === null);
const primary5 = mod.groupBoxRow(mkGroup({ 主行: 5, 单框: { 行: 5, 短名: "紫管", 角标: "必须换", 格: 5, "x%": 33.3, "y%": 33.3, "w%": 33.3, "h%": 33.3, 本卡: true } }));
check("主行改 #5 → 框行号跟着=主行（不回跳最小行）", primary5 !== null && primary5.row === 5);

check("镜次并集=成员并集升序去重", JSON.stringify(mod.mergeShotUnion([[3, 1], [1, 5]])) === JSON.stringify([1, 3, 5]));

// ---------------------------------------------------------------- 提交复核（§3.6 成员未变）
check("成员未变 → 空（放行）", mod.mergeMembershipChanged([1, 5], [{ n: 1, 同实体确认: [] }, { n: 5, 同实体确认: [] }]) === "");
check("行消失 → 报错可重选", mod.mergeMembershipChanged([1, 5], [{ n: 1, 同实体确认: [] }]).includes("已不存在"));
check("行入了别的组 → 报错可重选", mod.mergeMembershipChanged([1, 5], [{ n: 1, 同实体确认: [1, 5] }, { n: 5, 同实体确认: [1, 5] }]).includes("已在别的组"));

// ---------------------------------------------------------------- 源码接线护栏（渲染层 DOM 合同必须落在真 JSX）
const drawer = String(readUtf8("src/pages/hub/task-detail-drawer.tsx"));
function readUtf8(rel) {
    // node:fs 顶层 import 只引了 mkdirSync；这里用同步读兜一层（保持 import 面最小）。
    return require("node:fs").readFileSync(path.resolve(rel), "utf8");
}

check("常驻 checkbox（替换 AT-12 勾选式合并，无模式入口）", drawer.includes("data-hub-check-row={card.n}") && !drawer.includes("data-hub-merge-enter") && !drawer.includes("data-hub-merge-exit"));
check("多选态存行号 Set（useState<Set<number>>，不存卡对象）", drawer.includes("useState<Set<number>>(new Set())"));
check("taskId 切换清理多选态（useEffect [taskId] 内清空）", /useEffect\(\(\) => \{[\s\S]*?setSelectedRows\(new Set\(\)\);[^\n]*[\s\S]*?\}, \[taskId\]\)/.test(drawer));
check("浮条合同文案「合并勾选项(N)」+[清空]", drawer.includes("{mergeBarText(mergeableSelected.length)}") && drawer.includes("data-hub-merge-clear"));
check("浮条勾≥2 才出现（少于 2 不出浮条）", drawer.includes("mergeableSelected.length >= 2"));
check("合并成功清空选中、失败保留（catch 不动 selectedRows）", /message\.success\(`已合并[\s\S]*?setSelectedRows\(new Set\(\)\);/.test(drawer) && /catch \(error\) \{[\s\S]*?其中一行已在他组[\s\S]*?return false;/.test(drawer));
check("409 撞他组 → 点名「其中一行已在他组，先拆开」", drawer.includes("其中一行已在他组，先拆开"));
check("提交前复核成员未变（mergeMembershipChanged 接线）", drawer.includes("mergeMembershipChanged(opts.rows, detail.asset_cards)"));
check("多图分支先清落选行再成组（组内清空会整组联动）", drawer.includes("opts.keepN !== null") && /postHubAsset\(taskId, \{ n: card\.n, clear: true \}\)/.test(drawer));
check("groupHubRows 传主行/组名（组元数据落盘）", drawer.includes("groupHubRows(taskId, opts.rows, { primary: opts.primary, name: opts.name })"));
check("确认窗三分支 DOM 标记 empty/single/multi", drawer.includes('data-hub-merge-branch="empty"') && drawer.includes('data-hub-merge-branch="single"') && drawer.includes('data-hub-merge-branch="multi"'));
check("确认窗「沿用 #N 已传图统一到全组」文案", drawer.includes("已传图统一到全组"));
check("单帧单框：组卡走 GroupFrame（恰一枚 data-hub-box-row=主行）", drawer.includes("<GroupFrame group={group}") && /data-hub-box-row=\{boxRow\.row\}/.test(drawer) && drawer.includes("groupBoxRow(group)"));
check("主行未定位诚实文案（不搬他人框）", drawer.includes("主行未定位"));
check("合并卡徽标走 mergeBadge（#N 已合并）", drawer.includes("mergeBadge(members)"));
check("拆开合并文案=已解除联动保留当前传图（M3 I1 收尾，不是历史撤销）", drawer.includes("拆开合并") && drawer.includes("解除联动，各行保留当前传图"));
check("组卡不挂 checkbox（组=原子单位，勾选只挂非组卡）", (() => {
    const start = drawer.indexOf("function GroupCard(");
    const end = drawer.indexOf("function GroupFrame(");
    return start > 0 && end > start && !drawer.slice(start, end).includes("data-hub-check-row");
})());
check("置灰行点卡只弹原因不入选（warning reason）", /if \(verdict && !verdict\.selectable\) \{\s*\n\s*message\.warning\(verdict\.reason\);/.test(drawer));

// ---------------------------------------------------------------- 结果
console.log(`probe-at11-m2: ${pass} PASS, ${fails.length} FAIL`);
for (const item of fails) console.log(`  FAIL ${item}`);
process.exit(fails.length ? 1 : 0);
