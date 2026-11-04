import type { AgentAttachment } from "./types.js";

/**
 * 聊天附件的护栏。
 *
 * 画布是多媒体平台，但**附件通道只走图片**：图片有原生投递路径（`--image` / vision_analyze），
 * 视频与音频体积大、CLI 也不收，正解是画布 `@素材引用`（素材在 bridge 有实体，按路径处理）。
 * 这里在 HTTP 入口把「非图片」和「超 8MB」的附件挡掉并给出改道提示，避免 base64 撑爆请求、
 * 也避免把不支持的类型悄悄丢给 provider（那会变成「发了没反应」这种最难查的失败）。
 */

/** 单张图片附件的解码后大小上限。 */
export const MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024;

/** 附件允许的图片 MIME（与 Hermes `--image`、vision_analyze 能读的格式一致）。 */
export const IMAGE_ATTACHMENT_MIMES = ["image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif", "image/avif"] as const;

/** 护栏结果：`error` 直接作为 400 的文案返回给前端。 */
export type AttachmentGuardResult = { ok: true } | { ok: false; error: string };

/** 取 dataUrl 的 MIME；拿不到返回空串。 */
export function dataUrlMime(dataUrl: string) {
    const match = /^data:([^;,]+)/i.exec(dataUrl || "");
    return match ? match[1].toLowerCase() : "";
}

/** dataUrl 的 base64 载荷解码后字节数（不真的解码，按 base64 长度换算）。 */
export function dataUrlBytes(dataUrl: string) {
    const comma = (dataUrl || "").indexOf(",");
    if (comma < 0) return 0;
    const payload = dataUrl.slice(comma + 1).replace(/\s+/g, "");
    if (!payload) return 0;
    const padding = payload.endsWith("==") ? 2 : payload.endsWith("=") ? 1 : 0;
    return Math.max(0, Math.floor((payload.length * 3) / 4) - padding);
}

/**
 * 校验本轮附件：只收 8MB 以内的图片 dataUrl，其余返回可直接展示的错误。
 *
 * 纯函数，HTTP 路由与单测共用；`maxBytes` 只为测试边界留出注入口。
 */
export function guardTurnAttachments(attachments: AgentAttachment[], maxBytes = MAX_ATTACHMENT_BYTES): AttachmentGuardResult {
    for (const item of attachments) {
        const name = item?.name?.trim() || "未命名附件";
        const dataUrl = typeof item?.dataUrl === "string" ? item.dataUrl : "";
        if (!dataUrl) return { ok: false, error: `附件「${name}」缺少图片数据，请重新选择文件后再发送。` };
        const mime = dataUrlMime(dataUrl);
        if (!(IMAGE_ATTACHMENT_MIMES as readonly string[]).includes(mime)) {
            return {
                ok: false,
                error: `附件「${name}」是 ${mime || "未知类型"}，聊天附件只支持图片（png / jpeg / webp / gif / avif）；视频、音频请改用 @画布素材引用发送——素材按路径处理，不需要走附件 base64。`,
            };
        }
        if (!/;base64,/i.test(dataUrl)) return { ok: false, error: `附件「${name}」不是 base64 图片数据，请重新选择文件后再发送。` };
        const bytes = dataUrlBytes(dataUrl);
        if (bytes > maxBytes) {
            const size = (bytes / 1024 / 1024).toFixed(1);
            const limit = Math.max(1, Math.round(maxBytes / 1024 / 1024));
            return {
                ok: false,
                error: `附件「${name}」${size}MB，超过单张图片 ${limit}MB 上限；请压缩后再发，或在画布上用 @素材引用发送该文件（素材走路径，不受此限）。`,
            };
        }
    }
    return { ok: true };
}
