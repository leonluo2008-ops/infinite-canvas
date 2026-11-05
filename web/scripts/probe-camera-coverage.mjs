/**
 * 运镜覆盖度探针（B4a″/B4b′/B4c′，纯文本 + 纯函数，不连渠道、不花钱）。
 *
 * 转译真模块 web/src/lib/canvas/camera-coverage.ts（esbuild 打包后 import），断言口径：
 *   ① 三条有值调度行（推 / 跟 / 固定机位）+ 产物只有推与跟 → covered 2 / total 3、missing 含「固定机位」；
 *   ② 全部〔缺字段〕（兜底文字里带「推」「固定机位」）→ total 0，兜底不算命中；
 *   ③ 「移动/移步/移位」不是运镜：调度行只有这些词、产物也写「移动」→ total 0 不计；
 *   ④ covered === total 时计数照样正确（不因全绿而丢 total）；
 *   ⑤ 格式容错：`- 镜头调度：` / `**镜头调度**：` / 半角冒号 / 无项目符号都认；
 *   ⑥ 整词族不重复计：「平移」只算一个词，不再被单字「移」加一次；「机位固定」算命中；
 *   ⑦ 真实拉片 fixture（画布 pop-1-3 四镜原文）：4 条调度行 → total 3（镜3 无运镜词不进分母）；
 *   ⑧ 旧格式拉片（pop3test，7/7 镜〔缺字段〕）→ total 0，不虚报；
 *   ⑨ 接线没退化成空转：project.tsx 两处落盘都调 cameraCoverage、canvas-node.tsx 挂角标、
 *     types/canvas.ts 有字段、角标对 undefined / total 0 静默。
 *
 *   node scripts/probe-camera-coverage.mjs
 * 任何一条不成立退出码 1。
 */
