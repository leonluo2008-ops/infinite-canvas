/**
 * AT-13 B3 前端自检（免费探针）：esbuild 打包 web/src/lib/hub/gen-settings.ts + media-size.ts 后
 * 直接断言真代码 + 源码接线护栏（task-detail-drawer.tsx / hub-api.ts / model-picker.tsx）。
 *
 * 覆盖（对应执行卡质量门 1~7）：
 *   ① 枚举同源：画幅/清晰度/时长选项＝media-size.ts 派生（含 21:9 / 自适应 / WxH）
 *   ② 协议矩阵：SDGF+wan 可选；gemini/openai/模型脚本标「画布专用」禁选
 *   ②b 模型候选同源（AT-14 §3.2 / §五.2）：候选全集＝画布 selectableModelsByCapability 输出，
 *      行序逐行对比 + 三态标注（可选 / 画布可用·工作台暂不承接 / 画布专用（模型脚本））+ 负例必红
 *   ③ wan 如实 UI：固定 wan3.0·自动多图参考，model/mode 控件不渲染
 *   ④ approve 请求体带 config 且**无 key**；submit 请求体带 key（一次性）
 *   ⑤ 草稿按「渠道+模型」存任务草稿；失效引用显式提示；key 不进签名（只换 key 不失效）
 *   ⑥ 提交链阻断：active_submit / submit_unknown / dispatch / 孤儿三步 → 引导文案 + 禁重提
 *   ⑦ JSX/API 接线护栏（红条、阻断入口、续查、撤销批准、无直接重提按钮）
 *   ⑨ 画幅规约（AT-14 §3.1）：与 PY 探针共享 sdgf-dialect/cases.json 的 size 用例
 *
 * 跑法：cd web && node scripts/probe-at13-b3.mjs   （失败 rc=1）
 * 注：离线无 DOM runtime，真机 DOM 可见性/点选由 B5 收口；本探针管逻辑真值 + JSX 接线。
 *     模型候选的「两侧对比」= 画布真入口函数（store.selectableModelsByCapability）对 hub 模型区行，
 *     并在同一断言上跑旧行为替身做负例（防恒真，L2 审计 B-07）。
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

// AT-14 B2 起 gen-settings.ts 直接 import 画布共享函数（use-config-store.ts#selectableModelsByCapability）——
// 该模块加载时读 localStorage（i18n 取语言），node 侧给最小垫片；打包平台改用 node
// （store 依赖链里有 nanoid → node:crypto，neutral 平台打不过）。
const memory = new Map();
globalThis.localStorage = {
    getItem: (key) => (memory.has(key) ? memory.get(key) : null),
    setItem: (key, value) => memory.set(key, String(value)),
    removeItem: (key) => memory.delete(key),
    clear: () => memory.clear(),
};

const outDir = path.resolve("node_modules/.probe");
mkdirSync(outDir, { recursive: true });

let pass = 0;
const fails = [];
function check(name, condition, detail = "") {
    if (condition) {
        pass += 1;
        return;
    }
    fails.push(`${name}${detail ? ` — ${detail}` : ""}`);
}

async function bundle(entry, outName, platform = "node") {
    const outFile = path.join(outDir, outName);
    await esbuild.build({
        entryPoints: [path.resolve(entry)],
        bundle: true,
        format: "esm",
        platform,
        outfile: outFile,
        logLevel: "silent",
        alias: { "@": path.resolve("src") },
    });
    return import(pathToFileURL(outFile).href);
}

const gen = await bundle("src/lib/hub/gen-settings.ts", "at13-gen-settings.mjs");
const media = await bundle("src/lib/media-size.ts", "at13-media-size.mjs");
// 画布侧真入口（AT-14 B2 两侧对比用）：ModelPicker 默认路径就是它
const canvasStore = await bundle("src/stores/use-config-store.ts", "at14-config-store.mjs");

// ---------------------------------------------------------------- 夹具：三族渠道
const sdgfChannel = {
    id: "ch-sdgf",
    name: "聚鑫 SDGF",
    baseUrl: "https://sdapi.jxinai.com",
    apiKey: "sk-fake-sdgf",
    apiFormat: "sdgf",
    models: [
        { name: "sdgf-2.5-pro", capability: "video" },
        { name: "sdgf-2.0", capability: "video" },
        { name: "seedream-4", capability: "image" },
    ],
};
const wanChannel = {
    id: "ch-wan",
    name: "本地万相",
    baseUrl: "http://127.0.0.1:8779",
    apiKey: "",
    apiFormat: "wan",
    models: [{ name: "wan3.0", capability: "video" }],
};
const geminiChannel = {
    id: "ch-gemini",
    name: "Gemini 直连",
    baseUrl: "https://generativelanguage.googleapis.com",
    apiKey: "sk-fake-gemini",
    apiFormat: "gemini",
    models: [{ name: "veo-3", capability: "video" }],
};
const channels = [sdgfChannel, wanChannel, geminiChannel];

// ---------------------------------------------------------------- 模型候选夹具（AT-14 §五.2：归一配置，五族各置一模型）
// 画布侧真入口的入参形状＝AiConfig（use-config-store.ts 的类型）；这里只要函数用到的字段。
const aiConfig = {
    channelMode: "local",
    baseUrl: "",
    apiFormat: "sdgf",
    model: "sdgf-2.5-pro",
    imageModel: "seedream-4",
    videoModel: "ch-sdgf::sdgf-2.5-pro",
    textModel: "",
    audioModel: "",
    models: [],
    channels: [
        {
            id: "ch-sdgf",
            name: "聚鑫 SDGF",
            baseUrl: "https://sdapi.jxinai.com",
            apiFormat: "sdgf",
            models: [
                { name: "sdgf-2.5-pro", capability: "video" },
                { name: "sdgf-2.0", capability: "video" },
                { name: "scripted-video", capability: "video", script: "module.exports = () => ({})" },
                { name: "seedream-4", capability: "image" },
            ],
        },
        {
            id: "ch-wan",
            name: "本地万相",
            baseUrl: "http://127.0.0.1:8779",
            apiFormat: "wan",
            models: [{ name: "wan3.0", capability: "video" }],
        },
        {
            id: "ch-openai",
            name: "OpenAI 直连",
            baseUrl: "https://api.openai.com",
            apiFormat: "openai",
            models: [{ name: "sora-2", capability: "video" }],
        },
        {
            id: "ch-gemini",
            name: "Gemini 直连",
            baseUrl: "https://generativelanguage.googleapis.com",
            apiFormat: "gemini",
            models: [{ name: "veo-3", capability: "video" }],
        },
    ],
};

// ---------------------------------------------------------------- ① 枚举同源
const ratioRows = gen.genRatioRows(sdgfChannel);
check("画幅选项与 media-size.videoRatioOptions 同源同序（面板不另立白名单）",
    JSON.stringify(ratioRows.map((row) => row.value)) === JSON.stringify(media.videoRatioOptions.map((item) => item.value)),
    JSON.stringify(ratioRows.map((row) => row.value)));
check("画幅含 21:9 / auto（画布显示项照列）",
    ratioRows.some((row) => row.value === "21:9") && ratioRows.some((row) => row.value === "auto"));
check("21:9 在 SDGF 面标记渠道不接（照列 + 显式提示，不静默删）",
    ratioRows.find((row) => row.value === "21:9")?.accepted === false &&
    ratioRows.find((row) => row.value === "21:9")?.note.includes("画布专用"));
check("auto 在 SDGF 面是接受的（桥侧归一 adaptive）",
    ratioRows.find((row) => row.value === "auto")?.accepted === true);
check("wan 面只接 16:9/9:16/1:1，4:3 与 21:9 都提示渠道不接",
    JSON.stringify(gen.genRatioRows(wanChannel).filter((row) => row.accepted).map((row) => row.value).sort()) ===
        JSON.stringify(["16:9", "9:16", "1:1"].sort()),
    JSON.stringify(gen.genRatioRows(wanChannel).filter((row) => row.accepted).map((row) => row.value)));
check("清晰度枚举＝通道接收域（480/720/1080），与 media-size 解析同源",
    JSON.stringify(gen.GEN_VIDEO_RESOLUTIONS) === JSON.stringify(["480", "720", "1080"]) &&
    gen.GEN_VIDEO_RESOLUTIONS.every((value) => media.parseVideoResolution(value) === value));
check("时长上下限＝media-size.VIDEO_SECONDS_MIN/MAX",
    gen.GEN_DURATION_MIN === media.VIDEO_SECONDS_MIN && gen.GEN_DURATION_MAX === media.VIDEO_SECONDS_MAX);
check("自定义 W×H 一律接受（规约到枚举后渠道能接）",
    gen.genSizeAccepted("1920x1080", sdgfChannel) && gen.genSizeAccepted("1080x1920", wanChannel));
check("WxH 提交值＝画布 inferVideoRatio 规约结果（不再转像素串）",
    gen.genSizeForSubmit("1920x1080") === "16:9" && gen.genSizeForSubmit("240x427") === "9:16",
    gen.genSizeForSubmit("1920x1080"));
check("枚举比例提交原值照传（不再经 computeVideoSize 算像素）",
    gen.genSizeForSubmit("16:9") === "16:9" && gen.genSizeForSubmit("9:16") === "9:16");
check("提交体永不出现像素串（「9:16+480p→480x854」那条自造转换已删）",
    ["16:9", "9:16", "1:1", "auto", "1920x1080", "240x427"].every((value) => !/^\d+x\d+$/.test(gen.genSizeForSubmit(value))),
    ["16:9", "1920x1080", "240x427"].map((value) => `${value}→${gen.genSizeForSubmit(value)}`).join(" "));
check("auto 提交为 auto（桥归一到 adaptive）", gen.genSizeForSubmit("auto") === "auto");
check("零维输入显式拒绝（不静默换值，原样上送让渠道拒）",
    gen.genResolveSize("0x0", sdgfChannel).valid === false && gen.genSizeForSubmit("0x0") === "0x0");
check("W×H 规约提示「将按 9:16 生成」",
    gen.genResolveSize("240x427", sdgfChannel).note === "将按 9:16 生成",
    gen.genResolveSize("240x427", sdgfChannel).note);

// ---------------------------------------------------------------- ② 协议矩阵 / ③ wan 如实
check("协议族判定：sdgf/wan 承接，gemini/openai/未知=画布专用",
    gen.genFamily("sdgf") === "sdgf" && gen.genFamily("WAN") === "wan" &&
    gen.genFamily("gemini") === "canvas-only" && gen.genFamily("openai") === "canvas-only" && gen.genFamily("") === "canvas-only");
const channelRows = gen.genChannelRows(channels);
check("渠道列表：SDGF/wan 可选，gemini 禁选",
    channelRows.find((row) => row.id === "ch-sdgf").selectable === true &&
    channelRows.find((row) => row.id === "ch-wan").selectable === true &&
    channelRows.find((row) => row.id === "ch-gemini").selectable === false);
check("禁选渠道标「画布专用」（不是「选中必错路由」项）",
    channelRows.find((row) => row.id === "ch-gemini").note.includes(gen.GEN_CANVAS_ONLY_TAG));
const modelRows = gen.genModelRows(sdgfChannel, aiConfig);
check("模型列表只列视频能力模型（图像模型不进视频选择）",
    modelRows.every((row) => !row.name.startsWith("seedream")) && modelRows.length > 0);
check("模型脚本族禁选 + 标「画布专用（模型脚本）」",
    modelRows.find((row) => row.name === "scripted-video")?.selectable === false &&
    String(modelRows.find((row) => row.name === "scripted-video")?.note || "").includes(gen.GEN_CANVAS_ONLY_TAG));
check("wan 渠道不产模型选项（固定模型，不摆无效控件）", gen.genModelRows(wanChannel, aiConfig).length === 0);
const wanUi = gen.genUiFlags(wanChannel);
check("wan 如实 UI：model 下拉与 mode 切换都不渲染",
    wanUi.showModel === false && wanUi.showMode === false);
check("wan 固定文案＝「固定 wan3.0 · 自动多图参考」",
    wanUi.fixedNote === "固定 wan3.0 · 自动多图参考" && gen.GEN_WAN_FIXED_NOTE === wanUi.fixedNote);
const sdgfUi = gen.genUiFlags(sdgfChannel);
check("SDGF 族：model 与 mode 控件都在",
    sdgfUi.showModel === true && sdgfUi.showMode === true && sdgfUi.fixedNote === "");

// ------------------------------------------------ ②b 模型候选同源（AT-14 §3.2 档位 a / §五.2）+ 防恒真
// 画布侧真入口：ModelPicker **没有 options 覆盖**时的默认路径就是 selectableModelsByCapability(config, capability)
// （model-picker.tsx:37）。这里直接调该真函数取候选，与 hub 模型区行逐行对比。
const canvasValues = canvasStore.selectableModelsByCapability(aiConfig, "video");

/** 两侧逐行对比（行序 + 集合）；返回差异清单，空数组＝一致。 */
function modelParityFails(hubRows, canvasRowValues) {
    const fails = [];
    const hubValues = hubRows.map((row) => row.value);
    if (hubValues.length !== canvasRowValues.length) fails.push(`行数 ${hubValues.length} ≠ 画布 ${canvasRowValues.length}`);
    const shared = Math.min(hubValues.length, canvasRowValues.length);
    for (let index = 0; index < shared; index += 1) {
        if (hubValues[index] !== canvasRowValues[index]) fails.push(`第 ${index + 1} 行 ${hubValues[index]} ≠ 画布 ${canvasRowValues[index]}`);
    }
    const canvasSet = new Set(canvasRowValues);
    for (const value of hubValues) if (!canvasSet.has(value)) fails.push(`hub 多出画布没有的行 ${value}`);
    for (const value of canvasRowValues) if (!hubValues.includes(value)) fails.push(`hub 少了画布有的行 ${value}`);
    return fails;
}

