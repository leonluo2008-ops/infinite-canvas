import type { ModelChannel } from "@/stores/use-config-store";
import type { PresetMeta } from "@/lib/config/preset-meta";

/**
 * 预置完整性判据：预置里未被墓碑挡下、也未被 alias 升格「已存在同名渠道」吸收的模板渠道，
 * 是否都已出现在当前渠道列表。全部在场 → 版本守卫可以安全跳过；有缺 → 必须重新合并。
 *
 * 与 mergePresetChannels 的跳过口径保持一致（同名跳过/alias 认领都算「在场」），
 * 避免守卫判定过严导致每次启动都白跑一遍合并（V6 零写入退化）。
 */
export function presetLayerComplete(channels: ModelChannel[], preset: ModelChannel[], meta: PresetMeta): boolean {
    const tombstones = new Set((meta.tombstones || []).map((id) => (id || "").trim()).filter(Boolean));
    const aliases = meta.aliases || {};
    const ids = new Set((channels || []).map((channel) => channel?.id).filter(Boolean));
    const names = new Set((channels || []).map((channel) => (channel?.name || "").trim().toLowerCase()).filter(Boolean));
    for (const template of preset || []) {
        if (!template || typeof template !== "object") continue;
        const templateId = (template.id || "").trim();
        if (!templateId) continue;
        if (tombstones.has(templateId)) continue; // 本浏览器用户主动删过：不在场是预期
        const aliasedId = (aliases[templateId] || "").trim();
        if (aliasedId && ids.has(aliasedId)) continue; // 已升格成用户自己的渠道
        if (ids.has(templateId)) continue; // 模板渠道原样在场
        const nameKey = (template.name || "").trim().toLowerCase();
        if (nameKey && names.has(nameKey)) continue; // 同名渠道在场（另一设备升格/用户重命名过）
        return false; // 缺这个模板 → 必须重新合并
    }
    return true;
}
