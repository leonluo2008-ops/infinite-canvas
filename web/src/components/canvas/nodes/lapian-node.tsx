// 拉片节点（M3）：选拆解任务 → fetch ui-server bridge(:8776) → 展示即梦结构拉片。
// 原生节点：不走插件 SDK（POC S3 空白卡的教训——definition.Content 依赖 pluginHost），
// 直接注册进 canvas-node 的 nodeContentRenderers。
// 任务选择暂存组件本地 state（刷新恢复是 M5 的活）。
import { useEffect, useState } from "react";

import { canvasThemes } from "@/lib/canvas-theme";
import type { CanvasNodeData } from "@/types/canvas";

const BRIDGE = "http://127.0.0.1:8776";

type LapianBlock = { title: string; lines: string[]; dna: boolean; seg_head: boolean };
type LapianPayload = { task: string; spec: string; blocks: LapianBlock[]; markdown: string };
type TaskRow = { id?: string; task?: string; name?: string };

export function LapianNodeContent({ node, theme: _theme }: { node: CanvasNodeData; theme: (typeof canvasThemes)[keyof typeof canvasThemes] }) {
    void _theme;
    const [tasks, setTasks] = useState<TaskRow[]>([]);
    const [taskId, setTaskId] = useState<string>((node.metadata?.taskId as string) || "");
    const [data, setData] = useState<LapianPayload | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState("");

    useEffect(() => {
        fetch(`${BRIDGE}/api/tasks`)
            .then((r) => r.json())
            .then((d: { tasks?: TaskRow[] }) => setTasks(d.tasks || []))
            .catch((e: Error) => setError(`任务列表拉不到（bridge :8776 要先起）：${e.message}`));
    }, []);

    const load = (id: string) => {
        if (!id) return;
        setTaskId(id);
        setLoading(true);
        setError("");
        setData(null);
        fetch(`${BRIDGE}/api/lapian/${encodeURIComponent(id)}`)
            .then((r) => r.json())
            .then((d: LapianPayload & { error?: string }) => {
                if (d.error) throw new Error(d.error);
                setData(d);
                setLoading(false);
            })
            .catch((e: Error) => {
                setError(`拉片投影失败：${e.message}`);
                setLoading(false);
            });
    };

    const theme = canvasThemes.dark;
    return (
        <div className="flex h-full w-full flex-col overflow-hidden" data-lapian-node>
            <div className="flex items-center gap-1.5 border-b px-2 py-1.5 text-xs" style={{ borderColor: theme.node.stroke, background: theme.node.panel }}>
                <span className="shrink-0 font-medium" style={{ color: theme.node.muted }}>拉片</span>
                <select
                    className="min-w-0 flex-1 truncate rounded-md px-1.5 py-1 text-xs outline-none"
                    style={{ background: theme.node.panel, color: theme.node.text, border: `1px solid ${theme.node.stroke}` }}
                    value={taskId}
                    onChange={(e) => load(e.target.value)}
                    data-lapian-task-select
                >
                    <option value="">选择拆解任务…</option>
                    {tasks.map((t, i) => {
                        const id = String(t.id || t.task || t.name || i);
                        return <option key={id} value={id}>{id}</option>;
                    })}
                </select>
            </div>
            <div className="thin-scrollbar min-h-0 flex-1 overflow-auto p-2">
                {loading && <div className="text-xs" style={{ color: theme.node.muted }}>投影中…</div>}
                {error && <div className="text-xs leading-relaxed" style={{ color: "#f87171" }}>{error}</div>}
                {!loading && !error && !data && (
                    <div className="text-xs leading-relaxed" style={{ color: theme.node.muted }}>
                        选一个已拆解任务，节点里出即梦结构拉片（全局 DNA + 拉片段 + 衔接状态；缺项带〔未取证〕〔不适用〕〔缺字段〕标注）。
                    </div>
                )}
                {data && (
                    <pre className="whitespace-pre-wrap break-words text-[11px] leading-relaxed" style={{ color: theme.node.text, fontFamily: "inherit" }} data-lapian-body>
                        {data.markdown}
                    </pre>
                )}
            </div>
        </div>
    );
}