const parityFails = modelParityFails(modelRows, canvasValues);
check("候选全集＝画布视频模型全集（行序逐行一致，不缺不添）", parityFails.length === 0, parityFails.join("；"));
check("画布候选夹具齐全（sdgf×3 含脚本 / wan / openai / gemini，图像模型不进）",
    canvasValues.length === 6 && !canvasValues.some((value) => value.includes("seedream")), canvasValues.join(","));

// 负例（L2 审计 B-07 闭合项）：把候选源换成 B3 旧行为（只当前渠道 + 自造 capability 字面过滤）——
// 同一断言必须红；红的过程贴出来，证明这不是「自证恒真」的断言。
const legacyGenModelRows = (channel) =>
    (channel.models || [])
        .filter((model) => String(model.capability || "") === "video")
        .map((model) => ({ value: `${channel.id}::${model.name}`, name: model.name }));
const legacyFails = modelParityFails(legacyGenModelRows(sdgfChannel), canvasValues);
check("负例：旧 genModelRows 行为（仅当前渠道 + 自造过滤）必须红", legacyFails.length > 0, legacyFails.join("；"));
const reversedFails = modelParityFails([...modelRows].reverse(), canvasValues);
check("负例：行序被打乱必须红", reversedFails.length > 0, reversedFails.join("；"));
const hiddenFails = modelParityFails(modelRows.filter((row) => row.channelId === "ch-sdgf"), canvasValues);
check("负例：只留当前渠道的行（画布能用 hub 不见）必须红", hiddenFails.length > 0, hiddenFails.join("；"));
check("负例：候选为空（模型区空白）必须红", modelParityFails([], canvasValues).length > 0);
console.log(`  [负例红样本 旧行为] ${legacyFails.join(" / ")}`);
console.log(`  [负例红样本 行序反转] ${reversedFails.slice(0, 3).join(" / ")}`);

