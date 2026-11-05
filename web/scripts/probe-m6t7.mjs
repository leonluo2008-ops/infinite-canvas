/**
 * M6-T7 免费探针：拉片分析模型选择（multimodal 能力标签的消费端纯逻辑）。
 *
 * 直接转译真模块（esbuild 打包 src/lib/canvas/lapian-model.ts，不走 app、不连 bridge、不花钱）：
 *   node scripts/probe-m6t7.mjs
 * 断言口径见 docs/nodes/M6-T7拉片多模态模型选择.md；任何一条不成立就退出码 1。
 * 覆盖：multimodal 过滤分组、渠道引用编码、选中值校验、/api/lapian/run 请求体装配（含零回归与半截请求防御）。
 */
import { build } from "esbuild";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const workdir = mkdtempSync(path.join(tmpdir(), "probe-m6t7-"));

const outfile = path.join(workdir, "lapian-model.mjs");
await build({
    entryPoints: [path.join(root, "src/lib/canvas/lapian-model.ts")],
    outfile,
    bundle: true,
    format: "esm",
    platform: "node",
    logLevel: "silent",
    alias: { "@": path.join(root, "src") },
});
const mod = await import(pathToFileURL(outfile).href);

let passed = 0;
const failures = [];
function check(name, condition, detail) {
    if (condition) {
        passed += 1;
        return;
    }
    failures.push(`${name}${detail ? ` —— ${detail}` : ""}`);
}
function equal(name, actual, expected) {
    check(name, JSON.stringify(actual) === JSON.stringify(expected), `实际 ${JSON.stringify(actual)} / 期望 ${JSON.stringify(expected)}`);
}

// 测试渠道：2 个渠道；ch-a 带 1 个 multimodal + 干扰项；ch-b 只有生图（应整组消失）；ch-c 带 2 个 multimodal 但 key 缺失。
const CHANNELS = [
    {
        id: "ch-a",
        name: "渠道甲",
        baseUrl: "https://api.example-a.com",
        apiKey: "sk-a",
        apiFormat: "openai",
        models: [
            { name: "img-model", capability: "image" },
            { name: "vision-model-1", capability: "multimodal" },
            { name: "text-model", capability: "text" },
        ],
    },
    {
        id: "ch-b",
        name: "渠道乙",
        baseUrl: "https://api.example-b.com",
        apiKey: "sk-b",
        apiFormat: "openai",
        models: [{ name: "img-only", capability: "image" }],
    },
    {
        id: "ch-c",
        name: "渠道丙",
        baseUrl: "https://api.example-c.com",
        apiKey: "",
        apiFormat: "openai",
        models: [
            { name: "vision-model-2", capability: "multimodal" },
            { name: "vision-model-3", capability: "multimodal" },
        ],
    },
];

// ---------------------------------------------------------------------------
// 1. multimodal 过滤分组（T7-A 消费端口径）
// ---------------------------------------------------------------------------
const groups = mod.multimodalModelGroups(CHANNELS);
equal("只出带 multimodal 模型的渠道（乙整组消失）", groups.map((g) => g.channelId), ["ch-a", "ch-c"]);
equal("渠道甲只留 multimodal 模型", groups[0].models.map((m) => m.name), ["vision-model-1"]);
equal("渠道丙保持原序且不混入其它能力", groups[1].models.map((m) => m.name), ["vision-model-2", "vision-model-3"]);
equal("引用值=channelId::model（与 use-config-store 编码同格式）", groups[0].models[0].value, "ch-a::vision-model-1");
equal("undefined 渠道表不出组", mod.multimodalModelGroups(undefined), []);
check("渠道名字进组（供下拉 optgroup）", groups.every((g) => typeof g.channelName === "string" && g.channelName.length > 0));

