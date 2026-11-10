/**
 * AT-13 B4 方言合同锁 · TS 侧探针（纯逻辑层，不连渠道、不花钱）。
 *
 * 合同 = docs/nodes/AT-13.md v3 §2.9（I-06）：**同一份 JSON 夹具双端消费**——
 *   · 夹具：wanrepro/tests/fixtures/sdgf-dialect/cases.json（输入 model/ratio/sound/duration/mode/imageCount + 期望输出）
 *   · PY 侧：wanrepro/automation/probe_sdgf_dialect.py（加载同一份夹具）
 *   · TS 侧：本文件（加载同一份夹具）
 * 单改任一端实现（比例/声音生效位、代际时长、mode 映射）→ 至少一条共享 case 必红。
 *
 * TS 侧断言两层（都在**生产代码**上跑，不在探针里复制映射表）：
 *   ① 纯函数层：esbuild 转译真模块 `src/services/api/sdgf-payload.ts`
 *      （isSdgfFullModel/sdgfRatioFields/sdgfSoundFields/sdgfClampDuration）逐条等于夹具期望。
 *   ② 组装层：从 `src/services/api/video.ts` **原文抽取** sdgfDuration / sdgfAspectRatio / sdgfMode /
 *      resolveVideoMode 四个函数体 + `const body = {...}` 与 metadata 合并语句，用 new Function 求值
 *      —— 断言渠道请求体的 aspect_ratio / metadata 合并结果 / 顶层 enableSound / duration / mode
 *      与夹具期望全等；期望 ok=false 的 case 断言同一拒绝因（model|duration|ratio|reference-no-image）。
 *      生产链文件本身零改动：本探针只做只读抽取（抽取点找不到即红，不会静默跳过）。
 *
 * 接线护栏（不属于跨语言 case）：video.ts 确实调用这些函数、总开关未被改回 false。
 *
 * 跑法：cd web && node scripts/probe-sdgf-payload.mjs （任一不成立 exit 1）
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");   // web/
const src = path.join(root, "src");
const fixturePath = path.resolve(root, "..", "wanrepro", "tests", "fixtures", "sdgf-dialect", "cases.json");
const outDir = path.join(root, "node_modules", ".probe");
mkdirSync(outDir, { recursive: true });

let passed = 0;
const failures = [];
function check(name, condition, detail) {
    if (condition) {
        passed += 1;
        console.log(`  ok   ${name}`);
        return;
    }
    failures.push(`${name}${detail === undefined ? "" : ` — ${detail}`}`);
    console.log(`  FAIL ${name}${detail === undefined ? "" : ` — ${detail}`}`);
}

async function bundle(entry, outName) {
    const outfile = path.join(outDir, outName);
    await esbuild.build({
        entryPoints: [path.join(src, entry)],
        outfile,
        bundle: true,
        format: "esm",
        platform: "node",
        logLevel: "silent",
        alias: { "@": src },
    });
    return import(pathToFileURL(outfile).href);
}

/** 规范化 JSON 比对：键序不比（对象/数组/标量同一口径）。 */
const norm = (value) => {
    const canon = (item) => {
        if (Array.isArray(item)) return item.map(canon);
        if (item && typeof item === "object") {
            return Object.fromEntries(Object.keys(item).sort().map((key) => [key, canon(item[key])]));
        }
        return item;
    };
    return JSON.stringify(canon(value === undefined ? null : value));
};

/** 从 TS 原文里按大括号配平抽出一个具名函数体（跳字符串/注释里的大括号）。 */
function extractFunction(source, name) {
    const start = source.indexOf(`function ${name}(`);
    if (start < 0) return null;
    const open = source.indexOf("{", start);
    if (open < 0) return null;
    let depth = 0;
    for (let i = open; i < source.length; i += 1) {
        const ch = source[i];
        if (ch === '"' || ch === "'" || ch === "`") {
            const quote = ch;
            i += 1;
            while (i < source.length && source[i] !== quote) {
                if (source[i] === "\\") i += 1;
                i += 1;
            }
            continue;
        }
        if (ch === "/" && source[i + 1] === "/") {
            i = source.indexOf("\n", i);
            if (i < 0) return null;
            continue;
        }
        if (ch === "{") depth += 1;
        else if (ch === "}") {
            depth -= 1;
            if (depth === 0) return source.slice(start, i + 1);
        }
    }
    return null;
}

