/**
 * M6-T7-R 免费探针：渠道还原 + 全局拉片模型清单 + 节点模型选择项（纯逻辑层）。
 *
 * 直接转译真模块（esbuild 打包 src/lib/canvas/lapian-model.ts，不走 app、不连 bridge、不花钱），
 * 源码文本级断言覆盖「还原后无 multimodal 标签」与 store 接线：
 *   node scripts/probe-m6t7.mjs
 * 任何一条不成立就退出码 1。
 * 覆盖：①还原断言（ModelCapability 无 multimodal / 全局清单字段接线 / i18n 无多模态残留）
 *       ②全局清单存取（归一：失效引用剔除/去重/保序）③默认传导（节点换过 > 全局默认 > 空）
 *       ④/api/lapian/run 请求体契约不变（{model, channel:{baseUrl,key,model}}）⑤未配置时零字段。
 */
import { build } from "esbuild";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const src = path.join(root, "src");
const workdir = mkdtempSync(path.join(tmpdir(), "probe-m6t7-"));

const outfile = path.join(workdir, "lapian-model.mjs");
await build({
    entryPoints: [path.join(src, "lib/canvas/lapian-model.ts")],
    outfile,
    bundle: true,
    format: "esm",
    platform: "node",
    logLevel: "silent",
    alias: { "@": path.join(src) },
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
function sourceHas(name, relativePath, needle) {
    const text = readFileSync(path.join(src, relativePath), "utf8");
    check(name, text.includes(needle), `文件 ${relativePath} 中找不到：${needle}`);
}
function sourceLacks(name, relativePath, needle) {
    const text = readFileSync(path.join(src, relativePath), "utf8");
    check(name, !text.includes(needle), `文件 ${relativePath} 不应再含：${needle}`);
}

// 测试渠道：ch-a 带 3 个模型（生图/视觉/文本——能力归属不动）；ch-b 只有生图。
const CHANNELS = [
    {
        id: "ch-a",
        name: "渠道甲",
        baseUrl: "https://api.example-a.com",
        apiKey: "sk-a",
        apiFormat: "openai",
        models: [
            { name: "img-model", capability: "image" },
            { name: "vision-model-1", capability: "text" },  // T7-R 关键：读视频的分析模型归属「文本/推理」，不再有 multimodal 标签
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
];

// ---------------------------------------------------------------------------
// 1. 还原断言：能力标签撤 multimodal，全局清单字段接线就位
// ---------------------------------------------------------------------------
const configStore = readFileSync(path.join(src, "stores/use-config-store.ts"), "utf8");
check("ModelCapability 已还原为四类（无 multimodal）", /export type ModelCapability = "image" \| "video" \| "text" \| "audio";/.test(configStore));
sourceLacks("channel-editor-drawer 撤多模态标签", "components/layout/channel-editor-drawer.tsx", "multimodal");
sourceLacks("model-plugin 撤 multimodal 模板组", "services/api/model-plugin.ts", "multimodal");
sourceLacks("zh-CN i18n 无多模态文案残留", "i18n/locales/zh-CN.ts", "multimodal");
sourceLacks("en-US i18n 无多模态文案残留", "i18n/locales/en-US.ts", "multimodal");
sourceLacks("lapian-model 纯逻辑层不再按能力过滤", "lib/canvas/lapian-model.ts", "multimodal");
sourceHas("AiConfig 含 lapianModels 字段", "stores/use-config-store.ts", "lapianModels: string[];");
sourceHas("AiConfig 含 lapianDefaultModel 字段", "stores/use-config-store.ts", "lapianDefaultModel: string;");
sourceHas("defaultConfig 含全局清单默认值", "stores/use-config-store.ts", "lapianModels: [],");
sourceHas("persist merge 接线清单归一", "stores/use-config-store.ts", "normalizeLapianModelList(config.lapianModels, channels)");
sourceHas("persist merge 接线默认归一", "stores/use-config-store.ts", "normalizeLapianDefaultModel(config.lapianDefaultModel, lapianModels, channels)");
sourceHas("metadata.lapianModel 类型保留", "types/canvas.ts", "lapianModel?: string;");

// ---------------------------------------------------------------------------
// 2. 全局清单存取（归一口径：失效剔除/去重/保序）
// ---------------------------------------------------------------------------
equal("undefined 清单归一为空数组", mod.normalizeLapianModelList(undefined, CHANNELS), []);
equal("有效引用全部保留且保序", mod.normalizeLapianModelList(["ch-a::vision-model-1", "ch-a::text-model"], CHANNELS), ["ch-a::vision-model-1", "ch-a::text-model"]);
equal("渠道被删的引用自动剔除", mod.normalizeLapianModelList(["ch-gone::m", "ch-a::vision-model-1"], CHANNELS), ["ch-a::vision-model-1"]);
equal("模型被改名的引用自动剔除", mod.normalizeLapianModelList(["ch-a::renamed-away"], CHANNELS), []);
equal("重复引用去重", mod.normalizeLapianModelList(["ch-a::vision-model-1", "ch-a::vision-model-1"], CHANNELS), ["ch-a::vision-model-1"]);
check("空白/空串引用剔除", mod.normalizeLapianModelList(["  ", "", "ch-a::vision-model-1"], CHANNELS).length === 1);
equal("默认有效且在清单内 → 保留", mod.normalizeLapianDefaultModel("ch-a::vision-model-1", ["ch-a::vision-model-1", "ch-a::text-model"], CHANNELS), "ch-a::vision-model-1");
equal("默认不在清单内 → 回空", mod.normalizeLapianDefaultModel("ch-a::vision-model-1", ["ch-a::text-model"], CHANNELS), "");
equal("默认渠道已删 → 回空", mod.normalizeLapianDefaultModel("ch-gone::m", ["ch-gone::m"], CHANNELS), "");
equal("默认未设 → 回空", mod.normalizeLapianDefaultModel("", ["ch-a::vision-model-1"], CHANNELS), "");

// 清单按渠道分组（节点「模型」弹窗数据源）：只出勾选的、组序=渠道序、渠道内保持清单序
const groups = mod.lapianModelGroups(CHANNELS, ["ch-a::text-model", "ch-a::vision-model-1"]);
equal("分组只含勾选模型且保持清单序", groups.map((g) => g.models.map((m) => m.name)), [["text-model", "vision-model-1"]]);
equal("组序=渠道序且带渠道名", groups.map((g) => [g.channelId, g.channelName]), [["ch-a", "渠道甲"]]);
equal("未勾选模型的渠道不出现", mod.lapianModelGroups(CHANNELS, ["ch-a::vision-model-1"]).map((g) => g.channelId), ["ch-a"]);
equal("空清单 → 空组", mod.lapianModelGroups(CHANNELS, []), []);
equal("undefined 渠道表 → 空组", mod.lapianModelGroups(undefined, ["ch-a::vision-model-1"]), []);

// ---------------------------------------------------------------------------
// 3. 默认传导：节点换过 > 全局默认 > 空
// ---------------------------------------------------------------------------
equal("节点显式换过 → 节点优先", mod.resolveEffectiveLapianModel("ch-a::vision-model-1", "ch-a::text-model"), "ch-a::vision-model-1");
equal("节点未换（空串）→ 跟随全局默认", mod.resolveEffectiveLapianModel("", "ch-a::text-model"), "ch-a::text-model");
equal("节点未换（undefined）→ 跟随全局默认", mod.resolveEffectiveLapianModel(undefined, "ch-a::text-model"), "ch-a::text-model");
equal("节点未换且未设默认 → 空（走 bridge 默认）", mod.resolveEffectiveLapianModel(undefined, ""), "");
check("两端空白都裁剪", mod.resolveEffectiveLapianModel("  ", " ch-a::text-model ") === "ch-a::text-model");

// ---------------------------------------------------------------------------
// 4. /api/lapian/run 请求体契约（与 c4d9b8a 一致，bridge 不动）
// ---------------------------------------------------------------------------
const ID = "task-1";
const INTENT = "重点看产品镜头";
equal("配置了默认：补 model + channel{baseUrl,key,model}", mod.buildLapianRunBody(ID, INTENT, "ch-a::vision-model-1", CHANNELS), {
    id: ID,
    intent: INTENT,
    model: "vision-model-1",
    channel: { baseUrl: "https://api.example-a.com", key: "sk-a", model: "vision-model-1" },
});
check("请求体不含任何硬编码模型/品牌字样", JSON.stringify(mod.buildLapianRunBody(ID, INTENT, "ch-a::vision-model-1", CHANNELS)).includes("gemini") === false);

// ---------------------------------------------------------------------------
// 5. 未配置时零字段（旧链路逐字节零回归）
// ---------------------------------------------------------------------------
equal("未配置默认：只 {id,intent}", mod.buildLapianRunBody(ID, INTENT, undefined, CHANNELS), { id: ID, intent: INTENT });
equal("未配置默认（空串）：同样只 {id,intent}", mod.buildLapianRunBody(ID, INTENT, "", CHANNELS), { id: ID, intent: INTENT });
check("未配置时 body 无 model 字段", !("model" in mod.buildLapianRunBody(ID, INTENT, undefined, CHANNELS)));
check("未配置时 body 无 channel 字段", !("channel" in mod.buildLapianRunBody(ID, INTENT, undefined, CHANNELS)));
check("默认引用的渠道已删：绝不发半截请求", (() => {
    const body = mod.buildLapianRunBody(ID, INTENT, "ch-gone::m", CHANNELS);
    return !("model" in body) && !("channel" in body);
})());
check("默认引用的模型已被改名：同样零字段", (() => {
    const body = mod.buildLapianRunBody(ID, INTENT, "ch-a::renamed-away", CHANNELS);
    return !("model" in body) && !("channel" in body);
})());

// ---------------------------------------------------------------------------
// 6. 渠道引用编解码（channelId::model，与 use-config-store 同格式）
// ---------------------------------------------------------------------------
equal("解码标准引用", mod.decodeChannelModelRef("ch-a::vision-model-1"), { channelId: "ch-a", model: "vision-model-1" });
equal("模型名含 :: 时按首个分隔符拆", mod.decodeChannelModelRef("ch-a::we::ird"), { channelId: "ch-a", model: "we::ird" });
check("裸值（无分隔符）解码为 null", mod.decodeChannelModelRef("vision-model-1") === null);
equal("编码再解码 roundtrip", mod.decodeChannelModelRef(mod.encodeChannelModelRef("ch-a", " vision-model-1 ")), { channelId: "ch-a", model: "vision-model-1" });
check("isKnownChannelModelRef：渠道在且模型在", mod.isKnownChannelModelRef("ch-a::vision-model-1", CHANNELS) === true);
check("isKnownChannelModelRef：裸值不算", mod.isKnownChannelModelRef("vision-model-1", CHANNELS) === false);

console.log(`M6-T7-R 探针：${passed} 条通过${failures.length ? `，${failures.length} 条失败` : ""}`);
if (failures.length) {
    for (const failure of failures) console.error(`FAIL ${failure}`);
    process.exit(1);
}
