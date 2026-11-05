// 拉片节点（M3.1）：由「视频节点 → 工具栏拉片 → 执行」创建的结果节点。
// 只读结果 + 状态机：loading（轮询 job，无超时死线）/ done（拉片正文）/ error（重试）。
// 轮询自适应：前 2 分钟 2s、之后 5s——大拆解 10-20 分钟属正常，lost 只留给 job 404。
// 独立任务语义：每个节点一次任务（metadata.taskId/jobId/intent）；刷新恢复归 M5，
// 本组件给「重查」兜底（job 404 时按 taskId 直接拉投影）。
import { useEffect, useRef, useState } from "react";

import { canvasThemes } from "@/lib/canvas-theme";
import { decodeChannelModelRef } from "@/lib/canvas/lapian-model";
import type { CanvasNodeData } from "@/types/canvas";

const BRIDGE = ""; // 同域相对路径：本地 dev 由 vite proxy 转发，公网由隧道同域转发
const POLL_MS_FAST = 2000;      // 前 2 分钟：快速反馈
const POLL_MS_SLOW = 5000;      // 之后放慢：拆解可跑 10-20 分钟，持续轮询不设超时死线
const SLOW_AFTER_MS = 120000;   // 超过 2 分钟切慢轮询
const SLOW_HINT_AFTER_SEC = 180; // 已用时超 3 分钟，追加安抚文案
const MAX_NET_ERRORS = 3;

const fmtElapsed = (sec: number) => `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;

type Phase = "loading" | "done" | "error" | "lost";

export function LapianNodeContent({ node, onContentChange }: { node: CanvasNodeData; theme: (typeof canvasThemes)[keyof typeof canvasThemes]; onContentChange?: (nodeId: string, content: string) => void }) {
    const taskId = String(node.metadata?.taskId || "");
    const jobId = String(node.metadata?.jobId || "");
    const intent = String(node.metadata?.intent || "");
    // 透明（T7-R ③）：节点上直接可见本任务实际用的分析模型（创建时落盘的实际值；空 = 走服务端默认，不显示）。
    const modelName = decodeChannelModelRef(String(node.metadata?.lapianModel || ""))?.model || "";
    const [phase, setPhase] = useState<Phase>("loading");
    const [elapsed, setElapsed] = useState(0);
    const [stage, setStage] = useState("");
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
            // 自适应间隔按「已等了多久」算：前 2 分钟 2s，之后 5s——轮询永不因时长自杀。
            const delay = Date.now() - startedRef.current < SLOW_AFTER_MS ? POLL_MS_FAST : POLL_MS_SLOW;
            try {
                const r = await fetch(`${BRIDGE}/api/job/${jobId}`);
                if (r.status === 404) {
                    // bridge 重启过：job 查不到。任务可能已完成——按 taskId 重查。
                    const lp = await fetch(`${BRIDGE}/api/lapian/${encodeURIComponent(taskId)}`).then((x) => x.json());
                    if (lp && !lp.error) { setMarkdown(lp.markdown || ""); setPhase("done"); onContentChange?.(node.id, lp.markdown || ""); return; }
                    setPhase("lost"); setMessage("任务状态查不到（服务可能重启过）。可按任务号重查，或重新拉片。");
                    return;
                }
                const d = await r.json();
                netErrRef.current = 0;
                if (d.status === "done") {
                    const lp = await fetch(`${BRIDGE}/api/lapian/${encodeURIComponent(taskId)}`).then((x) => x.json());
                    if (lp && !lp.error) { setMarkdown(lp.markdown || ""); setPhase("done"); onContentChange?.(node.id, lp.markdown || ""); return; }
                    setPhase("error"); setMessage(lp?.error || "拉片投影失败");
                    return;
                }
                if (d.status === "error") {
                    setPhase("error"); setMessage(d.error || "拆解失败（见服务端日志）");
                    return;
                }
                // 还在跑：同步后端阶段（后端暂未上报 stage 时前端兜底「拆解中」），按自适应间隔继续。
                setStage(typeof d.stage === "string" && d.stage ? d.stage : "");
                timerRef.current = window.setTimeout(tick, delay);
            } catch {
                netErrRef.current += 1;
                if (netErrRef.current >= MAX_NET_ERRORS) {
                    setPhase("error"); setMessage("连不上 bridge（:8776）。确认 ui-server 在跑，然后重试。");
                    return;
                }
                timerRef.current = window.setTimeout(tick, delay);
            }
        };
        void tick();
        return () => { if (timerRef.current) window.clearTimeout(timerRef.current); };
    }, [phase, jobId, taskId]);

    // 已用时展示钟：1s 一跳、与轮询解耦——切到 5s 慢轮询后读数也不冻结。
    useEffect(() => {
        if (phase !== "loading") return;
        const t = window.setInterval(() => setElapsed(Math.round((Date.now() - startedRef.current) / 1000)), 1000);
        return () => window.clearInterval(t);
    }, [phase]);

    const requery = async () => {
        setPhase("loading"); setElapsed(0); setStage(""); startedRef.current = Date.now();
        try {
            const lp = await fetch(`${BRIDGE}/api/lapian/${encodeURIComponent(taskId)}`).then((x) => x.json());
            if (lp && !lp.error) { setMarkdown(lp.markdown || ""); setPhase("done"); onContentChange?.(node.id, lp.markdown || ""); return; }
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
                {modelName ? <span className="shrink-0 rounded-md px-1.5 py-0.5 text-[10px]" style={{ color: theme.node.muted, border: `1px solid ${theme.node.stroke}` }} data-lapian-model-name title={modelName}>{modelName}</span> : null}
                {phase === "done" || phase === "error" || phase === "lost" ? (
                    <button type="button" className="shrink-0 rounded-md px-1.5 py-0.5 text-[11px]" style={{ color: theme.node.muted, border: `1px solid ${theme.node.stroke}` }} onClick={() => void requery()} data-lapian-requery>重查</button>
                ) : null}
            </div>
            <div className="thin-scrollbar min-h-0 flex-1 overflow-auto p-2 text-[11px] leading-relaxed" style={{ color: theme.node.text }}>
                {phase === "loading" && (
                    <div data-lapian-loading style={{ color: theme.node.muted }}>
                        {stage || "拆解中"}… 已用时 {fmtElapsed(elapsed)}（job {jobId.slice(-4)}）
                        {intent ? <div className="mt-1" style={{ color: theme.node.faint }}>需求：{intent}</div> : null}
                        {elapsed >= SLOW_HINT_AFTER_SEC ? <div className="mt-1" style={{ color: theme.node.faint }}>大拆解较慢，通常 5-15 分钟，不用重开</div> : null}
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
