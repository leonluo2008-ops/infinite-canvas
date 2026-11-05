import axios from "axios";
import { nanoid } from "nanoid";

import i18n from "@/i18n";
import { dataUrlToFile, readFileAsDataUrl } from "@/lib/image-utils";
import { clampVideoSeconds, computeVideoSize, inferVideoRatio } from "@/lib/media-size";
import { waitForVideoTaskOutput } from "@/lib/canvas/video-task-wait";
import { getMediaBlob, resolveMediaUrl, uploadMediaFile, type UploadedFile } from "@/services/file-storage";
import { imageToDataUrl } from "@/services/image-storage";
import { boolConfig, buildApiUrl, modelOptionName, resolveModelRequestConfig, resolveModelScript, withLocalProxy, type AiConfig } from "@/stores/use-config-store";
import { runModelPlugin } from "./model-plugin";
import { sdgfClampDuration, sdgfRatioFields, sdgfSoundFields } from "./sdgf-payload";
import type { VideoTaskProvider } from "@/types/canvas";
import type { ReferenceImage } from "@/types/image";
import type { ReferenceAudio, ReferenceVideo } from "@/types/media";

type VideoResponse = { id: string; status?: string; error?: { message?: string }; url?: string; result_url?: string; video_url?: string; content?: { video_url?: string; url?: string } | null };
type ApiVideoResponse = VideoResponse | { code?: number | string; data?: VideoResponse | null; msg?: string; message?: string; error?: { message?: string } };
type ApiEnvelope<T> = T | { code?: number | string; data?: T | null; msg?: string; message?: string; error?: { message?: string } };
type RequestOptions = { signal?: AbortSignal };
type VideoMediaOptions = RequestOptions & { videos?: ReferenceVideo[]; audios?: ReferenceAudio[] };
const apiText = (key: string, options?: Record<string, unknown>) => i18n.t(`apiErrors.${key}`, options);

export type VideoGenerationResult = { blob?: Blob; url?: string; mimeType?: string };
export type VideoGenerationTask = { id: string; provider: VideoTaskProvider; model: string; durationClamped?: boolean };
type GeminiInlineData = { bytesBase64Encoded: string; mimeType: string };
type GeminiVideoOperation = {
    name?: string;
    done?: boolean;
    error?: { message?: string };
    response?: { generateVideoResponse?: { generatedSamples?: Array<{ video?: { uri?: string } }> } };
};
export type VideoGenerationTaskState = { status: "pending" } | { status: "completed"; result: VideoGenerationResult } | { status: "failed"; error: string };

/** Results for scripted (plugin) video models, which run their own create+poll in one shot at task creation. */
const pluginVideoResults = new Map<string, VideoGenerationResult>();

function aiApiUrl(config: AiConfig, path: string) {
    return buildApiUrl(config.baseUrl, path);
}

function aiHeaders(config: AiConfig, contentType?: string) {
    return {
        Authorization: `Bearer ${config.apiKey}`,
        ...(contentType ? { "Content-Type": contentType } : {}),
    };
}

export async function requestVideoGeneration(config: AiConfig, prompt: string, references: ReferenceImage[] = [], options?: VideoMediaOptions): Promise<VideoGenerationResult> {
    return waitForVideoGenerationTask(config, await createVideoGenerationTask(config, prompt, references, options), options);
}

export async function waitForVideoGenerationTask(config: AiConfig, task: VideoGenerationTask, options?: RequestOptions): Promise<VideoGenerationResult> {
    // 等待循环（含 SDGF 瞬态 failed 复核：连续 3 次间隔 8s 仍 failed 才判死，completed 立即返回）
    // 在 lib/canvas/video-task-wait.ts 纯逻辑层，此处只注入网络与错误文案；openai/gemini/plugin 维持「failed 即死」。
    return waitForVideoTaskOutput({
        provider: task.provider,
        poll: () => pollVideoGenerationTask(config, task, options),
        delay: (ms) => delay(ms, options?.signal),
        isAborted: () => Boolean(options?.signal?.aborted),
        onTaskFailed: (error) => {
            throw videoTaskFailed(error);
        },
        onTimeout: () => {
            throw new Error(apiText("videoTimeout", { provider: "" }));
        },
    });
}

export function isVideoTaskFailed(error: unknown) {
    return error instanceof Error && error.name === "VideoTaskFailed";
}

function videoTaskFailed(message: string) {
    const error = new Error(message);
    error.name = "VideoTaskFailed";
    return error;
}

export async function createVideoGenerationTask(config: AiConfig, prompt: string, references: ReferenceImage[] = [], options?: VideoMediaOptions): Promise<VideoGenerationTask> {
    const selectedModel = (config.model || config.videoModel).trim();
    const requestConfig = resolveModelRequestConfig(config, selectedModel);
    const script = resolveModelScript(config, selectedModel);
    if (script) return createPluginVideoTask(requestConfig, selectedModel, script, prompt, references, options);
    if (requestConfig.apiFormat === "wan") return createWanVideoTask(requestConfig, selectedModel, prompt, references, options);
    assertVideoConfig(requestConfig, requestConfig.model);
    if (requestConfig.apiFormat === "sdgf") return createSdgfVideoTask(requestConfig, selectedModel, prompt, references, options);
    if (requestConfig.apiFormat === "gemini") return createGeminiVideoTask(requestConfig, selectedModel, prompt, references, options);
    return createOpenAIVideoTask(requestConfig, selectedModel, prompt, references, options);
}

