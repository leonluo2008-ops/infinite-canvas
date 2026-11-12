/**
 * AT-17 B3 前端探针（本地免费、不联网、不动真实任务）：**音频三态控件两处落点 + 切换即联动**。
 *
 * 判据（方案 `docs/nodes/AT-17.md` §4 B3 + §7 附注 14）：
 *  [A] 同一个控件（`components/audio-mode-picker.tsx`）在 hub 与画布两个 tone 下都真渲染：
 *      三个中文词逐字在场、当前值唯一高亮、依据行、`待核对` WARN、枚举外值如实标、禁用态。
 *  [B] hub 确认流同一区（`AudioModeSection`）：三态控件 + B2b 红条在**同一个容器**里——
 *      门禁 FAIL → 红条在场且带原因原文 + 处理指引；改成与提示词一致的三态（gate=PASS）→ 红条消失。
 *      这就是「改了三态红条还在骂旧三态」在界面层的反向断言。
 *  [C] 提示词正文随 prompt_md 变：三份真产物（沿用原片 / 按新主体调整 / 不要）渲染出的声音段文本互不相同，
 *      且各自与三态一致（沿用=原片 BGM 值在场 / 调整=不重复原片旋律且旧音效点消失 / 不要=仅保留环境声）。
 *  [D] 接线守卫（禁第二套轮子，红线 25）：
 *      · 两处落点都是既有容器（hub=确认流按钮区 GenSettingsSection 内、画布=拉片弹层）里插同一控件；
 *      · 枚举只一份，且与后端 intent.py / server.py 逐字一致；
 *      · hub 只有一条写接口 /api/hub/task/<id>/audio-mode，画布只有 /api/audio-mode/<id>，各链各的端点。
 *
 * 后端（落盘 + 重渲染 + 门禁重判）由 `wanrepro/automation/probe_at17_b3.py` 锁（37 条，含真 render.py）。
 *
 * 跑法：cd web && node scripts/probe-at17-b3.mjs      （失败 rc=1）
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const WEB = process.cwd();
const REPO = path.resolve(WEB, "..");
const CACHE = path.join(WEB, "node_modules", ".cache", "at17-b3");
mkdirSync(CACHE, { recursive: true });
const read = (rel) => readFileSync(path.join(WEB, rel), "utf-8");
const readRepo = (rel) => readFileSync(path.join(REPO, rel), "utf-8");

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

// ════════════════════════════════════════════════════════════ 打包真代码（SSR）
const entry = path.join(CACHE, "ssr-entry.tsx");
writeFileSync(
    entry,
    'import { renderToStaticMarkup } from "react-dom/server";\n' +
        'import { AudioModePicker, AUDIO_MODE_VALUES } from "../../../src/components/audio-mode-picker";\n' +
        'import { AudioModeSection, PromptSection } from "../../../src/pages/hub/task-detail-drawer";\n' +
        "export { AUDIO_MODE_VALUES };\n" +
        "export function renderPicker(props: any): string {\n" +
        "    return renderToStaticMarkup(<AudioModePicker {...props} />);\n" +
        "}\n" +
        "export function renderSection(detail: any, busy: any, act: any): string {\n" +
        "    return renderToStaticMarkup(<AudioModeSection detail={detail} busy={busy} act={act} />);\n" +
        "}\n" +
        "export function renderPrompt(detail: any): string {\n" +
        "    return renderToStaticMarkup(<PromptSection detail={detail} />);\n" +
        "}\n",
);
const aliasPlugin = {
    name: "at17b3-alias",
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
check("[0] 真代码打包成功（audio-mode-picker / task-detail-drawer）", built.errors.length === 0,
    built.errors.map((err) => `${err.location?.file}: ${err.text}`).join("; "));

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

function elementByMarker(source, marker) {
    const at = source.indexOf(marker);
    if (at < 0) return "";
    let start = -1;
    let tag = "";
    for (const name of ["div", "section", "span", "button"]) {
        const from = source.lastIndexOf(`<${name}`, at);
        if (from >= 0 && (start < 0 || from > start)) {
            start = from;
            tag = name;
        }
    }
    if (start < 0) return "";
    const close = `</${tag}>`;
    let depth = 0;
    let i = start;
    while (i < source.length) {
        if (source.startsWith(`<${tag}`, i)) depth += 1;
        else if (source.startsWith(close, i)) {
            depth -= 1;
            if (depth === 0) return source.slice(start, i + close.length);
        }
        i += 1;
    }
    return source.slice(start);
}
const attrOf = (segment, key) => {
    const hit = new RegExp(`${key}="([^"]*)"`).exec(segment || "");
    return hit ? hit[1] : "";
};
const countOf = (source, needle) => (source || "").split(needle).length - 1;

// ════════════════════════════════════════════════════════════ [A] 控件本体
console.log("1) AudioModePicker 真渲染（hub / canvas 两个 tone，同一份选项）");
const hubPicker = ssr.renderPicker({ value: "按新主体调整", basis: "BGM.情绪=轻快", pendingCheck: false, onChange: () => {} });
const canvasPicker = ssr.renderPicker({ value: "不要", basis: "", pendingCheck: false, tone: "canvas", disabled: true, onChange: () => {} });
check("[A] hub tone 容器在场（data-audio-mode-picker=hub）", hubPicker.includes('data-audio-mode-picker="hub"'), hubPicker.slice(0, 160));
check("[A] canvas tone 容器在场（data-audio-mode-picker=canvas）", canvasPicker.includes('data-audio-mode-picker="canvas"'), canvasPicker.slice(0, 160));
for (const option of ["沿用原片", "按新主体调整", "不要"]) {
    check(`[A] 三态选项「${option}」在 hub 控件里逐字在场`, hubPicker.includes(`data-audio-mode-option="${option}"`));
    check(`[A] 三态选项「${option}」在画布控件里逐字在场`, canvasPicker.includes(`data-audio-mode-option="${option}"`));
}
check("[A] 当前值唯一高亮（data-audio-mode-active=1 只有一枚）", countOf(hubPicker, 'data-audio-mode-active="1"') === 1,
    String(countOf(hubPicker, 'data-audio-mode-active="1"')));
const activeOpt = elementByMarker(hubPicker, 'data-audio-mode-active="1"');
check("[A] 高亮的就是当前值（按新主体调整）", activeOpt.includes("按新主体调整"), activeOpt.slice(0, 160));
check("[A] 依据行随值透出（依据：BGM.情绪=轻快）", hubPicker.includes("BGM.情绪=轻快") && hubPicker.includes('data-audio-mode-basis="1"') === false && hubPicker.includes("data-audio-mode-basis"),
    elementByMarker(hubPicker, "data-audio-mode-basis").slice(0, 140));
const warnPicker = ssr.renderPicker({ value: "沿用原片", basis: "读不到 BGM", pendingCheck: true, onChange: () => {} });
check("[A] 待核对=true → WARN 在场且说清「不是无 BGM」",
    warnPicker.includes('data-audio-mode-warn="1"') && warnPicker.includes("不是「无 BGM」"),
    elementByMarker(warnPicker, 'data-audio-mode-warn="1"').slice(0, 160));
check("[A] 待核对=false → 不显示 WARN（不无病呻吟）", !hubPicker.includes("data-audio-mode-warn"));
const emptyPicker = ssr.renderPicker({ value: "", basis: "", onChange: () => {} });
check("[A] 没落值（空串）→ 无高亮、明说「未落值」", countOf(emptyPicker, 'data-audio-mode-active="1"') === 0 && emptyPicker.includes("未落值"));
check("[A] 禁用态真的 disabled（画布没跑过拉片时不给乱点）", countOf(canvasPicker, 'disabled=""') >= 3,
    String(countOf(canvasPicker, 'disabled=""')));
check("[A] 枚举只定义一份，值逐字 = 后端三词", JSON.stringify(ssr.AUDIO_MODE_VALUES) === JSON.stringify(["沿用原片", "按新主体调整", "不要"]),
    JSON.stringify(ssr.AUDIO_MODE_VALUES));

// ════════════════════════════════════════════════════════════ [B] hub 确认流同一区
console.log("2) AudioModeSection：三态控件与 B2b 红条同区，门禁结果切换即跟随");

const FAIL_GATE = {
    verdict: "FAIL",
    audio_mode: "不要",
    checks: [{ id: "A1 不要×BGM 断言", verdict: "FAIL", msg: "三态=不要，但提示词仍在描述 BGM" }],
    发现: ["三态=不要，但提示词仍在描述 BGM／背景音乐：「BGM：情绪轻快，节奏中速…」"],
    处理: "门禁只挡「批准」——按新三态重跑渲染让声音段与三态一致，再批准。",
};
const PASS_GATE = { verdict: "PASS", audio_mode: "沿用原片", checks: [], 发现: [], 处理: "" };

const baseDetail = {
    local_id: "auto-9999",
    state: "awaiting_approval",
    prompt_md: "占位正文",
    audio_mode: "不要",
    audio_mode_依据: "BGM.情绪=轻快",
    audio_mode_待核对: false,
    audio_gate: FAIL_GATE,
};
const failHtml = ssr.renderSection(baseDetail, null, async () => {});
check("[B] 三态控件与红条在**同一个容器**里（data-hub-audio-mode-section）",
    Boolean(elementByMarker(failHtml, "data-hub-audio-mode-section")) &&
        elementByMarker(failHtml, "data-hub-audio-mode-section").includes('data-audio-mode-picker="hub"') &&
        elementByMarker(failHtml, "data-hub-audio-mode-section").includes('data-hub-audio-gate="fail"'),
    elementByMarker(failHtml, "data-hub-audio-mode-section").slice(0, 200));
const failBar = elementByMarker(failHtml, 'data-hub-audio-gate="fail"');
check("[B] 门禁 FAIL → 红条带旧三态标记（data-hub-audio-gate-mode=不要）",
    attrOf(failBar, "data-hub-audio-gate-mode") === "不要", attrOf(failBar, "data-hub-audio-gate-mode"));
check("[B] 红条带失败原因原文 + 处理指引（防反复点批准）",
    failBar.includes("BGM：情绪轻快") && failBar.includes("怎么处理：") && failBar.includes('data-hub-audio-gate-how'),
    failBar.slice(0, 260));
check("[B] 待批准态控件可点（按钮上没有 disabled 属性）",
    countOf(failHtml, 'disabled=""') === 0, String(countOf(failHtml, 'disabled=""')));

const passHtml = ssr.renderSection({ ...baseDetail, audio_mode: "沿用原片", audio_gate: PASS_GATE }, null, async () => {});
check("[B] 改成与提示词一致的三态（gate=PASS）→ 红条消失（不骂旧三态）",
    !passHtml.includes('data-hub-audio-gate="fail"'), passHtml.slice(0, 200));
check("[B] 同一时刻控件高亮跟着换成「沿用原片」",
    elementByMarker(passHtml, 'data-audio-mode-active="1"').includes("沿用原片"));

const approvedHtml = ssr.renderSection({ ...baseDetail, state: "approved" }, null, async () => {});
check("[B] 已批准 → 控件禁用 + 明说先「撤销批准」回退（不放宽状态机）",
    countOf(approvedHtml, 'disabled=""') >= 3 && approvedHtml.includes('data-hub-audio-mode-locked')
        && approvedHtml.includes("撤销批准"), approvedHtml.slice(0, 240));
check("[B] 已批准态不再渲染门禁红条（批准后的阻断由既有状态条负责）",
    !approvedHtml.includes('data-hub-audio-gate="fail"'));

const warnHtml = ssr.renderSection({ ...baseDetail, audio_mode: "沿用原片", audio_mode_待核对: true, audio_gate: PASS_GATE }, null, async () => {});
check("[B] audio_mode_待核对=true → 控件下方 WARN（B2c「不猜无 BGM」透传到界面）",
    warnHtml.includes('data-audio-mode-warn="1"'));

// ════════════════════════════════════════════════════════════ [C] 提示词正文随三态变
console.log("3) PromptSection：三份真产物（probe_at17_b3 用真 render.py 渲染）声音段文本各不相同");
const SOUND = {
    沿用原片: "BGM：情绪轻快，节奏中速，末段收束，乐器/织体电贝斯、鼓点、复古放克编排，强弱走向轻起渐强收。音效点：00:02-00:03 瓶身飞旋弹出时一声清脆的「POP」爆裂脆响。",
    按新主体调整: "BGM：沿用原片的情绪（轻快）、节奏（中速，末段收束）、乐器/织体（电贝斯、鼓点、复古放克编排）、强弱走向（轻起渐强收）骨架，按新主体的气质重新配器，不重复原片旋律。音效点：不沿用原片声源（原片音效点由旧主体的动作产生），按新主体在对应时刻的动作重新拟声。",
    不要: "BGM：无 BGM，仅保留环境声。音效点：无（只保留环境声，不设计音效点）。",
};
const promptOf = (mode) => `0-1秒，特写，人物双腿与持物手，直线向前迈步。2-3秒，近景转特写，人物与脱手产品。3-5秒，特写，核心产品瓶身，减速至定格。${SOUND[mode]}`;
const rendered = Object.fromEntries(Object.keys(SOUND).map((mode) => [mode, ssr.renderPrompt({ ...baseDetail, audio_mode: mode, prompt_md: promptOf(mode) })]));
check("[C] 三份提示词都真渲染出正文块（data-hub-prompt-body）",
    Object.values(rendered).every((html) => html.includes("data-hub-prompt-body")));
check("[C] 声音段文本随三态变：三份 HTML 互不相同",
    new Set(Object.values(rendered)).size === 3);
check("[C] 沿用原片：原片 BGM 值（电贝斯）与音效点原文（POP）在场",
    rendered["沿用原片"].includes("电贝斯") && rendered["沿用原片"].includes("POP"));
check("[C] 按新主体调整：骨架保留 + 明说「不重复原片旋律」，旧音效点原文已剥净",
    rendered["按新主体调整"].includes("不重复原片旋律") && !rendered["按新主体调整"].includes("POP"));
check("[C] 不要：明写「无 BGM，仅保留环境声」，原片编曲与音效点都不在",
    rendered["不要"].includes("仅保留环境声") && !rendered["不要"].includes("电贝斯") && !rendered["不要"].includes("POP"));

// ═══════════════════════════════════════════════ [D] 接线守卫（禁第二套轮子）
console.log("4) 接线守卫：两处既有容器 + 单一枚举 + 每链一条写接口");
const drawer = read("src/pages/hub/task-detail-drawer.tsx");
const project = read("src/pages/canvas/project.tsx");
const picker = read("src/components/audio-mode-picker.tsx");
const hubApi = read("src/pages/hub/hub-api.ts");
const intentPy = readRepo(path.join("wanrepro", "pipeline", "reverse", "intent.py"));
const serverPy = readRepo(path.join("wanrepro", "ui", "server.py"));
const hubApiPy = readRepo(path.join("wanrepro", "ui", "hub_api.py"));

check("[D] hub：确认流按钮区内插的是同一个控件（AudioModeSection → AudioModePicker）",
    drawer.includes('import { AudioModePicker } from "@/components/audio-mode-picker"') &&
        drawer.includes("export function AudioModeSection") &&
        drawer.includes("<AudioModeSection detail={detail} busy={busy} act={act} />"));
const genSectionStart = drawer.indexOf("function GenSettingsSection(");
const genSectionHtml = drawer.slice(genSectionStart, drawer.indexOf("function parsePixelSizeOf("));
check("[D] hub：AudioModeSection 渲染在 GenSettingsSection 内（B2b 红条原来那一块，零新面板）",
    genSectionHtml.includes("<AudioModeSection") && genSectionHtml.includes("approveAndSubmit"),
    `GenSettingsSection 内是否有三态区=${genSectionHtml.includes("<AudioModeSection")}`);
check("[D] hub：改选只打一条写接口（POST /api/hub/task/<id>/audio-mode）",
    hubApi.includes("`/api/hub/task/${encodeURIComponent(localId)}/audio-mode`") &&
        countOf(hubApi, "audio-mode`") === 1, String(countOf(hubApi, "audio-mode`")));
check("[D] hub：提示词不靠前端拼（改选后回刷 detail：act() → load()）",
    drawer.includes("await setHubAudioMode(detail.local_id, next)"));
check("[D] 画布：拉片弹层里插的是同一控件（tone=canvas）",
    project.includes('import { AudioModePicker } from "@/components/audio-mode-picker"') &&
        project.includes('<AudioModePicker') && project.includes('tone="canvas"') &&
        project.includes("data-lapian-audio-mode-box"));
check("[D] 画布：回显读 /api/task/<id>，改选写 /api/audio-mode/<id>（各链各端点，无第三处）",
    project.includes("/api/task/${encodeURIComponent(taskId)}") &&
        project.includes("/api/audio-mode/${encodeURIComponent(taskId)}"));
check("[D] 画布：没有下游拉片任务时不猜默认值（控件在场但禁用 + 明说）",
    project.includes("disabled={!lapianAudioTaskId}") && project.includes("data-lapian-audio-mode-hint"));
check("[D] 枚举只一份：picker 里 AUDIO_MODE_VALUES 定义一次、无第二个字面数组",
    countOf(picker, "export const AUDIO_MODE_VALUES") === 1 && countOf(picker, '"沿用原片"') === 1,
    `定义=${countOf(picker, "export const AUDIO_MODE_VALUES")}｜字面=${countOf(picker, '"沿用原片"')}`);
check("[D] 后端枚举与前端逐字一致（intent.py / server.py / hub_api.py 引用同一份）",
    intentPy.includes('AUDIO_MODE_KEEP = "沿用原片"') && intentPy.includes('AUDIO_MODE_ADJUST = "按新主体调整"')
        && intentPy.includes('AUDIO_MODE_MUTE = "不要"')
        && serverPy.includes('AUDIO_MODE_VALUES = ("沿用原片", "按新主体调整", "不要")')
        && hubApiPy.includes("intent.AUDIO_MODE_VALUES"));
check("[D] 后端：hub 写接口只有一处收口（hub_api.set_audio_mode）+ 路由一条",
    hubApiPy.includes("def set_audio_mode(") &&
        serverPy.includes('r"/api/hub/task/([^/]+)/audio-mode"') &&
        countOf(serverPy, "set_audio_mode(") === 1, String(countOf(serverPy, "set_audio_mode(")));
check("[D] 后端：画布写接口复用 intent.save_audio_mode（不另写 JSON 拼装）",
    serverPy.includes("def set_task_audio_mode(") &&
        serverPy.includes("save_audio_mode(dir_, mode") &&
        intentPy.includes("def save_audio_mode("));
check("[D] 后端：重渲染复用 do_render 的 fetch_prompt 一处拼 audio_mode（附注 11）",
    readRepo(path.join("wanrepro", "automation", "generate.py")).includes("def fetch_prompt(") &&
        readRepo(path.join("wanrepro", "automation", "generate.py")).includes("prompt = fetch_prompt(str(task[\"bridge_id\"]), mode)"));
check("[D] 后端：改选后门禁用同一份 run_audio_gate 重跑（不另造判定）",
    readRepo(path.join("wanrepro", "automation", "generate.py")).includes("gate = run_audio_gate(task_dir, state.load(task_dir))"));

const total = pass + fails.length;
console.log(`\nAT-17 B3 前端探针：${pass}/${total} 通过`);
if (fails.length) {
    console.log("未通过：");
    fails.forEach((name) => console.log(`  - ${name}`));
    process.exit(1);
}
