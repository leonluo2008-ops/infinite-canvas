/**
 * SDGF ap 比例/无声修复探针（纯逻辑层，不连渠道、不花钱）。
 *
 * 转译真模块 web/src/services/api/sdgf-payload.ts（esbuild 打包后 import），断言：
 *   ①比例双写：非满血 GF 名（ap/zh/ds 别名）→ 顶层 aspect_ratio + metadata.ratio 双写；
 *   ②满血 GF 名（SD2.5-满血-GF-{480,720,1080}P）→ 只有顶层 aspect_ratio，**不带 metadata 键**（十四章合同原样）；
 *   ③文档枚举外比例（4:3 / 3:4 / adaptive）不塞 metadata；
 *   ④声音总开关 SDGF_SEND_SOUND_PARAM=true，且「生成音频」关掉时不发 enableSound（站方默认 off）；
 *   ⑤video.ts 真接线（文本级），防退化成硬编码或把总开关改回 false。
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

console.log("④ 声音开关语义（总开关 + 面板「生成音频」）");
check("SDGF_SEND_SOUND_PARAM 已置 true", mod.SDGF_SEND_SOUND_PARAM === true, String(mod.SDGF_SEND_SOUND_PARAM));
check("默认（开音频）→ enableSound:'on'", mod.sdgfSoundFields(true).enableSound === "on", JSON.stringify(mod.sdgfSoundFields(true)));
check("关音频 → 不发 enableSound", !Object.hasOwn(mod.sdgfSoundFields(false), "enableSound"), JSON.stringify(mod.sdgfSoundFields(false)));
check("总开关关闭 → 不发 enableSound", !Object.hasOwn(mod.sdgfSoundFields(true, false), "enableSound"), JSON.stringify(mod.sdgfSoundFields(true, false)));
check("总开关关闭 → 即使面板开音频也不发", !Object.hasOwn(mod.sdgfSoundFields(true, false), "enableSound"));

console.log("⑤ video.ts 真接线（文本级防退化）");
const videoSrc = readFileSync(path.join(src, "services/api/video.ts"), "utf8");
check("createSdgfVideoTask 用 sdgfRatioFields 算比例", /const ratioFields = sdgfRatioFields\(modelName,/.test(videoSrc));
check("body 带 metadata 条件展开", /\.\.\.\(ratioFields\.metadata \? \{ metadata: ratioFields\.metadata \} : \{\}\)/.test(videoSrc));
check("body 带声音字段展开", /\.\.\.sdgfSoundFields\(boolConfig\(config\.videoGenerateAudio, true\)\)/.test(videoSrc));
check("总开关不再被改回 false", !/const SDGF_SEND_SOUND_PARAM = false/.test(videoSrc));

console.log(`\n${passed} 通过 / ${failures.length} 失败`);
if (failures.length) {
    console.error("\n失败项：\n- " + failures.join("\n- "));
    process.exit(1);
}
console.log("PASS：SDGF ap 比例双写 + 满血 GF 合同不动 + enableSound 开关语义全部成立");
