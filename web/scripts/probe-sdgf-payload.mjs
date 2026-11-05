/**
 * SDGF ap 比例/无声修复探针（纯逻辑层，不连渠道、不花钱）。
 *
 * 转译真模块 web/src/services/api/sdgf-payload.ts（esbuild 打包后 import），断言：
 *   ①比例双写：非满血 GF 名（ap/zh/ds 别名）→ 顶层 aspect_ratio + metadata.ratio 双写；
 *   ②满血 GF 名（SD2.5-满血-GF-{480,720,1080}P）→ 只有顶层 aspect_ratio，**不带 metadata 键**（十四章合同原样）；
 *   ③文档枚举外比例（4:3 / 3:4 / adaptive）不塞 metadata；
 *   ④声音总开关 SDGF_SEND_SOUND_PARAM=true，且「生成音频」关掉时不发 enableSound（站方默认 off）；
 *   ⑤video.ts 真接线（文本级），防退化成硬编码或把总开关改回 false；
 *   ⑥ap 系声音生效位在 metadata.enableSound（V7），顶层不发 enableSound；
 *   ⑦满血 GF 声音仍在顶层 enableSound，body 不带 metadata 键；
 *   ⑧关音频 → 两族均不发任何声音字段；
 *   ⑨旧合同时长上界按代际（SD2.0 系 ≤15 截断 / SD2.5 全系 4–30 原样）；
 *   ⑩满血 GF 完整 body 字面 fixture 逐键逐序不漂移。
 *
 *   node scripts/probe-sdgf-payload.mjs
 * 任何一条不成立退出码 1。
 */
import { build } from "esbuild";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const src = path.join(root, "src");
const workdir = mkdtempSync(path.join(tmpdir(), "probe-sdgf-payload-"));
const outfile = path.join(workdir, "sdgf-payload.mjs");

await build({
    entryPoints: [path.join(src, "services/api/sdgf-payload.ts")],
    outfile,
    bundle: true,
    format: "esm",
    platform: "node",
    logLevel: "silent",
    alias: { "@": src },
});
const mod = await import(pathToFileURL(outfile).href);

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

const AP = "sd2.5-720p-ap";
const ZH = "sd2.0-720p-zh";
const GF720 = "SD2.5-满血-GF-720P";
const GF480 = "SD2.5-满血-GF-480P";
const GF1080 = "SD2.5-满血-GF-1080P";

console.log("① ap/zh 别名：双写 aspect_ratio + metadata.ratio");
for (const model of [AP, ZH]) {
    for (const ratio of ["9:16", "16:9", "1:1"]) {
        const fields = mod.sdgfRatioFields(model, ratio);
        check(`${model} · ${ratio} 顶层 aspect_ratio`, fields.aspect_ratio === ratio, JSON.stringify(fields));
        check(`${model} · ${ratio} metadata.ratio`, fields.metadata?.ratio === ratio, JSON.stringify(fields));
    }
}

console.log("② 满血 GF：合同原样（只顶层，无 metadata 键）");
for (const model of [GF480, GF720, GF1080, "sd2.5-满血-gf-720p", ` ${GF720} `]) {
    for (const ratio of ["9:16", "16:9"]) {
        const fields = mod.sdgfRatioFields(model, ratio);
        check(`${model.trim()} · ${ratio} 只发 aspect_ratio`, fields.aspect_ratio === ratio, JSON.stringify(fields));
        check(`${model.trim()} · ${ratio} 不带 metadata 键`, !Object.hasOwn(fields, "metadata"), JSON.stringify(fields));
    }
}

console.log("③ 文档枚举外比例：不塞 metadata");
for (const ratio of ["4:3", "3:4", "adaptive"]) {
    const fields = mod.sdgfRatioFields(AP, ratio);
    check(`ap · ${ratio} 无 metadata`, !Object.hasOwn(fields, "metadata"), JSON.stringify(fields));
    check(`ap · ${ratio} 顶层保留`, fields.aspect_ratio === ratio, JSON.stringify(fields));
}

