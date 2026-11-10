import { LoaderCircle, Sparkles, Upload, Video } from "lucide-react";
import { App, Button, Input } from "antd";
import { useRef, useState, type DragEvent } from "react";

import { formatBytes } from "@/lib/image-utils";
import { ingestCreatedMessage, ingestHitMessage, isIngestHit } from "@/lib/hub/ingest-hit";
import { errorText, ingestVideo, startLapianRun, type HubTaskSummary } from "./hub-api";

const VIDEO_PATTERN = /\.(mp4|mov|webm|mkv|avi|m4v)$/i;

/** 顶部新建卡片：拖参考视频 + 写意图 → 建任务并触发拆解。
 *  AT-10 C5：幂等命中已有任务时点名提示并刷新列表（不重触发拆解、不重复插卡）。 */
export function NewTaskCard({ onCreated, onSubmitted }: { onCreated: (task: HubTaskSummary) => void; onSubmitted: () => void }) {
    const { message } = App.useApp();
    const inputRef = useRef<HTMLInputElement>(null);
    const dragDepthRef = useRef(0);
    const [file, setFile] = useState<File | null>(null);
    const [intent, setIntent] = useState("");
    const [busy, setBusy] = useState(false);
    const [dragActive, setDragActive] = useState(false);

    const pickFile = (files?: FileList | null) => {
        const next = Array.from(files || []).find((item) => item.type.startsWith("video/") || VIDEO_PATTERN.test(item.name));
        if (!next) {
            message.warning("请选择视频文件（mp4 / mov / webm）");
            return;
        }
        setFile(next);
    };

    const handleDragEnter = (event: DragEvent<HTMLDivElement>) => {
        event.preventDefault();
        dragDepthRef.current += 1;
        if (event.dataTransfer.types.includes("Files")) setDragActive(true);
    };

    const handleDragLeave = (event: DragEvent<HTMLDivElement>) => {
        event.preventDefault();
        dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
        if (!dragDepthRef.current) setDragActive(false);
    };

    const handleDrop = (event: DragEvent<HTMLDivElement>) => {
        event.preventDefault();
        dragDepthRef.current = 0;
        setDragActive(false);
        pickFile(event.dataTransfer.files);
    };

    const createTask = async () => {
        const text = intent.trim();
        if (!file || !text || busy) return;
        setBusy(true);
        try {
            const created = await ingestVideo(file, text);
            if (!created.local_id) throw new Error("建任务返回缺少 local_id");
            // AT-10 C5：幂等命中已有任务 → 点名提示并刷新列表让它出现（不重触发拆解、不重复插卡）
            if (isIngestHit(created)) {
                message.info(ingestHitMessage(created.local_id, created.video_name || file.name));
                setFile(null);
                setIntent("");
                onSubmitted();
                return;
            }
            if (!created.bridge_id) throw new Error("建任务返回缺少 local_id / bridge_id");
            onCreated({
                local_id: created.local_id,
                state: "ingested",
                intent: text,
                created_at: new Date().toISOString(),
                video_name: file.name,
                gen_progress: null,
                has_output: false,
            });
            setFile(null);
            setIntent("");
            try {
                await startLapianRun(created.local_id, text);
                message.success(ingestCreatedMessage(created.local_id));
            } catch (error) {
                message.warning(`任务 ${created.local_id} 已建立，但触发拆解失败：${errorText(error)}`);
            }
            onSubmitted();
        } catch (error) {
            message.error(`建任务失败：${errorText(error)}`);
        } finally {
            setBusy(false);
        }
    };

    return (
        <section className="rounded-lg border border-stone-200 bg-card p-4 shadow-sm dark:border-stone-800">
            <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
                <h2 className="text-base font-semibold">新建任务</h2>
                <p className="text-xs text-stone-500 dark:text-stone-400">拖一条参考片、写清改造意图；建好后自动开始拆解（1–3 分钟）。</p>
            </div>

            <div className="grid gap-3 lg:grid-cols-[minmax(0,320px)_minmax(0,1fr)_auto] lg:items-end">
                <div className="min-w-0">
                    <div className="mb-1.5 text-xs font-medium text-stone-500 dark:text-stone-400">① 参考视频</div>
                    <div
                        role="button"
                        tabIndex={0}
                        className={`flex min-h-20 cursor-pointer flex-col items-center justify-center gap-1.5 rounded-lg border border-dashed px-3 py-3 text-center transition ${
                            dragActive ? "border-stone-900 bg-stone-100/80 dark:border-stone-100 dark:bg-stone-900/80" : "border-stone-300 bg-stone-50/60 hover:border-stone-400 dark:border-stone-700 dark:bg-stone-900/40 dark:hover:border-stone-500"
                        }`}
                        onClick={() => inputRef.current?.click()}
                        onKeyDown={(event) => {
                            if (event.key === "Enter" || event.key === " ") {
                                event.preventDefault();
                                inputRef.current?.click();
                            }
                        }}
                        onDragEnter={handleDragEnter}
                        onDragOver={(event) => {
                            event.preventDefault();
                            event.dataTransfer.dropEffect = "copy";
                        }}
                        onDragLeave={handleDragLeave}
                        onDrop={handleDrop}
                    >
                        {file ? (
                            <>
                                <div className="flex min-w-0 items-center gap-2 text-sm font-medium">
                                    <Video className="size-4 shrink-0" />
                                    <span className="truncate">{file.name}</span>
                                </div>
                                <div className="text-xs text-stone-500 dark:text-stone-400">{formatBytes(file.size)} · 点击可更换</div>
                            </>
                        ) : (
                            <>
                                <Upload className="size-5 text-stone-400" />
                                <div className="text-sm font-medium">{dragActive ? "松开即选此视频" : "拖入参考视频，或点击选择"}</div>
                                <div className="text-xs text-stone-500 dark:text-stone-400">mp4 / mov / webm</div>
                            </>
                        )}
                    </div>
                </div>

                <div className="min-w-0">
                    <div className="mb-1.5 text-xs font-medium text-stone-500 dark:text-stone-400">② 改造意图</div>
                    <Input value={intent} onChange={(event) => setIntent(event.target.value)} onPressEnter={() => void createTask()} placeholder="例如：把片中的产品换成我们的新品，保留原片节奏、运镜和字幕样式" className="!h-11" />
                </div>

                <Button
                    type="primary"
                    size="large"
                    className="!h-11"
                    icon={busy ? <LoaderCircle className="size-4 animate-spin" /> : <Sparkles className="size-4" />}
                    loading={busy}
                    disabled={!file || !intent.trim() || busy}
                    onClick={() => void createTask()}
                >
                    建任务并拆解
                </Button>
            </div>

            <input
                ref={inputRef}
                type="file"
                accept="video/*"
                className="hidden"
                onChange={(event) => {
                    pickFile(event.target.files);
                    event.target.value = "";
                }}
            />
        </section>
    );
}
