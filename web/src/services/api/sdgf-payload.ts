// SDGF（聚鑫统一视频 API）payload 细节——纯函数、无副作用，video.ts 与代码探针共用。
// 依据：docs/需求-SDGF新模型比例与无声问题-20261105.md §四/§五（V1–V3 付费实测，2026-11-05）。
//   V1 ap · aspect_ratio=16:9                       → 1278×720 横屏（参数链路通）
//   V2 ap · aspect_ratio=9:16 + metadata.ratio=9:16 → 720×1278 竖屏（假设 B 成立：渠道不吃顶层 9:16）
//   V3 满血 GF-720P · aspect_ratio=9:16 + enableSound:"on" → 720×1280 竖屏 + aac 真有声（mean −20.7 dB）

/** 满血 GF 精确模型名：聚鑫第十四章合同**唯一**宣称的模型名（SD2.5-满血-GF-{480,720,1080}P）。 */
const SDGF_FULL_MODEL_RE = /^SD2\.5-满血-GF-(480P|720P|1080P)$/i;

/** metadata.ratio 的站方文档枚举（16:9 / 9:16 / 1:1）；4:3 / 3:4 / adaptive 只在顶层 aspect_ratio 侧使用。 */
const SDGF_METADATA_RATIOS = ["16:9", "9:16", "1:1"];

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
 * 声音开关：S2 实测（V3）证明满血 GF + 顶层 `enableSound:"on"` 出真有声片——站方适配器实际吃该字段，
 * 十四章「不承诺 enableSound」只是免责表述。聚鑫通用章取值是字符串 "on"/"off"，故这里发字符串。
 * 面板「生成音频」关掉时不发字段（站方默认 off），保持旧 payload 形状不动。
 */
export const SDGF_SEND_SOUND_PARAM = true;

export function sdgfSoundFields(generateAudio: boolean, sendParam: boolean = SDGF_SEND_SOUND_PARAM): { enableSound?: "on" } {
    if (!sendParam || !generateAudio) return {};
    return { enableSound: "on" };
}