export async function pollVideoGenerationTask(config: AiConfig, task: VideoGenerationTask, options?: RequestOptions): Promise<VideoGenerationTaskState> {
    if (task.provider === "plugin") {
        const result = pluginVideoResults.get(task.id);
        return result ? { status: "completed", result } : { status: "failed", error: apiText("pluginVideoExpired") };
    }
    const requestConfig = resolveModelRequestConfig(config, task.model);
    if (task.provider === "wan") {
        // wan 的 key 可任意留空，不能走要求 apiKey 的 assertVideoConfig
        assertWanVideoConfig(requestConfig, requestConfig.model);
        return pollWanVideoTask(requestConfig, task, options);
    }
    assertVideoConfig(requestConfig, requestConfig.model);
    if (task.provider === "gemini") return pollGeminiVideoTask(requestConfig, task, options);
    if (task.provider === "sdgf") return pollSdgfVideoTask(requestConfig, task, options);
    return pollOpenAIVideoTask(requestConfig, task, options);
}

async function createPluginVideoTask(config: AiConfig, model: string, script: string, prompt: string, references: ReferenceImage[], options?: VideoMediaOptions): Promise<VideoGenerationTask> {
    if (!config.baseUrl.trim()) throw new Error(apiText("baseUrlRequired"));
    if (!config.apiKey.trim()) throw new Error(apiText("apiKeyRequired"));
    const refs = await Promise.all(references.map((image) => imageToDataUrl(image)));
    const videos = await Promise.all((options?.videos || []).map((video) => referenceMediaToFile(video, "ref.mp4", "invalidReferenceVideo", options)));
    const audios = await Promise.all((options?.audios || []).map((audio) => referenceMediaToFile(audio, "ref.mp3", "invalidReferenceAudio", options)));
    const result = videoPluginResult(
        await runModelPlugin({
            capability: "video",
            script,
            config,
            prompt,
            images: refs,
            videos,
            audios,
            params: {
                seconds: normalizeVideoSeconds(config.videoSeconds),
                size: normalizeVideoSize(config.size, config.vquality),
                resolution: normalizeVideoResolution(config.vquality),
                ratio: videoAspectRatio(config.size),
                generateAudio: boolConfig(config.videoGenerateAudio, true),
                watermark: boolConfig(config.videoWatermark, false),
                mode: resolveVideoMode(config.videoMode, refs.length),
            },
            signal: options?.signal,
        }),
    );
    const id = nanoid();
    pluginVideoResults.set(id, result);
    return { id, provider: "plugin", model };
}

function videoPluginResult(result: unknown): VideoGenerationResult {
    if (result instanceof Blob) return { blob: result };
    if (typeof result === "string") return { url: result, mimeType: "video/mp4" };
    if (result && typeof result === "object") {
        const record = result as Record<string, unknown>;
        if (record.blob instanceof Blob) return { blob: record.blob };
        const url = [record.url, record.video_url, record.result_url].find((value) => typeof value === "string" && value) as string | undefined;
        if (url) return { url, mimeType: "video/mp4" };
    }
    throw new Error(apiText("scriptNoVideo"));
}

// ---- SD GF（聚鑫统一视频 API）--------------------------------------------------
// 合同：POST /v1/videos（JSON：model/prompt/duration/aspect_ratio/mode/images）
//       GET /v1/videos/{id}（queued/in_progress/completed/failed + metadata.url）
//       GET /v1/videos/{id}/content（鉴权代理下载）
// 参考图只收公网 HTTPS URL：本地字节先经 bridge 图床路由换 URL（存储凭据不出服务端）。
// 分辨率由精确模型名定档——本分支不发送 size/resolution。

const SDGF_ASSET_BRIDGE = "/api/asset/upload";

// 聚鑫只收裸 HTTPS URL；重试链会从 metadata 恢复出 `@url:`...`` token 格式的引用——剥出内层 URL。
function stripReferenceToken(value: string): string {
    const m = value.match(/@url:\s*`?(https:\/\/[^`\s]+)`?/) || value.match(/^@?url:?\s*`?(https:\/\/[^`\s]+)`?$/);
    return m ? m[1] : value;
}