// disabled 差异＝执行范围层三态（画布侧无禁用；hub 侧只禁用「协议不承接」与「模型脚本」）
const rowOf = (value) => modelRows.find((row) => row.value === value) || {};
check("① SDGF 族＝hub 后端已承接 → 可选（无标注）",
    rowOf("ch-sdgf::sdgf-2.5-pro").selectable === true && rowOf("ch-sdgf::sdgf-2.0").selectable === true &&
    rowOf("ch-sdgf::sdgf-2.5-pro").note === "");
check("① wan 族＝hub 后端已承接 → 可选（渠道可切；模型区仍走固定文案）",
    rowOf("ch-wan::wan3.0").selectable === true && rowOf("ch-wan::wan3.0").note === "");
check("② openai/gemini 族 → 照列 + 禁用 + 「画布可用·工作台暂不承接」",
    ["ch-openai::sora-2", "ch-gemini::veo-3"].every((value) =>
        rowOf(value) && rowOf(value).selectable === false && rowOf(value).note === gen.GEN_HUB_UNSUPPORTED_TAG));
check("③ 带模型脚本 → 照列 + 禁用 + 「画布专用（模型脚本）」（脚本优先于协议层标注）",
    rowOf("ch-sdgf::scripted-video").selectable === false &&
    rowOf("ch-sdgf::scripted-video").note === `${gen.GEN_CANVAS_ONLY_TAG}（模型脚本）`);
