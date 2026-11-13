/**
 * 实例→hub 身份客户端（P4 生产适配）：把 `pages/hub/hub-api.ts` 的既有函数包成编排器要的窄接口。
 *
 * 为什么不另写一套请求：ingest（视频 sha 幂等）、资产上传（`X-Task-Id` → 本地引用）、正文保存
 * （写正文 → 重算快照 → 重跑自检，G16 唯一真源）、approve/rollback/revoke 的 URL、请求体、错误文案
 * 都已经在 hub-api.ts 里定死（红线 25：禁复制第二份）。本文件只做**形状转换**，不改任何合同。
 *
 * 状态一律现查（§5.3）：`waitForApproval` 只轮询 `getHubTask`，不缓存、不落盘、不猜。
 */

import { approveHubTask, getHubTask, ingestVideo, revokeHubTask, rollbackHubTask, saveHubPrompt, uploadHubImage } from "@/pages/hub/hub-api";

import type { WorkflowInstanceHubClient } from "@/lib/canvas/workflow-instance-run";

export type HubClientOptions = {
    /** 轮询参数（探针注入 sleep/now 实现零等待）。 */
    poll?: { intervalMs?: number; timeoutMs?: number; sleep?: (ms: number) => Promise<void>; now?: () => number };
};

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/**
 * 生产 hub 客户端：全部委托既有 hub-api 实现。
 * `waitForApproval` 的语义：hub 自己那套拆解/置换跑到 awaiting_approval 才轮到画布正文落库；
 * 超时如实抛出（带最后一次状态），绝不返回假的 awaiting_approval。
 */
export function createHubIdentityClient(options: HubClientOptions = {}): WorkflowInstanceHubClient {
    const poll = options.poll || {};
    const interval = Math.max(200, Number(poll.intervalMs ?? 5000));
    const timeout = Math.max(interval, Number(poll.timeoutMs ?? 30 * 60 * 1000));
    const sleep = poll.sleep || defaultSleep;
    const now = poll.now || Date.now;
    return {
        async ingest(input) {
            const file = new File([input.blob], input.filename || "canvas-video.mp4", { type: input.blob.type || "video/mp4" });
            const outcome = await ingestVideo(file, input.intent);
            return {
                localId: String(outcome?.local_id || ""),
                bridgeId: String(outcome?.bridge_id ?? ""),
                result: outcome?.result,
                hitExisting: outcome?.hit_existing === true,
                videoName: outcome?.video_name,
            };
        },
        async uploadAsset(input) {
            const file = new File([input.blob], input.filename || "asset.png", { type: input.blob.type || "image/png" });
            const uploaded = await uploadHubImage(file, input.localId);
            return { ref: uploaded.ref, source: uploaded.source };
        },
        async getTask(localId) {
            const task = await getHubTask(localId);
            return { state: String(task?.state || ""), bridgeId: String(task?.bridge_id ?? "") };
        },
        async saveRevision(input) {
            const saved = await saveHubPrompt(input.localId, input.text, input.expectText ?? undefined);
            return {
                state: saved?.state,
                unchanged: saved?.unchanged === true,
                promptEditedAt: saved?.prompt_edited_at,
                approvalSnapshot: saved?.approval_snapshot,
                selfcheck: Array.isArray(saved?.自检) ? saved.自检 : [],
                note: saved?.说明,
            };
        },
        approve: (localId) => approveHubTask(localId),
        rollback: (localId) => rollbackHubTask(localId),
        revoke: (localId) => revokeHubTask(localId),
        async waitForApproval(localId) {
            const startedAt = now();
            let last = "";
            for (;;) {
                last = (await this.getTask(localId)).state;
                if (last === "awaiting_approval") return last;
                if (last === "failed") throw new Error(`hub 任务 ${localId} 处于 failed：等不到待审批（先重试/修资产再绑）`);
                if (now() - startedAt >= timeout) throw new Error(`等 hub 任务 ${localId} 到待审批超时（${Math.round(timeout / 1000)}s，最后状态 ${last || "未知"}）`);
                await sleep(interval);
            }
        },
    };
}
