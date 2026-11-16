/**
 * S2.7 探针：拉片耗时摘要（前端纯逻辑 + 节点展示位接线）。
 *
 * 为什么要有这一条：耗时分解的数据在 bridge（`GET /api/lapian/timing`），前端只做两件事——
 * 一行摘要 + 完成态取一次。两件事都可能悄悄坏掉（格式串错、404 让节点报错、单人模式也发请求
 * → 凭空多一条 404），所以用不连 bridge、不起浏览器的方式把口径钉住。
 *
 * 判据：
 *  Y1 秒数格式：m:ss，缺失/负数/非数 → 「—」（不知道 ≠ 0）
 *  Y2 摘要口径：总/抽帧/模型等待/重拆四段，缺失段整段省略，全缺 → 空串（调用方不渲染）
 *  Y3 悬停明细写全四段；不可用时回 reason 而不是造数
 *  Y4 fetch：200+available:true → 视图；404（单人模式）/401/500/available:false/坏 JSON/网络抛 → null（绝不抛）
 *  Y5 节点接线：`data-lapian-timing` 只在 done 态渲染，且请求受 remoteMode 门控（单人模式零请求）
 *
 * 跑法：node scripts/probe-s2-7-lapian-timing.mjs   （纯逻辑 + 源码文本级断言，不连 bridge、不花钱）
 */
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const src = path.join(root, "src");
const workdir = mkdtempSync(path.join(tmpdir(), "probe-s2-7-"));

writeFileSync(
    path.join(workdir, "entry.ts"),
    `export * as timing from ${JSON.stringify(path.join(src, "lib/canvas/lapian-timing.ts").replace(/\\/g, "/"))};\n`,
);

const { build } = await import("esbuild");
const outfile = path.join(workdir, "bundle.mjs");
await build({
    entryPoints: [path.join(workdir, "entry.ts")],
    outfile,
    bundle: true,
    format: "esm",
    platform: "browser",
    logLevel: "silent",
    alias: { "@": src },
    define: { "process.env.NODE_ENV": '"test"' },
});
const mod = (await import(pathToFileURL(outfile).href)).timing;

// ---- 断言工具 -----------------------------------------------------------------------
let passed = 0;
const failures = [];
function check(name, condition, detail) {
    if (condition) {
        passed += 1;
        return;
    }
    failures.push(`${name}${detail ? ` —— ${detail}` : ""}`);
}

const FULL = {
    available: true,
    task: "t1",
    总耗时: 192.4,
    抽帧耗时: 18,
    大拆解等待: 160,
    门禁重拆轮次: 1,
};

// ---- Y1 秒数格式 ---------------------------------------------------------------------
check("Y1 192s → 3:12", mod.formatTimingSeconds(192) === "3:12", mod.formatTimingSeconds(192));
check("Y1 5s → 0:05（补零）", mod.formatTimingSeconds(5) === "0:05", mod.formatTimingSeconds(5));
check("Y1 0 → 0:00", mod.formatTimingSeconds(0) === "0:00", mod.formatTimingSeconds(0));
check(
    "Y1 缺失/负数/非数 → —",
    mod.formatTimingSeconds(null) === "—" &&
        mod.formatTimingSeconds(undefined) === "—" &&
        mod.formatTimingSeconds(-1) === "—" &&
        mod.formatTimingSeconds(Number.NaN) === "—",
    [mod.formatTimingSeconds(null), mod.formatTimingSeconds(-1)],
);

// ---- Y2 摘要口径 ---------------------------------------------------------------------
const fullSummary = mod.buildTimingSummary(FULL);
check(
    "Y2 四段齐全：总/抽帧/模型等待/重拆",
    fullSummary === "总 3:12｜抽帧 0:18｜模型等待 2:40｜重拆 1 轮",
    fullSummary,
);
const partial = mod.buildTimingSummary({ available: true, 总耗时: 192.4, 抽帧耗时: null, 大拆解等待: null, 门禁重拆轮次: 0 });
check("Y2 缺失段整段省略（不写 0）", partial === "总 3:12｜重拆 0 轮", partial);
check("Y2 全缺 → 空串（调用方不渲染）", mod.buildTimingSummary({ available: true }) === "", mod.buildTimingSummary({ available: true }));
check("Y2 available:false → 空串", mod.buildTimingSummary({ available: false, 总耗时: 10 }) === "");
check("Y2 null/undefined → 空串", mod.buildTimingSummary(null) === "" && mod.buildTimingSummary(undefined) === "");