check("hub 禁用集合＝执行范围层那几行（不是画布差异）",
    JSON.stringify(modelRows.filter((row) => !row.selectable).map((row) => row.value)) ===
        JSON.stringify(["ch-sdgf::scripted-video", "ch-openai::sora-2", "ch-gemini::veo-3"]),
    modelRows.filter((row) => !row.selectable).map((row) => row.value).join(","));

// 选模型＝渠道身份同步（模型值自带渠道 id：否则会拿 A 渠道身份派 B 渠道模型）
check("选别家渠道的模型 → 渠道身份同步（wan3.0 → ch-wan）",
    gen.genModelPick("ch-wan::wan3.0").channelId === "ch-wan" && gen.genModelPick("ch-wan::wan3.0").model === "wan3.0");
check("裸模型名（无渠道编码）→ 不动渠道",
    gen.genModelPick("sdgf-2.0").channelId === "" && gen.genModelPick("sdgf-2.0").model === "sdgf-2.0");
check("切渠道默认模型只取该渠道内候选（不跨渠道抓模型）",
    JSON.stringify(gen.genChannelModelRows(aiConfig, "ch-wan").map((row) => row.value)) === JSON.stringify(["ch-wan::wan3.0"]) &&
    gen.genChannelModelRows(aiConfig, "ch-sdgf").every((row) => row.channelId === "ch-sdgf"));
check("空配置 → 候选空列表（不臆造模型）", gen.genModelCandidates(null).length === 0);

// ---------------------------------------------------------------- ④ approve 带 config 无 key / submit 带 key
const sdgfDraft = {
    channelId: "ch-sdgf", model: "sdgf-2.5-pro", mode: "reference",
    size: "9:16", rawSize: "", vquality: "720", duration: 8, audio: true,
};
const approveConfig = gen.buildApproveConfig(sdgfDraft, sdgfChannel);
check("approve config 四键齐（渠道身份/model/mode/params）",
    Object.keys(approveConfig).sort().join(",") === ["model", "mode", "params", "渠道身份"].sort().join(","),
    Object.keys(approveConfig).join(","));
check("渠道身份只带非敏感身份（baseUrl/apiFormat/渠道名）",
    JSON.stringify(Object.keys(approveConfig.渠道身份).sort()) === JSON.stringify(["apiFormat", "baseUrl", "渠道名"]));
check("params 五键＝backend CONFIG_PARAM_KEYS（duration/size/rawSize/vquality/audio）",
    JSON.stringify(Object.keys(approveConfig.params).sort()) === JSON.stringify(["audio", "duration", "rawSize", "size", "vquality"]));
check("params.size 是枚举（不是像素串）", approveConfig.params.size === "9:16");
check("自定义 W×H：params.size=规约结果 + params.rawSize=原始输入",
    (() => {
        const cfg = gen.buildApproveConfig({ ...sdgfDraft, size: "9:16", rawSize: "240x427" }, sdgfChannel);
        return cfg.params.size === "9:16" && cfg.params.rawSize === "240x427";
    })());
check("params.duration 是整数秒（不是字符串）", typeof approveConfig.params.duration === "number" && approveConfig.params.duration === 8);
check("params.audio 是布尔", approveConfig.params.audio === true);
check("approve config 全程无 key（任何层级都不出现 key 值/键名）", (() => {
    const text = JSON.stringify(approveConfig);
    return !text.includes("sk-fake-sdgf") && !/"(api_?key|key|secret|token|authorization)"/i.test(text);
})(), JSON.stringify(approveConfig));
check("mode 真透传（reference/frames）", approveConfig.mode === "reference" &&
    gen.buildApproveConfig({ ...sdgfDraft, mode: "frames" }, sdgfChannel).mode === "frames");
const wanDraft = { ...sdgfDraft, channelId: "ch-wan", model: "wan3.0", mode: "frames" };
const wanConfig = gen.buildApproveConfig(wanDraft, wanChannel);
check("wan config：model/mode 落空串（固定语义，不进快照自由度）",
    wanConfig.model === "" && wanConfig.mode === "" && wanConfig.渠道身份.apiFormat === "wan");
check("wan config 仍带渠道名与画幅（同名模型不同渠道＝不同快照）",
    wanConfig.渠道身份.渠道名 === "本地万相" && wanConfig.params.size === gen.genSizeForSubmit("9:16"));