const mod = await bundle("services/api/sdgf-payload.ts", "at13-sdgf-payload.mjs");
const media = await bundle("lib/media-size.ts", "at13-media-size.mjs");
const videoSrc = readFileSync(path.join(src, "services/api/video.ts"), "utf8");

/** 抽出的片段带 TS 类型标注（`mode: string | undefined` 这类），喂 new Function 前先用 esbuild 剥类型。 */
const stripTypes = async (code) => (await esbuild.transform(code, { loader: "ts", format: "esm", target: "es2020", logLevel: "silent" })).code;

// ---------------------------------------------------------------- 生产代码抽取（找不到即红）
const extracted = {
    resolveVideoMode: extractFunction(videoSrc, "resolveVideoMode"),
    sdgfMode: extractFunction(videoSrc, "sdgfMode"),
    sdgfDuration: extractFunction(videoSrc, "sdgfDuration"),
    sdgfAspectRatio: extractFunction(videoSrc, "sdgfAspectRatio"),
};
for (const [name, text] of Object.entries(extracted)) {
    check(`生产代码抽取：video.ts 的 ${name}（找不到=探针红，不静默跳过）`, typeof text === "string" && text.length > 0);
}
const gatesSrc = await stripTypes(
    `${extracted.resolveVideoMode || ""}\n${extracted.sdgfMode || ""}\n${extracted.sdgfDuration || ""}\n${extracted.sdgfAspectRatio || ""}`,
);
const gates = new Function(
    "inferVideoRatio",
    `${gatesSrc}\nreturn { resolveVideoMode, sdgfMode, sdgfDuration, sdgfAspectRatio };`,
)(media.inferVideoRatio);

// 组装块：`const body: Record<string, unknown> = {...}` + metadata 合并语句（原样抽出求值）
const bodyStart = videoSrc.indexOf("const body: Record<string, unknown> = {");
const mergeAt = videoSrc.indexOf("if (soundFields.metadata) body.metadata");
const bodyEnd = mergeAt < 0 ? -1 : videoSrc.indexOf("\n", mergeAt);
const bodySrc = bodyStart >= 0 && bodyEnd > mergeAt ? videoSrc.slice(bodyStart, bodyEnd) : "";
check("生产代码抽取：video.ts 的请求体组装块（body 字面 + metadata 合并语句）",
    bodySrc.includes("mode: sdgfMode(config.videoMode, images.length)")
        && bodySrc.includes("body.metadata = { ...(ratioFields.metadata || {}), ...soundFields.metadata }"),
    bodySrc.slice(-160));
const assembleSrc = bodySrc
    ? await stripTypes(`function __assemble(modelName, prompt, duration, ratioFields, soundFields, config, images, sdgfMode) {\n${bodySrc}\nreturn body;\n}`)
    : "";
const makeBody = assembleSrc ? new Function(`${assembleSrc}\nreturn __assemble;`)() : null;

// ---------------------------------------------------------------- 夹具
const fixture = JSON.parse(readFileSync(fixturePath, "utf8"));
const cases = fixture.cases || [];
console.log(`夹具：${path.relative(root, fixturePath)}`);
console.log(`fixture 案例数: ${cases.length}（合同 ${fixture.contract || "?"}）`);

const ids = cases.map((item) => item.id);
check("夹具 case id 唯一且非空",
    ids.length === ids.filter(Boolean).length && new Set(ids).size === ids.length,
    `${ids.length} 条 / ${new Set(ids).size} 唯一`);