// ---- Y3 悬停明细 ---------------------------------------------------------------------
const title = mod.buildTimingTitle(FULL);
check(
    "Y3 明细四行（总/抽帧/大拆解/重拆）",
    title.split("\n").length === 4 && title.includes("总耗时 3:12") && title.includes("重拆 1 轮"),
    JSON.stringify(title),
);
check(
    "Y3 不可用 → 回 reason，不造数",
    mod.buildTimingTitle({ available: false, reason: "耗时.json 还没生成" }) === "耗时.json 还没生成",
    mod.buildTimingTitle({ available: false, reason: "耗时.json 还没生成" }),
);

// ---- Y4 fetch 口径（stub，不出网）-----------------------------------------------------
function stubFetch(status, body, { throws = false } = {}) {
    return async () => {
        if (throws) throw new Error("network down");
        return { ok: status >= 200 && status < 300, status, json: async () => body };
    };
}
const ok = await mod.fetchLapianTiming("t1", { fetch: stubFetch(200, FULL) });
check("Y4 200+available:true → 视图", ok && ok["总耗时"] === 192.4, JSON.stringify(ok));

for (const [label, stub] of [
    ["404（单人模式 / 他人任务）", stubFetch(404, { error: "任务不存在" })],
    ["401（未登录）", stubFetch(401, { error: "未登录" })],
    ["500", stubFetch(500, { error: "服务内部异常" })],
    ["200 但 available:false", stubFetch(200, { available: false, reason: "还没生成" })],
    ["200 但坏 JSON", stubFetch(200, null)],
    ["网络异常", stubFetch(0, null, { throws: true })],
]) {
    const result = await mod.fetchLapianTiming("t1", { fetch: stub });
    check(`Y4 ${label} → null（不抛）`, result === null, JSON.stringify(result));
}
let threw = false;
try {
    await mod.fetchLapianTiming("", { fetch: stubFetch(200, FULL) });
} catch {
    threw = true;
}
check("Y4 空 taskId → null 且不发请求（不抛）", threw === false);

// ---- Y5 节点接线（源码文本级）--------------------------------------------------------
const node = readFileSync(path.join(src, "components/canvas/nodes/lapian-node.tsx"), "utf8");
// 取「耗时摘要 effect」块（从注释到 deps 数组）——只在这块里断言，避免全文误命中
const effectStart = node.indexOf("S2.7 耗时摘要：完成态拉一次分解");
const effectEnd = node.indexOf("const requery", effectStart);
const effect = effectStart >= 0 && effectEnd > effectStart ? node.slice(effectStart, effectEnd) : "";
const doneBlock = node.slice(node.indexOf('{phase === "done" && ('));
check("Y5 节点渲染 data-lapian-timing", node.includes("data-lapian-timing"));
check(
    "Y5 摘要只在 done 态块内渲染（不在 loading/error 里）",
    doneBlock.includes("data-lapian-timing") && !node.slice(0, node.indexOf('{phase === "done" && (')).includes("data-lapian-timing"),
    `doneBlock=${doneBlock.length}`,
);
check(
    "Y5 请求受 remoteMode 门控（单人模式零请求）",
    node.includes('channelMode === "remote"') && effect.includes('if (!remoteMode || phase !== "done"'),
    effect ? effect.slice(0, 120) : "effect not found",
);
check(
    "Y5 取数失败不改变节点相位（effect 内无 setPhase）",
    Boolean(effect) && !effect.includes("setPhase"),
    effect.includes("setPhase") ? "setPhase leaked into timing effect" : "",
);

// ---- 汇总 ---------------------------------------------------------------------------
if (failures.length) {
    console.log(`\nS2.7 耗时摘要探针：${passed} passed / ${failures.length} failed`);
    for (const item of failures) console.log(`FAIL ${item}`);
    process.exit(1);
}
console.log(`S2.7 耗时摘要探针：${passed} passed / 0 failed`);