check("submit 请求体＝{key}（一次性透传）",
    JSON.stringify(gen.buildSubmitBody("sk-fake-sdgf")) === JSON.stringify({ key: "sk-fake-sdgf" }));
check("wan 允许空 key（CLI 本地承载）", gen.buildSubmitBody("").key === "" && gen.buildSubmitBody(null).key === "");

// ---------------------------------------------------------------- ⑤ 草稿（按任务）+ 失效引用 + 签名
const memoryStorage = () => {
    const map = new Map();
    return {
        getItem: (key) => (map.has(key) ? map.get(key) : null),
        setItem: (key, value) => map.set(key, String(value)),
        removeItem: (key) => map.delete(key),
        dump: () => Object.fromEntries(map),
    };
};
const store = memoryStorage();
gen.saveGenDraft("auto-0012", sdgfDraft, store);
check("草稿按任务号存（一任务一草稿，键含任务号）",
    Object.keys(store.dump()).includes(gen.genDraftKey("auto-0012")) && gen.genDraftKey("auto-0012").endsWith("auto-0012"));
const readBack = gen.readGenDraft("auto-0012", store);
check("草稿读回保真（渠道/模型/模式/画幅/清晰度/时长/声音）",
    readBack.channelId === "ch-sdgf" && readBack.model === "sdgf-2.5-pro" && readBack.mode === "reference" &&
    readBack.size === "9:16" && readBack.vquality === "720" && readBack.duration === 8 && readBack.audio === true);
check("另一任务读不到别人的草稿（不串任务）", gen.readGenDraft("auto-0001", store) === null);
check("畸形草稿归一不炸（缺键/越界兜底）", (() => {
    const normalized = gen.normalizeGenDraft({ channelId: 1, duration: 999, mode: "weird" });
    return normalized.channelId === "1" && normalized.duration === gen.GEN_DURATION_MAX && normalized.mode === "" && normalized.audio === true;
})());
check("无存储环境（node/隐私模式）读草稿返回 null 不抛", gen.readGenDraft("auto-0012", null) === null);
check("失效引用：草稿渠道被删 → 显式提示且点名重新选",
    gen.genDraftStaleNote({ ...sdgfDraft, channelId: "ch-deleted" }, channels).includes("重新选"));
check("未选渠道 → 提示去画布配渠道", gen.genDraftStaleNote({ ...sdgfDraft, channelId: "" }, channels).includes("渠道"));
check("引用有效 → 无提示", gen.genDraftStaleNote(sdgfDraft, channels) === "");
const sigA = gen.genDraftSignature(sdgfDraft, sdgfChannel);
check("签名不含 key（只换 key 不失效）", !sigA.includes("sk-fake-sdgf") && sigA.includes("sdgf-2.5-pro"));
check("只换 key → 签名不变", gen.genDraftSignature(sdgfDraft, { ...sdgfChannel, apiKey: "sk-rotated" }) === sigA);
check("同名模型换渠道 → 签名变（快照失效）",
    gen.genDraftSignature({ ...sdgfDraft, channelId: "ch-other" }, { ...sdgfChannel, id: "ch-other", name: "另一个同名渠道" }) !== sigA);
check("改 params（时长）→ 签名变", gen.genDraftSignature({ ...sdgfDraft, duration: 9 }, sdgfChannel) !== sigA);
const approvedStore = memoryStorage();
gen.saveApprovedSignature("auto-0012", sigA, approvedStore);
check("批准签名已记 → 草稿未改时不需要重批",
    gen.genNeedsReapproval("auto-0012", sdgfDraft, sdgfChannel, approvedStore) === false);
check("批准后改画幅 → 需要重新批准",
    gen.genNeedsReapproval("auto-0012", { ...sdgfDraft, size: "16:9" }, sdgfChannel, approvedStore) === true);
check("没有本地批准基线（别处批准）→ 不臆造「需要重批」",
    gen.genNeedsReapproval("auto-0002", sdgfDraft, sdgfChannel, memoryStorage()) === false);
check("默认草稿从画布全局默认派生且不写全局（size 比例化 / 时长夹紧）", (() => {
    const draft = gen.defaultGenDraft(sdgfChannel, { videoSeconds: "999", vquality: "1080p", size: "1920x1080", videoMode: "reference" }, aiConfig);
    return draft.duration === gen.GEN_DURATION_MAX && draft.vquality === "1080" && draft.size === "16:9" && draft.mode === "reference" && draft.model === "sdgf-2.5-pro";
})());
check("默认渠道避开画布专用渠道（不预选 gemini/openai）",
    gen.genDefaultChannelId(channels, "ch-gemini") === "ch-sdgf" && gen.genDefaultChannelId([geminiChannel, wanChannel], "") === "ch-wan");

// ---------------------------------------------------------------- ⑥ 提交前校验（显式提示，不静默改值）
check("SDGF 缺模型 → 批准前拦住",
    gen.genApproveIssues({ ...sdgfDraft, model: "" }, sdgfChannel).some((issue) => issue.includes("必须选模型")));
check("画布专用渠道 → 批准前拦住（hub 不承接）",
    gen.genApproveIssues({ ...sdgfDraft, channelId: "ch-gemini" }, geminiChannel).some((issue) => issue.includes("画布专用")));