const pick = (pred) => cases.filter((item) => pred(item.input, item.expect));
const coverage = {
    "满血 GF 名命中": pick((_i, e) => e.fullModel === true).length > 0,
    "别名/未知模型不命中": pick((_i, e) => e.fullModel === false).length > 0,
    "音频开": pick((i) => i.sound === true).length > 0,
    "音频关": pick((i) => i.sound === false).length > 0,
    "代际截断（clamp<请求）": pick((i, e) => e.clampDuration < i.duration).length > 0,
    "不截断（clamp==请求）": pick((i, e) => e.clampDuration === i.duration).length > 0,
    "metadata 双写（ratio）": pick((_i, e) => "metadata" in (e.ratioFields || {})).length > 0,
    "metadata 不写（枚举外画幅）": pick((_i, e) => !("metadata" in (e.ratioFields || {}))).length > 0,
    "metadata 合并（ratio+sound 同对象）": pick((_i, e) => e.metadata && "ratio" in e.metadata && "enableSound" in e.metadata).length > 0,
    "自适应画幅（ratio=adaptive）": pick((i) => i.ratio === "adaptive").length > 0,
    "mode=文生视频": pick((_i, e) => e.modeName === "文生视频").length > 0,
    "mode=首帧生视频": pick((_i, e) => e.modeName === "首帧生视频").length > 0,
    "mode=首尾帧": pick((_i, e) => e.modeName === "首尾帧").length > 0,
    "mode=参考生视频": pick((_i, e) => e.modeName === "参考生视频").length > 0,
    "拒绝因=model": pick((_i, e) => (e.assembly || {}).reject === "model").length > 0,
    "拒绝因=duration": pick((_i, e) => (e.assembly || {}).reject === "duration").length > 0,
    "拒绝因=ratio": pick((_i, e) => (e.assembly || {}).reject === "ratio").length > 0,
    "拒绝因=reference-no-image": pick((_i, e) => (e.assembly || {}).reject === "reference-no-image").length > 0,
};
for (const [name, ok] of Object.entries(coverage)) check(`夹具覆盖：${name}`, ok);

// ---------------------------------------------------------------- 逐 case：纯函数层 + 组装层
let asserts = 0;
for (const item of cases) {
    const { model, ratio, sound, duration, mode, imageCount } = item.input;
    const size = item.input.size ?? ratio;             // 画布 size → 比例（只有 auto 这类需归一的才显式给）
    const want = item.expect;
    const cid = item.id;

    asserts += 1;
    check(`${cid} · 纯函数 isSdgfFullModel`, mod.isSdgfFullModel(model) === want.fullModel, `${JSON.stringify(model)} → ${mod.isSdgfFullModel(model)}`);
    asserts += 1;
    const gotRatio = mod.sdgfRatioFields(model, ratio);
    check(`${cid} · 纯函数 sdgfRatioFields`, norm(gotRatio) === norm(want.ratioFields), `${norm(gotRatio)} ≠ ${norm(want.ratioFields)}`);
    asserts += 1;
    const gotSound = mod.sdgfSoundFields(model, sound);
    check(`${cid} · 纯函数 sdgfSoundFields`, norm(gotSound) === norm(want.soundFields), `${norm(gotSound)} ≠ ${norm(want.soundFields)}`);
    asserts += 1;
    check(`${cid} · 纯函数 sdgfClampDuration`, mod.sdgfClampDuration(model, duration) === want.clampDuration,
        `${mod.sdgfClampDuration(model, duration)} ≠ ${want.clampDuration}`);
    if (want.modeName !== null) {
        asserts += 1;
        check(`${cid} · 纯函数 sdgfMode`, gates.sdgfMode(mode, imageCount) === want.modeName,
            `${gates.sdgfMode(mode, imageCount)} ≠ ${want.modeName}`);
    }

    // ---- 组装层：与 createSdgfVideoTask 同序过闸（画幅 → 时长 → mode）
    let reject = null;
    let aspect = null;
    let requested = null;
    let modeName = null;
    // 画布 UI 保证模型名非空；桥 preflight 是硬门——夹具合同在此两端同锁「无模型名不得组装」。
    if (!String(model).trim()) reject = "model";
    if (!reject) {
        try {
            aspect = gates.sdgfAspectRatio(size);
        } catch {
            reject = "ratio";
        }
    }
    if (!reject) {
        try {
            requested = gates.sdgfDuration(String(duration));
        } catch {
            reject = "duration";
        }
    }
    if (!reject) {
        try {
            modeName = gates.sdgfMode(mode, imageCount);
        } catch {
            reject = "reference-no-image";
        }
    }

    const assembly = want.assembly || {};
    asserts += 1;
    if (assembly.ok) {
        check(`${cid} · 组装通过（无拒绝）`, reject === null, String(reject));
        asserts += 1;
        check(`${cid} · 请求体 aspect_ratio`, aspect === want.ratioFields.aspect_ratio, `${aspect} ≠ ${want.ratioFields.aspect_ratio}`);
        asserts += 1;
        check(`${cid} · 请求体 duration（代际截断后）`, mod.sdgfClampDuration(model, requested) === want.clampDuration,
            `${mod.sdgfClampDuration(model, requested)} ≠ ${want.clampDuration}`);
        asserts += 1;
        check(`${cid} · 请求体 mode（站方中文枚举）`, modeName === want.modeName, `${modeName} ≠ ${want.modeName}`);
        const ratioFields = mod.sdgfRatioFields(model, aspect);
        const soundFields = mod.sdgfSoundFields(model, sound);
        const body = makeBody
            ? makeBody(String(model).trim(), "夹具提示词（探针不提交）", mod.sdgfClampDuration(model, requested),
                ratioFields, soundFields, { videoMode: mode },
                new Array(imageCount).fill("https://fixture.invalid/ref.png"), gates.sdgfMode)
            : null;
        asserts += 1;
        check(`${cid} · 请求体 metadata 合并结果`, body !== null && norm(body.metadata) === norm(want.metadata),
            `${norm(body && body.metadata)} ≠ ${norm(want.metadata)}`);
        asserts += 1;
        check(`${cid} · 请求体顶层 enableSound`, body !== null && (body.enableSound ?? null) === want.topEnableSound,
            `${body && body.enableSound} ≠ ${want.topEnableSound}`);
        asserts += 1;
        check(`${cid} · 请求体 model 原样透传`, body !== null && body.model === String(model).trim(), `${body && body.model}`);
    } else {
        check(`${cid} · 组装被拒（${assembly.reject}）`, reject === assembly.reject, `reject=${reject} ≠ ${assembly.reject}`);
    }
}