console.log("④ 声音开关语义（总开关 + 面板「生成音频」，满血 GF 顶层位）");
check("SDGF_SEND_SOUND_PARAM 已置 true", mod.SDGF_SEND_SOUND_PARAM === true, String(mod.SDGF_SEND_SOUND_PARAM));
check("满血 GF 默认（开音频）→ 顶层 enableSound:'on'", mod.sdgfSoundFields(GF720, true).enableSound === "on", JSON.stringify(mod.sdgfSoundFields(GF720, true)));
check("满血 GF 关音频 → 不发 enableSound", !Object.hasOwn(mod.sdgfSoundFields(GF720, false), "enableSound"), JSON.stringify(mod.sdgfSoundFields(GF720, false)));
check("总开关关闭 → 不发 enableSound", !Object.hasOwn(mod.sdgfSoundFields(GF720, true, false), "enableSound"), JSON.stringify(mod.sdgfSoundFields(GF720, true, false)));
check("总开关关闭 → 即使面板开音频也不发（ap 同）", Object.keys(mod.sdgfSoundFields(AP, true, false)).length === 0, JSON.stringify(mod.sdgfSoundFields(AP, true, false)));

console.log("⑤ video.ts 真接线（文本级防退化）");
const videoSrc = readFileSync(path.join(src, "services/api/video.ts"), "utf8");
check("createSdgfVideoTask 用 sdgfRatioFields 算比例", /const ratioFields = sdgfRatioFields\(modelName,/.test(videoSrc));
check("body 带 metadata 条件展开", /\.\.\.\(ratioFields\.metadata \? \{ metadata: ratioFields\.metadata \} : \{\}\)/.test(videoSrc));
check("声音字段按模型名分流调用", /const soundFields = sdgfSoundFields\(modelName, boolConfig\(config\.videoGenerateAudio, true\)\);/.test(videoSrc));
check("body 展开顶层 enableSound（满血 GF 位）", /\.\.\.\(soundFields\.enableSound \? \{ enableSound: soundFields\.enableSound \} : \{\}\)/.test(videoSrc));
check("声音 metadata 与比例 metadata 合并同一对象", /if \(soundFields\.metadata\) body\.metadata = \{ \.\.\.\(ratioFields\.metadata \|\| \{\}\), \.\.\.soundFields\.metadata \};/.test(videoSrc));
check("duration 走 sdgfClampDuration", /const duration = sdgfClampDuration\(modelName, requestedDuration\);/.test(videoSrc) && /\n\s+duration,\r?\n/.test(videoSrc));
check("总开关不再被改回 false", !/const SDGF_SEND_SOUND_PARAM = false/.test(videoSrc));

// body 级断言：与 video.ts createSdgfVideoTask 同构的最小组装器（接线本身由 ⑤ 的文本断言兜住）
function buildBody(model, { prompt = "probe prompt", seconds = 8, ratio = "16:9", generateAudio = true, mode = "文生视频" } = {}) {
    const ratioFields = mod.sdgfRatioFields(model, ratio);
    const soundFields = mod.sdgfSoundFields(model, generateAudio);
    const body = {
        model,
        prompt,
        duration: mod.sdgfClampDuration(model, seconds),
        aspect_ratio: ratioFields.aspect_ratio,
        ...(ratioFields.metadata ? { metadata: ratioFields.metadata } : {}),
        mode,
        ...(soundFields.enableSound ? { enableSound: soundFields.enableSound } : {}),
    };
    if (soundFields.metadata) body.metadata = { ...(ratioFields.metadata || {}), ...soundFields.metadata };
    return body;
}

console.log("⑥ ap 系：声音生效位在 metadata.enableSound（V7），顶层不发");
const apBody = buildBody(AP, { ratio: "9:16", generateAudio: true });
check("ap · metadata.enableSound === 'on'", apBody.metadata?.enableSound === "on", JSON.stringify(apBody));
check("ap · 顶层无 enableSound 键", !Object.hasOwn(apBody, "enableSound"), JSON.stringify(apBody));
check("ap · metadata 同时带 ratio 与 enableSound（同一对象不互相覆盖）", apBody.metadata?.ratio === "9:16" && apBody.metadata?.enableSound === "on", JSON.stringify(apBody.metadata));

console.log("⑦ 满血 GF：声音仍在顶层，body 不带 metadata 键（合同原样）");
const gfBody = buildBody(GF720, { ratio: "9:16", generateAudio: true });
check("GF · 顶层 enableSound === 'on'", gfBody.enableSound === "on", JSON.stringify(gfBody));
check("GF · body 无 metadata 键", !Object.hasOwn(gfBody, "metadata"), JSON.stringify(gfBody));

console.log("⑧ 关音频：两族均不发任何声音字段");
for (const model of [AP, GF720]) {
    const body = buildBody(model, { ratio: "9:16", generateAudio: false });
    check(`${model} · 顶层无 enableSound`, !Object.hasOwn(body, "enableSound"), JSON.stringify(body));
    check(`${model} · metadata 无 enableSound`, !Object.hasOwn(body.metadata || {}, "enableSound"), JSON.stringify(body.metadata));
}

console.log("⑨ 旧合同时长上界按代际（SD2.0 系 ≤15 截断 / SD2.5 全系 4–30 原样）");
check("SDGF_LEGACY_MAX_SECONDS === 15", mod.SDGF_LEGACY_MAX_SECONDS === 15, String(mod.SDGF_LEGACY_MAX_SECONDS));
check("sd2.5-720p-ap · 20 → 20（2.5 系不截断）", mod.sdgfClampDuration(AP, 20) === 20, String(mod.sdgfClampDuration(AP, 20)));
check("sd2.5-720p-ap · 30 → 30", mod.sdgfClampDuration(AP, 30) === 30, String(mod.sdgfClampDuration(AP, 30)));
check("sd2.5-720p-ap · 12 → 12", mod.sdgfClampDuration(AP, 12) === 12, String(mod.sdgfClampDuration(AP, 12)));
check("sd2.0-720p-zh · 20 → 15（2.0 旧合同截断）", mod.sdgfClampDuration(ZH, 20) === 15, String(mod.sdgfClampDuration(ZH, 20)));
check("sd2-720p-ds-v933 · 20 → 15", mod.sdgfClampDuration("sd2-720p-ds-v933", 20) === 15, String(mod.sdgfClampDuration("sd2-720p-ds-v933", 20)));
check("sd2-720p-ds-v933 · 12 → 12", mod.sdgfClampDuration("sd2-720p-ds-v933", 12) === 12, String(mod.sdgfClampDuration("sd2-720p-ds-v933", 12)));
check("SD2.5-满血-GF-720P · 20 → 20", mod.sdgfClampDuration(GF720, 20) === 20, String(mod.sdgfClampDuration(GF720, 20)));
check("SD2.5-满血-GF-720P · 30 → 30", mod.sdgfClampDuration(GF720, 30) === 30, String(mod.sdgfClampDuration(GF720, 30)));

console.log("⑩ 满血 GF 完整 body 字面 fixture 不漂移（改动前后逐键一致）");
const GF_FIXTURE = { model: GF720, prompt: "probe prompt", duration: 20, aspect_ratio: "9:16", mode: "文生视频", enableSound: "on" };
const gfFixtureBody = buildBody(GF720, { seconds: 20, ratio: "9:16" });
check("GF fixture 键集合一致", JSON.stringify(Object.keys(gfFixtureBody).sort()) === JSON.stringify(Object.keys(GF_FIXTURE).sort()), JSON.stringify(Object.keys(gfFixtureBody)));
check("GF fixture 逐键取值一致", JSON.stringify(gfFixtureBody) === JSON.stringify(GF_FIXTURE), JSON.stringify(gfFixtureBody));
check("GF fixture 键序一致（无新增字段、无挪位）", Object.keys(gfFixtureBody).join(",") === "model,prompt,duration,aspect_ratio,mode,enableSound", Object.keys(gfFixtureBody).join(","));

console.log(`\n${passed} 通过 / ${failures.length} 失败`);
if (failures.length) {
    console.error("\n失败项：\n- " + failures.join("\n- "));
    process.exit(1);
}
console.log("PASS：SDGF ap 比例双写 + 满血 GF 合同不动 + enableSound 开关语义全部成立");