async function sdgfImageUrls(references: ReferenceImage[], options?: RequestOptions): Promise<string[]> {
    const urls: string[] = [];
    for (const image of references) {
        // 已是公网 URL（含 @url:`...` token 包裹的，可能在 url 或 dataUrl 字段）直接剥用，不再重新上传
        const rawUrl = typeof (image as { url?: unknown }).url === "string" ? String((image as { url: string }).url) : "";
        const rawData = typeof image.dataUrl === "string" ? image.dataUrl : "";
        const stripped = stripReferenceToken(rawUrl || rawData);
        if (/^https:\/\//.test(stripped)) { urls.push(stripped); continue; }
        const dataUrl = await imageToDataUrl(image);
        const blob = await (await fetch(dataUrl)).blob();
        if (!blob.size) throw new Error("参考图字节为空：" + (image.name || "未命名"));
        const ext = (blob.type.split("/")[1] || "png").replace("jpeg", "jpg");
        const name = encodeURIComponent(image.name || `ref-${urls.length + 1}.${ext}`);
        const res = await axios.post<{ url?: string; error?: string }>(SDGF_ASSET_BRIDGE, blob, { headers: { "X-Filename": name, "Content-Type": "application/octet-stream" }, signal: options?.signal });
        const url = res.data?.url;
        if (typeof url !== "string" || !/^https:\/\//.test(url)) throw new Error(`参考图上云失败：${res.data?.error || JSON.stringify(res.data).slice(0, 120)}`);
        urls.push(url);
    }
    return urls;
}

function sdgfDuration(seconds: string): number {
    // GF 合同 4–30 整数；读原值显式拒绝（clampVideoSeconds 会把 40 吞成 30，禁用它）
    const raw = Math.round(Number(seconds));
    if (!Number.isFinite(raw)) throw new Error(`SD GF 时长不是数字：${seconds}`);
    if (raw < 4 || raw > 30) throw new Error(`SD GF 时长须 4–30 秒（收到 ${seconds}）`);
    return raw;
}

function sdgfAspectRatio(size: string): string {
    const ratio = inferVideoRatio(size || "16:9");
    const mapped = ratio === "auto" ? "adaptive" : ratio;
    if (!["16:9", "9:16", "1:1", "4:3", "3:4", "adaptive"].includes(mapped)) throw new Error(`SD GF 不支持比例 ${mapped}（支持 16:9/9:16/1:1/4:3/3:4/adaptive）`);
    return mapped;
}

function sdgfMode(videoMode: string | undefined, imageCount: number): string {
    // 站方中文 mode 枚举（本期子集：站方 2026-10-22 拒音视频参考，视频编辑不可用）
    const mode = resolveVideoMode(videoMode, imageCount); // "reference" | "frames"
    if (mode === "reference") {
        if (imageCount < 1) throw new Error("参考生视频至少需要 1 张参考图（SD GF 无 0 图参考模式）");
        return "参考生视频";
    }
    if (imageCount === 0) return "文生视频";
    if (imageCount === 1) return "首帧生视频";
    if (imageCount === 2) return "首尾帧";
    return "参考生视频";
}

async function createSdgfVideoTask(config: AiConfig, model: string, prompt: string, references: ReferenceImage[], options?: VideoMediaOptions): Promise<VideoGenerationTask> {
    const images = await sdgfImageUrls(references, options);
    const modelName = modelOptionName(model);
    // 比例：非满血 GF 名（ap/zh 等聚鑫别名）双写 aspect_ratio+metadata.ratio——V2 实测（720×1278 竖屏）；
    // 满血 GF 名只发 aspect_ratio，保持十四章合同原样。
    // 声音：满血 GF 顶层 enableSound:"on" 生效（V3）；ap 系顶层被静默忽略（V6），生效位是 metadata.enableSound（V7）。
    // 时长按代际：SD2.0 系旧合同上限 15 秒，超出截断并提示；SD2.5 全系 4–30 原样（services 层不引 UI，用 console.warn + 任务字段）。
    // 实测依据见 docs/需求-SDGF新模型比例与无声问题-20261105.md §四/§五。
    const ratioFields = sdgfRatioFields(modelName, sdgfAspectRatio(config.size || "16:9"));
    const soundFields = sdgfSoundFields(modelName, boolConfig(config.videoGenerateAudio, true));
    const requestedDuration = sdgfDuration(config.videoSeconds);
    const duration = sdgfClampDuration(modelName, requestedDuration);
    const durationClamped = duration !== requestedDuration;
    if (durationClamped) console.warn(`SD GF：${modelName} 该模型上限 ${duration} 秒，已按 ${duration} 秒提交（请求 ${requestedDuration} 秒）`);
    const body: Record<string, unknown> = {
        model: modelName,
        prompt,
        duration,
        aspect_ratio: ratioFields.aspect_ratio,
        ...(ratioFields.metadata ? { metadata: ratioFields.metadata } : {}),
        mode: sdgfMode(config.videoMode, images.length),
        ...(soundFields.enableSound ? { enableSound: soundFields.enableSound } : {}),
    };
    // ap 系声音的生效位在 metadata：与比例的 metadata 共用同一对象，合并而非覆盖
    if (soundFields.metadata) body.metadata = { ...(ratioFields.metadata || {}), ...soundFields.metadata };
    if (images.length) body.images = images;
    let created: unknown;
    try {
        created = (await axios.post(aiApiUrl(config, "/videos"), body, { headers: aiHeaders(config, "application/json"), signal: options?.signal, timeout: 90000 })).data;
    } catch (error) {
        // 排障透出：响应体全文（含聚鑫 Request ID）+ 请求体快照（prompt 截断），一次重试即可定位
        let detail = readAxiosError(error, "SD GF 创建请求失败（状态不明时勿重复提交，先到站内控制台核对）");
        if (axios.isAxiosError(error) && error.response?.data !== undefined) {
            try { detail += ` | 响应体: ${JSON.stringify(error.response.data).slice(0, 400)}`; } catch { /* 不可序列化则略 */ }
        }
        const snapshot = { ...body, prompt: typeof body.prompt === "string" ? body.prompt.slice(0, 80) + `…(共${body.prompt.length}字)` : body.prompt };
        throw new Error(`${detail} | 请求体: ${JSON.stringify(snapshot)}`);
    }
    const record = (created || {}) as Record<string, unknown>;
    const id = record.id || record.task_id;
    if (typeof id !== "string" || !id) throw new Error("SD GF 未返回任务 ID（提交状态不明——到站内控制台核对，勿重复提交）");
    return { id, provider: "sdgf", model, ...(durationClamped ? { durationClamped: true } : {}) };
}

async function pollSdgfVideoTask(config: AiConfig, task: VideoGenerationTask, options?: RequestOptions): Promise<VideoGenerationTaskState> {
    try {
        const raw = (await axios.get<Record<string, unknown>>(aiApiUrl(config, `/videos/${task.id}`), { headers: aiHeaders(config), signal: options?.signal })).data;
        const status = String(raw?.status || "");
        const metadata = (raw?.metadata || {}) as Record<string, unknown>;
        // 聚鑫实测：metadata.url 可能是相对路径（/v1/videos/{id}/content）——只有完整 http(s) 才直连，否则走鉴权 content 端点
        const rawUrl = typeof metadata.url === "string" ? metadata.url : "";
        const url = /^https?:\/\//.test(rawUrl) ? rawUrl : "";
        if (status === "completed") {
            if (url) return { status: "completed", result: await videoResultFromUrl(url, options) };
            const content = await axios.get<Blob>(aiApiUrl(config, `/videos/${task.id}/content`), { headers: aiHeaders(config), responseType: "blob", signal: options?.signal });
            await assertVideoBlob(content.data);
            return { status: "completed", result: { blob: content.data } };
        }
        if (status === "failed" || status === "cancelled") {
            const err = (raw?.error || {}) as { message?: string };
            return { status: "failed", error: readApiErrorMessage(err.message) || "SD GF 生成失败" };
        }
        return { status: "pending" };
    } catch (error) {
        throw new Error(readAxiosError(error, "SD GF 任务查询失败（沿用同一任务 ID 重查，勿重新创建）"));
    }
}

// ---- 万相 CLI（宿主 bridge，wan-repro）-------------------------------------------
// 合同（bridge 侧并行开发，三端点固定）：
//   POST {base}/api/wan/video/run    body {prompt, images:[{name,dataUrl}], duration, ratio, resolution, audio, confirm:true} → {job, taskId}
//   GET  {base}/api/wan/video/job/{job}   → {status: running|done|failed, stage, elapsed, error?}
//   GET  {base}/api/wan/video/file/{taskId} → video/mp4
// 本机 bridge 走画布 vite proxy 同域相对路径（/api → 127.0.0.1:8779），免 CORS、公网隧道部署同样可用；
// baseUrl 是非回环地址（远程 bridge）时才绝对直连。SDGF 专用校验（时长 4–30 硬拒、比例白名单、mode 枚举）对本分支不生效，
// 参数直接从画布配置映射；两段式确认由画布生成流程自身承担，confirm 恒 true。
// videoTaskId 透存 jobId；taskId（成片文件 ID）只随 run 返回一次——job→taskId 映射落 localStorage，
// 页面刷新后的重查仍能拉到成片（pluginVideoResults 的纯内存 Map 刷新即失效，这里必须活过刷新）。

const WAN_FILE_TASK_STORE_KEY = "wan_video_file_tasks";
const wanVideoFileTasks = new Map<string, string>(); // job → taskId

function rememberWanFileTask(job: string, taskId: string) {
    if (!job || !taskId) return;
    wanVideoFileTasks.set(job, taskId);
    try {
        if (typeof localStorage === "undefined") return;
        localStorage.setItem(WAN_FILE_TASK_STORE_KEY, JSON.stringify(Object.fromEntries([...wanVideoFileTasks.entries()].slice(-200))));
    } catch {
        // 隐私模式等存不进就算了——本会话 Map 仍可用
    }
}

function recallWanFileTask(job: string): string {
    if (wanVideoFileTasks.has(job)) return wanVideoFileTasks.get(job) || "";
    try {
        if (typeof localStorage !== "undefined") {
            const raw = localStorage.getItem(WAN_FILE_TASK_STORE_KEY);
            const parsed: unknown = raw ? JSON.parse(raw) : null;
            if (parsed && typeof parsed === "object") for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) wanVideoFileTasks.set(key, String(value));
        }
    } catch {
        // 坏载荷当不存在
    }
    return wanVideoFileTasks.get(job) || "";
}

// 回环 bridge（含默认 127.0.0.1:8779）→ 相对路径走 vite proxy /api；远程 bridge → 绝对地址直连
function wanVideoUrl(config: Pick<AiConfig, "baseUrl">, path: string) {
    const base = config.baseUrl.trim().replace(/\/+$/, "");
    const isLoopback = !base || /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$/i.test(base);
    return withLocalProxy(`${isLoopback ? "" : base}/api/wan/video${path}`);
}

function assertWanVideoConfig(config: AiConfig, model: string) {
    // wan 不校验 API Key（key 可任意留空）；只认模型 + bridge 地址
    if (!model) throw new Error(apiText("videoModelRequired"));
    if (!config.baseUrl.trim()) throw new Error(apiText("wanBridgeRequired"));
}

function wanDuration(seconds: string): number {
    return Number(clampVideoSeconds(seconds)) || 6;
}

async function createWanVideoTask(config: AiConfig, model: string, prompt: string, references: ReferenceImage[], options?: VideoMediaOptions): Promise<VideoGenerationTask> {
    assertWanVideoConfig(config, model);
    const images = await Promise.all(references.map(async (image, index) => ({ name: image.name || `ref-${index + 1}.png`, dataUrl: await imageToDataUrl(image) })));
    const body: Record<string, unknown> = {
        prompt,
        images,
        duration: wanDuration(config.videoSeconds),
        ratio: videoAspectRatio(config.size || "16:9"),
        resolution: normalizeVideoResolution(config.vquality).replace(/p$/i, ""), // wan 契约：数字（"720"），不带 p 后缀（bridge WAN_RESOLUTIONS 键）
        audio: boolConfig(config.videoGenerateAudio, true),
        confirm: true,
    };
    let created: unknown;
    try {
        created = (await axios.post<Record<string, unknown>>(wanVideoUrl(config, "/run"), body, { headers: { "Content-Type": "application/json" }, signal: options?.signal, timeout: 300000 })).data;
    } catch (error) {
        // 与 SDGF 同款排障透出：响应体全文 + 请求体快照（images 摘要、prompt 截断）——run 触发真实生成，状态不明时勿重复提交
        let detail = readAxiosError(error, apiText("wanVideoCreateFailed"));
        if (axios.isAxiosError(error) && error.response?.data !== undefined) {
            try { detail += ` | 响应体: ${JSON.stringify(error.response.data).slice(0, 400)}`; } catch { /* 不可序列化则略 */ }
        }
        const snapshot = { ...body, images: `${images.length} 张`, prompt: `${prompt.slice(0, 80)}…(共${prompt.length}字)` };
        throw new Error(`${detail} | 请求体: ${JSON.stringify(snapshot)}`);
    }
    const record = (created || {}) as Record<string, unknown>;
    const job = typeof record.job === "string" ? record.job.trim() : "";
    const fileTaskId = typeof record.taskId === "string" ? record.taskId.trim() : "";
    if (!job) throw new Error(apiText("wanVideoNoJob"));
    rememberWanFileTask(job, fileTaskId);
    return { id: job, provider: "wan", model };
}

async function pollWanVideoTask(config: AiConfig, task: VideoGenerationTask, options?: RequestOptions): Promise<VideoGenerationTaskState> {
    try {
        const raw = (await axios.get<Record<string, unknown>>(wanVideoUrl(config, `/job/${encodeURIComponent(task.id)}`), { signal: options?.signal })).data;
        const status = String(raw?.status || "");
        if (status === "failed") return { status: "failed", error: readApiErrorMessage(raw?.error) || apiText("wanVideoFailed") };
        if (status === "done") {
            const fileTaskId = recallWanFileTask(task.id);
            if (!fileTaskId) throw new Error(apiText("wanVideoFileMissing"));
            const content = await axios.get<Blob>(wanVideoUrl(config, `/file/${encodeURIComponent(fileTaskId)}`), { responseType: "blob", signal: options?.signal });
            await assertVideoBlob(content.data);
            return { status: "completed", result: { blob: content.data } };
        }
        return { status: "pending" };
    } catch (error) {
        throw new Error(readAxiosError(error, apiText("wanVideoQueryFailed")));
    }
}

export async function storeGeneratedVideo(result: VideoGenerationResult): Promise<UploadedFile> {
    if (result.blob) return uploadMediaFile(result.blob, "video");
    if (result.url) {
        try {
            return await uploadMediaFile(result.url, "video");
        } catch {
            return { url: result.url, storageKey: "", bytes: 0, mimeType: result.mimeType || "video/mp4" };
        }
    }
    throw new Error(apiText("noPlayableVideo"));
}

async function createOpenAIVideoTask(config: AiConfig, model: string, prompt: string, references: ReferenceImage[], options?: VideoMediaOptions): Promise<VideoGenerationTask> {
    const images = await Promise.all(references.map(async (image) => dataUrlToFile({ ...image, dataUrl: await imageToDataUrl(image) })));
    const videos = await Promise.all((options?.videos || []).map((video) => referenceMediaToFile(video, "ref.mp4", "invalidReferenceVideo", options)));
    const audios = await Promise.all((options?.audios || []).map((audio) => referenceMediaToFile(audio, "ref.mp3", "invalidReferenceAudio", options)));
    const mode = resolveVideoMode(config.videoMode, images.length);
    const ratio = inferVideoRatio(config.size || "16:9");
    const body = new FormData();
    body.append("model", modelOptionName(model));
    body.append("prompt", prompt);
    body.append("seconds", normalizeVideoSeconds(config.videoSeconds));
    body.append("size", normalizeVideoSize(config.size, config.vquality) || "1280x720");
    if (ratio && ratio !== "auto") {
        body.append("ratio", ratio);
    }
    body.append("resolution_name", normalizeVideoResolution(config.vquality));
    body.append("generate_audio", String(boolConfig(config.videoGenerateAudio, true)));
    body.append("watermark", String(boolConfig(config.videoWatermark, false)));
    body.append("mode", mode);
    if (mode === "frames") {
        if (images[0]) {
            body.append("first_frame", images[0], "first.png");
            body.append("images", images[0], "first.png");
        }
        if (images[1]) {
            body.append("last_frame", images[1], "last.png");
            body.append("images", images[1], "last.png");
        }
    } else {
        images.forEach((file, idx) => {
            body.append("image[]", file, `ref_${idx + 1}.png`);
            body.append("images", file, `ref_${idx + 1}.png`);
        });
    }
    videos.forEach((file) => body.append("video[]", file));
    audios.forEach((file) => body.append("audio[]", file));
    try {
        const created = unwrapVideoResponse((await axios.post<ApiVideoResponse>(aiApiUrl(config, "/videos"), body, { headers: aiHeaders(config), signal: options?.signal })).data);
        if (!created.id) throw new Error(apiText("noVideoTaskId"));
        return { id: created.id, provider: "openai", model };
    } catch (error) {
        throw new Error(readAxiosError(error, apiText("videoTaskCreateFailed")));
    }
}

async function pollOpenAIVideoTask(config: AiConfig, task: VideoGenerationTask, options?: RequestOptions): Promise<VideoGenerationTaskState> {
    try {
        const video = unwrapVideoResponse((await axios.get<ApiVideoResponse>(aiApiUrl(config, `/videos/${task.id}`), { headers: aiHeaders(config), signal: options?.signal })).data);
        const url = videoResultUrl(video);
        if (url) return { status: "completed", result: await videoResultFromUrl(url, options) };
        if (video.status === "completed") {
            const content = await axios.get<Blob>(aiApiUrl(config, `/videos/${task.id}/content`), { headers: aiHeaders(config), responseType: "blob", signal: options?.signal });
            await assertVideoBlob(content.data);
            return { status: "completed", result: { blob: content.data } };
        }
        if (video.status === "failed" || video.status === "cancelled") return { status: "failed", error: readApiErrorMessage(video.error?.message) || apiText("videoGenerationFailed") };
        return { status: "pending" };
    } catch (error) {
        throw new Error(readAxiosError(error, apiText("videoTaskQueryFailed")));
    }
}

async function videoResultFromUrl(url: string, options?: RequestOptions): Promise<VideoGenerationResult> {
    try {
        const response = await axios.get<Blob>(withLocalProxy(url), { responseType: "blob", signal: options?.signal });
        await assertVideoBlob(response.data);
        return { blob: response.data };
    } catch (error) {
        if (axios.isCancel(error) || options?.signal?.aborted) throw error;
        return { url, mimeType: "video/mp4" };
    }
}

async function createGeminiVideoTask(config: AiConfig, model: string, prompt: string, references: ReferenceImage[], options?: VideoMediaOptions): Promise<VideoGenerationTask> {
    const images = await Promise.all(references.map((image) => imageToDataUrl(image)));
    const videos = await Promise.all((options?.videos || []).map((video) => referenceMediaToFile(video, "ref.mp4", "invalidReferenceVideo", options)));
    const audios = await Promise.all((options?.audios || []).map((audio) => referenceMediaToFile(audio, "ref.mp3", "invalidReferenceAudio", options)));
    const mode = resolveVideoMode(config.videoMode, images.length);
    const instance: Record<string, unknown> = { prompt };
    if (mode === "frames") {
        if (images[0]) instance.image = parseDataUrlInline(images[0]);
        if (images[1]) instance.lastFrame = parseDataUrlInline(images[1]);
    } else {
        instance.referenceImages = images.map((dataUrl) => ({ image: parseDataUrlInline(dataUrl), referenceType: "asset" }));
    }
    if (videos[0]) instance.video = await fileToGeminiInline(videos[0]);
    if (audios[0]) instance.audio = await fileToGeminiInline(audios[0]);
    try {
        const created = unwrapEnvelope((await axios.post<ApiEnvelope<GeminiVideoOperation>>(geminiVideoUrl(config, model, "predictLongRunning"), {
            instances: [instance],
            parameters: {
                aspectRatio: videoAspectRatio(config.size),
                durationSeconds: Number(normalizeVideoSeconds(config.videoSeconds)) || 8,
                resolution: normalizeVideoResolution(config.vquality),
                generateAudio: boolConfig(config.videoGenerateAudio, true),
                addWatermark: boolConfig(config.videoWatermark, false),
            },
        }, { headers: geminiVideoHeaders(config), signal: options?.signal })).data, apiText("noVideoTask"));
        if (!created.name) throw new Error(apiText("noVideoTaskId"));
        return { id: created.name, provider: "gemini", model };
    } catch (error) {
        throw new Error(readAxiosError(error, apiText("videoTaskCreateFailed")));
    }
}

async function pollGeminiVideoTask(config: AiConfig, task: VideoGenerationTask, options?: RequestOptions): Promise<VideoGenerationTaskState> {
    try {
        const state = unwrapEnvelope((await axios.get<ApiEnvelope<GeminiVideoOperation>>(geminiOperationUrl(config, task.id), { headers: geminiVideoHeaders(config), signal: options?.signal })).data, apiText("videoTaskQueryFailed"));
        if (state.error) return { status: "failed", error: readApiErrorMessage(state.error.message) || apiText("videoGenerationFailed") };
        if (!state.done) return { status: "pending" };
        const uri = state.response?.generateVideoResponse?.generatedSamples?.[0]?.video?.uri;
        if (!uri) return { status: "failed", error: apiText("noPlayableVideo") };
        const url = uri.includes("key=") ? uri : `${uri}${uri.includes("?") ? "&" : "?"}key=${config.apiKey}`;
        return { status: "completed", result: await videoResultFromUrl(url, options) };
    } catch (error) {
        throw new Error(readAxiosError(error, apiText("videoTaskQueryFailed")));
    }
}

function assertVideoConfig(config: AiConfig, model: string) {
    if (!model) throw new Error(apiText("videoModelRequired"));
    if (!config.baseUrl.trim()) throw new Error(apiText("baseUrlRequired"));
    if (!config.apiKey.trim()) throw new Error(apiText("apiKeyRequired"));
}

function geminiVideoBaseUrl(config: Pick<AiConfig, "baseUrl">) {
    const normalizedBaseUrl = config.baseUrl.trim().replace(/\/+$/, "");
    const lowerBaseUrl = normalizedBaseUrl.toLowerCase();
    return lowerBaseUrl.endsWith("/v1") || lowerBaseUrl.endsWith("/v1beta") ? normalizedBaseUrl : `${normalizedBaseUrl}/v1beta`;
}

function geminiVideoUrl(config: Pick<AiConfig, "baseUrl">, model: string, action: string) {
    return withLocalProxy(`${geminiVideoBaseUrl(config)}/models/${encodeURIComponent(modelOptionName(model).replace(/^models\//, ""))}:${action}`);
}

function geminiOperationUrl(config: Pick<AiConfig, "baseUrl">, name: string) {
    return withLocalProxy(`${geminiVideoBaseUrl(config)}/${name.replace(/^\//, "")}`);
}

function geminiVideoHeaders(config: Pick<AiConfig, "apiKey">) {
    return { "x-goog-api-key": config.apiKey, "Content-Type": "application/json" };
}

function videoAspectRatio(size: string) {
    const ratio = inferVideoRatio(size);
    return ratio === "auto" ? "16:9" : ratio;
}

function parseDataUrlInline(dataUrl: string, fallbackType = "image/png"): GeminiInlineData {
    const match = dataUrl.match(/^data:([^;]+);base64,(.*)$/);
    return { bytesBase64Encoded: match?.[2] || "", mimeType: match?.[1] || fallbackType };
}

async function fileToGeminiInline(file: File): Promise<GeminiInlineData> {
    return parseDataUrlInline(await readFileAsDataUrl(file), file.type || "application/octet-stream");
}

async function referenceMediaToFile(item: { name: string; type?: string; url?: string; storageKey?: string }, fallbackName: string, errorKey: "invalidReferenceVideo" | "invalidReferenceAudio", options?: RequestOptions) {
    let blob = item.storageKey ? await getMediaBlob(item.storageKey) : null;
    if (!blob) {
        const url = item.storageKey ? await resolveMediaUrl(item.storageKey, item.url || "") : item.url || "";
        if (!url) throw new Error(apiText(errorKey));
        try {
            blob = await (await fetch(url, { signal: options?.signal })).blob();
        } catch (error) {
            if (error instanceof DOMException && error.name === "AbortError") throw error;
            throw new Error(apiText(errorKey));
        }
    }
    if (!blob.size) throw new Error(apiText(errorKey));
    return new File([blob], item.name || fallbackName, { type: item.type || blob.type || "application/octet-stream" });
}

function normalizeVideoSeconds(value: string) {
    return clampVideoSeconds(value);
}

function resolveVideoMode(mode: string | undefined, imageCount: number) {
    if (mode === "reference" || imageCount > 2) return "reference";
    return "frames";
}

function normalizeVideoSize(value: string, resolution?: string) {
    if (value === "auto") return null;
    if (/^\d+x\d+$/.test(value || "")) return value;
    const ratio = inferVideoRatio(value || "16:9");
    if (ratio === "auto") return null;
    return computeVideoSize(resolution || "720", ratio);
}

function normalizeVideoResolution(value: string) {
    if (value === "low") return "480p";
    if (value === "auto" || value === "high" || value === "medium") return "720p";
    const resolution = value.replace(/p$/i, "") || "720";
    return `${resolution}p`;
}

function unwrapVideoResponse(payload: ApiVideoResponse) {
    return unwrapEnvelope(payload, apiText("noVideoTask"));
}

function unwrapEnvelope<T>(payload: ApiEnvelope<T>, emptyMessage: string): T {
    if (!payload) throw new Error(emptyMessage);
    if (typeof payload === "object" && "code" in payload && payload.code !== undefined) {
        if (payload.code !== 0 && payload.code !== "0") throw new Error(readApiErrorMessage(payload) || apiText("requestFailed"));
        if (!payload.data) throw new Error(emptyMessage);
        return payload.data;
    }
    return payload as T;
}

function videoResultUrl(payload: VideoResponse) {
    return [payload.video_url, payload.result_url, payload.url, payload.content?.video_url, payload.content?.url].find((url) => typeof url === "string" && (isPublicMediaUrl(url) || /\.mp4(\?|#|$)/i.test(url)));
}

function readApiErrorMessage(value: unknown): string {
    if (!value) return "";
    if (typeof value === "string") {
        try {
            const parsed = JSON.parse(value);
            const inner = readApiErrorMessage(parsed) || value;
            if (inner === value && typeof parsed === "object" && Object.keys(parsed).length === 0) return "";
            return inner;
        } catch {
            if (/<[a-z][\s\S]*>/i.test(value)) return apiText("htmlError", { preview: `${value.slice(0, 80)}...` });
            return value;
        }
    }
    if (typeof value !== "object") return "";
    const payload = value as { msg?: unknown; message?: unknown; error?: unknown; detail?: unknown };
    // error may be a string or an object containing a message.
    const errorMsg =
        typeof payload.error === "string"
            ? payload.error
            : (payload.error as { message?: unknown })?.message;
    return (
        readApiErrorMessage(payload.msg) ||
        readApiErrorMessage(payload.message) ||
        readApiErrorMessage(errorMsg) ||
        readApiErrorMessage(payload.detail) ||
        ""
    );
}

function readAxiosError(error: unknown, fallback: string) {
    if (axios.isCancel(error)) return apiText("requestCanceled");
    if (axios.isAxiosError<{ error?: { message?: string }; msg?: string; message?: string; code?: number | string }>(error)) {
        if (!error.response && error.code === "ERR_NETWORK") return apiText("requestFailed");
        const responseData = error.response?.data;
        return readApiErrorMessage(responseData) || statusMessage(error.response?.status, fallback);
    }
    if (error instanceof DOMException && error.name === "AbortError") return apiText("requestCanceled");
    return error instanceof Error ? readApiErrorMessage(error.message) || error.message : fallback;
}

function statusMessage(status: number | undefined, fallback: string) {
    if (status === 401 || status === 403) return apiText("authenticationFailed");
    if (status === 429) return apiText("rateLimited");
    return status ? `${fallback}（${status}）` : fallback;
}

async function assertVideoBlob(blob: Blob) {
    if (!blob.type.includes("json")) return;
    let payload: { code?: number; msg?: string; error?: { message?: string } };
    try {
        payload = JSON.parse(await blob.text()) as { code?: number; msg?: string; error?: { message?: string } };
    } catch {
        return;
    }
    if (typeof payload.code === "number" && payload.code !== 0) throw new Error(readApiErrorMessage(payload) || apiText("videoDownloadFailed"));
    if (payload.error?.message) throw new Error(readApiErrorMessage(payload.error.message) || payload.error.message);
}

function isPublicMediaUrl(value: string) {
    return /^https?:\/\//i.test(value || "");
}

function delay(ms: number, signal?: AbortSignal) {
    return new Promise<void>((resolve, reject) => {
        if (signal?.aborted) {
            reject(new DOMException("Aborted", "AbortError"));
            return;
        }
        const timer = setTimeout(resolve, ms);
        signal?.addEventListener(
            "abort",
            () => {
                clearTimeout(timer);
                reject(new DOMException("Aborted", "AbortError"));
            },
            { once: true },
        );
    });
}
