/**
 * AT-16 B4a 前端探针（本地免费、不联网、不动真实任务）：**拆解进度双锚走表**。
 *
 * 缺陷：拆解远程等待段进度假冻结——`lapian_stage` 是静态阶段字符串，界面上的「已 Xs」是
 * 日志行里的 job 总耗时快照，10 分钟不动。本探针锁「读数」这一件事（锚由
 * wanrepro/automation/probe_at16_b4a.py 锁写侧/读侧），全程**可控时钟**，不真等 2 分钟：
 *
 *  [A] 纯函数口径（lapian-clock + hub-api 归一）：
 *      · 无阶段变化期间计时持续推进（同一批锚，now 前进 → 两个值都前进）
 *      · 换阶段重置阶段计时、总耗时不归零（两个值语义分开）
 *      · 终态（成功/失败）停表：ended_at 有值后，now 再前进读数不动
 *      · 缺锚 → 两值 null 且文案「计时不可用」，不写 0 秒冒充
 *      · 日志行里的「已 600s」/只有 stage 字符串都不当锚（不从字符串/日志反推）
 *      · 归一函数对畸形输入（null/数字/字符串）一律空串
 *  [B] 真渲染双处（真代码 + SSR，Date.now 打桩成可控时钟）：
 *      · 主表 LapianStageCell（index.tsx）与详情 ProgressSection（task-detail-drawer.tsx）
 *        运行中出「总耗时 X / 本阶段已等待 Y」两值；缺锚出「计时不可用」；终态 frozen=1
 *      · 两处文本逐字一致（同一份文案口径，不是各写一套）
 *  [C] 红线 25 静态守卫：计时只有一套——两个消费方都从 ./lapian-clock 引入，
 *      文件内不自己算 elapsed（不出现 Date.now() - 锚 的算式）。
 *
 * 跑法：cd web && node scripts/probe-at16-b4a.mjs      （失败 rc=1）
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const WEB = process.cwd();
const CACHE = path.join(WEB, "node_modules", ".cache", "at16-b4a");
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

// ════════════════════════════════════════════════════════════ 夹具（时间全部相对可控时钟）

const BASE = Date.parse("2026-11-11T20:00:00+08:00");
const iso = (offsetSec) => new Date(BASE + offsetSec * 1000).toISOString();
/** 运行中：启动 65s 前、阶段 25s 前（两个值应该不同，不能混成一个）。 */
const RUNNING = { started_at: iso(-65), stage_at: iso(-25), ended_at: "" };
/** 终态：启动 90s 前、阶段 40s 前、30s 前已结束（停在结束时刻）。 */
const FROZEN = { started_at: iso(-90), stage_at: iso(-40), ended_at: iso(-30) };
/** 旧任务：只有阶段字符串 + 日志行（含「已 600s」），没有任何锚。 */
const LEGACY = {};
const LEGACY_STAGE = "阶段B：远程等待（已 600s）";

