/**
 * M6-T6 SDGF 假失败修复探针（provider 五值透存（含 wan） / 瞬态 failed 复核 / 失败节点重查入口）。
 *
 * 直接转译真模块（esbuild 打包 src/lib/canvas/video-task-wait.ts，注入假 poll/delay，
 * 不走网络、不连 bridge、不花钱、不出网）；接线层（project.tsx / video.ts / 工具栏 / i18n）
 * 用源码断言钉住，防止回归成三元折叠。
 *   node scripts/probe-video-recheck.mjs
 * 任何一条不成立就退出码 1。
 */
import { build } from "esbuild";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const workdir = mkdtempSync(path.join(tmpdir(), "probe-video-recheck-"));

async function loadModule(relativePath, name) {
    const outfile = path.join(workdir, `${name}.mjs`);
    await build({
        entryPoints: [path.join(root, relativePath)],
        outfile,
        bundle: true,
        format: "esm",
        platform: "node",
        logLevel: "silent",
        alias: { "@": path.join(root, "src") },
    });
    return import(pathToFileURL(outfile).href);
}

const mod = await loadModule("src/lib/canvas/video-task-wait.ts", "video-task-wait");

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
function readSrc(relativePath) {
    return readFileSync(path.join(root, relativePath), "utf8");
}

const F = (message = "Qiling task failed") => ({ status: "failed", error: message }); // 站方瞬态 failed 的真实报文（本次事故三单同款）
const P = { status: "pending" };
const C = (url = "https://cdn.example.com/v.mp4") => ({ status: "completed", result: { url, mimeType: "video/mp4" } });

/** 用假 poll/delay 驱动真等待循环，记录轮询次数/间隔/终局。 */
async function drive(provider, sequence) {
    let i = 0;
    const delays = [];
    const out = { polls: 0, delays, failedError: null, timedOut: false, result: undefined, error: null };
    try {
        out.result = await mod.waitForVideoTaskOutput({
            provider,
            poll: async () => {
                // 轮询端点状态会保持：序列耗尽后重复最后一态（真死任务每次查都是 failed）
                const state = i < sequence.length ? sequence[i] : sequence[sequence.length - 1] ?? P;
                i += 1;
                out.polls = i;
                return state;
            },
            delay: async (ms) => {
                delays.push(ms);
            },
            onTaskFailed: (error) => {
                out.failedError = error;
                throw new Error(`task-failed:${error}`);
            },
            onTimeout: () => {
                out.timedOut = true;
                throw new Error("wait-timeout");
            },
        });
    } catch (error) {
        out.error = error;
    }
    return out;
}

// ---------------------------------------------------------------------------
// 1. 合同常量
// ---------------------------------------------------------------------------
equal("总轮询上限 360 次（思念云普遍 5 分钟以上，15 分钟上限）", mod.VIDEO_WAIT_MAX_ATTEMPTS, 360);
equal("常规轮询间隔 2500ms", mod.VIDEO_WAIT_POLL_INTERVAL_MS, 2500);
equal("sdgf 复核连败阈值 3", mod.SDGF_FAILED_RECHECK_LIMIT, 3);
equal("sdgf 复核间隔 8000ms", mod.SDGF_FAILED_RECHECK_DELAY_MS, 8000);

// ---------------------------------------------------------------------------
// 2. provider 五值透存不折叠（wan=万相 CLI bridge，2026-11-05 加）
// ---------------------------------------------------------------------------
for (const provider of ["openai", "gemini", "plugin", "sdgf", "wan"]) {
    equal(`创建回写透存 ${provider} 原值`, mod.storedVideoTaskProvider(provider), provider);
}
equal("恢复：sdgf 原值恢复（本次事故根因）", mod.resumedVideoTaskProvider("sdgf"), "sdgf");
equal("恢复：gemini 原值恢复", mod.resumedVideoTaskProvider("gemini"), "gemini");
equal("恢复：openai 原值恢复", mod.resumedVideoTaskProvider("openai"), "openai");
equal("恢复：plugin 原值恢复", mod.resumedVideoTaskProvider("plugin"), "plugin");
equal("恢复：wan 原值恢复", mod.resumedVideoTaskProvider("wan"), "wan");
equal("恢复：老数据缺 provider 回退 openai", mod.resumedVideoTaskProvider(undefined), "openai");
equal("恢复：脏数据回退 openai", mod.resumedVideoTaskProvider("junk"), "openai");
equal("恢复：null 回退 openai", mod.resumedVideoTaskProvider(null), "openai");

