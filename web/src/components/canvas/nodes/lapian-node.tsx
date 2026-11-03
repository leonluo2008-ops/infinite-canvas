// 拉片节点（M3.1）：由「视频节点 → 工具栏拉片 → 执行」创建的结果节点。
// 只读结果 + 状态机：loading（轮询 job）/ done（拉片正文）/ error（重试）。
// 独立任务语义：每个节点一次任务（metadata.taskId/jobId/intent）；刷新恢复归 M5，
// 本组件给「重查」兜底（job 404 时按 taskId 直接拉投影）。
import { useEffect, useRef, useState } from "react";

import { canvasThemes } from "@/lib/canvas-theme";
import type { CanvasNodeData } from "@/types/canvas";

const BRIDGE = "http://127.0.0.1:8776";
const POLL_MS = 2000;
const POLL_TIMEOUT_MS = 180000;
const MAX_NET_ERRORS = 3;

type Phase = "loading" | "done" | "error" | "lost";

export function LapianNodeContent({ node }: { node: CanvasNodeData; theme: (typeof canvasThemes)[keyof typeof canvasThemes] }) {
    const taskId = String(node.metadata?.taskId || "");
    const jobId = String(node.metadata?.jobId || "");
    const intent = String(node.metadata?.intent || "");
    const [phase, setPhase] = useState<Phase>("loading");
    const [elapsed, setElapsed] = useState(0);
    const [message, setMessage] = useState("");
    const [markdown, setMarkdown] = useState("");
    const timerRef = useRef<number | null>(null);
    const startedRef = useRef(Date.now());
    const netErrRef = useRef(0);
    const theme = canvasThemes.dark;

    useEffect(() => {
        if (phase !== "loading" || !jobId) return;
        startedRef.current = Date.now();
        netErrRef.current = 0;
        const tick = async () => {
            try {
                const r = await fetch(`${BRIDGE}/api/job/${jobId}`);
                if (r.status === 404) {
                    // bridge 重启过：job 查不到。任务可能已完成——按 taskId 重查。
                    const lp = await fetch(`${BRIDGE}/api/lapian/${encodeURIComponent(taskId)}`).then((x) => x.json());
                    if (lp && !lp.error) { setMarkdown(lp.markdown || ""); setPhase("done"); return; }
                    setPhase("lost"); setMessage("任务状态查不到（服务可能重启过）。可按任务号重查，或重新拉片。");
                    return;
                }
                const d = await r.json();
                netErrRef.current = 0;
                if (d.status === "done") {
                    const lp = await fetch(`${BRIDGE}/api/lapian/${encodeURIComponent(taskId)}`).then((x) => x.json());
                    if (lp && !lp.error) { setMarkdown(lp.markdown || ""); setPhase("done"); return; }
                    setPhase("error"); setMessage(lp?.error || "拉片投影失败");
                    return;
                }
                if (d.status === "error") {
                    setPhase("error"); setMessage(d.error || "拆解失败（见服务端日志）");
                    return;
                }
                setElapsed(Math.round((Date.now() - startedRef.current) / 1000));
                if (Date.now() - startedRef.current > POLL_TIMEOUT_MS) {
                    setPhase("lost"); setMessage("超过 3 分钟还没跑完（可重查，或稍后回来）。");
                    return;
                }
                timerRef.current = window.setTimeout(tick, POLL_MS);
            } catch {
                netErrRef.current += 1;
                if (netErrRef.current >= MAX_NET_ERRORS) {
                    setPhase("error"); setMessage("连不上 bridge（:8776）。确认 ui-server 在跑，然后重试。");
                    return;
                }
                timerRef.current = window.setTimeout(tick, POLL_MS);
            }
        };
        void tick();
        return () => { if (timerRef.current) window.clearTimeout(timerRef.current); };
    }, [phase, jobId, taskId]);

    const requery = async () => {
        setPhase("loading"); setElapsed(0); startedRef.current = Date.now();
        try {
            const lp = await fetch(`${BRIDGE}/api/lapian/${encodeURIComponent(taskId)}`).then((x) => x.json());
            if (lp && !lp.error) { setMarkdown(lp.markdown || ""); setPhase("done"); return; }
            setPhase("error"); setMessage(lp?.error || "重查失败");
        } catch (e) {
            setPhase("error"); setMessage(`重查失败：${(e as Error).message}`);
        }
    };

    return (
        <div className="flex h-full w-full flex-col overflow-hidden" data-lapian-node data-lapian-phase={phase}>
            <div className="flex shrink-0 items-center gap-1.5 border-b px-2 py-1.5 text-xs" style={{ borderColor: theme.node.stroke, background: theme.node.panel }}>
                <span className="shrink-0 font-medium" style={{ color: theme.node.muted }}>拉片</span>
                <span className="min-w-0 flex-1 truncate" style={{ color: theme.node.faint }} data-lapian-taskid>{taskId}</span>
                {phase === "done" || phase === "error" || phase === "lost" ? (
                    <button type="button" className="shrink-0 rounded-md px-1.5 py-0.5 text-[11px]" style={{ color: theme.node.muted, border: `1px solid ${theme.node.stroke}` }} onClick={() => void requery()} data-lapian-requery>重查</button>
                ) : null}
            </div>
            <div className="thin-scrollbar min-h-0 flex-1 overflow-auto p-2 text-[11px] leading-relaxed" style={{ color: theme.node.text }}>
                {phase === "loading" && (
                    <div data-lapian-loading style={{ color: theme.node.muted }}>
                        拆解中… 已 {elapsed}s（job {jobId.slice(-4)}）
                        {intent ? <div className="mt-1" style={{ color: theme.node.faint }}>需求：{intent}</div> : null}
                    </div>
                )}
                {phase === "lost" && <div data-lapian-lost style={{ color: "#fbbf24" }}>{message}</div>}
                {phase === "error" && <div data-lapian-error style={{ color: "#f87171" }}>{message}</div>}
                {phase === "done" && (
                    <pre className="whitespace-pre-wrap break-words" style={{ fontFamily: "inherit" }} data-lapian-body>{markdown}</pre>
                )}
            </div>
        </div>
    );
}
