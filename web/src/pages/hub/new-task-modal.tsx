import { LoaderCircle, Sparkles, Upload, Video } from "lucide-react";
import { App, Button, Input, Modal } from "antd";
import { useRef, useState, type DragEvent } from "react";

import { formatBytes } from "@/lib/image-utils";
import { errorText, ingestVideo, startLapianRun } from "./hub-api";

const VIDEO_PATTERN = /\.(mp4|mov|webm|mkv|avi|m4v)$/i;

/**
 * 「新建任务」弹窗（AT-9 B3 表格视图顶部入口）。
 * 复用 new-task-card 的建任务逻辑：选/拖参考视频 + 意图 → ingest → 自动触发拆解。
 */
export function NewTaskModal({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (localId: string) => void }) {
    const { message } = App.useApp();
    const inputRef = useRef<HTMLInputElement>(null);
    const dragDepthRef = useRef(0);
    const [file, setFile] = useState<File | null>(null);
    const [intent, setIntent] = useState("");
    const [busy, setBusy] = useState(false);
    const [dragActive, setDragActive] = useState(false);

    const reset = () => {
        setFile(null);
        setIntent("");
        setBusy(false);
        setDragActive(false);
    };

    const pickFile = (files?: FileList | null) => {
        const next = Array.from(files || []).find((item) => item.type.startsWith("video/") || VIDEO_PATTERN.test(item.name));
        if (!next) {
            message.warning("请选择视频文件（mp4 / mov / webm）");
            return;
        }
        setFile(next);
    };

    const createTask = async () => {
        const text = intent.trim();
        if (!file || !text || busy) return;
        setBusy(true);
        try {
            const created = await ingestVideo(file, text);
            if (!created.local_id || !created.bridge_id) throw new Error("建任务返回缺少 local_id / bridge_id");
            try {
                await startLapianRun(created.local_id, text);
                message.success(`任务 ${created.local_id} 已建立，开始拆解`);
            } catch (error) {
                message.warning(`任务 ${created.local_id} 已建立，但触发拆解失败：${errorText(error)}`);
            }
            onCreated(created.local_id);
            reset();
            onClose();
        } catch (error) {
            message.error(`建任务失败：${errorText(error)}`);
        } finally {
            setBusy(false);
        }
    };

    return (
        <Modal
            title="新建任务"
            open={open}
            onCancel={() => {
                reset();
                onClose();
            }}
            footer={null}
            width={560}
            maskClosable={!busy}
        >
            <div className="space-y-4 pt-1">
                <div className="text-xs text-stone-500 dark:text-stone-400">拖一条参考片、写清改造意图；建好后自动开始拆解（1–3 分钟）。</div>

                <div>
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
                        onDragEnter={(event) => {
                            event.preventDefault();
                            dragDepthRef.current += 1;
                            if (event.dataTransfer.types.includes("Files")) setDragActive(true);
                        }}
                        onDragOver={(event) => {
                            event.preventDefault();
                            event.dataTransfer.dropEffect = "copy";
                        }}
                        onDragLeave={(event) => {
                            event.preventDefault();
                            dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
                            if (!dragDepthRef.current) setDragActive(false);
                        }}
                        onDrop={(event) => {
                            event.preventDefault();
                            dragDepthRef.current = 0;
                            setDragActive(false);
                            pickFile(event.dataTransfer.files);
                        }}
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

                <div>
                    <div className="mb-1.5 text-xs font-medium text-stone-500 dark:text-stone-400">② 改造意图</div>
                    <Input.TextArea
                        value={intent}
                        onChange={(event) => setIntent(event.target.value)}
                        placeholder="例如：把片中的产品换成我们的新品，保留原片节奏、运镜和字幕样式"
                        autoSize={{ minRows: 2, maxRows: 5 }}
                        disabled={busy}
                    />
                </div>

                <div className="flex justify-end">
                    <Button
                        type="primary"
                        icon={busy ? <LoaderCircle className="size-4 animate-spin" /> : <Sparkles className="size-4" />}
                        loading={busy}
                        disabled={!file || !intent.trim() || busy}
                        onClick={() => void createTask()}
                    >
                        建任务并拆解
                    </Button>
                </div>
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
        </Modal>
    );
}