// ---------------------------------------------------------------------------
// 3. sdgf 瞬态 failed 复核（连续 3 次才死、中途 completed 即活）
// ---------------------------------------------------------------------------
const heal = await drive("sdgf", [F(), F(), C()]);
check("sdgf 假死两连败后 completed 自愈 → 返回成片", heal.result?.url === "https://cdn.example.com/v.mp4" && !heal.error, `终局 ${JSON.stringify({ result: heal.result, error: heal.error?.message })}`);
equal("sdgf 自愈路径轮询 3 次", heal.polls, 3);
equal("sdgf 自愈路径两次复核间隔均 8s", heal.delays, [8000, 8000]);

const quickHeal = await drive("sdgf", [F(), C()]);
check("sdgf 一次 failed 后 completed 也算活", Boolean(quickHeal.result) && quickHeal.polls === 2, `轮询 ${quickHeal.polls}`);

const dead = await drive("sdgf", [F()]);
check("sdgf 连续 3 次 failed 才判死（站方报文原样透出）", dead.failedError === "Qiling task failed" && dead.error?.message === "task-failed:Qiling task failed", `终局 ${JSON.stringify({ failedError: dead.failedError, error: dead.error?.message })}`);
equal("sdgf 真死路径共轮询 3 次", dead.polls, 3);
equal("sdgf 真死路径复核间隔 8s×2（判死后不再等待）", dead.delays, [8000, 8000]);

const reset = await drive("sdgf", [F(), P, F(), F(), F()]);
check("sdgf failed→pending 复位连败计数（第 5 次轮询才判死）", reset.failedError === "Qiling task failed" && reset.polls === 5, `轮询 ${reset.polls} / failedError ${reset.failedError}`);
equal("pending 复位后下一轮回到常规间隔", reset.delays, [8000, 2500, 8000, 8000]);

const healViaPending = await drive("sdgf", [F(), P, C()]);
check("sdgf failed→pending→completed 同样算自愈", Boolean(healViaPending.result) && healViaPending.polls === 3, `轮询 ${healViaPending.polls}`);

const oscillation = await drive("sdgf", Array.from({ length: 360 }, (_, k) => (k % 2 === 0 ? F() : P)));
check("sdgf failed/pending 振荡任务不会永久轮询（360 轮超时）", oscillation.timedOut && oscillation.polls === 360, `轮询 ${oscillation.polls} / timedOut ${oscillation.timedOut}`);

// ---------------------------------------------------------------------------
// 4. openai/gemini/plugin 维持「failed 即死」，零行为变化
// ---------------------------------------------------------------------------
for (const provider of ["openai", "gemini", "plugin"]) {
    const immediate = await drive(provider, [F()]);
    check(`${provider} 首次 failed 立即判死（不复核）`, immediate.failedError === "Qiling task failed" && immediate.polls === 1, `轮询 ${immediate.polls} / failedError ${immediate.failedError}`);
    equal(`${provider} 无复核等待`, immediate.delays, []);
}

// ---------------------------------------------------------------------------
// 5. completed 立即返回 / abort 立即中断
// ---------------------------------------------------------------------------
const instant = await drive("sdgf", [C()]);
check("completed 首轮即返回", Boolean(instant.result) && instant.polls === 1 && instant.delays.length === 0, `轮询 ${instant.polls}`);

const capped = await drive("sdgf", [P]);
check("一直 pending 也在 360 轮超时", capped.timedOut && capped.polls === 360, `轮询 ${capped.polls}`);

let abortError = null;
try {
    await mod.waitForVideoTaskOutput({ provider: "sdgf", poll: async () => P, delay: async () => {}, isAborted: () => true, onTaskFailed: () => {}, onTimeout: () => {} });
} catch (error) {
    abortError = error;
}
check("isAborted 立即抛 AbortError", abortError?.name === "AbortError", `error ${abortError?.name}`);