check("渠道不接的画幅 → 提示具体值不行（不静默换成别的）",
    gen.genApproveIssues({ ...sdgfDraft, size: "21:9" }, sdgfChannel).some((issue) => issue.includes("21:9")));
check("时长越界 → 提示区间", gen.genApproveIssues({ ...sdgfDraft, duration: 2 }, sdgfChannel).some((issue) => issue.includes("4–30")));
check("SDGF 缺 key → 提交前拦（approve 不拦，key 不进批准）",
    gen.genApproveIssues(sdgfDraft, { ...sdgfChannel, apiKey: "" }).length === 0 &&
    gen.genSubmitIssues(sdgfDraft, { ...sdgfChannel, apiKey: "" }, "").some((issue) => issue.includes("API Key")));
check("wan 空 key 可提交（本地承载不需要 key）",
    gen.genSubmitIssues(wanConfig ? { ...wanDraft } : wanDraft, wanChannel, "").length === 0);

// ---------------------------------------------------------------- ⑦ 提交链阻断态（§2.3/§2.4）
const none = gen.genIssue({ state: "awaiting_approval" });
check("普通待批准 → 无阻断、不禁重提", none.kind === "none" && none.blocksRetry === false);
const active = gen.genIssue({ state: "generating", active_submit: true });
check("活跃提交占用 → 显示「提交进行中」且禁重试",
    active.kind === "active" && active.title === "提交进行中" && active.blocksRetry === true && active.canResume === false);
const blocked = gen.genIssue({ state: "approved", submit_unknown: { reason: "超时未知", dispatch: "dispatched" } });
check("submit_unknown → 阻断态 + 三步指引（核对/按 taskId 续查/解除阻断后重提）",
    blocked.kind === "blocked" && blocked.steps.length === 3 && blocked.steps[0].includes("控制台") &&
    blocked.steps[2].includes("解除阻断") && blocked.blocksRetry === true);
check("阻断态无 cloud id → 不给续查按钮（不臆造找回路径）", blocked.cloudTaskId === "" && blocked.canResume === false);
const blockedWithId = gen.genIssue({ state: "generating", submit_unknown: { reason: "断连" }, cloud_task_id: "task-abc123" });
check("阻断态有 cloud id → 给「按 taskId 续查」入口（不是重提按钮）",
    blockedWithId.canResume === true && blockedWithId.steps.join("").includes("task-abc123"));
const dispatchOnly = gen.genIssue({ state: "approved", dispatch: { phase: "dispatched" } });
check("dispatch=dispatched 无回执 → 同样按未知收单阻断",
    dispatchOnly.kind === "blocked" && dispatchOnly.title.includes("dispatched"));
check("dispatch=confirmed → 不算阻断（有确定回执）",
    gen.genIssue({ state: "generating", dispatch: { phase: "confirmed" }, cloud_task_id: "t-1" }).kind === "none");
const orphan1 = gen.genIssue({ state: "generating", submit_lost: { step: 1, reason: "提交进行中", submit_lost: false } });
check("孤儿三步①→ 在途占用文案", orphan1.kind === "active" && orphan1.blocksRetry === true);
const orphan2 = gen.genIssue({ state: "generating", cloud_task_id: "task-xyz", submit_lost: { step: 2, reason: "有云端 taskId" } });
check("孤儿三步②→ 续查该 id、禁重提", orphan2.kind === "orphan" && orphan2.canResume === true && orphan2.blocksRetry === true);
const orphan3 = gen.genIssue({ state: "generating", submit_lost: { step: 3, submit_lost: true, reason: "无在途证据" } });
check("孤儿三步③→ 核对无单后受控恢复，禁直接重提",
    orphan3.kind === "orphan" && orphan3.canResume === false && orphan3.steps.join("").includes("受控恢复"));
check("旧 bridge 缺 submit_lost → 不误判孤儿", gen.genIssue({ state: "generating", submit_lost: null }).kind === "none");
check("wan 失败分层文案：超时未知 ≠ 失败（不靠错误文案猜分类）",
    gen.genFailureHint(wanChannel, "generating").includes("超时") &&
    gen.genFailureHint(wanChannel, "generating").includes("≠ 失败") &&
    gen.genFailureHint(sdgfChannel, "failed").includes("明确失败"));
check("面板摘要含渠道/模型/画幅/时长（审批与生成中回显）", (() => {
    const summary = gen.genDraftSummary(sdgfDraft, sdgfChannel);
    return summary.includes("聚鑫 SDGF") && summary.includes("sdgf-2.5-pro") && summary.includes("8s") && summary.includes("720p");
})());
check("wan 摘要走固定文案（不显示假模型名）",
    gen.genDraftSummary(wanDraft, wanChannel).includes("固定 wan3.0 · 自动多图参考"));

// ---------------------------------------------------------------- ⑨ 画幅规约（AT-14 §3.1/§五.1，与 PY 共享同一份夹具）
const fixturePath = path.resolve("../wanrepro/tests/fixtures/sdgf-dialect/cases.json");
const sizeBlock = JSON.parse(readFileSync(fixturePath, "utf8")).sizeCases || {};
const sizeCases = sizeBlock.cases || [];
const channelOfFormat = (apiFormat) => (apiFormat === "wan" ? wanChannel : sdgfChannel);
check("画幅夹具 sizeCases 已接入（与 PY 探针同一份 cases.json）", sizeCases.length >= 10, `${sizeCases.length} 例`);
check("夹具含 §五.1 十个规定输入",
    ["240x427", "480x854", "240X427", "240×427", "9:16", "21:9", "auto", "0x0", "abc", "2000x100"]
        .every((value) => sizeCases.some((item) => item.input.size === value)));

