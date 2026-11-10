/**
 * 摄取（建任务）结果的幂等命中判定（AT-10 C5）。
 *
 * 背景：后端 PUT/POST /api/hub/ingest 幂等键=视频内容 sha，同片重投会命中已有任务
 * （`result="HIT"` + `hit_existing=true` + 已有 `local_id`），旧前端把这个正常命中当
 * 普通建任务处理——弹窗一关零反馈，还顺手对新任务重触发一次拆解。这里把「是不是命中」
 * 与「命中要说的话」收敛成纯函数，两个入口（表格视图弹窗 / legacy 卡片）共用一份判定，
 * 探针可直接断言文案与判定，不必起界面。
 */

/** ingest 返回的最小形状（后端 automation/ingest.py 返回契约的超集）。 */
export type IngestOutcome = {
    local_id: string | null;
    bridge_id?: string | number | null;
    /** 后端标记：HIT=命中已有任务且未新建；OK=新建；DRY-OK=只校验。 */
    result?: string;
    /** 后端显式标记（C5 新增；旧后端没有该字段时按 result 兜底判）。 */
    hit_existing?: boolean;
    video_name?: string;
    note?: string;
};

/** 是否幂等命中已有任务（命中 → 不许再触发拆解，要提示并打开该任务）。 */
export function isIngestHit(outcome: IngestOutcome | null | undefined): boolean {
    if (!outcome) return false;
    if (outcome.hit_existing === true) return true;
    return String(outcome.result || "").trim().toUpperCase() === "HIT";
}

/** 命中提示文案：必须点名命中的是哪条任务，禁止只说「已完成」。 */
export function ingestHitMessage(localId: string, videoName = ""): string {
    const name = videoName ? `（${videoName}）` : "";
    return `该视频已有任务 ${localId}${name}，已为你打开`;
}

/** 新建成功提示文案（与命中区分开，命中不喊「已建立，开始拆解」）。 */
export function ingestCreatedMessage(localId: string): string {
    return `任务 ${localId} 已建立，开始拆解`;
}