// ════════════════════════════════════════════════════════════ 打包真代码
const entry = path.join(CACHE, "ssr-entry.tsx");
writeFileSync(
    entry,
    'import { renderToStaticMarkup } from "react-dom/server";\n' +
        'import { LapianStageCell } from "../../../src/pages/hub/index";\n' +
        'import { ProgressSection } from "../../../src/pages/hub/task-detail-drawer";\n' +
        'import { formatLapianDuration, lapianClockAt, lapianClockTexts, lapianStageName } from "../../../src/pages/hub/lapian-clock";\n' +
        'import { normalizeLapianAnchors } from "../../../src/pages/hub/hub-api";\n' +
        "export function renderCell(task: any): string {\n" +
        "    return renderToStaticMarkup(<LapianStageCell task={task} />);\n" +
        "}\n" +
        "export function renderProgress(detail: any, state: string): string {\n" +
        "    return renderToStaticMarkup(<ProgressSection detail={detail} state={state} />);\n" +
        "}\n" +
        "export { formatLapianDuration, lapianClockAt, lapianClockTexts, lapianStageName, normalizeLapianAnchors };\n",
);
const aliasPlugin = {
    name: "at16b4a-alias",
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
check("[B] 真渲染打包成功（index.tsx / task-detail-drawer.tsx / lapian-clock 真代码，React·antd 走宿主机）",
    built.errors.length === 0, built.errors.map((err) => `${err.location?.file}:${err.text}`).join("; "));

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

// ════════════════════════════════════════════════════════════ [A] 纯函数口径
console.log("1) 可控时钟：持续推进 / 换阶段重置 / 终态停表 / 缺锚诚实");

const t0 = ssr.lapianClockAt(RUNNING, BASE);
check("[A] 运行中读数：总耗时 65s、本阶段已等待 25s（两个值分开算，不共用）",
    t0.total === 65 && t0.stage === 25, JSON.stringify(t0));
const t1 = ssr.lapianClockAt(RUNNING, BASE + 60_000);
check("[A] 无阶段变化期间计时持续推进（now +60s → 两个值都 +60s，不再冻结）",
    t1.total === 125 && t1.stage === 85, JSON.stringify(t1));
check("[A] 未停表（无 ended_at → frozen=false）", t0.frozen === false, JSON.stringify(t0));

const tSwitch = ssr.lapianClockAt({ ...RUNNING, stage_at: iso(-5) }, BASE);
check("[A] 换阶段重置阶段计时（阶段锚前进 → 本阶段 5s），总耗时不归零（仍 65s）",
    tSwitch.total === 65 && tSwitch.stage === 5, JSON.stringify(tSwitch));

const tFrozen = ssr.lapianClockAt(FROZEN, BASE);
const tFrozenLater = ssr.lapianClockAt(FROZEN, BASE + 3600_000);
check("[A] 终态停表：读数冻结在 ended_at（总 60s / 阶段 10s），frozen=true",
    tFrozen.total === 60 && tFrozen.stage === 10 && tFrozen.frozen === true, JSON.stringify(tFrozen));
check("[A] 终态停表：一小时后读数仍不变（不再随墙上钟走）",
    JSON.stringify(tFrozen) === JSON.stringify(tFrozenLater), `${JSON.stringify(tFrozen)} vs ${JSON.stringify(tFrozenLater)}`);
check("[A] 停表有人话说明（「已停表」），不静默",
    /已停表/.test(tFrozen.note), tFrozen.note);

const tMissing = ssr.lapianClockAt(LEGACY, BASE);
const textsMissing = ssr.lapianClockTexts(tMissing);
check("[A] 缺锚 → 两值 null（不写 0 秒冒充）",
    tMissing.total === null && tMissing.stage === null, JSON.stringify(tMissing));
check("[A] 缺锚文案明说「计时不可用」",
    /计时不可用/.test(textsMissing.total) && /计时不可用/.test(textsMissing.stage)
    && /拆解启动锚/.test(tMissing.note), JSON.stringify(textsMissing));
check("[A] 缺锚时不出现伪造秒数（文案里没有「总耗时 0 秒」）",
    !/总耗时 0 秒/.test(textsMissing.total), textsMissing.total);

const tStageOnly = ssr.lapianClockAt({ stage_at: iso(-30) }, BASE);
const textsStageOnly = ssr.lapianClockTexts(tStageOnly);
check("[A] 只有阶段锚：本阶段照走（30s），总耗时诚实说不可用（逐值诚实，不整体降级）",
    tStageOnly.stage === 30 && tStageOnly.total === null && /计时不可用/.test(textsStageOnly.total),
    JSON.stringify({ clock: tStageOnly, texts: textsStageOnly }));

const tBad = ssr.lapianClockAt({ started_at: "不是时间", stage_at: "2026-13-40T99:99:99Z" }, BASE);
check("[A] 坏 ISO → 当缺锚（null），不抛异常不猜",
    tBad.total === null && tBad.stage === null, JSON.stringify(tBad));

const tSkew = ssr.lapianClockAt({ started_at: iso(120), stage_at: iso(120) }, BASE);
check("[A] 时钟回拨/锚在未来 → 夹到 0 秒（不出现负数）",
    tSkew.total === 0 && tSkew.stage === 0, JSON.stringify(tSkew));

// 日志行里的「已 Xs」不是锚：给了阶段字符串与日志，没有锚就必须是「计时不可用」
const legacyFull = { lapian_stage: LEGACY_STAGE, log_tail: "[20:31:02] 拆解启动\n[20:41:05] 阶段B：远程等待（已 600s）", lapian: LEGACY };
const tLegacy = ssr.lapianClockAt(legacyFull.lapian, BASE);
check("[A] 日志「已 600s」/stage 字符串都不被当锚（不从字符串或日志反推启动时刻）",
    tLegacy.total === null && tLegacy.stage === null && /不计/.test(tLegacy.note || "") === false,
    JSON.stringify({ clock: tLegacy, stage: legacyFull.lapian_stage }));

check("[A] 时长格式化：35s / 125s / 3725s",
    ssr.formatLapianDuration(35) === "35 秒"
    && ssr.formatLapianDuration(125) === "2 分 05 秒"
    && ssr.formatLapianDuration(3725) === "1 小时 02 分",
    [ssr.formatLapianDuration(35), ssr.formatLapianDuration(125), ssr.formatLapianDuration(3725)].join(" | "));

// 归一：只认字符串真值
const normGarbage = ssr.normalizeLapianAnchors({ started_at: 123, stage_at: null, ended_at: {} });
check("[A] 归一：畸形锚（数字/null/对象）→ 空串（前端不把怪值当时间）",
    normGarbage.started_at === "" && normGarbage.stage_at === "" && normGarbage.ended_at === "",
    JSON.stringify(normGarbage));
const normValid = ssr.normalizeLapianAnchors(RUNNING);
check("[A] 归一：合法 ISO 原样透传（不重排、不改格式）",
    normValid.started_at === RUNNING.started_at && normValid.stage_at === RUNNING.stage_at,
    JSON.stringify(normValid));
check("[A] 归一：非对象（null / 字符串 / 缺字段）→ 三空串",
    JSON.stringify(ssr.normalizeLapianAnchors(null)) === JSON.stringify({ started_at: "", stage_at: "", ended_at: "" })
    && JSON.stringify(ssr.normalizeLapianAnchors("x")) === JSON.stringify({ started_at: "", stage_at: "", ended_at: "" }),
    JSON.stringify(ssr.normalizeLapianAnchors(null)));

// 阶段文案显示裁剪：丢掉末尾「（已 Xs）」快照，数据层原文不动
check("[A] 阶段显示裁剪：末尾「（已 95s）」不显示（那个数字是 job 总耗时快照，会和走表读数自相矛盾）",
    ssr.lapianStageName("阶段B：远程等待（已 95s）") === "阶段B：远程等待",
    ssr.lapianStageName("阶段B：远程等待（已 95s）"));
check("[A] 阶段显示裁剪：半角括号 / 无后缀 / 终态行都不误伤",
    ssr.lapianStageName("远程等待 (已 600s)") === "远程等待"
    && ssr.lapianStageName("阶段A：提交拆解") === "阶段A：提交拆解"
    && ssr.lapianStageName("终态：awaiting_assets（等置换）") === "终态：awaiting_assets（等置换）",
    [ssr.lapianStageName("远程等待 (已 600s)"), ssr.lapianStageName("终态：awaiting_assets（等置换）")].join(" | "));
check("[A] 阶段显示裁剪：非字符串/空 → 空串（调用方兜「拆解中」）",
    ssr.lapianStageName(null) === "" && ssr.lapianStageName(undefined) === "" && ssr.lapianStageName("   ") === "",
    JSON.stringify([ssr.lapianStageName(null), ssr.lapianStageName("   ")]));

// ════════════════════════════════════════════════════════════ [B] 真渲染双处
console.log("2) 真渲染：主表 LapianStageCell + 详情 ProgressSection（Date.now 打桩成可控时钟）");

const realNow = Date.now;
Date.now = () => BASE;
let cellRunning = "";
let cellMissing = "";
let cellFrozen = "";
let progressRunning = "";
let progressMissing = "";
let progressFrozen = "";
try {
    cellRunning = ssr.renderCell({ local_id: "auto-9999", state: "discussing", stage: RUNNING && LEGACY_STAGE, lapian: RUNNING });
    cellMissing = ssr.renderCell({ local_id: "auto-9999", state: "discussing", stage: LEGACY_STAGE, lapian: LEGACY });
    cellFrozen = ssr.renderCell({ local_id: "auto-9999", state: "discussing", stage: LEGACY_STAGE, lapian: FROZEN });
    progressRunning = ssr.renderProgress({ local_id: "auto-9999", state: "discussing", lapian_stage: LEGACY_STAGE, log_tail: "", lapian: RUNNING }, "discussing");
    progressMissing = ssr.renderProgress({ local_id: "auto-9999", state: "discussing", lapian_stage: LEGACY_STAGE, log_tail: "", lapian: LEGACY }, "discussing");
    progressFrozen = ssr.renderProgress({ local_id: "auto-9999", state: "discussing", lapian_stage: LEGACY_STAGE, log_tail: "", lapian: FROZEN }, "discussing");
} finally {
    Date.now = realNow;
}

/** 取被标记元素的正文。 */
function textOf(html, marker) {
    const at = html.indexOf(marker);
    if (at < 0) return "";
    const gt = html.indexOf(">", at);
    if (gt < 0) return "";
    const end = html.indexOf("<", gt + 1);
    return html.slice(gt + 1, end < 0 ? undefined : end);
}
function attrOf(html, marker, name) {
    const at = html.indexOf(marker);
    if (at < 0) return "";
    const seg = html.slice(at, html.indexOf(">", at) + 1);
    const m = seg.match(new RegExp(`${name}="([^"]*)"`));
    return m ? m[1] : "";
}

check("[B] 主表渲染成功且带锚点标记（真组件，不是打桩）",
    cellRunning.includes("data-hub-lapian-cell"), cellRunning.slice(0, 200));
check("[B] 主表运行中：总耗时 65s + 本阶段已等待 25s 两值都出",
    textOf(cellRunning, "data-hub-lapian-total").includes("总耗时 1 分 05 秒")
    && textOf(cellRunning, "data-hub-lapian-stage-wait").includes("本阶段已等待 25 秒"),
    `${textOf(cellRunning, "data-hub-lapian-total")} | ${textOf(cellRunning, "data-hub-lapian-stage-wait")}`);
check("[B] 主表运行中：frozen=0（未停表）", attrOf(cellRunning, "data-hub-lapian-frozen", "data-hub-lapian-frozen") === "0",
    cellRunning.slice(0, 160));
check("[B] 主表缺锚：两值明说「计时不可用」",
    /计时不可用/.test(textOf(cellMissing, "data-hub-lapian-total"))
    && /计时不可用/.test(textOf(cellMissing, "data-hub-lapian-stage-wait")),
    `${textOf(cellMissing, "data-hub-lapian-total")} | ${textOf(cellMissing, "data-hub-lapian-stage-wait")}`);
check("[B] 主表缺锚：阶段文案照旧可读（不因缺锚丢阶段）",
    textOf(cellMissing, "data-hub-lapian-stage-name").includes("阶段B"), textOf(cellMissing, "data-hub-lapian-stage-name"));
check("[B] 主表：阶段名里的「（已 95s）」快照不上屏（只剩纯阶段名）",
    !/已\s*95s/.test(textOf(cellRunning, "data-hub-lapian-stage-name"))
    && textOf(cellRunning, "data-hub-lapian-stage-name").trim() === "阶段B：远程等待",
    textOf(cellRunning, "data-hub-lapian-stage-name"));
check("[B] 详情：同上，阶段 Tag 不带快照秒数（Tag 里只有阶段名）",
    !/已\s*95s/.test(progressRunning) && /阶段B：远程等待/.test(progressRunning),
    (progressRunning.match(/<span[^>]*ant-tag[^>]*>[^<]*/) || [""])[0]);
check("[B] 主表终态：frozen=1 且读数冻结在结束时刻（总 1 分 00 秒 / 阶段 10 秒）",
    attrOf(cellFrozen, "data-hub-lapian-frozen", "data-hub-lapian-frozen") === "1"
    && textOf(cellFrozen, "data-hub-lapian-total").includes("1 分 00 秒")
    && textOf(cellFrozen, "data-hub-lapian-stage-wait").includes("10 秒"),
    `${attrOf(cellFrozen, "data-hub-lapian-frozen", "data-hub-lapian-frozen")} | ${textOf(cellFrozen, "data-hub-lapian-total")}`);

check("[B] 详情渲染成功且带锚点标记（ProgressSection 真组件）",
    progressRunning.includes("data-hub-lapian-clock"), progressRunning.slice(0, 200));
check("[B] 详情运行中：总耗时 65s + 本阶段已等待 25s 两值都出",
    textOf(progressRunning, "data-hub-lapian-total").includes("总耗时 1 分 05 秒")
    && textOf(progressRunning, "data-hub-lapian-stage-wait").includes("本阶段已等待 25 秒"),
    `${textOf(progressRunning, "data-hub-lapian-total")} | ${textOf(progressRunning, "data-hub-lapian-stage-wait")}`);
check("[B] 详情缺锚：「计时不可用」+ 人话原因（缺拆解启动锚与阶段锚）",
    /计时不可用/.test(textOf(progressMissing, "data-hub-lapian-total"))
    && /拆解启动锚/.test(textOf(progressMissing, "data-hub-lapian-note")),
    `${textOf(progressMissing, "data-hub-lapian-total")} | ${textOf(progressMissing, "data-hub-lapian-note")}`);
check("[B] 详情终态：frozen=1 且带「已停表」说明",
    attrOf(progressFrozen, "data-hub-lapian-frozen", "data-hub-lapian-frozen") === "1"
    && /已停表/.test(textOf(progressFrozen, "data-hub-lapian-note")),
    `${attrOf(progressFrozen, "data-hub-lapian-frozen", "data-hub-lapian-frozen")} | ${textOf(progressFrozen, "data-hub-lapian-note")}`);
check("[B] 两处读数逐字一致（同一份文案口径，不是各写一套）",
    textOf(cellRunning, "data-hub-lapian-total") === textOf(progressRunning, "data-hub-lapian-total")
    && textOf(cellRunning, "data-hub-lapian-stage-wait") === textOf(progressRunning, "data-hub-lapian-stage-wait")
    && textOf(cellMissing, "data-hub-lapian-total") === textOf(progressMissing, "data-hub-lapian-total"),
    `${textOf(cellRunning, "data-hub-lapian-total")} vs ${textOf(progressRunning, "data-hub-lapian-total")}`);
check("[B] 两处都在（主表 + 详情双覆盖：列表摘要无日志兜底也走表，详情同源）",
    cellRunning.includes("data-hub-lapian-cell") && progressRunning.includes("data-hub-progress-section"),
    `cell=${cellRunning.includes("data-hub-lapian-cell")} progress=${progressRunning.includes("data-hub-progress-section")}`);

// ════════════════════════════════════════════════════════════ [C] 红线 25 静态守卫
console.log("3) 红线 25：计时只有一套（两个消费方都从 ./lapian-clock 取读数）");

const indexSrc = read("src/pages/hub/index.tsx");
const drawerSrc = read("src/pages/hub/task-detail-drawer.tsx");
const clockSrc = read("src/pages/hub/lapian-clock.ts");
check("[C] 主表从 ./lapian-clock 引入计时口径", /from "\.\/lapian-clock"/.test(indexSrc) && /useLapianClock/.test(indexSrc));
check("[C] 详情从 ./lapian-clock 引入计时口径", /from "\.\/lapian-clock"/.test(drawerSrc) && /useLapianClock/.test(drawerSrc));
check("[C] 两个消费方都不自己算 elapsed（没有 Date.now() − 锚 的算式）",
    !/Date\.now\(\)\s*-/.test(indexSrc) && !/Date\.now\(\)\s*-/.test(drawerSrc),
    `index=${/Date\.now\(\)\s*-/.test(indexSrc)} drawer=${/Date\.now\(\)\s*-/.test(drawerSrc)}`);
check("[C] 1 秒展示钟只存在于 lapian-clock.ts（且步长常量唯一）",
    /LAPIAN_TICK_MS = 1000/.test(clockSrc) && !/setInterval\([^)]*1000/.test(indexSrc) && !/setInterval\([^)]*1000/.test(drawerSrc),
    `clock=${/LAPIAN_TICK_MS = 1000/.test(clockSrc)}`);
check("[C] 主表仍只在 discussing 态出进度行（其他态不显示走表）",
    /task\.state === "discussing" \? <LapianStageCell task=\{task\} \/> : null/.test(indexSrc),
    (indexSrc.match(/task\.state === "discussing"[^\n]*/) || [""])[0]);
check("[C] 详情 ProgressSection 仍按状态门控渲染（ingesting/discussing/rendering）",
    /<ProgressSection detail=\{detail\} state=\{state\} \/>/.test(drawerSrc));

console.log(`\n[probe-at16-b4a] ${pass}/${pass + fails.length} PASS`);
if (fails.length) {
    for (const name of fails) console.log(`  FAIL ${name}`);
    process.exitCode = 1;
}
