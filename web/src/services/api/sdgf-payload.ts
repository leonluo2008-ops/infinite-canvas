// SDGF（聚鑫统一视频 API）payload 细节——纯函数、无副作用，video.ts 与代码探针共用。
// 依据：docs/需求-SDGF新模型比例与无声问题-20261105.md §四/§五（V1–V3、V6–V7 付费实测，2026-11-05）。
// 同一渠道共存两族模型，**同一字段名的生效位不同**，故 payload 按模型名分流：
//   满血 GF（SD2.5-满血-GF-{480,720,1080}P）：顶层 aspect_ratio、顶层 enableSound:"on" 均生效
//       —— V3：720×1280 竖屏 + aac 真有声（mean −20.7 dB）。
//   ap 系别名（sd2.5-720p-ap 等）：顶层 aspect_ratio 与顶层 enableSound 都被静默忽略
//       —— V6：落默认 16:9 横屏 + 无声；生效位在 metadata
//       —— V7：metadata.ratio=9:16 → 竖屏，metadata.enableSound="on" → 有声。
//   V2：ap 系顶层 + metadata 双写比例，720×1278 竖屏，双写无害（保留双写）。
// 时长按代际：SD2.0 系旧合同上限 15 秒（截断）；SD2.5 全系 4–30 原样 —— 见 sdgfClampDuration。

/** 满血 GF 精确模型名：聚鑫第十四章合同**唯一**宣称的模型名（SD2.5-满血-GF-{480,720,1080}P）。 */
const SDGF_FULL_MODEL_RE = /^SD2\.5-满血-GF-(480P|720P|1080P)$/i;

/** metadata.ratio 的站方文档枚举（16:9 / 9:16 / 1:1）；4:3 / 3:4 / adaptive 只在顶层 aspect_ratio 侧使用。 */
const SDGF_METADATA_RATIOS = ["16:9", "9:16", "1:1"];

/** SD2.0 系（第一章旧合同）时长上限；SD2.5 全系 4–30，不截断。 */
export const SDGF_LEGACY_MAX_SECONDS = 15;

/** SD2.0 代际模型名：`sd2-*` / `sd2.0-*`（第一章合同模型列表全属 2.0 系；`sd2.5-*` 不匹配）。 */
const SDGF_LEGACY_MODEL_RE = /^sd2(\.0)?-/i;

export function isSdgfFullModel(model: string): boolean {
    return SDGF_FULL_MODEL_RE.test((model || "").trim());
}

export function sdgfMetadataRatio(aspectRatio: string): string | null {
    return SDGF_METADATA_RATIOS.includes(aspectRatio) ? aspectRatio : null;
}

/**
 * 比例字段。非满血 GF 名（ap/zh/ds 等聚鑫别名）走站方另一条渠道：网关只认 metadata.ratio，
 * 顶层 aspect_ratio=9:16 会静默落回 16:9（V1/V2 对照）。故双写。
 * 满血 GF 名保持十四章合同原样——只发 aspect_ratio，不塞 metadata（合同未列即不发）。
 * 文档枚举外的比例（4:3 / 3:4 / adaptive）只发顶层，避免向渠道塞未定义值。
 */
export function sdgfRatioFields(model: string, aspectRatio: string): { aspect_ratio: string; metadata?: { ratio: string } } {
    if (isSdgfFullModel(model)) return { aspect_ratio: aspectRatio };
    const ratio = sdgfMetadataRatio(aspectRatio);
    return ratio ? { aspect_ratio: aspectRatio, metadata: { ratio } } : { aspect_ratio: aspectRatio };
}

/**
 * 声音开关：生效位随模型族不同。
 *   满血 GF：顶层 `enableSound:"on"` —— V3 成片 aac 实证（站方适配器实际吃该字段，十四章「不承诺」只是免责表述）。
 *   ap 系：顶层 `enableSound` 被静默忽略（V6 无声）；**生效位是 metadata.enableSound**（V7 有声）。
 * 两族取值都是字符串 "on"/"off"，故这里发字符串。
 * 面板「生成音频」关掉、或总开关关闭时不发字段（站方默认 off），保持旧 payload 形状不动。
 */
export const SDGF_SEND_SOUND_PARAM = true;

export function sdgfSoundFields(model: string, generateAudio: boolean, sendParam: boolean = SDGF_SEND_SOUND_PARAM): { enableSound?: "on"; metadata?: { enableSound: "on" } } {
    if (!sendParam || !generateAudio) return {};
    return isSdgfFullModel(model) ? { enableSound: "on" } : { metadata: { enableSound: "on" } };
}

/**
 * 时长度量按**代际**（不是按满血/ap）：
 *   SD2.0 系（sd2-720p-ds-v933 / sd2.0-720p-zh / sd2.0-720p-zh2）→ 第一章旧合同上限 15，Math.min 截断；
 *   SD2.5 系（sd2.5-720p-ap / sd2.5-1080p-ap / sd2.5-720p-zh* / SD2.5-满血-GF-*）→ 4–30 原样返回；
 *   未知代际 → 原样（只有明确落在 2.0 旧合同上的才截断）。
 * 纯函数——是否提示由调用方（video.ts 提交处）决定。
 */
export function sdgfClampDuration(model: string, seconds: number): number {
    return SDGF_LEGACY_MODEL_RE.test((model || "").trim()) ? Math.min(seconds, SDGF_LEGACY_MAX_SECONDS) : seconds;
}
