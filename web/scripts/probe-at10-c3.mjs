/**
 * AT-10 C3 几何自检（免费探针）：esbuild 打包 web/src/lib/hub/frame-overlay.ts 后直接断言真代码。
 * 覆盖：舞台尺寸/内容盒（含 letterbox）/九宫格 px 换算与越界夹取/框内角标门槛/
 *       图例 rail 不相交不越界/三态与文案/秒数格式。
 * 跑法：cd web && node scripts/probe-at10-c3.mjs   （失败 rc=1）
 */
import { createRequire } from "node:module";
import { readFileSync, rmSync, mkdirSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const outDir = path.resolve("node_modules/.probe");
mkdirSync(outDir, { recursive: true });
const outFile = path.join(outDir, "at10-c3.mjs");

let pass = 0;
const fails = [];
function check(name, condition, detail = "") {
    if (condition) {
        pass += 1;
        return;
    }
    fails.push(`${name}${detail ? ` — ${detail}` : ""}`);
}
function near(a, b, tol = 1e-6) {
    return Math.abs(a - b) <= tol;
}
function overlap(a, b) {
    return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

await esbuild.build({
    entryPoints: [path.resolve("src/lib/hub/frame-overlay.ts")],
    bundle: true,
    format: "esm",
    platform: "neutral",
    outfile: outFile,
    logLevel: "silent",
    alias: { "@": path.resolve("src") },
});

const mod = await import(pathToFileURL(outFile).href);

// ---------------------------------------------------------------- 舞台 / 内容盒
const portrait = mod.stageSize(1080 / 1888); // pop-4 真帧比例
check("竖屏舞台宽按真实比例", portrait.w === Math.round(208 * (1080 / 1888)), JSON.stringify(portrait));
check("舞台高固定 208", portrait.h === 208, String(portrait.h));

const wide = mod.stageSize(16 / 9);
check("横屏舞台宽夹到上限 240", wide.w === mod.STAGE_MAX_W, JSON.stringify(wide));
check("未知比例按 16:9 兜底", mod.stageSize(null).w === mod.STAGE_MAX_W && mod.safeRatio(NaN) === mod.FALLBACK_RATIO);
check("极窄比例夹到下限", mod.stageSize(0.1).w === mod.STAGE_MIN_W, JSON.stringify(mod.stageSize(0.1)));

const contentPortrait = mod.containRect(1080 / 1888, portrait);
check(
    "舞台按真实比例 → 内容盒=整框",
    near(contentPortrait.x, 0) && near(contentPortrait.y, 0) && near(contentPortrait.w, portrait.w) && near(contentPortrait.h, portrait.h),
    JSON.stringify(contentPortrait),
);

const contentWide = mod.containRect(16 / 9, { w: 240, h: 208 });
check("横屏内容盒左右留白 letterbox", near(contentWide.x, 0) && near(contentWide.w, 240) && contentWide.h < 208, JSON.stringify(contentWide));
check("letterbox 上下居中", near(contentWide.y, (208 - contentWide.h) / 2), JSON.stringify(contentWide));

const contentTall = mod.containRect(0.3, { w: 240, h: 208 });
check("超窄比例内容盒左右居中", contentTall.w < 240 && near(contentTall.x, (240 - contentTall.w) / 2), JSON.stringify(contentTall));

// ---------------------------------------------------------------- 九宫格 px 换算
const gridBox = (cell) => ({
    "x%": ((cell - 1) % 3) * (100 / 3),
    "y%": Math.floor((cell - 1) / 3) * (100 / 3),
    "w%": 100 / 3,
    "h%": 100 / 3,
});
const content = { x: 0, y: 0, w: 119, h: 208 };
const cell5 = mod.boxRect(gridBox(5), content);
check("格5 = 正中（x/y 各 1/3）", near(cell5.x, 119 / 3) && near(cell5.y, 208 / 3), JSON.stringify(cell5));
const cell8 = mod.boxRect(gridBox(8), content);
check("格8 = 中下（x=1/3, y=2/3）", near(cell8.x, 119 / 3) && near(cell8.y, (208 * 2) / 3), JSON.stringify(cell8));

for (let cell = 1; cell <= 9; cell += 1) {
    const rect = mod.boxRect(gridBox(cell), content);
    check(
        `格${cell} 框不越内容盒`,
        rect.x >= -1e-9 && rect.y >= -1e-9 && rect.x + rect.w <= content.w + 1e-9 && rect.y + rect.h <= content.h + 1e-9,
        JSON.stringify(rect),
    );
    check(`格${cell} 框画满一格（宽高>0）`, rect.w > 1 && rect.h > 1, JSON.stringify(rect));
}

const overflow = mod.boxRect({ "x%": 120, "y%": -20, "w%": 50, "h%": 50 }, content);
check("越界百分比夹取到内容盒内（w/h 收缩为 0）", overflow.x === content.w && overflow.y === 0 && overflow.w === 0, JSON.stringify(overflow));

// ---------------------------------------------------------------- 角标门槛
check("格框放得下 #行号 角标（119x208 竖屏格 39.7x69.3）", mod.badgeFits(cell5) === true, JSON.stringify(cell5));
check("过窄框不画角标", mod.badgeFits({ x: 0, y: 0, w: 24, h: 60 }) === false);
check("过矮框不画角标", mod.badgeFits({ x: 0, y: 0, w: 60, h: 12 }) === false);

// ---------------------------------------------------------------- 图例 rail
const stage = portrait;
const sameCell = [2, 3, 4, 6].map((row) => ({ 行: row, box: mod.boxRect(gridBox(5), contentPortrait) }));
const legend4 = mod.layoutLegend(sameCell, stage);
check("同格 4 框 → 图例 4 条（不按格去重身份）", legend4.chips.length === 4, String(legend4.chips.length));
check("图例 rail 在舞台右侧不压图", legend4.chips.every((chip) => chip.rect.x >= stage.w), JSON.stringify(legend4.chips[0]?.rect));
check(
    "图例行号升序",
    legend4.chips.map((chip) => chip.行).join(",") === "2,3,4,6",
    legend4.chips.map((chip) => chip.行).join(","),
);
let legendOverlap = false;
for (let i = 0; i < legend4.chips.length; i += 1) {
    for (let j = i + 1; j < legend4.chips.length; j += 1) {
        if (overlap(legend4.chips[i].rect, legend4.chips[j].rect)) legendOverlap = true;
    }
}
check("图例 chip rect 两两不相交", legendOverlap === false);
check(
    "图例不越舞台高度",
    legend4.chips.every((chip) => chip.rect.y + chip.rect.h <= legend4.height + 1e-9) && legend4.height >= stage.h,
    `height=${legend4.height}`,
);
check("引线端点各指向本行框右缘中点", legend4.links.length === 4 && near(legend4.links[0].x2, cell5.x + cell5.w), JSON.stringify(legend4.links[0]));
check("引线起点在 chip 左缘中点", near(legend4.links[0].y1, legend4.chips[0].rect.y + legend4.chips[0].rect.h / 2));

const legend9 = mod.layoutLegend(
    Array.from({ length: 9 }, (_, i) => ({ 行: i + 1, box: mod.boxRect(gridBox((i % 9) + 1), content) })),
    stage,
);
let overlap9 = false;
for (let i = 0; i < legend9.chips.length; i += 1) {
    for (let j = i + 1; j < legend9.chips.length; j += 1) {
        if (overlap(legend9.chips[i].rect, legend9.chips[j].rect)) overlap9 = true;
    }
}
check("9 行满格图例仍不相交", overlap9 === false);
check("9 行时 chip 缩到紧凑态", legend9.chips[0].compact === true && legend9.chips[0].rect.h >= mod.LEGEND_CHIP_H_MIN);
check("空图例不占宽", mod.layoutLegend([], stage).chips.length === 0 && mod.layoutLegend([], stage).w === 0);

// ---------------------------------------------------------------- 三态 + 文案
const preciseCard = {
    代表帧: { url: "/api/hub/frame/auto-0012/5.35.png", 秒: 5.35, 镜次: 3, 定位: "精确" },
    自身框: true,
    帧内框: [
        { 行: 1, 本卡: true },
        { 行: 5, 本卡: false },
    ],
    帧: [{ url: "/api/hub/frame/auto-0012/5.35.png" }],
    画面位置: { 格: 8, 镜次: 3 },
};
const noOwn = {
    代表帧: { url: "/api/hub/frame/auto-0012/5.35.png", 秒: 5.35, 镜次: 3, 定位: "精确" },
    自身框: false,
    帧内框: [
        { 行: 1, 本卡: false },
        { 行: 5, 本卡: false },
    ],
    帧: [],
    画面位置: null,
};
const unlocated = {
    代表帧: { url: "/api/hub/frame/auto-0012/3.42.png", 秒: 3.42, 镜次: null, 定位: "未定位" },
    自身框: false,
    帧内框: [],
    帧: [],
    画面位置: null,
};
const legacy = {
    代表帧: { url: "", 秒: 0, 镜次: null, 定位: "未定位" },
    自身框: false,
    帧内框: [],
    帧: [{ url: "/api/hub/frame/auto-0012/at8-1-3.42.png" }],
    画面位置: { 格: 5, 镜次: 2 },
};

check("精确态判定", mod.frameState(preciseCard) === "precise", mod.frameState(preciseCard));
check("无自身位置态判定（#7 形态：本卡全 false）", mod.frameState(noOwn) === "no-own-box", mod.frameState(noOwn));
check("未定位态判定", mod.frameState(unlocated) === "unlocated", mod.frameState(unlocated));
check("旧字段驱动走 legacy（代表帧空→帧[0]）", mod.frameState(legacy) === "legacy", mod.frameState(legacy));
check("代表帧与帧[] 都空 → 未定位", mod.frameState({ 代表帧: { url: "" }, 帧: [], 帧内框: [] }) === "unlocated");

const capPrecise = mod.frameCaption(preciseCard);
check("精确态标注含镜次+秒+AI粗标", capPrecise.main === "第3镜 · 5.35s · AI粗标", capPrecise.main);
check("精确态无诚实补语", capPrecise.honest === "", capPrecise.honest);
const capNoOwn = mod.frameCaption(noOwn);
check("无自身位置态仍标镜次", capNoOwn.main === "第3镜 · 5.35s · AI粗标", capNoOwn.main);
check("无自身位置态明示未能标注位置", capNoOwn.honest === "未能标注位置", capNoOwn.honest);
const capUnlocated = mod.frameCaption(unlocated);
check("未定位态文案逐字", capUnlocated.main === "未能定位到该资产出现的镜头 · 暂用整片画面", capUnlocated.main);
check("未定位态不含镜次/秒", !/第\d+镜|\d+s/.test(capUnlocated.main), capUnlocated.main);
check("未定位态无诚实补语/提示", capUnlocated.honest === "" && capUnlocated.hint === "");
const capLegacy = mod.frameCaption(legacy);
check("legacy 文案沿用九宫格口径", capLegacy.main.includes("九宫格第5格") && capLegacy.main.includes("基于第2镜"), capLegacy.main);

check("秒数保小数去尾零", mod.formatSeconds(5.35) === "5.35s" && mod.formatSeconds(1) === "1s" && mod.formatSeconds(0) === "", `${mod.formatSeconds(5.35)}/${mod.formatSeconds(1)}/${mod.formatSeconds(0)}`);
check("同画面其他资产计数", mod.otherBoxCount([{ 本卡: true }, { 本卡: false }, { 本卡: false }]) === 2);
check("本卡唯一时其他资产数=总数-1", mod.otherBoxCount([{ 本卡: false }, { 本卡: true }]) === 1);

// ---------------------------------------------------------------- 同一帧多卡：仅本卡不同
const cardA = { 帧内框: [{ 行: 1, 本卡: true }, { 行: 5, 本卡: false }] };
const cardB = { 帧内框: [{ 行: 1, 本卡: false }, { 行: 5, 本卡: true }] };
const setOf = (card) => card.帧内框.map((box) => box.行).sort((a, b) => a - b).join(",");
check("共享帧：两卡框集合相同", setOf(cardA) === setOf(cardB), `${setOf(cardA)} vs ${setOf(cardB)}`);
check("共享帧：本卡标记各自唯一", cardA.帧内框.filter((b) => b.本卡).length === 1 && cardB.帧内框.filter((b) => b.本卡).length === 1);

// ---------------------------------------------------------------- C4 回归：缓存命中挂载（onLoad 不触发）
// 真机实锤（auto-0012/pop-4）：antd Image 不透传 onLoad，且缓存命中的 load 早于 React 挂载
// → ratio 恒 null、竖屏帧按 16:9 画、框落到 letterbox 黑边上。以下断言锁死「挂载期主动查 complete」这条路。
const cachedImg = { complete: true, naturalWidth: 960, naturalHeight: 1678 }; // 真机竖图 960×1678
const cachedRatio = mod.ratioFromImage(cachedImg);
check("缓存命中：complete=true 即刻给出真实比例", cachedRatio !== null && near(cachedRatio, 960 / 1678, 1e-9), String(cachedRatio));

const cachedStage = mod.stageSize(cachedRatio);
check(
    "缓存命中：竖屏舞台宽<高（119×208 型，不是 240×135 横盒）",
    cachedStage.h === 208 && cachedStage.w < cachedStage.h && cachedStage.w === Math.round(208 * (960 / 1678)),
    JSON.stringify(cachedStage),
);
const cachedContent = mod.containRect(cachedRatio, cachedStage);
check(
    "缓存命中：内容盒=舞台（框百分比落在真实像素区）",
    near(cachedContent.x, 0) && near(cachedContent.y, 0) && near(cachedContent.w, cachedStage.w) && near(cachedContent.h, cachedStage.h),
    JSON.stringify(cachedContent),
);
const cachedGrid8 = mod.boxRect(gridBox(8), cachedContent);
check(
    "缓存命中：格8 框完整落在内容盒内（不再飘到 letterbox 外）",
    cachedGrid8.y >= -1e-9 && cachedGrid8.y + cachedGrid8.h <= cachedContent.y + cachedContent.h + 1e-9,
    JSON.stringify(cachedGrid8),
);

check("未加载完成（complete=false）不臆测比例", mod.ratioFromImage({ complete: false, naturalWidth: 0, naturalHeight: 0 }) === null);
check("坏图（complete=true 但自然尺寸 0）不给比例", mod.ratioFromImage({ complete: true, naturalWidth: 0, naturalHeight: 0 }) === null);
check("缺元素/空值安全", mod.ratioFromImage(null) === null && mod.ratioFromImage(undefined) === null);

// 缺陷特征对照：旧实现 ratio 恒 null 时舞台就是 240×135 横盒——上面的断言正是在抓这个态。
const buggyStage = mod.stageSize(null);
check("缺陷特征对照：ratio=null 时舞台横向（旧实现即此态）", buggyStage.w === mod.STAGE_MAX_W && buggyStage.w > buggyStage.h, JSON.stringify(buggyStage));

// 接线护栏（源码级）：组件必须走「挂载期查 complete」，且不得再依赖 antd Image 的 onLoad（该链路不透传）。
const drawerSrc = readFileSync(path.resolve("src/pages/hub/task-detail-drawer.tsx"), "utf8");
check("drawer 已接线 ratioFromImage（挂载/换图期判定比例）", /ratioFromImage\(/.test(drawerSrc));
check("drawer 自带 Image 探针兜底（new Image() 在 effect 内）", /new Image\(\)/.test(drawerSrc));
check("drawer 不再给 AntdImage 传 onLoad（antd 不透传，写了也是死代码）", !/<AntdImage[\s\S]{0,300}?onLoad=\{/.test(drawerSrc));

rmSync(outDir, { recursive: true, force: true });

const total = pass + fails.length;
if (fails.length) {
    console.error(`AT-10 C3 几何自检 FAIL ${pass}/${total}`);
    for (const item of fails) console.error(`  ✗ ${item}`);
    process.exit(1);
}
console.log(`AT-10 C3 几何自检 PASS ${pass}/${total}`);
