import { saveAs } from "file-saver";

import i18n from "@/i18n";
import { createZip } from "@/lib/zip";
import { getMediaBlob } from "@/services/file-storage";
import { getImageBlob } from "@/services/image-storage";
import type { CanvasProject } from "@/stores/canvas/use-canvas-store";
import { CanvasNodeType, type CanvasNodeData } from "@/types/canvas";
import { buildCanvasExport, buildCanvasTemplateExport, collectStorageKeys, fileExtension, safeFileName, type CanvasAssetReader } from "@/lib/canvas/canvas-export-builder";

export { buildCanvasExport, buildCanvasTemplateExport };
export type { CanvasAssetReader, CanvasExportBuild, CanvasExportMissingAsset } from "@/lib/canvas/canvas-export-builder";

/** 生产环境字节读取器：image: 前缀走图片库，其余走媒体库（与旧实现同一分支口径）。 */
export const defaultCanvasAssetReader: CanvasAssetReader = (storageKey) => (storageKey.startsWith("image:") ? getImageBlob(storageKey) : getMediaBlob(storageKey));

export async function exportCanvasProjects(projects: CanvasProject[], fileName = i18n.t("canvas.export.defaultProjectName")) {
    const { file, zipFiles, missing } = await buildCanvasExport(projects, defaultCanvasAssetReader);
    // 既有整项目导出保持旧行为（缺字节不阻断），只把缺失打日志留给排障。
    if (missing.length) console.warn("[canvas-export] 缺素材字节", missing);
    const zip = await createZip([{ name: "projects.json", data: JSON.stringify(file, null, 2) }, ...zipFiles]);
    saveAs(zip, `${safeFileName(fileName)}.zip`);
    return { missing };
}

export type CanvasTemplateExportResult = { fileName: string; bytes: number; missing: Awaited<ReturnType<typeof buildCanvasTemplateExport>>["missing"]; assetCount: number };

/** 模板导出落盘（浏览器侧，P2）。缺字节 → console 明示 + 返回报告，绝不静默跳过；不新增页面（P3 走薄壳 API/侧栏）。 */
export async function exportCanvasTemplate(project: CanvasProject, fileName?: string): Promise<CanvasTemplateExportResult> {
    const { file, zipFiles, missing } = await buildCanvasTemplateExport(project, defaultCanvasAssetReader);
    if (missing.length) console.warn("[canvas-template-export] 缺素材字节（已写入报告，未静默跳过）", missing);
    const zip = await createZip([{ name: "projects.json", data: JSON.stringify(file, null, 2) }, ...zipFiles]);
    const name = fileName || project.workflowTemplate?.name || project.title || i18n.t("canvas.export.defaultProjectName");
    saveAs(zip, `${safeFileName(name)}.zip`);
    return { fileName: `${safeFileName(name)}.zip`, bytes: zip.size, missing, assetCount: file.projects[0]?.files.length || 0 };
}

export async function exportCanvasNodes(nodes: CanvasNodeData[], fileName = i18n.t("canvas.export.defaultNodesName")) {
    const zipFiles: { name: string; data: BlobPart }[] = [];
    const used = new Set<string>();
    const uniqueName = (base: string, ext: string) => {
        const safe = safeFileName(base) || i18n.t("canvas.export.item");
        let name = `${safe}.${ext}`;
        for (let i = 1; used.has(name); i += 1) name = `${safe}-${i}.${ext}`;
        used.add(name);
        return name;
    };

    await Promise.all(
        nodes.map(async (node) => {
            const title = node.title || node.type;
            const storageKey = node.metadata?.storageKey || "";
            if (storageKey) {
                const blob = storageKey.startsWith("image:") ? await getImageBlob(storageKey) : await getMediaBlob(storageKey);
                if (blob) return void zipFiles.push({ name: uniqueName(title, fileExtension(blob.type, storageKey)), data: blob });
            }
            if (node.type === CanvasNodeType.Text) return void zipFiles.push({ name: uniqueName(title, "txt"), data: node.metadata?.content || node.metadata?.prompt || "" });
            const content = node.metadata?.content;
            if (content && content.startsWith("data:")) {
                const blob = await (await fetch(content)).blob();
                return void zipFiles.push({ name: uniqueName(title, fileExtension(blob.type, storageKey)), data: blob });
            }
            zipFiles.push({ name: uniqueName(title, "json"), data: JSON.stringify(node, null, 2) });
        }),
    );

    const zip = await createZip(zipFiles);
    saveAs(zip, `${safeFileName(fileName)}.zip`);
}

