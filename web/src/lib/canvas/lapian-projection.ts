/**
 * 拉片投影渲染的共享实现（P4 抽取；红线 25：页面与编排器同一份，禁复制第二份）。
 *
 * 存在理由：拉片正文有**两份**东西——
 *  ① 节点 `metadata.content`：`/api/lapian/<taskId>` 的全文 markdown（拉片节点卡渲染的那份，不含可选段选择）；
 *  ② `/api/render/<taskId>/<template>?optional=<字母>` 的**投影**：真正拿去做提示词的那份，可选段只在
 *     这条 URL 的 `optional` 参数上生效（`project.tsx:lapianPromptPreset` 原实现，手工「拉片→生成视频」走它）。
 * 槽⑤「可选段真实生效」验收要求「本次实际发送正文变化，不只是 metadata 变」——那就必须在组装前按
 * 本次可选段重取投影，而不是读节点上那份与可选段无关的原始 markdown。
 *
 * 本文件把这条 URL 的口径（encodeURIComponent 两次、模板 id、「# 附注」截断）收成唯一实现：
 * 页面 `lapianPromptPreset` 与实例编排器 `workflow-instance-run.ts` 都调它。不读 i18n、不碰 React。
 */

/** 默认投影模板：即梦画布/集梦工作台单段流式（所有者 2026-11-05 拍板默认；与页面常量同值）。 */
export const LAPIAN_PROJECTION_TEMPLATE = "seedance-flow";

/** 可选段字母（A/B/C，与 `lapian-optional-modal.tsx` 的 LAPIAN_OPTIONAL_SEGMENTS 同源）。 */
export const LAPIAN_OPTIONAL_VALUES = ["A", "B", "C"] as const;

/** metadata 里存的是逗号分隔字母（"A,C"）；解析容错空白/大小写，不认识的字母丢弃，输出按 A→B→C 归一。 */
export function parseLapianOptional(value?: string): string[] {
    const picked = String(value || "")
        .split(",")
        .map((item) => item.trim().toUpperCase())
        .filter((item) => (LAPIAN_OPTIONAL_VALUES as readonly string[]).includes(item));
    return LAPIAN_OPTIONAL_VALUES.filter((item) => picked.includes(item));
}

/** 可选段字母归一成 URL 参数值（"c, a" → "A,C"）。 */
export function lapianOptionalLetters(value?: string): string {
    return parseLapianOptional(value).join(",");
}

/** 投影 URL（与改造前逐字节一致：`/api/render/<taskId>/<template>?optional=<letters>`）。 */
export function lapianProjectionUrl(taskId: string, optional = "", template: string = LAPIAN_PROJECTION_TEMPLATE): string {
    return `/api/render/${encodeURIComponent(taskId)}/${template}?optional=${encodeURIComponent(optional)}`;
}

/** 投影正文截断口径：去掉「# 附注」节及其后内容（v4 审计 B-03：附注随提示词提交会在聚鑫链复发）。 */
export function stripLapianNotes(text: string): string {
    const body = String(text || "");
    const cut = body.indexOf("\n# 附注");
    return (cut >= 0 ? body.slice(0, cut) : body).trim();
}

export type LapianProjectionFetch = (url: string, init?: RequestInit) => Promise<Response>;

export type LapianProjectionInput = {
    taskId: string;
    /** 本次可选段字母（逗号分隔；空 = 全关，与手工路径同义）。 */
    optionalSegments?: string;
    template?: string;
};

export type LapianProjectionResult = {
    /** 投影正文（已截断附注）；调用方负责把它写进拉片节点/用于组装。 */
    text: string;
    /** 实际请求的 URL（审计：optional 是否真的透传）。 */
    url: string;
    /** 归一后的可选段字母。 */
    optional: string;
};

/** 投影渲染错误：与网络异常区分，调用方按原行为提示（页面 warning / 编排器 failure），不静默吞。 */
export class LapianProjectionError extends Error {
    readonly url: string;
    constructor(message: string, url: string) {
        super(message);
        this.name = "LapianProjectionError";
        this.url = url;
    }
}

/**
 * 取本次投影正文（页面「拉片→配置」与实例组装共用）。
 * 失败即抛 `LapianProjectionError`——绝不返回空串冒充成功（空正文会被当成「可选段没生效」）。
 */
export async function renderLapianProjection(input: LapianProjectionInput, deps: { fetch: LapianProjectionFetch }): Promise<LapianProjectionResult> {
    const taskId = String(input.taskId || "").trim();
    const optional = lapianOptionalLetters(input.optionalSegments);
    const url = lapianProjectionUrl(taskId, optional, input.template || LAPIAN_PROJECTION_TEMPLATE);
    if (!taskId) throw new LapianProjectionError("拉片节点没有任务号，拉不了投影", url);
    const payload = (await deps.fetch(url).then((response) => response.json())) as { text?: string; error?: string };
    if (!payload || payload.error) throw new LapianProjectionError(String(payload?.error || "拉片投影渲染失败"), url);
    return { text: stripLapianNotes(String(payload.text || "")), url, optional };
}
