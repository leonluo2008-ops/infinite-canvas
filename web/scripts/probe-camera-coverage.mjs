/**
 * 运镜覆盖度探针（B4a″/B4b′/B4c′/B7，纯文本 + 纯函数，不连渠道、不花钱）。
 *
 * 转译真模块 web/src/lib/canvas/camera-coverage.ts（esbuild 打包后 import），断言口径：
 *   A 段 老口径回归（B7b 换标准词表后逐条重算，改过期望值的地方都写了理由）：
 *     A1 三条有值调度行 + 产物缺词族；A2 全部〔缺字段〕兜底不算；A3「移动/移步/移位」不是运镜；
 *     A4 covered === total 计数正确；A5 格式容错；A6 整词族不重复计；
 *     A7 真实 fixture（画布 pop-1-3 四镜原文）；A8 旧格式拉片（pop3test 7/7 缺字段）；
 *     A9 接线没退化成空转（project.tsx / canvas-node.tsx / types/canvas.ts）。
 *   B 段 B7c 新八条：标准词表 + 兼容层 + 镜头主语窗（产物侧误命中防线）。
 *   C 段 三源一致：拆解 prompt 两处词表段逐字相同、改写契约沿用条款、coverage 词表同源。
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

console.log("A1 三条有值调度行 + 产物缺词族");
const threeLines = ["【拉片段 01】", "0-1秒｜原片镜1", "- 镜头调度：低机位起幅缓慢推近，特写", "1-2秒｜原片镜2", "- 镜头调度：手持稳定跟随人物前行，中景", "2-3秒｜原片镜3", "- 镜头调度：固定机位平视，全景"].join("\n");
const productMissingOne = "镜头从低机位缓推靠近，随后稳定跟随人物，浅景深电影感。";
const case1 = cameraCoverage(threeLines, productMissingOne);
// B7b 口径变更重算：原期望 total 3 / covered 2 / missing 一个「固定机位」。
// 标准词表把「低机位」「平视」「手持」这些机位状态词也收进镜头运动列，分母从 3 变 6；
// 「推近」认基础族「推」（产物写「缓推靠近」），所以 covered 3。
check("total === 6", case1.total === 6, show(case1));
check("covered === 3", case1.covered === 3, show(case1));
check("missing 是 手持 / 固定机位 / 平视", show(case1.missing) === show(["手持", "固定机位", "平视"]), show(case1.missing));

console.log("A2 全部〔缺字段〕：兜底文字里的运镜词不算，分母 0");
const missingField = ["- 镜头调度：〔缺字段〕（表无镜头列；五层镜1 兜底：机位与运镜：00:00-00:02固定机位；00:03微推向悬浮手机）", "- 镜头调度：〔缺字段〕（表无镜头列，五 镜头 层无对应行）", "- 镜头调度：〔旧格式无运镜列〕（分镜表只有景别，00:01 跟拍）"].join("\n");
const case2 = cameraCoverage(missingField, "推近、跟随、固定机位全都有");
check("total === 0", case2.total === 0, show(case2));
check("covered === 0", case2.covered === 0, show(case2));
check("missing 为空", case2.missing.length === 0, show(case2.missing));

console.log("A3 「移动/移步/移位」不是运镜：调度行与产物都出现也不算");
for (const daily of ["移动", "移步", "移位"]) {
    const lapian = `- 镜头调度：人物沿斑马线${daily}，景别不变`;
    const prompt = `人物在画面中${daily}，背景保持虚化。`;
    const result = cameraCoverage(lapian, prompt);
    check(`「${daily}」→ total 0`, result.total === 0 && result.covered === 0, show(result));
}
// 产物侧改成带镜头主语的写法：B7b 加了镜头主语窗，「机身轻微微移」这种没有
// 镜头/机位/画面/视角/运镜的句子不再算命中（口径变更，夹具跟着改）。
const realMove = cameraCoverage("- 镜头调度：定焦锁定微移，特写", "镜头轻微微移。");
check("对照：「微移」是真运镜 → total 1 / covered 1", realMove.total === 1 && realMove.covered === 1, show(realMove));
const mixedDaily = cameraCoverage("- 镜头调度：人物移动后镜头前推，近景", "镜头先移动再前推。");
check("同行「移动」+「前推」→ 只算「前推」（前推占位后不再计「推」）", mixedDaily.total === 1 && mixedDaily.covered === 1, show(mixedDaily));

console.log("A4 covered === total：全命中也要正确计数");
const fullLapian = "- 镜头调度：匀速前推，特写\n- 镜头调度：缓慢摇移下落，全景至中景";
// 产物加「镜头」并缩短句距：摇/移 要落在镜头主语 14 字窗内（B7b 口径）。
const fullPrompt = "镜头先匀速前推至特写，再缓慢摇移下落。";
const case4 = cameraCoverage(fullLapian, fullPrompt);
check("covered === total === 3", case4.covered === 3 && case4.total === 3, show(case4));
check("missing 为空", case4.missing.length === 0, show(case4));

console.log("A5 格式容错：项目符号 / 加粗 / 半角冒号 / 无符号");
// 第二行原为「后拉远离」，其中「拉远」与「后拉」在同一位置重叠；词表加长扫描后
// 会取「拉远」吃掉两个词。夹具加一个连接字去歧义（口径没变，只是让夹具不再踩重叠）。
const formats = ["- **镜头调度**：前推逼近", "镜头调度: 后拉并远离", "* 镜头调度：环绕一周", "- 镜头调度 ： 变焦推近"].join("\n");
const case5 = cameraCoverage(formats, "随便什么产物");
// 词表扩容后：前推 / 后拉 + 远离 / 环绕 / 变焦 + 推近 = 6（原期望 5：旧的「推」与「推近」是同一个词）。
check("四种写法都进分母 → total 6（两行各两个词）", case5.total === 6, show(case5));
check("全部未写 → covered 0 / missing 6", case5.covered === 0 && case5.missing.length === 6, show(case5));

console.log("A6 整词族不重复计：平移、机位固定");
const phraseLapian = "- 镜头调度：平移跟拍，中景\n- 镜头调度：机位固定，特写\n- 镜头调度：环绕半圈，近景";
// 产物改成带镜头主语的写法：「跟拍」一族认基础族「跟」，所以「跟随」也算命中（B7b 同族归并）。
const case6 = cameraCoverage(phraseLapian, "机位固定为主，环绕半圈；镜头横向平移并跟随一次。");
check("「平移跟拍」= 两个词，「平移」不再额外计一个「移」→ total 4", case6.total === 4, show(case6));
check("四词全命中 → covered 4", case6.covered === 4, show(case6));

console.log("A7 真实 fixture：画布 pop-1-3 四镜调度原文");
const realLapian = [
    "- 镜头调度：仰角后退并上摇，特写",
    "- 镜头调度：匀速后退拉远，中景",
    "- 镜头调度：后退减速并轻仰，中近景",
    "- 镜头调度：定焦锁定微移，特写",
].join("\n");
const realProduct = "低机位仰拍起幅，随后后退上摇再匀速拉远，最后推近定格……焦点从足部平滑移到面部，最终锁死在悬浮瓶体上。";
const case7 = cameraCoverage(realLapian, realProduct);
// B7b 口径变更重算：原期望 total 3（推/摇/移三个旧词）。标准词表把 仰角、后退、上摇、拉远
// 各立成词，镜 1/2/3 的后退各计一次 → total 7；产物命中 后退/上摇/拉远 与「推」（推近的
// 基础族），缺 仰角（产物写的是「仰拍」）与 移（「移到面部」离镜头主语超过 14 字）。
check("4 条调度行 → total 7", case7.total === 7, show(case7));
check("covered 5", case7.covered === 5, show(case7));
check("missing 是 仰角 / 移", show(case7.missing) === show(["仰角", "移"]), show(case7.missing));

console.log("A8 旧格式拉片（pop3test 7/7 镜〔缺字段〕）→ 分母 0");
const legacyPath = path.resolve(root, "..", "wanrepro/analysis/reverse/pop3test/拉片-jimeng-lapian.md");
if (existsSync(legacyPath)) {
    const legacy = cameraCoverage(readFileSync(legacyPath, "utf8"), "前推、跟拍、固定机位随便写");
    check("total === 0", legacy.total === 0, show(legacy));
    check("missing 空（无〔缺字段〕行进分母）", legacy.missing.length === 0, show(legacy.missing));
} else {
    console.log(`  skip ${legacyPath} 不在（跨仓证据未随行）`);
}

console.log("A9 接线没退化成空转（文本级防退化）");
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

console.log("B7c-① 调度行「仰角后退并上摇，特写」→ 三词全命中");
const shotWords = cameraCoverage("- 镜头调度：仰角后退并上摇，特写", "");
check("分母 = 仰角 / 后退 / 上摇，景别「特写」不算", shotWords.total === 3 && show(shotWords.missing) === show(["仰角", "后退", "上摇"]), show(shotWords));
const shotHit = cameraCoverage("- 镜头调度：仰角后退并上摇，特写", "镜头仰角后退，随后上摇到面部。");
check("产物三词都有 → covered 3 / missing 空", shotHit.covered === 3 && shotHit.total === 3 && shotHit.missing.length === 0, show(shotHit));

console.log("B7c-② 「后退减速并轻仰，中近景」→ 后退命中，「轻仰」不另立词");
const backPedal = cameraCoverage("- 镜头调度：后退减速并轻仰，中近景", "镜头后退减速，重心轻仰。");
check("total 1 / covered 1（「轻仰」不进词表）", backPedal.total === 1 && backPedal.covered === 1 && show(backPedal.missing) === "[]", show(backPedal));

console.log("B7c-③ 产物句「瓶体在中央悬停旋转」不算旋转");
const bottleSpin = cameraCoverage("- 镜头调度：旋转半圈，近景", "瓶体在中央悬停旋转。");
check("旋转没被物体自转骗到 → covered 0 / missing 旋转", bottleSpin.total === 1 && bottleSpin.covered === 0 && show(bottleSpin.missing) === show(["旋转"]), show(bottleSpin));

console.log("B7c-④ 产物句「人物后退一步」不算后退");
const personBack = cameraCoverage("- 镜头调度：后退一步，中景", "人物后退一步。");
check("人走路的后退不算运镜 → covered 0", personBack.total === 1 && personBack.covered === 0, show(personBack));

console.log("B7c-⑤ 产物句「镜头匀速后退拉远」→ 后退 + 拉远都算");
const cameraBack = cameraCoverage("- 镜头调度：后退拉远，远景", "镜头匀速后退拉远。");
check("两个词都过窗 → covered 2 / total 2", cameraBack.total === 2 && cameraBack.covered === 2, show(cameraBack));

console.log("B7c-⑥ 「固定，特写」裸固定 → 算固定机位族");
const bareFixed = cameraCoverage("- 镜头调度：固定，特写", "镜头全程固定。");
check("裸「固定」归一成「固定机位」→ covered 1", bareFixed.total === 1 && bareFixed.covered === 1, show(bareFixed));
const bareFixedWords = cameraCoverage("- 镜头调度：固定，特写", "");
check("词名归一：missing 里写「固定机位」", show(bareFixedWords.missing) === show(["固定机位"]), show(bareFixedWords));

console.log("B7c-⑦ 「升格慢动作」不计（时间节奏，不是镜头运动）");
const slowMotion = cameraCoverage("- 镜头调度：升格慢动作，特写", "镜头升格慢动作。");
check("「升格」里的「升」不算 → total 0", slowMotion.total === 0 && slowMotion.covered === 0, show(slowMotion));

console.log("B7c-⑧ 特写 / 中景 / 全景 / 远景属景别，不计");
const shotSizes = cameraCoverage("- 镜头调度：特写转中景，全景，远景", "镜头特写转中景再拉全景。");
check("全是景别词 → total 0", shotSizes.total === 0 && shotSizes.covered === 0 && shotSizes.missing.length === 0, show(shotSizes));

console.log("C 契约单一事实源：拆解占位注入 / 改写契约 raw import / coverage 逐词可识别");
// C-1 契约文件本体
const contractPath = path.resolve(root, "..", "wanrepro/contracts/camera-contract.md");
const contractSrc = readFileSync(contractPath, "utf8");
const contractLines = contractSrc.split(/\r?\n/);
const wordLine = (prefix) => contractLines.find((line) => line.startsWith(prefix)) ?? "";
const baseLine = wordLine("- 基础族：");
const compoundLine = wordLine("- 复合与方向：");
const stateLine = wordLine("- 机位状态：");
check("契约含 §1/§2/§3 三节标记", contractSrc.includes("## §1 选词表") && contractSrc.includes("## §2 镜头感受与灯光表述") && contractSrc.includes("## §3 口吻"));
check("契约 §1 三行词表（基础族/复合与方向/机位状态）", baseLine.length > 10 && compoundLine.length > 10 && stateLine.length > 10);
check("契约含升格/景别排除说明", contractSrc.includes("升格") && contractSrc.includes("景别"));

// C-2 拆解侧：占位注入（extract.py 发送前替换）
const promptPath = path.resolve(root, "..", "wanrepro/prompts/视频拆解.md");
const promptSrc = readFileSync(promptPath, "utf8");
check("拆解 prompt 分镜表处用占位 {{镜头语言契约}}（恰好 1 处）", (promptSrc.match(/\{\{镜头语言契约\}\}/g) ?? []).length === 1);
check("拆解 prompt 五层处是指向句（不重复全文）", promptSrc.includes("遵循上文分镜表部分的【镜头语言契约】"));
const at2 = (needle) => promptSrc.indexOf(needle);
const between2 = (from, to) => promptSrc.slice(at2(from), at2(to));
check("占位落在「七 分镜表」段", between2("## 七 分镜表", "## 八 声音").includes("{{镜头语言契约}}"));
check("指向句落在「五 镜头」层", between2("## 五 镜头", "## 六 意境").includes("遵循上文分镜表部分"));
const extractPy = readFileSync(path.resolve(root, "..", "wanrepro/pipeline/reverse/extract.py"), "utf8");
check("extract.py 有契约注入函数且读契约文件", extractPy.includes("_inject_camera_contract") && extractPy.includes("camera-contract.md"));

// C-3 改写侧：raw import 契约，不另抄词表
const templatesPath = path.join(src, "lib/canvas/lapian-prompt-templates.ts");
const templatesSrc = readFileSync(templatesPath, "utf8");
check("改写契约 raw import 契约文件", templatesSrc.includes("camera-contract.md?raw"));
check("改写契约含沿用条款（不自创同义说法）", templatesSrc.includes("标准镜头语言") && templatesSrc.includes("不自创同义说法"));
const importZone = templatesSrc.slice(templatesSrc.indexOf("CAMERA_LANGUAGE_RULES"), templatesSrc.indexOf("COMMON_RULES"));
check("改写契约切片注入 §1 与 §2", importZone.includes("contractSection") && importZone.includes("§1") && importZone.includes("§2"));

// C-4 消费层逐词校验：契约 §1 每个词都必须能被 coverage 识别（加词不跟 → 这里红）
const parseWords = (line) => line.replace(/^- [^：]+：/, "").split("、").map((word) => word.trim()).filter(Boolean);
const allContractWords = [...parseWords(baseLine), ...parseWords(compoundLine), ...parseWords(stateLine)];
check("契约词表非空（≥40 词）", allContractWords.length >= 40, `${allContractWords.length} 词`);
const unrecognized = allContractWords.filter((word) => {
    const result = cameraCoverage(`- 镜头调度：${word}，特写`, `镜头${word}。`);
    return result.total < 1 || result.covered < 1;
});
check("契约 §1 全部词 coverage 可识别（进分母且命中）", unrecognized.length === 0, unrecognized.join("/"));

console.log(`\n${passed} 通过 / ${failures.length} 失败`);
if (failures.length) {
    console.error("\n失败项：\n- " + failures.join("\n- "));
    process.exit(1);
}
console.log("PASS：标准镜头词表（拆解 prompt / 改写契约 / coverage 三源一致）+ 兼容层 + 镜头主语窗 + 两处落盘接线 + 卡片角标接线全部成立");