// ---------------------------------------------------------------------------
// 6. 失败节点「重查任务」入口开关
// ---------------------------------------------------------------------------
const failedNode = { type: "video", metadata: { videoTaskId: "vid-123", errorDetails: "Qiling task failed", status: "error" } };
check("失败节点（videoTaskId+errorDetails 同存）显示重查入口", mod.canRecheckVideoTask(failedNode) === true);
check("中断恢复节点（videoTaskId、无成片、status 已重置）也命中重查", mod.canRecheckVideoTask({ type: "video", metadata: { videoTaskId: "vid-123", status: "idle" } }) === true);
check("无 videoTaskId 不出重查入口", mod.canRecheckVideoTask({ type: "video", metadata: { errorDetails: "x", status: "error" } }) === false);
check("已有成片不出重查入口", mod.canRecheckVideoTask({ type: "video", metadata: { videoTaskId: "vid-123", content: "video:blob", status: "success" } }) === false);
check("生成中不出重查入口", mod.canRecheckVideoTask({ type: "video", metadata: { videoTaskId: "vid-123", status: "loading" } }) === false);
check("非 video 节点不出重查入口", mod.canRecheckVideoTask({ type: "image", metadata: { videoTaskId: "vid-123", status: "error" } }) === false);

// ---------------------------------------------------------------------------
// 7. 接线层源码断言（防回归）
// ---------------------------------------------------------------------------
const projectSrc = readSrc("src/pages/canvas/project.tsx");
check("project.tsx 创建回写走 storedVideoTaskProvider", projectSrc.includes("videoTaskProvider: storedVideoTaskProvider(task.provider)"), "回写接线缺失");
check("project.tsx 恢复轮询走 resumedVideoTaskProvider", projectSrc.includes("provider: resumedVideoTaskProvider(node.metadata?.videoTaskProvider)"), "恢复接线缺失");
check("project.tsx 不再有任何 gemini/openai 三元折叠", !projectSrc.includes('? "gemini" : "openai"'), "仍残留折叠三元");
check("project.tsx 重查路由：有 taskId 先 pollVideoNodeTask（不重新创建）", /hasResumableVideoTask\(node\)[\s\S]{0,120}await pollVideoNodeTask\(node\)/.test(projectSrc), "重查路由缺失");

const canvasTypes = readSrc("src/types/canvas.ts");
check("metadata.videoTaskProvider 放宽为五值联合（含 wan）", canvasTypes.includes("videoTaskProvider?: VideoTaskProvider") && /export type VideoTaskProvider = "openai" \| "gemini" \| "plugin" \| "sdgf" \| "wan"/.test(canvasTypes), "类型未放宽");

const videoSrc = readSrc("src/services/api/video.ts");
check("video.ts 等待循环委托给纯逻辑层 waitForVideoTaskOutput", videoSrc.includes("waitForVideoTaskOutput({") && videoSrc.includes("provider: task.provider,"), "委托缺失");
check("video.ts 不再内联 120 次硬编码循环", !videoSrc.includes("for (let attempt = 0; attempt < 120"), "旧循环残留");
check("video.ts 轮询分流保留 sdgf 分支", videoSrc.includes('if (task.provider === "sdgf") return pollSdgfVideoTask'), "sdgf 分流缺失");
check("video.ts sdgf 单次查询仍如实上报 failed（复核在等待层）", videoSrc.includes('if (status === "failed" || status === "cancelled")'), "sdgf failed 上报缺失");

const toolbarSrc = readSrc("src/components/canvas/canvas-node-hover-toolbar.tsx");
check("工具栏重查入口走 canRecheckVideoTask", toolbarSrc.includes("const canQueryVideoTask = canRecheckVideoTask(node);"), "入口接线缺失");
check("工具栏重查点击走 onRetry（复用重试通道→pollVideoNodeTask）", toolbarSrc.includes('id: "queryVideoTask"') && toolbarSrc.includes("onClick: () => onRetry(node)"), "点击接线缺失");

const zhSrc = readSrc("src/i18n/locales/zh-CN.ts");
const enSrc = readSrc("src/i18n/locales/en-US.ts");
check("zh 文案：重查任务", zhSrc.includes('queryVideoTask: "重查任务"') && zhSrc.includes('queryVideoTaskTitle: "按已存任务 ID 重查生成状态（不重新创建任务）"'), "中文文案缺失");
check("en 文案：Recheck task", enSrc.includes('queryVideoTask: "Recheck task"') && enSrc.includes('queryVideoTaskTitle: "Recheck generation status by stored task ID (no new task)"'), "英文文案缺失");

// ---------------------------------------------------------------------------
console.log(`probe-video-recheck: ${passed} passed, ${failures.length} failed`);
if (failures.length) {
    console.error(failures.map((line) => `  ✗ ${line}`).join("\n"));
    process.exit(1);
}
