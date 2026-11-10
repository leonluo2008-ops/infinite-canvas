/**
 * AT-10 C5 幂等命中判定自检（免费探针）：esbuild 打包 web/src/lib/hub/ingest-hit.ts 后直接断言真代码
 * + 源码接线护栏（两个入口的命中分支必须在触发拆解之前，且详情打开走 onCreated(localId, hit)）。
 *
 * 跑法：cd web && node scripts/probe-at10-c5.mjs   （失败 rc=1）
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const outDir = path.resolve("node_modules/.probe");
mkdirSync(outDir, { recursive: true });
const outFile = path.join(outDir, "at10-c5.mjs");

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
    entryPoints: [path.resolve("src/lib/hub/ingest-hit.ts")],
    bundle: true,
    format: "esm",
    platform: "neutral",
    outfile: outFile,
    logLevel: "silent",
    alias: { "@": path.resolve("src") },
});

const mod = await import(pathToFileURL(outFile).href);

// ---------------------------------------------------------------- 命中判定
check("新后端返回（result=HIT + hit_existing=true）→ 命中", mod.isIngestHit({ local_id: "auto-0007", result: "HIT", hit_existing: true }) === true);
check("只有 hit_existing=true 也判命中（result 缺省不误判）", mod.isIngestHit({ local_id: "auto-0007", hit_existing: true }) === true);
check("只有 result=HIT（旧后端形状）也判命中（向后兼容）", mod.isIngestHit({ local_id: "auto-0007", result: "HIT" }) === true);
check("result 小写/带空格也不漏判", mod.isIngestHit({ local_id: "auto-0007", result: " hit " }) === true);
check("新建路径（OK + hit_existing=false）→ 不命中", mod.isIngestHit({ local_id: "auto-0008", result: "OK", hit_existing: false }) === false);
check("旧后端 OK 形状（无 hit_existing 字段）→ 不命中", mod.isIngestHit({ local_id: "auto-0008", result: "OK" }) === false);
check("DRY-OK → 不命中", mod.isIngestHit({ local_id: null, result: "DRY-OK", hit_existing: false }) === false);
check("空/未定义结果 → 不命中（不炸）", mod.isIngestHit(null) === false && mod.isIngestHit(undefined) === false && mod.isIngestHit({ local_id: null }) === false);

// ---------------------------------------------------------------- 文案
check("命中文案点名任务号+已打开", mod.ingestHitMessage("auto-0007") === "该视频已有任务 auto-0007，已为你打开", mod.ingestHitMessage("auto-0007"));
const withName = mod.ingestHitMessage("auto-0007", "参考片.mp4");
check("带片名时片名入文案且任务号仍在", withName.includes("auto-0007") && withName.includes("参考片.mp4"), withName);
check("新建文案不冒充命中（与命中文案区分）", mod.ingestCreatedMessage("auto-0007") === "任务 auto-0007 已建立，开始拆解" && mod.ingestCreatedMessage("auto-0007") !== mod.ingestHitMessage("auto-0007"));
check("命中文案含「已有任务」而不是「已建立」（语义不混淆）", mod.ingestHitMessage("auto-0007").includes("已有任务") && !mod.ingestHitMessage("auto-0007").includes("已建立"));

// ---------------------------------------------------------------- 源码接线（表格视图弹窗）
const modal = readFileSync("src/pages/hub/new-task-modal.tsx", "utf8");
const modalHitAt = modal.indexOf("if (isIngestHit(created))");
const modalRunAt = modal.indexOf("await startLapianRun(");
check("弹窗调用 isIngestHit 判命中", modalHitAt >= 0);
check("弹窗在触发拆解之前判命中（命中分支不被拆解抢先）", modalHitAt >= 0 && modalRunAt >= 0 && modalHitAt < modalRunAt, `hit@${modalHitAt} run@${modalRunAt}`);
check("命中分支提前 return（命中不落到建任务/拆解路径）", /if \(isIngestHit\(created\)\) \{[\s\S]{0,700}?\n\s*return;/.test(modal));
check("命中提示走 ingestHitMessage（点名任务号）", /message\.info\(ingestHitMessage\(/.test(modal));
check("命中回报交给上层打开详情（onCreated(localId, true)）", /onCreated\(created\.local_id, true\)/.test(modal));
check("新建路径回报 hit=false（不误开详情）", /onCreated\(created\.local_id, false\)/.test(modal));

// ---------------------------------------------------------------- 源码接线（表格视图容器）
const page = readFileSync("src/pages/hub/index.tsx", "utf8");
check("表格视图 onCreated 解构 hit 标记", /onCreated=\{\(localId, hit\) =>/.test(page));
check("命中时打开该任务详情（setOpenId(localId) 在 hit 分支里）", /if \(hit\) setOpenId\(localId\)/.test(page));

// ---------------------------------------------------------------- 源码接线（legacy 卡片也是同一判定）
const card = readFileSync("src/pages/hub/new-task-card.tsx", "utf8");
const cardHitAt = card.indexOf("if (isIngestHit(created))");
const cardRunAt = card.indexOf("await startLapianRun(");
check("legacy 卡片同样在触发拆解前判命中", cardHitAt >= 0 && cardRunAt >= 0 && cardHitAt < cardRunAt, `hit@${cardHitAt} run@${cardRunAt}`);
check("legacy 卡片命中提示同样点名任务号", /message\.info\(ingestHitMessage\(/.test(card));

// ---------------------------------------------------------------- 接口层透传
const api = readFileSync("src/pages/hub/hub-api.ts", "utf8");
check("hub-api 的 ingestVideo 返回 IngestOutcome（含 hit_existing/result 字段）", /ingestVideo\(video: File, intent: string\): Promise<IngestOutcome>/.test(api) && api.includes('export type { IngestOutcome }'));
check("IngestOutcome 类型声明带 hit_existing", readFileSync("src/lib/hub/ingest-hit.ts", "utf8").includes("hit_existing"));

console.log(`[probe-at10-c5-ts] ${pass}/${pass + fails.length} PASS`);
for (const f of fails) console.log(`  FAIL ${f}`);
process.exit(fails.length ? 1 : 0);
