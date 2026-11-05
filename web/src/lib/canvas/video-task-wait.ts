import { CanvasNodeType, type VideoTaskProvider } from "@/types/canvas";

/**
 * 视频生成任务等待/重查的纯逻辑层（M6-T6 SDGF 假失败修复）。
 * 不 import axios/i18n/React——网络与文案由 services/api/video.ts 注入，
 * 探针（web/scripts/probe-video-recheck.mjs）注入假 poll/delay 直接断言真代码行为。
 *
 * 背景（2026-11-05，三单「Qiling task failed」而站方 status=completed）：
 * 聚鑫站方引擎存在「瞬态 failed → 自愈转 completed」行为，前端把单次 failed 当终态
 * 立即判死；且创建回写/恢复轮询处把 provider 二值化，sdgf 被折成 openai 走错协议。
 */

// 常规轮询总上限与间隔（与重构前 waitForVideoGenerationTask 行为一致）。
export const VIDEO_WAIT_MAX_ATTEMPTS = 120;
export const VIDEO_WAIT_POLL_INTERVAL_MS = 2500;
// SDGF 瞬态 failed 复核：连续 3 次 failed（间隔 8s）才判死；站方自愈窗口内 completed 即活。
export const SDGF_FAILED_RECHECK_LIMIT = 3;
export const SDGF_FAILED_RECHECK_DELAY_MS = 8000;

export type VideoWaitPollState<T> = { status: "pending" } | { status: "completed"; result: T } | { status: "failed"; error: string };

export type VideoWaitDriverOptions<T> = {
    provider: VideoTaskProvider;
    poll: () => Promise<VideoWaitPollState<T>>;
    delay: (ms: number) => Promise<void>;
    isAborted?: () => boolean;
    onTaskFailed: (error: string) => never;
    onTimeout: () => never;
};

/**
 * 视频任务等待循环（纯逻辑，错误文案/网络由调用方注入）。
 * - completed 立即返回（含 sdgf 连败复核中途自愈的那一次）。
 * - failed：仅 sdgf 不立即终判——连败计数达 SDGF_FAILED_RECHECK_LIMIT（间隔 8s 复核）才判死；
 *   openai/gemini/plugin 维持「failed 即死」原行为，其它渠道零行为变化。
 * - pending 复位连败计数（站方 failed → pending → completed 同样算自愈）。
 * - 总尝试仍受 VIDEO_WAIT_MAX_ATTEMPTS 上限：真死任务 3 轮即出结果，反复 failed/pending 振荡的任务
 *   到 120 轮超时，不会永久轮询。
 */
export async function waitForVideoTaskOutput<T>(options: VideoWaitDriverOptions<T>): Promise<T> {
    let failedStreak = 0;
    for (let attempt = 0; attempt < VIDEO_WAIT_MAX_ATTEMPTS; attempt += 1) {
        if (options.isAborted?.()) throw new DOMException("Aborted", "AbortError");
        const state = await options.poll();
        if (state.status === "completed") return state.result;
        if (state.status === "failed") {
            failedStreak += 1;
            if (options.provider !== "sdgf" || failedStreak >= SDGF_FAILED_RECHECK_LIMIT) options.onTaskFailed(state.error);
            await options.delay(SDGF_FAILED_RECHECK_DELAY_MS);
            continue;
        }
        failedStreak = 0;
        if (attempt === VIDEO_WAIT_MAX_ATTEMPTS - 1) options.onTimeout();
        await options.delay(VIDEO_WAIT_POLL_INTERVAL_MS);
    }
    options.onTimeout();
}

/**
 * 创建成功后回写节点 metadata 的 provider——原值透存，四值不折叠。
 * 历史 bug：`task.provider === "gemini" ? "gemini" : "openai"` 把 sdgf 折成 openai，
 * 刷新后恢复轮询走 pollOpenAIVideoTask，站方 completed 也查不出成片（假失败根因之一）。
 */
export function storedVideoTaskProvider(provider: VideoTaskProvider): VideoTaskProvider {
    return provider;
}

/**
 * 从节点 metadata 恢复轮询 provider：存的原值直接用；缺失/未知/老数据回退 openai（维持旧行为）。
 */
export function resumedVideoTaskProvider(provider: unknown): VideoTaskProvider {
    return provider === "openai" || provider === "gemini" || provider === "plugin" || provider === "sdgf" || provider === "wan" ? provider : "openai";
}

/**
 * 失败视频节点的「重查任务」入口开关：video 节点 + 存有 videoTaskId + 尚无成片 + 不在生成中。
 * 失败节点（metadata 同时有 videoTaskId 与 errorDetails）必然命中；刷新中断的恢复节点
 * （status 已被重置、无 errorDetails）也命中——同一入口服务手动重查与中断续查。
 * 重查按存的 provider+taskId 重新走 waitForVideoGenerationTask，不重新创建任务、不花钱。
 */
export function canRecheckVideoTask(node: { type: string; metadata?: { videoTaskId?: string; content?: unknown; status?: unknown } }): boolean {
    return node.type === CanvasNodeType.Video && Boolean(node.metadata?.videoTaskId) && !node.metadata?.content && node.metadata?.status !== "loading";
}