for (const item of sizeCases) {
    const rawSize = item.input.size;
    const channel = channelOfFormat(item.input.apiFormat);
    const want = item.expect;
    const resolved = gen.genResolveSize(rawSize, channel);
    const submit = gen.genSizeForSubmit(rawSize);
    const expectedSize = want.ratio ?? "";
    const expectedSubmit = want.ratio ?? rawSize;          // 无效输入原样上送（渠道显式拒）

    check(`${item.id} · genResolveSize 规约结果`, resolved.size === expectedSize, `${resolved.size} ≠ ${expectedSize}`);
    check(`${item.id} · genSizeForSubmit 提交值（无像素串）`, submit === expectedSubmit, `${submit} ≠ ${expectedSubmit}`);
    check(`${item.id} · 渠道接收标记（false=按钮置灰）`, resolved.accepted === want.accepted, `accepted=${resolved.accepted} ≠ ${want.accepted}`);
    if (want.note) check(`${item.id} · 面板提示文案`, resolved.note.includes(want.note), resolved.note);

    // approve 请求体走真函数：size=规约值、rawSize=原始输入
    const draft = {
        channelId: channel.id,
        model: channel.id === "ch-wan" ? "" : "sdgf-2.5-pro",
        mode: channel.id === "ch-wan" ? "" : "frames",
        size: resolved.valid ? resolved.size : rawSize,
        rawSize,
        vquality: "720",
        duration: 8,
        audio: true,
    };
    const cfg = gen.buildApproveConfig(draft, channel);
    const issues = gen.genApproveIssues(draft, channel);
    check(`${item.id} · approve params.size＝规约结果`, cfg.params.size === expectedSubmit, `${cfg.params.size} ≠ ${expectedSubmit}`);
    check(`${item.id} · approve params.rawSize 在场（原始输入留痕）`, cfg.params.rawSize === rawSize, cfg.params.rawSize);
    check(`${item.id} · 提交前置校验与接收标记一致（不静默放行）`,
        want.accepted ? issues.length === 0 : issues.length > 0,
        issues.join("；"));
    // 枚举输入不再触发「非枚举画幅」拒绝（D1 缺陷在该输入集内不可复现）
    if (want.accepted) {
        check(`${item.id} · 枚举输入不触发非枚举画幅报错`,
            !issues.some((issue) => issue.includes("不在渠道接收域")), issues.join("；"));
    }
}

// 21:9 = 画布专用 → 渠道不接：置灰（不静默换值、不静默删项）
const ratio21 = gen.genResolveSize("21:9", sdgfChannel);
check("21:9 在 SDGF 面 accepted=false（按钮置灰）+ 文案含「画布专用」",
    ratio21.accepted === false && ratio21.note.includes("画布专用"), ratio21.note);
check("21:9 提交值仍是 21:9（不静默换成别的比例）", gen.genSizeForSubmit("21:9") === "21:9");

// ---------------------------------------------------------------- ⑧ JSX / API 接线护栏
const drawer = readFileSync(path.resolve("src/pages/hub/task-detail-drawer.tsx"), "utf8");
const api = readFileSync(path.resolve("src/pages/hub/hub-api.ts"), "utf8");
const picker = readFileSync(path.resolve("src/components/model-picker.tsx"), "utf8");
const genSrc = readFileSync(path.resolve("src/lib/hub/gen-settings.ts"), "utf8");

check("drawer 接 ModelPicker（capability=video，非 model-select-modal）",
    drawer.includes("<ModelPicker") && drawer.includes('capability="video"') && !drawer.includes("model-select-modal"));
check("drawer 模型区选项＝同源候选行（modelRows 由 genModelRows(channel, config) 派生，逐行渲染）",
    drawer.includes("const modelRows = genModelRows(channel, config);") &&
    drawer.includes("options={modelRows.map((row) => row.value)}") &&
    drawer.includes("optionDisabled={(value) => !(modelRows.find((row) => row.value === value)?.selectable ?? true)}") &&
    drawer.includes("optionSuffix={(value) => modelRows.find((row) => row.value === value)?.note || \"\"}"));
check("drawer 选模型走 genModelPick（渠道身份与模型绑定，防跨渠道错配）",
    drawer.includes("const picked = genModelPick(value);") && drawer.includes("channelId: picked.channelId,"));
check("drawer 模型区带 DOM 钩子（候选行数 / 禁用行数，B5 真机逐行读）",
    drawer.includes("data-hub-gen-model-count={modelRows.length}") && drawer.includes("data-hub-gen-model-blocked="));
check("gen-settings 直调画布共享函数（禁复制实现）",
    genSrc.includes("selectableModelsByCapability(config, \"video\")") &&
    genSrc.includes("} from \"@/stores/use-config-store\";"));
check("gen-settings 不再自造 capability 字面过滤（旧行为已删）",
    !genSrc.includes('String(model.capability || "") === "video"') && !genSrc.includes("model.hasScript"));
check("ModelPicker 支持显式 options / 禁选 / 后缀（复用改造，不改画布默认行为）",
    picker.includes("optionsOverride ?? selectableModelsByCapability(config, capability)") &&
    picker.includes("disabled={optionDisabled?.(model) ?? false}"));
