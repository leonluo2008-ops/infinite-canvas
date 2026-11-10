/**
 * AT-13 B3 前端自检（免费探针）：esbuild 打包 web/src/lib/hub/gen-settings.ts + media-size.ts 后
 * 直接断言真代码 + 源码接线护栏（task-detail-drawer.tsx / hub-api.ts / model-picker.tsx）。
 *
 * 覆盖（对应执行卡质量门 1~7）：
 *   ① 枚举同源：画幅/清晰度/时长选项＝media-size.ts 派生（含 21:9 / 自适应 / WxH）
 *   ② 协议矩阵：SDGF+wan 可选；gemini/openai/模型脚本标「画布专用」禁选
 *   ③ wan 如实 UI：固定 wan3.0·自动多图参考，model/mode 控件不渲染
 *   ④ approve 请求体带 config 且**无 key**；submit 请求体带 key（一次性）
 *   ⑤ 草稿按「渠道+模型」存任务草稿；失效引用显式提示；key 不进签名（只换 key 不失效）
 *   ⑥ 提交链阻断：active_submit / submit_unknown / dispatch / 孤儿三步 → 引导文案 + 禁重提
 *   ⑦ JSX/API 接线护栏（红条、阻断入口、续查、撤销批准、无直接重提按钮）
 *
 * 跑法：cd web && node scripts/probe-at13-b3.mjs   （失败 rc=1）
 * 注：离线无 DOM runtime，真机 DOM 可见性/点选由 B5 收口；本探针管逻辑真值 + JSX 接线。
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

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

async function bundle(entry, outName) {
    const outFile = path.join(outDir, outName);
    await esbuild.build({
        entryPoints: [path.resolve(entry)],
        bundle: true,
        format: "esm",
        platform: "neutral",
        outfile: outFile,
        logLevel: "silent",
        alias: { "@": path.resolve("src") },
    });
    return import(pathToFileURL(outFile).href);
}

const gen = await bundle("src/lib/hub/gen-settings.ts", "at13-gen-settings.mjs");
const media = await bundle("src/lib/media-size.ts", "at13-media-size.mjs");

// ---------------------------------------------------------------- 夹具：三族渠道
const sdgfChannel = {
    id: "ch-sdgf",
    name: "聚鑫 SDGF",
    baseUrl: "https://sdapi.jxinai.com",
    apiKey: "sk-fake-sdgf",
    apiFormat: "sdgf",
    models: [
        { name: "sdgf-2.5-pro", capability: "video", hasScript: false },
        { name: "sdgf-2.0", capability: "video", hasScript: false },
        { name: "scripted-video", capability: "video", hasScript: true }, // 模型脚本族 → 画布专用
        { name: "seedream-4", capability: "image", hasScript: false },
    ],
};
const wanChannel = {
    id: "ch-wan",
    name: "本地万相",
    baseUrl: "http://127.0.0.1:8779",
    apiKey: "",
    apiFormat: "wan",
    models: [{ name: "wan3.0", capability: "video", hasScript: false }],
};
const geminiChannel = {
    id: "ch-gemini",
    name: "Gemini 直连",
    baseUrl: "https://generativelanguage.googleapis.com",
    apiKey: "sk-fake-gemini",
    apiFormat: "gemini",
    models: [{ name: "veo-3", capability: "video", hasScript: false }],
};
const channels = [sdgfChannel, wanChannel, geminiChannel];

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
check("自定义 W×H 一律接受（桥按 gcd 归一成比例）",
    gen.genSizeAccepted("1920x1080", sdgfChannel) && gen.genSizeAccepted("1080x1920", wanChannel));
check("WxH 提交值原样透传（由桥归一）",
    gen.genSizeForSubmit("1920x1080", "720") === "1920x1080");
check("比例值提交时按清晰度算像素（与画布 computeVideoSize 同口径）",
    gen.genSizeForSubmit("16:9", "720") === media.computeVideoSize("720", "16:9"));
check("auto 提交为 auto（桥归一到 adaptive）", gen.genSizeForSubmit("auto", "720") === "auto");

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
const modelRows = gen.genModelRows(sdgfChannel);
check("模型列表只列视频能力模型（图像模型不进视频选择）",
    modelRows.every((row) => !row.name.startsWith("seedream")) && modelRows.length === 3);
check("模型脚本族禁选 + 标「画布专用（模型脚本）」",
    modelRows.find((row) => row.name === "scripted-video").selectable === false &&
    modelRows.find((row) => row.name === "scripted-video").note.includes(gen.GEN_CANVAS_ONLY_TAG));
check("wan 渠道不产模型选项（固定模型，不摆无效控件）", gen.genModelRows(wanChannel).length === 0);
const wanUi = gen.genUiFlags(wanChannel);
check("wan 如实 UI：model 下拉与 mode 切换都不渲染",
    wanUi.showModel === false && wanUi.showMode === false);
check("wan 固定文案＝「固定 wan3.0 · 自动多图参考」",
    wanUi.fixedNote === "固定 wan3.0 · 自动多图参考" && gen.GEN_WAN_FIXED_NOTE === wanUi.fixedNote);
const sdgfUi = gen.genUiFlags(sdgfChannel);
check("SDGF 族：model 与 mode 控件都在",
    sdgfUi.showModel === true && sdgfUi.showMode === true && sdgfUi.fixedNote === "");

// ---------------------------------------------------------------- ④ approve 带 config 无 key / submit 带 key
const sdgfDraft = {
    channelId: "ch-sdgf", model: "sdgf-2.5-pro", mode: "reference",
    size: "9:16", vquality: "720", duration: 8, audio: true,
};
const approveConfig = gen.buildApproveConfig(sdgfDraft, sdgfChannel);
check("approve config 四键齐（渠道身份/model/mode/params）",
    Object.keys(approveConfig).sort().join(",") === ["model", "mode", "params", "渠道身份"].sort().join(","),
    Object.keys(approveConfig).join(","));
check("渠道身份只带非敏感身份（baseUrl/apiFormat/渠道名）",
    JSON.stringify(Object.keys(approveConfig.渠道身份).sort()) === JSON.stringify(["apiFormat", "baseUrl", "渠道名"]));
check("params 四键＝backend CONFIG_PARAM_KEYS（duration/size/vquality/audio）",
    JSON.stringify(Object.keys(approveConfig.params).sort()) === JSON.stringify(["audio", "duration", "size", "vquality"]));
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
    wanConfig.渠道身份.渠道名 === "本地万相" && wanConfig.params.size === gen.genSizeForSubmit("9:16", "720"));
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
    const draft = gen.defaultGenDraft(sdgfChannel, { videoSeconds: "999", vquality: "1080p", size: "1920x1080", videoMode: "reference" });
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

// ---------------------------------------------------------------- ⑧ JSX / API 接线护栏
const drawer = readFileSync(path.resolve("src/pages/hub/task-detail-drawer.tsx"), "utf8");
const api = readFileSync(path.resolve("src/pages/hub/hub-api.ts"), "utf8");
const picker = readFileSync(path.resolve("src/components/model-picker.tsx"), "utf8");

check("drawer 接 ModelPicker（capability=video，非 model-select-modal）",
    drawer.includes("<ModelPicker") && drawer.includes('capability="video"') && !drawer.includes("model-select-modal"));
check("drawer 模型选项由 genModelRows 派生（渠道内视频模型 + 脚本禁选）",
    drawer.includes("options={genModelRows(channel).map((row) => row.value)}") &&
    drawer.includes("optionDisabled={") && drawer.includes("optionSuffix={"));
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

// ---------------------------------------------------------------- 结果
console.log(`probe-at13-b3: ${pass} PASS, ${fails.length} FAIL`);
for (const item of fails) console.log(`  FAIL ${item}`);
process.exit(fails.length ? 1 : 0);
