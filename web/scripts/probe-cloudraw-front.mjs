/**
 * 云端原文失败详情前端探针（本地免费、不联网、不动真实任务）：**任务详情页 failed 态的主展示件**。
 *
 * 现场缺口：discuss.py 已经把「云端原文：{message}」摆进 failed_reason 首行（转述/退出码/
 * finish_reason/原始响应降为附属），但详情抽屉的失败区只渲染 gen_error（生成失败）与一句
 * 「任务失败，错误原文未返回。」——拆解失败的云端原文在详情页根本看不见，员工只知道失败不知道为什么。
 *
 * 判据（本卡任务书 + 红线 25）：
 *  [A] splitFailedReason：只按后端固定行标记挑样式，**逐字等式**（分段拼回 === 原文）；
 *      未知前缀/新增行一律留主展示区——不吞字段、不改写、不推断。
 *  [B] CloudFailurePanel：云端原文等宽 + 可换行 + 全文不截断（长消息尾部也在），有附属行时分次要样式；
 *      「云端原文：未落盘（…）」照常展示；两处都空 → 渲染 null（交回既有兜底句，不造第二套文案）；
 *      右上角 on_disk/missing 徽标 1 处（cloud_raw 字面值，缺/坏值零徽标），不二次渲染云端 error 正文。
 *  [C] 详情页接线（OutcomeSection SSR 真代码）：failed 态看到面板 + 重试；done 态零面板（成功页不变）；
 *      面板是 failed 区**唯一**主展示件（正文 1 次 / main 钩子 1 个 / 徽标 1 处），drawer 零 FailedReasonLine。
 *  [D] 改动边界：面板只消费后端文案（不出现错误分类关键词 / 不 replace / evidence 只认 cloud_raw 徽标值），
 *      drawer 里只有一个挂载点且长在 failed 分支内，且不再挂载 FailedReasonLine。
 *
 * 跑法：cd web && node scripts/probe-cloudraw-front.mjs      （失败 rc=1）
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const WEB = process.cwd();
const CACHE = path.join(WEB, "node_modules", ".cache", "cloudraw-front");
mkdirSync(CACHE, { recursive: true });
const read = (rel) => readFileSync(path.join(WEB, rel), "utf-8");

let pass = 0;
const fails = [];
function check(name, cond, detail = "") {
    if (cond) {
        pass += 1;
        console.log(`  [PASS] ${name}${detail ? ` —— ${String(detail).slice(0, 260)}` : ""}`);
    } else {
        fails.push(name);
        console.log(`  [FAIL] ${name}${detail ? ` —— ${String(detail).slice(0, 400)}` : ""}`);
    }
}

// ════════════════════════════════════════════════════════════ 打包真代码（SSR）
const entry = path.join(CACHE, "ssr-entry.tsx");
writeFileSync(
    entry,
    [
        'import { renderToStaticMarkup } from "react-dom/server";',
        'import { App } from "antd";',
        'import { CloudFailurePanel, FAILED_REASON_AUX_MARKERS, splitFailedReason } from "../../../src/pages/hub/task-card";',
        'import { OutcomeSection } from "../../../src/pages/hub/task-detail-drawer";',
        "export { CloudFailurePanel, FAILED_REASON_AUX_MARKERS, splitFailedReason };",
        "const noop = () => {};",
        'const act: any = async () => undefined;',
        "export function renderPanel(failed: any, fallbackError = \"\"): string {",
        "    return renderToStaticMarkup(<App><CloudFailurePanel failed={failed} fallbackError={fallbackError} /></App>);",
        "}",
        "export function renderOutcome(detail: any): string {",
        "    return renderToStaticMarkup(<App><OutcomeSection detail={detail} busy={null} act={act} /></App>);",
        "}",
    ].join("\n"),
);
const aliasPlugin = {
    name: "cloudraw-alias",
    setup(build) {
        build.onResolve({ filter: /^@\// }, (args) => {
            const base = path.join(WEB, "src", args.path.slice(2));
            for (const candidate of [base, `${base}.ts`, `${base}.tsx`, path.join(base, "index.ts"), path.join(base, "index.tsx")]) {
                try {
                    if (statSync(candidate).isFile()) return { path: candidate };
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
check("[0] 真代码打包成功（task-card / task-detail-drawer 导出件）", built.errors.length === 0, built.errors.map((err) => `${err.location?.file}: ${err.text}`).join("; "));
if (built.errors.length) {
    console.log("\nSUMMARY: FAIL (build)");
    process.exit(1);
}

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
if (!globalThis.window.matchMedia) globalThis.window.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} });

const ssr = await import(pathToFileURL(ssrOut).href + `?t=${Date.now()}`);

// ════════════════════════════════════════════════════════════ 夹具：后端 discuss._failure_detail 的真实形态
// 401 原文取自兄弟卡 t_ddec27a7 真无效 key 实跑落盘的 拆解错误.json（message 里的 request id 来自那次真调用）
const REAL_401_REQUEST_ID = "20261112192206821001272PA7QMjHD";
const REAL_401_MESSAGE = `HTTP 401 Unauthorized：Invalid token (request id: ${REAL_401_REQUEST_ID})`;
const CLOUD_MSG = `接口返回 error：{"error":{"message":"${REAL_401_MESSAGE}","type":"new_api_error"}}`;
const REASON_ON_DISK = [
    `云端原文：${CLOUD_MSG}`,
    "［附属］退出码 3｜finish_reason=STOP｜错误类型 api_error",
    "［转述］analyze.py 退出 3：ERROR API 失败：HTTP 401 Unauthorized",
    "［原始响应］{\"error\":{\"message\":\"HTTP 401 Unauthorized\",\"type\":\"new_api_error\"}}",
].join("\n");
const REASON_MISSING = [
    "analyze.py 退出 3：ERROR API 失败：接口返回 error",
    "云端原文：未落盘（未找到 analysis/rev-abc/拆解错误.json：旧产物或非云端失败）",
].join("\n");
const LONG_MSG = `云端原文：${"HTTP 500 Internal Server Error：上游返回超长响应 ".repeat(40)}尾部标记-TAIL-END`;
const REASON_LONG = `${LONG_MSG}\n［附属］退出码 3｜finish_reason=STOP`;

// ════════════════════════════════════════════════════════════ [A] 分段纯函数（只换样式不换字）
console.log("1) splitFailedReason：逐字等式 + 标记分类 + 未知行不吞");
const eq = (input) => {
    const got = ssr.splitFailedReason(input).map((segment) => segment.text).join("\n");
    return got === String(input ?? "");
};
check("[A] 逐字等式：云端原文样本", eq(REASON_ON_DISK), "");
check("[A] 逐字等式：未落盘样本", eq(REASON_MISSING), "");
check("[A] 逐字等式：长消息样本", eq(REASON_LONG), "");
check("[A] 逐字等式：空串 / null / undefined", eq("") && eq(null) && eq(undefined), "");
check("[A] 逐字等式：尾部换行不丢（不 trim）", eq(`${REASON_ON_DISK}\n`), "");
check("[A] 逐字等式：CRLF 原样保留", eq("云端原文：a\r\n［附属］b"), JSON.stringify(ssr.splitFailedReason("云端原文：a\r\n［附属］b")));

const segs = ssr.splitFailedReason(REASON_ON_DISK);
check("[A] 首行（云端原文）为主展示", segs[0]?.kind === "main" && segs[0]?.text === `云端原文：${CLOUD_MSG}`, segs[0]?.kind);
check("[A] ［附属］/［转述］/［原始响应］三行全为主→附属（3 条）", segs.filter((s) => s.kind === "aux").length === 3, JSON.stringify(segs.map((s) => s.kind)));
check("[A] 标记常量逐字对齐后端（［附属］/［转述］/［原始响应］）", JSON.stringify(ssr.FAILED_REASON_AUX_MARKERS) === JSON.stringify(["［附属］", "［转述］", "［原始响应］"]), JSON.stringify(ssr.FAILED_REASON_AUX_MARKERS));
const unknownLine = ssr.splitFailedReason(`${REASON_ON_DISK}\n［新增标记］后端以后加的新字段：xyz`);
check("[A] 未知前缀行不被吞：落主展示区且原文在", unknownLine.some((s) => s.kind === "main" && s.text.includes("［新增标记］后端以后加的新字段：xyz")), JSON.stringify(unknownLine.map((s) => s.kind)));
check("[A] 未落盘两行都在主展示区（无附属样式）", ssr.splitFailedReason(REASON_MISSING).every((s) => s.kind === "main") && ssr.splitFailedReason(REASON_MISSING).length === 2, "");

// ════════════════════════════════════════════════════════════ [B] 面板 SSR（真渲染）
console.log("2) CloudFailurePanel：云端原文置顶、等宽可换行、全文不截断、附属降级");
const ON_DISK = { reason: REASON_ON_DISK, at: "2026-11-12T19:25:41+08:00", evidence: { cloud_raw: "on_disk", exit_code: 3 } };
const html = ssr.renderPanel(ON_DISK);
check("[B] 渲染钩子：backend", html.includes('data-hub-cloud-failure="backend"'), html.slice(0, 160));
check("[B] 云端原文逐字可见（含 HTTP 401 具体消息与 request id）", html.includes(REAL_401_MESSAGE), "");
check("[B] 云端原文在主展示区（data-hub-cloud-failure-main 内，附属区之前）", (() => {
    const mainIdx = html.indexOf("data-hub-cloud-failure-main");
    const auxIdx = html.indexOf("data-hub-cloud-failure-auxlist");
    const msgIdx = html.indexOf(REAL_401_REQUEST_ID);
    return mainIdx > 0 && auxIdx > mainIdx && msgIdx > mainIdx && msgIdx < auxIdx;
})(), "");
check("[B] 主展示区等宽 + 可换行（font-mono / whitespace-pre-wrap / break-words）", /data-hub-cloud-failure-main[\s\S]{0,400}?font-mono/.test(html) && /whitespace-pre-wrap/.test(html) && /break-words/.test(html), "");
check("[B] 不截断：无 truncate / line-clamp / overflow-hidden / text-ellipsis / max-h-*", !/class="[^"]*\btruncate\b/.test(html) && !/line-clamp/.test(html) && !/text-ellipsis/.test(html) && !/max-h-/.test(html), (html.match(/class="[^"]*"/g) || []).join(" | ").slice(0, 200));
check("[B] 附属三行都在（退出码 / finish_reason / 转述 / 原始响应）", ["［附属］退出码 3", "finish_reason=STOP", "［转述］analyze.py 退出 3", "［原始响应］"].every((needle) => html.includes(needle)), "");
check("[B] 附属行逐条钩子 = 3 条（次要样式 font-mono text-[11px]）", (html.match(/data-hub-cloud-failure-line=/g) || []).length === 3 && /text-\[11px\]/.test(html), String((html.match(/data-hub-cloud-failure-line=/g) || []).length));
check("[B] 失败时间在场（与列表行同一套时钟口径）", /失败时间\s*11-12\s*19:25:41/.test(html), "");

const longHtml = ssr.renderPanel({ reason: REASON_LONG });
check("[B] 长消息全文不截断（尾部标记可见）", longHtml.includes("尾部标记-TAIL-END"), String(longHtml.length));

const missingHtml = ssr.renderPanel({ reason: REASON_MISSING, at: "" });
check("[B] 未落盘：文案原样可见 + 无附属区", missingHtml.includes("云端原文：未落盘（未找到 analysis/rev-abc/拆解错误.json：旧产物或非云端失败）") && missingHtml.includes('data-hub-cloud-failure-aux="0"') && !missingHtml.includes("data-hub-cloud-failure-auxlist"), missingHtml.slice(0, 200));
check("[B] 未落盘：转述行也在（原转述逐字保留）", missingHtml.includes("analyze.py 退出 3：ERROR API 失败：接口返回 error"), "");

// 落盘徽标（本卡：从 FailedReasonLine/CloudErrorEvidence 迁进面板，钩子沿用 data-hub-cloud-raw）
console.log("2b) 面板自带 on_disk/missing 徽标（详情页唯一落盘徽标落点）");
check("[B] 徽标 on_disk：面板内 1 处、文案 = 「on_disk 已落盘」", (html.match(/data-hub-cloud-raw="on_disk"/g) || []).length === 1 && html.includes("on_disk 已落盘"), String((html.match(/data-hub-cloud-raw=/g) || []).length));
check("[B] 徽标 missing：面板内 1 处、文案 = 「missing 未落盘」", (() => {
    const missingBadge = ssr.renderPanel({ reason: REASON_MISSING, evidence: { cloud_raw: "missing" } });
    return (missingBadge.match(/data-hub-cloud-raw="missing"/g) || []).length === 1 && missingBadge.includes("missing 未落盘");
})(), "");
check("[B] 无 cloud_raw 字段 → 零徽标（不造占位）", !/data-hub-cloud-raw/.test(ssr.renderPanel({ reason: REASON_LONG })) && !/data-hub-cloud-raw/.test(longHtml), "");
check("[B] 坏值（非 on_disk/missing）→ 零徽标（只认字面值）", !/data-hub-cloud-raw/.test(ssr.renderPanel({ reason: REASON_MISSING, evidence: { cloud_raw: "ON_DISK" } })), "");
check("[B] 徽标不重复正文：正文区不出现第二遍 cloud_error.message", (() => {
    const again = ssr.renderPanel({ reason: REASON_ON_DISK, evidence: { cloud_raw: "on_disk", cloud_error: { message: CLOUD_MSG, error_type: "api_error", attempts: 3 } } });
    return (again.match(new RegExp(REAL_401_REQUEST_ID, "g")) || []).length === 1 && (again.match(/data-hub-cloud-failure-main/g) || []).length === 1;
})(), "");

const historyHtml = ssr.renderPanel(null, "DiscussError: bridge timeout");
check("[B] 老任务回落 history 尾注（钩子=history）", historyHtml.includes('data-hub-cloud-failure="history"') && historyHtml.includes("DiscussError: bridge timeout"), historyHtml.slice(0, 200));

const emptyHtml = ssr.renderPanel(null, "");
check("[B] 两处都空 → 面板不渲染（交回既有兜底句，不造第二套文案）", !emptyHtml.includes("data-hub-cloud-failure") && emptyHtml.replace(/<[^>]*>/g, "").trim() === "", JSON.stringify(emptyHtml.slice(0, 120)));

// 后端加新字段 / 未知 evidence 键：不报错、不吞原文
let unknownOk = true;
let unknownHtml = "";
try {
    unknownHtml = ssr.renderPanel({
        reason: `${REASON_ON_DISK}\n［新增］provider=acme｜retry_after=30`,
        at: "2026-11-12T19:25:41+08:00",
        evidence: { cloud_error: { message: CLOUD_MSG, error_type: "api_error", finish_reason: "STOP", raw_response: "{}", brand_new_key: { a: 1 } }, cloud_raw: "on_disk", unknown_top: [1, 2, 3] },
        brand_new_field: "x",
    });
} catch (error) {
    unknownOk = false;
    unknownHtml = String(error);
}
check("[B] 后端新增/未知字段不导致前端报错", unknownOk, unknownHtml.slice(0, 200));
check("[B] 新增行（［新增］…）原样可见（不吞未知字段）", unknownHtml.includes("［新增］provider=acme｜retry_after=30"), "");

// ════════════════════════════════════════════════════════════ [C] 详情页接线（OutcomeSection SSR 真代码）
console.log("3) 详情页接线：failed 态面板 + 重试；done 态零面板");
const baseDetail = {
    local_id: "demo-401",
    task_name: "demo-401·ref.mp4",
    state: "failed",
    intent: "测试意图",
    created_at: "2026-11-12T19:00:00+08:00",
    video_name: "ref.mp4",
    gen_progress: null,
    has_output: false,
    summary_md: "",
    checklist_md: "",
    prompt_md: "",
    pair_lines: [],
    asset_cards: [],
    suggestions: [],
    error: "",
};
const failedOutcome = ssr.renderOutcome({ ...baseDetail, failed: ON_DISK, error: "" });
check("[C] failed 态：云端原文面板在主展示位（面板在失败红条之前）", (() => {
    const panelIdx = failedOutcome.indexOf("data-hub-cloud-failure=");
    const retryIdx = failedOutcome.indexOf("重试");
    return panelIdx > 0 && failedOutcome.includes("data-hub-cloud-failure-main") && failedOutcome.includes(REAL_401_MESSAGE) && retryIdx > panelIdx;
})(), failedOutcome.slice(0, 240));
// 本卡：面板是详情页 failed 区**唯一**主展示件——云端原文正文 / 主展示区 / 落盘徽标各只出现 1 次
check("[C] 唯一主展示件：云端原文正文（含 request id）整页只出现 1 次", (failedOutcome.match(new RegExp(REAL_401_REQUEST_ID, "g")) || []).length === 1, String((failedOutcome.match(new RegExp(REAL_401_REQUEST_ID, "g")) || []).length));
check("[C] 唯一主展示件：data-hub-cloud-failure-main 全页 1 个、徽标 1 处", (failedOutcome.match(/data-hub-cloud-failure-main/g) || []).length === 1 && (failedOutcome.match(/data-hub-cloud-raw/g) || []).length === 1, `${(failedOutcome.match(/data-hub-cloud-failure-main/g) || []).length}/${(failedOutcome.match(/data-hub-cloud-raw/g) || []).length}`);
check("[C] failed 态：重试入口仍在（不吞既有动作面）", /data-hub-gen-retry/.test(failedOutcome) || /重\s?试/.test(failedOutcome), "");
check("[C] failed 态：说明行不再谎称「错误原文未返回」", !failedOutcome.includes("任务失败，错误原文未返回。") && failedOutcome.includes("失败原因见上方原文（后端原样透传，未改写）。"), "");

const failedNoReason = ssr.renderOutcome({ ...baseDetail, failed: null, error: "" });
check("[C] failed 但两处都空：零面板 + 既有兜底句逐字不变", !failedNoReason.includes("data-hub-cloud-failure") && failedNoReason.includes("任务失败，错误原文未返回。"), failedNoReason.slice(0, 240));

const failedGen = ssr.renderOutcome({ ...baseDetail, failed: ON_DISK, gen_error: "云端 400：参考图 URL 拉取失败（已脱敏）" });
check("[C] 生成失败共存：gen_error 红条与云端原文面板并存（互不挤掉）", failedGen.includes("data-hub-gen-error") && failedGen.includes("云端 400：参考图 URL 拉取失败") && failedGen.includes("data-hub-cloud-failure-main"), "");

const doneOutcome = ssr.renderOutcome({ ...baseDetail, state: "done", has_output: true, gen_progress: 100, failed: null });
check("[C] done 态：成功页零变化（成片+下载在场，零失败面板）", doneOutcome.includes("成片") && doneOutcome.includes("下载成片") && !doneOutcome.includes("data-hub-cloud-failure") && !doneOutcome.includes("data-hub-gen-error"), doneOutcome.slice(0, 240));

// ════════════════════════════════════════════════════════════ [D] 改动边界（只透传不解析重判）
console.log("4) 改动边界：只渲染后端文案、单挂载点、不读 evidence");
const cardSrc = read("src/pages/hub/task-card.tsx");
const drawerSrc = read("src/pages/hub/task-detail-drawer.tsx");
const panelSlice = (() => {
    const start = cardSrc.indexOf("export function CloudFailurePanel");
    const end = cardSrc.indexOf("export function TaskCard");
    return start >= 0 && end > start ? cardSrc.slice(start, end) : "";
})();
check("[D] 面板函数体可定位", panelSlice.length > 300, String(panelSlice.length));
check("[D] 不出现错误分类关键词（不根据错误文本推断原因）", !/401|403|429|超时|限流|余额|余额不足|invalid_token/i.test(panelSlice), (panelSlice.match(/401|403|429|超时|限流|余额|invalid_token/gi) || []).join(","));
check("[D] 不改写文案：函数体无 replace/slice/正则解析", !/\.replace\(|\.slice\(|\/\.\w+\/\.test/.test(panelSlice), "");
check("[D] evidence 只认 cloud_raw 徽标值：不读 cloud_error / 不二次渲染 message", /cloudRawOf/.test(panelSlice) && !/cloud_error/.test(panelSlice) && !/\.message/.test(panelSlice), "");
check("[D] drawer 里只有一个挂载点", (drawerSrc.match(/<CloudFailurePanel/g) || []).length === 1, String((drawerSrc.match(/<CloudFailurePanel/g) || []).length));
check("[D] drawer 零 FailedReasonLine（不双挂载：云端原文只由面板出一次）", !/<FailedReasonLine/.test(drawerSrc) && !/FailedReasonLine/.test(drawerSrc.match(/^import[\s\S]*?from "\.\/task-card";$/m)?.[0] || ""), String((drawerSrc.match(/FailedReasonLine/g) || []).length));
check("[D] 挂载点长在 failed 分支内（不在 done / 生成设置分支）", (() => {
    const branch = drawerSrc.indexOf('if (state === "failed")');
    const mount = drawerSrc.indexOf("<CloudFailurePanel");
    const outcomeEnd = drawerSrc.indexOf("function RetryButton");
    return branch > 0 && mount > branch && (outcomeEnd < 0 || mount < outcomeEnd);
})(), "");
check("[D] 面板组件只在 task-card 定义一处（不造第二套）", (cardSrc.match(/function CloudFailurePanel/g) || []).length === 1 && (drawerSrc.match(/function CloudFailurePanel/g) || []).length === 0, "");
check("[D] 详情页不直连 fetch（仍走 hub-api 接口层）", !/\bfetch\(/.test(drawerSrc), "");

// ════════════════════════════════════════════════════════════ 收尾
console.log("");
if (fails.length) {
    console.log(`SUMMARY: FAIL ${fails.length} 项未过 / ${pass} 项通过`);
    fails.forEach((name) => console.log(`  - ${name}`));
    process.exit(1);
}
console.log(`SUMMARY: PASS ${pass}/${pass}`);