check("wan 如实 UI：模型控件与 mode 控件都走 ui.showModel / ui.showMode 条件（不渲染）",
    drawer.includes("{ui.showModel ? (") && drawer.includes("{ui.showMode ? (") && drawer.includes("data-hub-gen-fixed"));
check("渠道下拉禁选画布专用渠道并标 Tag",
    drawer.includes('disabled: genFamily(row.apiFormat) === "canvas-only"') && drawer.includes("{GEN_CANVAS_ONLY_TAG}"));
check("画幅/清晰度/时长/声音控件带 data-hub-gen-* 钩子（B5 DOM 断言面）",
    ["data-hub-gen-ratio=", "data-hub-gen-resolution=", "data-hub-gen-duration", "data-hub-gen-audio", "data-hub-gen-mode="]
        .every((token) => drawer.includes(token)));
check("approve 走 buildApproveConfig（带 config），不再裸 approveHubTask(localId)",
    drawer.includes("approveHubTask(localId, buildApproveConfig(draft, channel))") && !/approveHubTask\(localId\)/.test(drawer));
check("submit 走 submitHubTask(localId, apiKey)（key 一次性透传）",
    drawer.includes("submitHubTask(localId, apiKey)") && !/submitHubTask\(localId\)/.test(drawer));
check("hub-api：approve 带 body.config、submit 带 body.key",
    api.includes("JSON.stringify({ config })") && api.includes("JSON.stringify({ key: String(key || \"\").trim() })"));
check("hub-api 透出提交链字段（gen_error/submit_unknown/dispatch/cloud_task_id/active_submit/submit_lost）",
    ["gen_error:", "submit_unknown: normalizeSubmitUnknown", "dispatch: normalizeDispatch",
     "cloud_task_id: hubText", "active_submit: Boolean", "submit_lost: normalizeSubmitLost"]
        .every((token) => api.includes(token)));
check("失败红条接线（gen_error 原文 + 脱敏说明）", drawer.includes("data-hub-gen-error") && drawer.includes("genFailureHint("));
check("阻断/在途/孤儿面板接线（kind 三态 + 云端 taskId 回显 + 续查入口）",
    drawer.includes("data-hub-gen-issue={issue.kind}") && drawer.includes("data-hub-gen-cloud-id") && drawer.includes("data-hub-gen-resume"));
check("无直接重提按钮：阻断态禁用「提交生成」与「重试」",
    drawer.includes("blocked.blocksRetry") && drawer.includes("issue.blocksRetry") && drawer.includes("<RetryButton localId={localId} busy={busy} act={act} disabled />"));
check("批准后改配置 → 撤销批准重批（不自动重批）",
    drawer.includes("revokeHubTask(localId)") && drawer.includes("data-hub-gen-reapproval") && drawer.includes("clearApprovedSignature(localId)"));
check("草稿按任务读写（切任务重读；轮询不重置输入）",
    drawer.includes("readGenDraft(localId)") && drawer.includes("saveGenDraft(localId, next)") && drawer.includes("initRef.current === localId"));
check("不随任务改写全局 videoModel（无 updateConfig(\"videoModel\") / 无写入 store）",
    !drawer.includes('updateConfig("videoModel"') && !drawer.includes("setConfig(") && drawer.includes("useConfigStore((state) => state.config)"));
check("生成中不再由 OutcomeSection 出进度条（统一走生成设置段，避免双份 UI）",
    drawer.includes('state === "awaiting_approval" || state === "approved" || state === "generating"') &&
    drawer.includes("<GenSettingsSection detail={detail} busy={busy} act={act} />"));
// AT-14 B1 画幅根治接线：自定义 W×H 就地规约 + 渠道不接置灰（数据层断言见上，这里锁 JSX 真把结果画出来）
check("drawer 画幅段调 genResolveSize（画布 inferVideoRatio 同口径，禁自造换算）",
    drawer.includes("genResolveSize(draft.size, channel)") && drawer.includes("const applyPixels ="));
check("自定义 W×H 输入即规约并落 rawSize（草稿存规约结果 + 原始输入）",
    drawer.includes("const resolved = genResolveSize(raw, channel);") &&
    drawer.includes("update({ size: resolved.valid ? resolved.size : raw, rawSize: raw })") &&
    drawer.includes("parsePixelSizeOf(draft.rawSize || draft.size)"));
check("画幅提示显示规约结果（data-hub-gen-size-resolved + 「将按 X 生成」）",
    drawer.includes("data-hub-gen-size-resolved") && drawer.includes("将按 {sizeInfo.size} 生成"));
check("渠道不接的枚举标「渠道不生效」（照列不静默删）", drawer.includes("渠道不生效"));
check("批准/提交按钮按画幅置灰（data-hub-gen-approve-size-blocked / submit 同款）",
    drawer.includes("sizeBlocked") && drawer.includes("data-hub-gen-approve-size-blocked") &&
    drawer.includes("data-hub-gen-submit-size-blocked") &&
    drawer.includes("blocked.blocksRetry || sizeBlocked"));
check("drawer 不再用 computeVideoSize 转像素串（自造转换已删）",
    !drawer.includes("computeVideoSize") && !drawer.includes("genSizeForSubmit(draft.size, draft.vquality)"));

// ---------------------------------------------------------------- 结果
console.log(`probe-at13-b3: ${pass} PASS, ${fails.length} FAIL`);
for (const item of fails) console.log(`  FAIL ${item}`);
process.exit(fails.length ? 1 : 0);