// ---------------------------------------------------------------- 接线护栏（生产链真用了这些函数）
console.log("接线护栏：video.ts / sdgf-payload.ts 的真接线（文本级防退化，不属于跨语言 case）");
check("createSdgfVideoTask 用 sdgfRatioFields 算比例", /const ratioFields = sdgfRatioFields\(modelName,/.test(videoSrc));
check("createSdgfVideoTask 用 sdgfSoundFields 算声音", /const soundFields = sdgfSoundFields\(modelName, boolConfig\(config\.videoGenerateAudio, true\)\);/.test(videoSrc));
check("createSdgfVideoTask 用 sdgfClampDuration 截断时长", /const duration = sdgfClampDuration\(modelName, requestedDuration\);/.test(videoSrc));
check("声音与比例 metadata 合并同一对象（生产体原文，见上方抽取值）",
    bodySrc.includes("if (soundFields.metadata) body.metadata = { ...(ratioFields.metadata || {}), ...soundFields.metadata };"));
check("总开关 SDGF_SEND_SOUND_PARAM 已置 true（sendParam 不进夹具，映射范围在夹具内显式锁定）",
    mod.SDGF_SEND_SOUND_PARAM === true, String(mod.SDGF_SEND_SOUND_PARAM));
check("总开关关掉 → 两族都不发声音字段（开关语义独立验证，不占夹具条目）",
    Object.keys(mod.sdgfSoundFields("sd2.5-720p-ap", true, false)).length === 0
        && Object.keys(mod.sdgfSoundFields("SD2.5-满血-GF-720P", true, false)).length === 0);
check("总开关不再被改回 false", !/const SDGF_SEND_SOUND_PARAM = false/.test(videoSrc));
check("时长上界常量 SDGF_LEGACY_MAX_SECONDS === 15（夹具 clamp 期望以此为据）",
    mod.SDGF_LEGACY_MAX_SECONDS === 15, String(mod.SDGF_LEGACY_MAX_SECONDS));

rmSync(outDir, { recursive: true, force: true });

console.log(`\nprobe-sdgf-payload: ${passed}/${passed + failures.length} PASS（fixture 案例数 ${cases.length}，逐 case 断言 ${asserts} 条）`);
if (failures.length) {
    console.error("\n失败项：\n- " + failures.join("\n- "));
    process.exit(1);
}
console.log("PASS：TS 侧方言函数 + video.ts 请求体组装块与共享夹具逐条全等（单改任一端必红）");