// ---------------------------------------------------------------------------
// 2. 渠道引用编解码
// ---------------------------------------------------------------------------
equal("解码标准引用", mod.decodeChannelModelRef("ch-a::vision-model-1"), { channelId: "ch-a", model: "vision-model-1" });
equal("模型名含 :: 时按首个分隔符拆（与 store 的 decodeChannelModel 同口径）", mod.decodeChannelModelRef("ch-a::we::ird"), { channelId: "ch-a", model: "we::ird" });
check("裸值（无分隔符）解码为 null", mod.decodeChannelModelRef("vision-model-1") === null);
equal("编码再解码 roundtrip", mod.decodeChannelModelRef(mod.encodeChannelModelRef("ch-c", " vision-model-2 ")), { channelId: "ch-c", model: "vision-model-2" });

// ---------------------------------------------------------------------------
// 3. 选中值校验（执行前拦，不发半截请求）
// ---------------------------------------------------------------------------
check("不选恒过", mod.checkLapianSelection(undefined, CHANNELS).ok && mod.checkLapianSelection("", CHANNELS).ok && mod.checkLapianSelection("  ", CHANNELS).ok);
equal("渠道还在且配置齐 → 放行", mod.checkLapianSelection("ch-a::vision-model-1", CHANNELS), { ok: true });
equal("渠道被删 → not-found", mod.checkLapianSelection("ch-gone::m", CHANNELS), { ok: false, reason: "not-found" });
equal("渠道缺 API Key → incomplete", mod.checkLapianSelection("ch-c::vision-model-2", CHANNELS), { ok: false, reason: "incomplete" });

// ---------------------------------------------------------------------------
// 4. /api/lapian/run 请求体装配（T7-B 合同）
// ---------------------------------------------------------------------------
const ID = "task-1";
const INTENT = "重点看产品镜头";
equal("不选模型：请求体与现状逐字节一致（旧链路零回归）", mod.buildLapianRunBody(ID, INTENT, undefined, CHANNELS), { id: ID, intent: INTENT });
equal("不选模型（空串）同样只 {id,intent}", mod.buildLapianRunBody(ID, INTENT, "", CHANNELS), { id: ID, intent: INTENT });
equal("选了模型：补 model + channel{baseUrl,key,model}", mod.buildLapianRunBody(ID, INTENT, "ch-a::vision-model-1", CHANNELS), {
    id: ID,
    intent: INTENT,
    model: "vision-model-1",
    channel: { baseUrl: "https://api.example-a.com", key: "sk-a", model: "vision-model-1" },
});
check("渠道被删：绝不发半截请求（不带 model/channel，交上层拦）", (() => {
    const body = mod.buildLapianRunBody(ID, INTENT, "ch-gone::m", CHANNELS);
    return !("model" in body) && !("channel" in body);
})());
check("渠道缺 Key：同样不带 model/channel", (() => {
    const body = mod.buildLapianRunBody(ID, INTENT, "ch-c::vision-model-2", CHANNELS);
    return !("model" in body) && !("channel" in body);
})());
check("请求体不含任何硬编码模型/品牌字样", JSON.stringify(mod.buildLapianRunBody(ID, INTENT, "ch-a::vision-model-1", CHANNELS)).includes("gemini") === false);

// ---------------------------------------------------------------------------
// 5. 多模态模型列表口径（selectableModelsByCapability 同构行为的独立实现自检）
// ---------------------------------------------------------------------------
const allMultimodal = mod.multimodalModelGroups(CHANNELS).flatMap((g) => g.models.map((m) => m.value));
equal("全部引用可被解码回渠道+模型", allMultimodal.map((v) => mod.decodeChannelModelRef(v)), [
    { channelId: "ch-a", model: "vision-model-1" },
    { channelId: "ch-c", model: "vision-model-2" },
    { channelId: "ch-c", model: "vision-model-3" },
]);
check("空渠道表 → 空组（下拉走空提示，不崩）", mod.multimodalModelGroups([]).length === 0);

console.log(`M6-T7 探针：${passed} 条通过${failures.length ? `，${failures.length} 条失败` : ""}`);
if (failures.length) {
    for (const failure of failures) console.error(`FAIL ${failure}`);
    process.exit(1);
}