import { build } from "esbuild";
import { existsSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const src = path.join(root, "src");
const workdir = mkdtempSync(path.join(tmpdir(), "probe-camera-coverage-"));
const outfile = path.join(workdir, "camera-coverage.mjs");

await build({
    entryPoints: [path.join(src, "lib/canvas/camera-coverage.ts")],
    outfile,
    bundle: true,
    format: "esm",
    platform: "node",
    logLevel: "silent",
    alias: { "@": src },
});
const mod = await import(pathToFileURL(outfile).href);
const cameraCoverage = mod.cameraCoverage;

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
const show = (value) => JSON.stringify(value);

console.log("① 三条有值调度行 + 产物缺一个词族");
const threeLines = ["【拉片段 01】", "0-1秒｜原片镜1", "- 镜头调度：低机位起幅缓慢推近，特写", "1-2秒｜原片镜2", "- 镜头调度：手持稳定跟随人物前行，中景", "2-3秒｜原片镜3", "- 镜头调度：固定机位平视，全景"].join("\n");
const productMissingOne = "镜头从低机位缓推靠近，随后稳定跟随人物，浅景深电影感。";
const case1 = cameraCoverage(threeLines, productMissingOne);
check("total === 3", case1.total === 3, show(case1));
check("covered === 2", case1.covered === 2, show(case1));
check("missing 含「固定机位」且只有它", case1.missing.length === 1 && case1.missing[0] === "固定机位", show(case1.missing));

console.log("② 全部〔缺字段〕：兜底文字里的运镜词不算，分母 0");
const missingField = ["- 镜头调度：〔缺字段〕（表无镜头列；五层镜1 兜底：机位与运镜：00:00-00:02固定机位；00:03微推向悬浮手机）", "- 镜头调度：〔缺字段〕（表无镜头列，五 镜头 层无对应行）", "- 镜头调度：〔旧格式无运镜列〕（分镜表只有景别，00:01 跟拍）"].join("\n");
const case2 = cameraCoverage(missingField, "推近、跟随、固定机位全都有");
check("total === 0", case2.total === 0, show(case2));
check("covered === 0", case2.covered === 0, show(case2));
check("missing 为空", case2.missing.length === 0, show(case2.missing));

console.log("③ 「移动/移步/移位」不是运镜：调度行与产物都出现也不算");
for (const daily of ["移动", "移步", "移位"]) {
    const lapian = `- 镜头调度：人物沿斑马线${daily}，景别不变`;
    const prompt = `人物在画面中${daily}，背景保持虚化。`;
    const result = cameraCoverage(lapian, prompt);
    check(`「${daily}」→ total 0`, result.total === 0 && result.covered === 0, show(result));
}
const realMove = cameraCoverage("- 镜头调度：定焦锁定微移，特写", "机身轻微微移。");
check("对照：「微移」是真运镜 → total 1 / covered 1", realMove.total === 1 && realMove.covered === 1, show(realMove));
const mixedDaily = cameraCoverage("- 镜头调度：人物移动后镜头前推，近景", "先移动再前推。");
check("同行「移动」+「推」→ 只算「推」", mixedDaily.total === 1 && mixedDaily.covered === 1, show(mixedDaily));

console.log("④ covered === total：全命中也要正确计数");
const fullLapian = "- 镜头调度：匀速前推，特写\n- 镜头调度：缓慢摇移下落，全景至中景";
const fullPrompt = "匀速前推至产品特写，随后缓慢摇移下落拉出全景。";
const case4 = cameraCoverage(fullLapian, fullPrompt);
check("covered === total === 3", case4.covered === 3 && case4.total === 3, show(case4));
check("missing 为空", case4.missing.length === 0, show(case4.missing));

console.log("⑤ 格式容错：项目符号 / 加粗 / 半角冒号 / 无符号");
const formats = ["- **镜头调度**：前推逼近", "镜头调度: 后拉远离", "* 镜头调度：环绕一周", "- 镜头调度 ： 变焦推近"].join("\n");
const case5 = cameraCoverage(formats, "随便什么产物");
// 第四行「变焦推近」是两个动作（变焦 + 推），一行两词都要进分母。
check("四种写法都进分母 → total 5（含一行两词）", case5.total === 5, show(case5));
check("全部未写 → covered 0 / missing 4（「推」出现两次只列一次）", case5.covered === 0 && case5.missing.length === 4, show(case5));

console.log("⑥ 整词族不重复计：平移、机位固定");
const phraseLapian = "- 镜头调度：平移跟拍，中景\n- 镜头调度：机位固定，特写\n- 镜头调度：环绕半圈，近景";
const case6 = cameraCoverage(phraseLapian, "机位固定为主，环绕半圈，横向平移并跟随一次。");
check("「平移跟拍」= 两个词，「平移」不再额外计一个「移」→ total 4", case6.total === 4, show(case6));
check("四词全命中 → covered 4", case6.covered === 4, show(case6));

console.log("⑦ 真实 fixture：画布 pop-1-3 四镜调度原文");
const realLapian = [
    "- 镜头调度：仰角后退并上摇，特写",
    "- 镜头调度：匀速后退拉远，中景",
    "- 镜头调度：后退减速并轻仰，中近景",
    "- 镜头调度：定焦锁定微移，特写",
].join("\n");
const realProduct = "低机位仰拍起幅，随后后退上摇再匀速拉远，最后推近定格……焦点从足部平滑移到面部，最终锁死在悬浮瓶体上。";
const case7 = cameraCoverage(realLapian, realProduct);
check("4 条调度行 → total 3（镜3 无运镜词不进分母）", case7.total === 3, show(case7));
check("镜1 摇 / 镜2 拉 / 镜4 移 全在产物里 → covered 3", case7.covered === 3, show(case7));
check("missing 空", case7.missing.length === 0, show(case7.missing));

console.log("⑧ 旧格式拉片（pop3test 7/7 镜〔缺字段〕）→ 分母 0");
const legacyPath = path.resolve(root, "..", "wanrepro/analysis/reverse/pop3test/拉片-jimeng-lapian.md");
if (existsSync(legacyPath)) {
    const legacy = cameraCoverage(readFileSync(legacyPath, "utf8"), "前推、跟拍、固定机位随便写");
    check("total === 0", legacy.total === 0, show(legacy));
    check("missing 空（无〔缺字段〕行进分母）", legacy.missing.length === 0, show(legacy.missing));
} else {
    console.log(`  skip ${legacyPath} 不在（跨仓证据未随行）`);
}

console.log("⑨ 接线没退化成空转（文本级防退化）");
const projectSrc = readFileSync(path.join(src, "pages/canvas/project.tsx"), "utf8");
const nodeSrc = readFileSync(path.join(src, "components/canvas/canvas-node.tsx"), "utf8");
const typesSrc = readFileSync(path.join(src, "types/canvas.ts"), "utf8");
check("project.tsx 引入 cameraCoverage", /import \{ cameraCoverage \} from "@\/lib\/canvas\/camera-coverage";/.test(projectSrc));
check("批量落盘按产物正文算覆盖度", /cameraCoverage\(lapianText, coverageText\)/.test(projectSrc));
check("重试落盘也补覆盖度", /cameraCoverage\(retryLapianText, retryText\)/.test(projectSrc));
check("两个落点都写进 metadata.cameraCoverage", /\.\.\.\(coverage \? \{ cameraCoverage: coverage \} : \{\}\)/.test(projectSrc) && /\.\.\.\(retryCoverage \? \{ cameraCoverage: retryCoverage \} : \{\}\)/.test(projectSrc));
check("拉片正文经 lapianSourceId 取（定义 + 两处调用）", /function lapianTextOfSource\(/.test(projectSrc) && (projectSrc.match(/lapianTextOfSource\(sourceNode, nodesRef\.current\)/g) || []).length >= 2);
check("types 声明 cameraCoverage 可选字段", /cameraCoverage\?: \{/.test(typesSrc));
check("卡片挂上 CameraCoverageChip", /<CameraCoverageChip node=\{data\} offsetTop=\{isBatchRoot\} \/>/.test(nodeSrc));
check("角标对 undefined / total 0 静默", /if \(!coverage \|\| coverage\.total <= 0\) return null;/.test(nodeSrc));
check("角标文案「运镜 N/M」", /运镜 \{coverage\.covered\}\/\{coverage\.total\}/.test(nodeSrc));
check("未覆盖用红色、全覆盖用中性色", /bg-red-600\/85/.test(nodeSrc) && /bg-black\/55/.test(nodeSrc));

console.log(`\n${passed} 通过 / ${failures.length} 失败`);
if (failures.length) {
    console.error("\n失败项：\n- " + failures.join("\n- "));
    process.exit(1);
}
console.log("PASS：运镜覆盖度口径 + 两处落盘接线 + 卡片角标接线全部成立");
