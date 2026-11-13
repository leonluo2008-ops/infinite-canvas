/**
 * v3 导出组装（纯函数 + 可注入字节读取器）：模板导出与既有整项目导出共用，禁第二套信封（合同 C1）。
 *
 * 为什么单独一层：`canvas-export.ts` 依赖 file-saver / i18n / IndexedDB 存储服务，属浏览器层；
 * 组装逻辑（收集 storageKey、缺字节报告）是纯逻辑，拆出来才能被探针/单测直接打包真跑（与 P1 的
 * `lib/agent/agent-node-text.ts` 同口径）。信封类型仍取自 `types/canvas-export.ts`，不新增格式。
 */

import type { CanvasProject } from "@/stores/canvas/use-canvas-store";
import type { CanvasExportAsset, CanvasExportFile } from "@/types/canvas-export";

/** 素材字节读取器（生产 = IndexedDB；探针/单测可注入假 reader）。 */
export type CanvasAssetReader = (storageKey: string) => Promise<Blob | null>;

export type CanvasExportMissingAsset = {
    projectId: string;
    storageKey: string;
    nodeIds: string[];
    reason: string;
};

export type CanvasExportBuild = {
    file: CanvasExportFile;
    zipFiles: { name: string; data: BlobPart }[];
    /** 取不回字节的 storageKey：**显式报告，不静默跳过**（P2 模板导出验收项；旧实现 `if (!blob) return;` 静默丢弃）。 */
    missing: CanvasExportMissingAsset[];
};

/** 组装 v3 导出对象（不落盘、不触发下载）。 */
export async function buildCanvasExport(projects: CanvasProject[], reader: CanvasAssetReader): Promise<CanvasExportBuild> {
    const zipFiles: { name: string; data: BlobPart }[] = [];
    const missing: CanvasExportMissingAsset[] = [];
    const exportedProjects = await Promise.all(
        projects.map(async (project) => {
            const files: CanvasExportAsset[] = [];
            await Promise.all(
                collectStorageKeys(project).map(async (storageKey) => {
                    const blob = await reader(storageKey);
                    if (!blob) {
                        missing.push({
                            projectId: project.id,
                            storageKey,
                            nodeIds: ownersOfStorageKey(project, storageKey),
                            reason: "IndexedDB 里取不到该 storageKey 的字节（媒体已被清理/导入时缺文件）",
                        });
                        return;
                    }
                    const path = `projects/${project.id}/files/${safeFileName(storageKey)}.${fileExtension(blob.type, storageKey)}`;
                    files.push({ storageKey, path, mimeType: blob.type || "application/octet-stream", bytes: blob.size });
                    zipFiles.push({ name: path, data: blob });
                }),
            );
            return { project, files };
        }),
    );

    const file: CanvasExportFile = { app: "infinite-canvas", version: 3, exportedAt: new Date().toISOString(), projects: exportedProjects };
    return { file, zipFiles, missing };
}

/**
 * 模板导出（P2）：模板项目必须自带 `workflowTemplate` 声明（否则说明抽错了对象）且声明为五槽。
 * 缺字节时**返回报告**，调用方（P3 薄壳 API/侧栏）负责明示给用户。
 */
export async function buildCanvasTemplateExport(project: CanvasProject, reader: CanvasAssetReader): Promise<CanvasExportBuild> {
    const template = project.workflowTemplate;
    if (!template) throw new Error("模板导出失败：项目上没有 workflowTemplate 声明（不是模板项目）");
    if (!Array.isArray(template.slots) || template.slots.length !== 5) throw new Error(`模板导出失败：参数声明应为五槽，实际 ${template.slots?.length ?? 0}`);
    return buildCanvasExport([project], reader);
}

/** storageKey 的归属节点（缺字节报告里要能定位到人看得懂的节点标题）。 */
function ownersOfStorageKey(project: CanvasProject, storageKey: string) {
    return project.nodes.filter((node) => node.metadata?.storageKey === storageKey || Boolean(node.metadata?.references?.includes(storageKey))).map((node) => node.id);
}

/** 递归收集项目里所有 storageKey（含 assetSlot/挂图/素材节点；源视频已剥离则自然不出现）。 */
export function collectStorageKeys(value: unknown, keys = new Set<string>()) {
    if (!value || typeof value !== "object") return [...keys];
    if ("storageKey" in value && typeof value.storageKey === "string" && value.storageKey.includes(":")) keys.add(value.storageKey);
    Object.values(value).forEach((item) => (Array.isArray(item) ? item.forEach((child) => collectStorageKeys(child, keys)) : collectStorageKeys(item, keys)));
    return [...keys];
}

export function safeFileName(value: string) {
    return value.replace(/[\\/:*?"<>|]/g, "_");
}

export function fileExtension(mimeType: string, storageKey: string) {
    if (mimeType.includes("png")) return "png";
    if (mimeType.includes("jpeg")) return "jpg";
    if (mimeType.includes("webp")) return "webp";
    if (mimeType.includes("gif")) return "gif";
    if (mimeType.includes("mp4")) return "mp4";
    if (mimeType.includes("webm")) return "webm";
    if (mimeType.includes("mpeg") || mimeType.includes("mp3")) return "mp3";
    if (mimeType.includes("wav")) return "wav";
    if (mimeType.includes("ogg")) return "ogg";
    return storageKey.startsWith("image:") ? "png" : "bin";
}
