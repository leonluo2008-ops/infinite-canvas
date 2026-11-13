/**
 * 拉片受控执行入口（P3，方案A 批量代办）：`storageKey → /api/video/upload → /api/lapian/run → 本次正文回填`。
 *
 * 存在理由（红线 25 复用）：拉片链在画布上只有一条手工路径——`project.tsx:handleLapianExecute`（上传+发起）
 * 与 `components/canvas/nodes/lapian-node.tsx`（轮询 job → 取 /api/lapian 正文）。实例化器不能再抄一遍
 * 上传/请求体/正文取值口径（P0a §3.3：`run_generation` 不覆盖拉片链，handleGenerateNode 无上传无 /api/lapian/run），
 * 因此把这两段抽成无 React 依赖的公共实现：
 *  - 手工路径（project.tsx / lapian-node.tsx）改成调用本文件 → **行为逐字节不变**（同 URL、同请求头/体、同错误文案、同 LOG 文案）；
 *  - 实例化器（`workflow-instance.ts`）用它做槽①的受控执行入口 → 同源，不复制模型判定/请求体装配。
 *
 * 职责边界：只做「上传 + 发起 + 读正文」，不建节点、不碰 store、不读 i18n。
 * 付费归属：上传与 `/api/lapian/run` 属前置自动段（拉片/LLM 拆解），**不是**视频付费生成；P0b 付费闸
 * （run_generation 独立确认）不拦本入口，费用按 B0-D12 逐批记账。
 */

import type { ModelChannel } from "@/stores/use-config-store";

import { buildLapianRunBody, resolveEffectiveLapianModel, type LapianRunBody } from "@/lib/canvas/lapian-model";

/** 同域相对路径：本地 dev 由 vite proxy 转发，公网由隧道同域转发（与页面 `LAPIAN_BRIDGE` 同值）。 */
const BRIDGE = "";

export type LapianFetch = (url: string, init?: RequestInit) => Promise<Response>;

export type LapianExecuteDeps = {
    /** 画布本地媒体取字节（页面传 `services/file-storage.getMediaBlob`；探针传 stub）。 */
    getBlob: (storageKey: string) => Promise<Blob | null>;
    fetch: LapianFetch;
    /** 页面 `__lapianLog` 同口径的分步日志（可选）。 */
    log?: (message: string) => void;
};

export type LapianExecuteConfig = {
    /** 全局默认拉片模型（设置 → 拉片模型；空串 = 走 bridge 默认）。 */
    lapianDefaultModel?: string;
    channels?: ModelChannel[];
};

export type LapianExecuteInput = {
    /** 源视频媒体键（槽①写入的 storageKey）。 */
    storageKey: string;
    /** 上传文件名（节点标题；与手工路径同口径，缺省 `canvas-video.mp4`）。 */
    title?: string;
    intent: string;
    config: LapianExecuteConfig;
    /** 节点上显式选过的模型（默认传导 nodeOverride → globalDefault）。 */
    nodeModel?: string;
};

export type LapianExecuteResult = {
    /** 上传回执 id（= 画布拉片节点 metadata.taskId，也是 hub 任务号口径）。 */
    taskId: string;
    /** 拆解 job 号（bridge /api/job/<jobId> 轮询键；可能为空串=走 taskId 直查）。 */
    jobId: string;
    intent: string;
    /** 实际发出的请求体（审计：模型/渠道是否透传）。 */
    runBody: LapianRunBody;
    /** 落盘的模型引用（`channelId::model`）；空串=走 bridge 默认，节点上不写 lapianModel。 */
    lapianModel: string;
    upload: Record<string, unknown>;
    run: Record<string, unknown>;
};

/**
 * 上传源视频字节（`POST /api/video/upload`，X-Filename + octet-stream）。
 * 与 `handleLapianExecute` 的请求逐字节一致（含错误文案）。
 */
export async function uploadSourceVideo(input: { blob: Blob; title?: string }, deps: Pick<LapianExecuteDeps, "fetch" | "log">): Promise<{ taskId: string; upload: Record<string, unknown> }> {
    const filename = encodeURIComponent(input.title || "canvas-video.mp4");
    const upload = (await deps.fetch(`${BRIDGE}/api/video/upload`, {
        method: "POST",
        headers: { "X-Filename": filename, "Content-Type": "application/octet-stream" },
        body: input.blob,
    }).then((response) => response.json())) as Record<string, unknown>;
    deps.log?.(`upload=${JSON.stringify(upload).slice(0, 120)}`);
    if (upload?.error) throw new Error(String(upload.error));
    return { taskId: String(upload.id || ""), upload };
}

/**
 * 发起拉片（`POST /api/lapian/run`）。请求体装配复用 `buildLapianRunBody`（模型判定/方言透传禁复制）。
 */
