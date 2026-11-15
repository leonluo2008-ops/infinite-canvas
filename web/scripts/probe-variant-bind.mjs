/**
 * 卡C2b 验收探针：变体直绑通道（workflow-variant-bind.ts）行为断言（stub 客户端，零真桥调用）。
 *   node scripts/probe-variant-bind.mjs
 * 先例 probe-variant-derive.mjs：esbuild 打包真模块跑断言；任何一条不成立退出码 1。
 */
import { build } from "esbuild";
import { rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const outdir = path.join(root, "node_modules", ".probe", "variant-bind");
const outfile = path.join(outdir, "workflow-variant-bind.mjs");

await build({
    entryPoints: [path.join(root, "src/lib/canvas/workflow-variant-bind.ts")],
    outfile,
    bundle: true,
    format: "esm",
    platform: "node",
    logLevel: "silent",
    alias: { "@": path.join(root, "src") },
    define: { "import.meta.env.DEV": "false", "import.meta.env": "{}" },
});
const mod = await import(pathToFileURL(outfile).href);
const { bindVariantToHub, variantAdvanceGate, VariantBindError } = mod;
rmSync(outdir, { recursive: true, force: true });

let passed = 0;
const failures = [];
function check(name, condition, detail = "") {
    if (condition) { passed += 1; console.log("PASS | " + name + (detail ? " | " + detail : "")); }
    else { failures.push(name); console.log("FAIL | " + name + (detail ? " | " + detail : "")); }
}
const fork = { localId: "auto-0900", bridgeId: "3", sourceLocalId: "auto-0066", boundAt: "2026-11-15T12:00:00+08:00" };
const instantSleep = () => Promise.resolve();

// ---- 1. 推进门（纯函数）----
check("gate awaiting_assets=upload_and_confirm", variantAdvanceGate("awaiting_assets").action === "upload_and_confirm");
check("gate rendering=wait_render", variantAdvanceGate("rendering").action === "wait_render");
check("gate awaiting_approval=ready", variantAdvanceGate("awaiting_approval").action === "ready");
check("gate failed=failed", variantAdvanceGate("failed").action === "failed");
check("gate approved=blocked（付费红线：直绑不动已批态）", variantAdvanceGate("approved").action === "blocked");
check("gate generating=blocked", variantAdvanceGate("generating").action === "blocked");
check("gate done=blocked", variantAdvanceGate("done").action === "blocked");

// ---- 2. fork 身份不完整 → 拒绝且绝无 ingest（客户端根本不含 ingest 面）----
const clientShape = Object.keys({ getTask: 0, uploadAssets: 0, confirmReplace: 0, savePrompt: 0 });
check("client 契约无 ingest/approve/submit/generate", !clientShape.some((k) => /ingest|approve|submit|generate/i.test(k)));
const emptyFork = { localId: "", bridgeId: "3", sourceLocalId: "auto-0066", boundAt: "" };
try {
    await bindVariantToHub({ fork: emptyFork }, { client: stub(), sleep: instantSleep });
    check("身份不完整拒绝", false);
} catch (error) {
    check("身份不完整拒绝", error instanceof VariantBindError && error.code === "identity", error.message);
}
function stub(states = ["awaiting_assets", "rendering", "awaiting_approval"], opts = {}) {
    let i = 0;
    const calls = { confirm: 0, uploads: 0, prompts: [] };
    return {
        calls,
        async getTask() { const s = states[Math.min(i, states.length - 1)]; i += 1; return { state: s, bridgeId: opts.bridgeId || "3" }; },
        async uploadAssets(localId, files) { calls.uploads += 1; return { filled: files.map((_, n) => ({ n })) }; },
        async confirmReplace(localId, input) { calls.confirm += 1; calls.lastConfirm = input; return { state: "rendering", jobId: "job-1" }; },
        async savePrompt(input) { calls.prompts.push(input); return { state: "awaiting_approval", approvalSnapshot: "snap-" + "a".repeat(60), selfcheck: [{ 结论: "PASS" }] }; },
    };
}

// ---- 3. 全链：awaiting_assets → 上传 → 确认（全沿用=false）→ rendering → awaiting_approval → 保存正文 ----
{
    const client = stub(["awaiting_assets", "rendering", "awaiting_approval", "awaiting_approval"]);
    const result = await bindVariantToHub({ fork, assets: [{ blob: new Blob(["x"]), filename: "slot.png" }], promptText: "  变体正文（>0字）  " }, { client, sleep: instantSleep });
    check("推进到 awaiting_approval", result.state === "awaiting_approval", result.state);
    check("资产上传 1 次", client.calls.uploads === 1);
    check("确认置换 1 次", client.calls.confirm === 1);
    check("有资产 → 不走全沿用", client.calls.lastConfirm && client.calls.lastConfirm.emptyConfirm === false);
    check("正文保存到 fork 新任务号", client.calls.prompts.length === 1 && client.calls.prompts[0].localId === "auto-0900" && client.calls.prompts[0].text === "变体正文（>0字）");
    check("快照回传", result.approvalSnapshot === "snap-" + "a".repeat(60));
    check("自检回传", Array.isArray(result.selfcheck) && result.selfcheck.length === 1);
    check("promptSaved=true", result.promptSaved === true);
}

// ---- 4. 无资产 → 全沿用空确认；不给正文 → 停待批等人 ----
{
    const client = stub(["awaiting_assets", "rendering", "awaiting_approval", "awaiting_approval"]);
    const result = await bindVariantToHub({ fork }, { client, sleep: instantSleep });
    check("无资产走 empty_confirm=true", client.calls.lastConfirm && client.calls.lastConfirm.emptyConfirm === true);
    check("不给正文 → promptSaved=false 停待批", result.promptSaved === false && result.state === "awaiting_approval");
    check("停待批有 warning 交代", result.warnings.some((w) => w.includes("停到 awaiting_approval") || w.includes("不自动批")));
}

// ---- 5. 已在待批 → 幂等跳过确认 ----
{
    const client = stub(["awaiting_approval", "awaiting_approval"]);
    const result = await bindVariantToHub({ fork, promptText: "正文" }, { client, sleep: instantSleep });
    check("已在待批 → 确认 0 次（幂等）", client.calls.confirm === 0 && result.state === "awaiting_approval");
}

// ---- 6. bridge_id 不一致 → identity 拒绝 ----
{
    const client = stub(["awaiting_assets"], { bridgeId: "9" });
    try {
        await bindVariantToHub({ fork }, { client, sleep: instantSleep });
        check("bridge 不一致拒绝", false);
    } catch (error) {
        check("bridge 不一致拒绝", error instanceof VariantBindError && error.code === "identity", error.message);
    }
}

// ---- 7. 渲染落 failed → 如实上报（不自动重试）----
{
    const client = stub(["awaiting_assets", "failed"]);
    try {
        await bindVariantToHub({ fork }, { client, sleep: instantSleep });
        check("failed 如实抛", false);
    } catch (error) {
        check("failed 如实抛", error instanceof VariantBindError && error.code === "confirm" && error.message.includes("failed"), error.message);
    }
}

// ---- 8. 超时 → timeout 如实抛 ----
{
    const client = stub(["awaiting_assets", "rendering", "rendering", "rendering"]);
    try {
        await bindVariantToHub({ fork, timeoutMs: 3000, intervalMs: 2000 }, { client, sleep: instantSleep });
        check("超时如实抛", false);
    } catch (error) {
        check("超时如实抛", error instanceof VariantBindError && error.code === "timeout", error.message);
    }
}

// ---- 9. rendering 中途入口 → 直接等待（不重复确认）----
{
    const client = stub(["rendering", "awaiting_approval", "awaiting_approval"]);
    const result = await bindVariantToHub({ fork }, { client, sleep: instantSleep });
    check("rendering 入口不重复确认", client.calls.confirm === 0 && result.state === "awaiting_approval");
}

// ---- 10. 带资产但缺 uploadAssets → 拒绝静默丢资产 ----
{
    const client = stub(["awaiting_assets"]);
    delete client.uploadAssets;
    try {
        await bindVariantToHub({ fork, assets: [{ blob: new Blob(["x"]), filename: "a.png" }] }, { client, sleep: instantSleep });
        check("缺 uploadAssets 拒绝", false);
    } catch (error) {
        check("缺 uploadAssets 拒绝", error instanceof VariantBindError && error.code === "confirm", error.message);
    }
}

// ---- 11. 正文保存失败（如 revoked 抢态）→ prompt_save 如实抛 ----
{
    const client = stub(["awaiting_assets", "rendering", "awaiting_approval", "awaiting_approval"]);
    client.savePrompt = async () => { throw new Error("正文保存只在待批准"); };
    try {
        await bindVariantToHub({ fork, promptText: "正文" }, { client, sleep: instantSleep });
        check("保存失败如实抛", false);
    } catch (error) {
        check("保存失败如实抛", error instanceof VariantBindError && error.code === "prompt_save", error.message);
    }
}

console.log(`\n${passed} passed, ${failures.length} failed`);
if (failures.length) { console.log("FAILURES: " + failures.join(" | ")); process.exit(1); }
