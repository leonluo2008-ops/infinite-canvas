/**
 * 终稿正文 → 视频参数预填（T3-J5）。
 *
 * 所有者原话：「提示词里面已经其实有写了：全片时长5秒，竖屏9:16……这些生成的参数能不能够预填？」
 * 这里只做**白名单正则**，不引入模型抽取（零成本零延迟，审计决策 3）：
 *   - 时长：提到才填，且必须落在播放器允许的 4–30 秒整数区间；
 *   - 画幅：竖屏/横屏/方形 或显式 `9:16`，只认万相真参数域（16:9 / 9:16 / 1:1 / 4:3 / 3:4）；
 *   - 分辨率：`720p` 这类写法，只认 480 / 720 / 1080；
 *   - 参考模式：参考图 ≥2 张 → 全能参考。
 * 冲突口径（路B#8）：同一类多处矛盾取**文中最先出现**的那一个；非法值 / 匹配失败一律不给值，
 * 由调用方保持默认——「抽不到不填」比「猜一个」可靠。
 *
 * 纯函数、无副作用，探针可直接跑（scripts/probe-video-params.ts）。
 */

import { computeVideoSize, VIDEO_SECONDS_MAX, VIDEO_SECONDS_MIN } from "@/lib/media-size";

/** 万相 omni2video 的真参数域（bridge server.py 的 RATIOS 同源）；不在里面的当没写。 */
export const VIDEO_PARAM_RATIOS = ["16:9", "9:16", "1:1", "4:3", "3:4"] as const;
/** 画质档位（video-settings-panel 的 480p/720p/1080p）。 */
export const VIDEO_PARAM_RESOLUTIONS = ["480", "720", "1080"] as const;
/** 参考图 ≥2 张就必须全能参考（单图仍按继承结果走）。 */
export const VIDEO_PARAM_REFERENCE_MIN_IMAGES = 2;

export type VideoParamFill = {
    /** 秒数（字符串，与 config.videoSeconds 同型）。 */
    seconds?: string;
    /** 画幅比例（9:16 这类字面值）。 */
    ratio?: string;
    /** 画质档位（"480" / "720" / "1080"）。 */
    resolution?: string;
    /** 与画幅+画质算出来的 WxH（直接写进节点 metadata.size）。 */
    size?: string;
    /** 视频模式："reference" = 全能参考。 */
    videoMode?: string;
};

type Candidate = { index: number; value: string };

/** 取文中最先出现的候选（同位置取先入列表的）。 */
function firstCandidate(candidates: Candidate[]): string | undefined {
    const hit = candidates.filter((item) => item.index >= 0).sort((left, right) => left.index - right.index)[0];
    return hit?.value;
}

function allMatches(text: string, pattern: RegExp): Candidate[] {
    const out: Candidate[] = [];
    const flags = pattern.flags.includes("g") ? pattern.flags : `${pattern.flags}g`;
    for (const match of text.matchAll(new RegExp(pattern.source, flags))) {
        out.push({ index: match.index ?? -1, value: match[1] ?? "" });
    }
    return out;
}

/** 时长：`时长5秒` / `5.00秒` / `6s` / `时长：12 秒`。 */
function pickSeconds(text: string): string | undefined {
    const patterns = [/(?:全片|全视频|总)?时长[^\d\n]{0,4}(\d+(?:\.\d+)?)\s*(?:秒|s|S)/g, /(\d+(?:\.\d+)?)\s*秒/g, /(\d+(?:\.\d+)?)\s*s(?![a-zA-Z])/g];
    for (const pattern of patterns) {
        const candidate = firstCandidate(allMatches(text, pattern));
        if (candidate == null) continue;
        const value = Number(candidate);
        if (!Number.isFinite(value)) continue;
        const seconds = Math.round(value);
        if (seconds < VIDEO_SECONDS_MIN || seconds > VIDEO_SECONDS_MAX) continue;
        return String(seconds);
    }
    return undefined;
}

/** 画幅：中文口径 + 显式比例，取最先出现的一个；不在白名单的当没写。 */
function pickRatio(text: string): string | undefined {
    const candidates: Candidate[] = [];
    const words: Array<[RegExp, string]> = [
        [/竖屏|竖版/g, "9:16"],
        [/横屏|横版/g, "16:9"],
        [/方形|方图|正方/g, "1:1"],
    ];
    for (const [pattern, value] of words) {
        const hit = allMatches(text, pattern)[0];
        if (hit) candidates.push({ index: hit.index, value });
    }
    // 显式比例：排除时间码（00:02 / 04:07 这类前后带数字、冒号、点的片段）。
    for (const match of text.matchAll(/(?<![\d:.])(\d{1,2})\s*[:：]\s*(\d{1,2})(?![\d:.])/g)) {
        const value = `${Number(match[1])}:${Number(match[2])}`;
        if ((VIDEO_PARAM_RATIOS as readonly string[]).includes(value)) candidates.push({ index: match.index ?? -1, value });
    }
    return firstCandidate(candidates);
}

/** 分辨率：`720p` / `1080P`；只认 480/720/1080。 */
function pickResolution(text: string): string | undefined {
    for (const match of text.matchAll(/(?<![\d.])(\d{3,4})\s*[pP](?![a-zA-Z])/g)) {
        const value = String(Number(match[1]));
        if ((VIDEO_PARAM_RESOLUTIONS as readonly string[]).includes(value)) return value;
    }
    return undefined;
}

/**
 * 从终稿正文抽视频参数。
 * @param text 终稿正文（metadata.content）
 * @param options.imageCount 本次要带进视频的参考图张数；≥2 张自动全能参考
 * @param options.resolution 调用方当前的画质档位：只用来算 size，不会被写回（没提到画质就不动默认）
 */
export function extractVideoParams(text: string, options: { imageCount?: number; resolution?: string } = {}): VideoParamFill {
    const source = String(text || "");
    if (!source.trim()) return {};
    const fill: VideoParamFill = {};
    const seconds = pickSeconds(source);
    if (seconds) fill.seconds = seconds;
    const ratio = pickRatio(source);
    const resolution = pickResolution(source);
    if (ratio) fill.ratio = ratio;
    if (resolution) {
        fill.resolution = resolution;
    }
    if (ratio) {
        // size 只在有画幅时写：没有画幅就别改用户的尺寸设置。
        const effectiveResolution = resolution || options.resolution || "720";
        fill.size = computeVideoSize(effectiveResolution, ratio);
    }
    if ((options.imageCount ?? 0) >= VIDEO_PARAM_REFERENCE_MIN_IMAGES) fill.videoMode = "reference";
    return fill;
}