export async function runLapianTask(
    input: { taskId: string; intent: string; config: LapianExecuteConfig; nodeModel?: string },
    deps: Pick<LapianExecuteDeps, "fetch" | "log">,
): Promise<{ jobId: string; runBody: LapianRunBody; lapianModel: string; run: Record<string, unknown> }> {
    const effectiveModel = resolveEffectiveLapianModel(input.nodeModel, input.config.lapianDefaultModel);
    const runBody = buildLapianRunBody(input.taskId, input.intent, effectiveModel, input.config.channels);
    const run = (await deps.fetch(`${BRIDGE}/api/lapian/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(runBody),
    }).then((response) => response.json())) as Record<string, unknown>;
    deps.log?.(`run=${JSON.stringify(run).slice(0, 120)}`);
    if (run?.error) throw new Error(String(run.error));
    return { jobId: String(run.job || ""), runBody, lapianModel: runBody.channel ? effectiveModel : "", run };
}

/**
 * 槽①受控执行入口：源视频字节 → 上传 → 发起拉片。
 * 换新源视频必须整条走这里（旧 taskId/jobId/旧正文一律由实例化器侧剥离，不带入）。
 */
export async function executeLapianChain(input: LapianExecuteInput, deps: LapianExecuteDeps): Promise<LapianExecuteResult> {
    const storageKey = String(input.storageKey || "").trim();
    if (!storageKey) throw new Error("这个视频节点没有本地字节（可能是外部链接）——重新把视频文件拖进画布再试");
    deps.log?.("getting blob");
    const blob = await deps.getBlob(storageKey);
    deps.log?.(`blob=${blob ? blob.size : "null"}`);
    if (!blob) throw new Error("视频字节取不回（浏览器存储里没有这个文件）——重新拖入视频");
    const { taskId, upload } = await uploadSourceVideo({ blob, title: input.title }, deps);
    const { jobId, runBody, lapianModel, run } = await runLapianTask(
        { taskId, intent: input.intent, config: input.config, nodeModel: input.nodeModel },
        deps,
    );
    return { taskId, jobId, intent: input.intent, runBody, lapianModel, upload, run };
}

/** `/api/lapian/<taskId>` 正文投影错误（payload.error）：与网络/其它异常区分，调用方沿用各自的原文案。 */
export class LapianProjectionError extends Error {
    constructor(message: string) {
        super(message);
        this.name = "LapianProjectionError";
    }
}

/** `/api/lapian/<taskId>` 的正文（markdown）——与拉片节点渲染同一取值口径。错误即抛，不吞。 */
export async function fetchLapianMarkdown(taskId: string, deps: Pick<LapianExecuteDeps, "fetch">): Promise<string> {
    const payload = (await deps.fetch(`${BRIDGE}/api/lapian/${encodeURIComponent(taskId)}`).then((response) => response.json())) as { markdown?: string; error?: string };
    if (!payload || payload.error) throw new LapianProjectionError(String(payload?.error || "拉片投影失败"));
    return String(payload.markdown || "");
}

export type LapianContentWaitInput = {
    taskId: string;
    jobId: string;
    /** 轮询间隔（默认 2000ms，同页面快轮询口径）。 */
    pollMs?: number;
    /** 最长等待（默认 20 分钟，覆盖「大拆解 10-20 分钟属正常」）。 */
    maxWaitMs?: number;
    /** 注入 sleep 便于探针零等待（默认 setTimeout）。 */
    sleep?: (ms: number) => Promise<void>;
    now?: () => number;
};

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/**
 * 本次拉片正文回填（非 React 版轮询）：`/api/job/<jobId>` 到 done → `/api/lapian/<taskId>` 取 markdown。
 * 与 `lapian-node.tsx` 的轮询同一份契约（job 404 → 按 taskId 直查；done → 取正文；error → 抛错）。
 * 页面组件保留自己的渲染相位（loading/done/error/lost），本函数给批量/实例化路径用。
 */
export async function waitLapianContent(input: LapianContentWaitInput, deps: Pick<LapianExecuteDeps, "fetch">): Promise<string> {
    const pollMs = Math.max(50, Number(input.pollMs ?? 2000));
    const maxWaitMs = Math.max(pollMs, Number(input.maxWaitMs ?? 20 * 60 * 1000));
    const sleep = input.sleep || defaultSleep;
    const now = input.now || Date.now;
    const startedAt = now();
    for (;;) {
        const job = (await deps.fetch(`${BRIDGE}/api/job/${input.jobId}`).then(async (response) => (response.status === 404 ? null : response.json()))) as { status?: string; stage?: string; error?: string } | null;
        if (job === null) {
            // bridge 重启过：job 查不到时按 taskId 直查投影（与页面「重查」同一出口）。
            const markdown = await fetchLapianMarkdown(input.taskId, deps);
            return markdown;
        }
        if (job.status === "done") return fetchLapianMarkdown(input.taskId, deps);
        if (job.status === "error") throw new Error(String(job.error || "拆解失败（见服务端日志）"));
        if (now() - startedAt >= maxWaitMs) throw new Error(`拉片超时（${Math.round(maxWaitMs / 1000)}s）：job ${input.jobId} 仍在 ${job.stage || job.status || "运行中"}`);
        await sleep(pollMs);
    }
}
