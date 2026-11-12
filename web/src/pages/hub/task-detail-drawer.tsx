import { ArrowRight, CheckCircle2, CircleAlert, Download, FileText, ImageOff, LoaderCircle, MoreHorizontal, RefreshCw, Sparkles, Type, Upload } from "lucide-react";
import { App, Button, Checkbox, Drawer, Dropdown, Empty, Image as AntdImage, Input, Modal, Radio, Select, Slider, Tag, Tooltip } from "antd";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { Streamdown } from "streamdown";

import { ModelPicker } from "@/components/model-picker";
import { AudioModePicker } from "@/components/audio-mode-picker";
import { badgeFits, boxRect, containRect, formatSeconds, frameCaption, frameState, layoutLegend, otherBoxCount, ratioFromImage, stageSize } from "@/lib/hub/frame-overlay";
import {
    buildApproveConfig,
    clearApprovedSignature,
    defaultGenDraft,
    findGenChannel,
    genApproveIssues,
    genChannelModelRows,
    genDefaultChannelId,
    genDraftSignature,
    genDraftStaleNote,
    genDraftSummary,
    genDispatchPhase,
    genFailureHint,
    genFamily,
    genIssue,
    genModelPick,
    genModelRows,
    genNeedsReapproval,
    genRatioRows,
    genResolutionRows,
    genResolveSize,
    genUiFlags,
    readApprovedSignature,
    readGenDraft,
    saveApprovedSignature,
    saveGenDraft,
    GEN_CANVAS_ONLY_TAG,
    GEN_DURATION_MAX,
    GEN_DURATION_MIN,
    type GenChannelDesc,
    type GenDraft,
} from "@/lib/hub/gen-settings";
import { mergeBadge, mergeBarText, mergeImageBranch, mergeMembershipChanged, mergeSelectable, mergeShotUnion, groupBoxRow, pruneSelection, toggleMergeRow } from "@/lib/hub/merge-mode";
import { activeCards, decisionBadgeText, decisionErrorText, decisionFeedback, lockCounters, lockRows, originRowNumbers, originStripLabel, rowDecisionAction, type RowDecisionAction } from "@/lib/hub/row-decision";
import { useConfigStore, type AiConfig } from "@/stores/use-config-store";

import {
    approveHubTask,
    confirmHubReplace,
    errorText,
    forceFinishHubTask,
    getHubTask,
    groupHubRows,
    hubAssetDisplayUrl,
    hubAssetRefView,
    hubOutputUrl,
    hubQuickAction,
    hubQuickChannels,
    hubQuickPhaseText,
    hubReworkGate,
    hubReworkUnknownSteps,
    postHubAsset,
    probeHubAssets,
    readApprovedDraft,
    releaseHubTask,
    retryHubTask,
    revokeHubTask,
    rollbackHubTask,
    runHubQuickAction,
    setHubAudioMode,
    startLapianRun,
    submitHubTask,
    ungroupHubRows,
    uploadHubImage,
    type HubAssetCard,
    type HubCardGroup,
    type HubQuickAction,
    type HubQuickPhase,
    type HubReworkGate,
    type HubSelfCheckItem,
    type HubSuggestion,
    type HubTaskDetail,
    type HubTaskSummary,
} from "./hub-api";
// AT-16 B9：解除阻断后的换配置直达条（判定=起态门 + dispatch=cleared，动作=既有撤销批准链）
import { GenClearedBar, hubClearedDirect } from "./gen-cleared";
import { lapianClockTexts, lapianStageName, useLapianClock } from "./lapian-clock";
import { HubStateTag, SubmitResultLine, forceFinishGuard } from "./hub-status";
import { FORCE_FINISH_CONFIRM_TITLE, ForceFinishButton, ForceFinishConfirmBody } from "./task-card";
import { TaskDeleteButton } from "./task-delete-confirm";

/** 九宫格粗标配色（同帧多目标分色，AT-9 层1）。 */
const OVERLAY_COLORS = [
    "rgba(244,63,94,0.30)",
    "rgba(59,130,246,0.30)",
    "rgba(16,185,129,0.30)",
    "rgba(245,158,11,0.30)",
    "rgba(139,92,246,0.30)",
    "rgba(6,182,212,0.30)",
    "rgba(236,72,153,0.30)",
    "rgba(132,204,22,0.30)",
];

const POST_DECOMPOSE_STATES = ["awaiting_assets", "rendering", "awaiting_approval", "approved", "generating", "done", "failed"];

type BusyKind = "upload" | "confirm" | "empty" | "approve" | "submit" | "retry" | "rollback" | "revoke" | "release" | "group" | "ungroup" | "discuss" | "decision" | "dismiss-batch" | "audio-mode" | null;

/**
 * 任务详情全屏 Drawer（AT-9 §五）：头部 → Agent 建议 → 资产置换 → 提示词 → 审批/生成/成片。
 * 操作合同 §四：每状态只渲染该表列出的动作；确认置换后立即关 Drawer（渲染由后端推进，前端零执行责任）。
 * 轮询治理：详情轮询仅 Drawer 开启时；切任务/关 Drawer 丢弃过期响应（序号守卫）；失联显示错误+上次更新时间。
 */
export function TaskDetailDrawer({
    task,
    open,
    onClose,
    onStateChange,
    onRefresh,
    onDeleted,
}: {
    task: HubTaskSummary | null;
    open: boolean;
    onClose: () => void;
    onStateChange: (localId: string, state: string) => void;
    onRefresh: () => void;
    /** 删除成功（AT-16 B6：确认在 TaskDeleteButton 内二键完成，父级只收结果）。 */
    onDeleted: (localId: string) => void;
}) {
    const { message } = App.useApp();
    const [detail, setDetail] = useState<HubTaskDetail | null>(null);
    const [loadError, setLoadError] = useState("");
    const [lastUpdated, setLastUpdated] = useState<number | null>(null);
    const [busy, setBusy] = useState<BusyKind>(null);
    // AT-14 B3：资产引用健康态（显示 URL → local|ok|expired|unknown）；旧任务里失效图床直链据此
    // 出「图已过期，请重传 / 图片不可用，请重试」，正常引用不打扰（缺键=不显示）。
    const [assetStates, setAssetStates] = useState<Record<string, string>>({});
    /** AT-18 B3b：抽屉内动作的接口错误原文（行内可见，不吞、不白屏）。 */
    const [actionError, setActionError] = useState("");
    const seqRef = useRef(0);
    const reportedStateRef = useRef("");

    const localId = task?.local_id || "";

    const load = useCallback(async () => {
        if (!localId) return;
        const seq = ++seqRef.current;
        try {
            const next = await getHubTask(localId);
            if (seq !== seqRef.current) return; // 过期响应（切任务/已关闭）→ 丢弃
            setDetail(next);
            setLoadError("");
            setLastUpdated(Date.now());
            if (next.state && next.state !== reportedStateRef.current) {
                reportedStateRef.current = next.state;
                onStateChange(localId, next.state);
            }
            // 引用健康态独立拉（服务端 60s 缓存）：不阻塞详情渲染，失败一律静默（面板退化=不提示）。
            void probeHubAssets(localId)
                .then((rows) => {
                    if (seq !== seqRef.current) return;
                    const states: Record<string, string> = {};
                    for (const row of rows) {
                        if (row.display) states[row.display] = row.state;
                        if (row.ref) states[row.ref] = row.state;
                    }
                    setAssetStates(states);
                })
                .catch(() => undefined);
        } catch (error) {
            if (seq !== seqRef.current) return;
            setLoadError(errorText(error));
        }
    }, [localId, onStateChange]);

    // 详情轮询仅 Drawer 开启时；关闭瞬间作废在途响应。
    // AT-16 B8：提交在途（busy=submit，最长 40s）时加密到 2s——顶栏主状态条要跟着 submit
    // 形态走（提交处理中 → 结果未知/失败），5s 粒度会把短于一轮的在途窗口整个跳过去。
    useEffect(() => {
        if (!open || !localId) return;
        reportedStateRef.current = "";
        void load();
        const timer = window.setInterval(() => void load(), busy === "submit" ? 2000 : 5000);
        return () => {
            window.clearInterval(timer);
            seqRef.current += 1;
        };
    }, [open, localId, load, busy]);

    // Drawer 已关闭 → 在途响应一律丢弃。
    useEffect(() => {
        if (!open) seqRef.current += 1;
    }, [open]);

    const state = detail?.state || task?.state || "";
    const summary: HubTaskSummary | null = detail
        ? { ...detail, task_name: detail.task_name || task?.task_name || detail.local_id }
        : task;
    // AT-16 B5a-1：抽屉顶部与详情头共用同一份提交诊断（详情有就用详情的，回落到列表摘要）。
    // 三处（列表 / 详情头 / 抽屉顶部）同一来源 = hub_api._summary → automation/submit_view.build。
    const submitView = detail?.submit ?? summary?.submit ?? undefined;

    // AT-16 B5a-2：抽屉顶部重提 / 续查入口（缺陷 D6：真机实证入口埋在 4-5 屏深）。
    // 判定与执行全走 hub-api 的 hubQuickAction / runHubQuickAction——与列表行内、底部 submitOnly 同一条链。
    const config = useConfigStore((cfg) => cfg.config);
    const quickChannels = useMemo(() => hubQuickChannels(config), [config]);
    const [quickBusy, setQuickBusy] = useState(false);
    const quick = hubQuickAction({
        localId,
        state,
        submit: submitView,
        cloudTaskId: detail?.cloud_task_id || "",
        channels: quickChannels,
        approvedSignature: readApprovedSignature(localId),
        draft: readGenDraft(localId),
        busy: quickBusy || busy !== null,
    });
    const fireQuick = useCallback(
        async (action: HubQuickAction) => {
            if (!action.enabled) {
                message.warning(action.reason || "这个入口现在不可用");
                return;
            }
            setQuickBusy(true);
            try {
                await runHubQuickAction(localId, action, {
                    onPhase: (phase, phaseState, note) => {
                        // AT-16 B8：受理/收尾立刻重取主状态（顶栏状态条 + 列表行同步），
                        // 不等 runHubQuickAction 内部 40s 轮询跑完。
                        void load();
                        onRefresh();
                        const line = hubQuickPhaseText(phase, phaseState);
                        const text = note || line.text;
                        if (!text) return;
                        if (line.level === "success") message.success(text);
                        else if (line.level === "warning") message.warning(text);
                        else message.info(text);
                    },
                });
                await load();
                onRefresh();
            } catch (quickError) {
                message.error(errorText(quickError));      // 后端原文（如「已有提交在跑」409）直接透出
                void load();                               // 被拒也回到后端事实
            } finally {
                setQuickBusy(false);
            }
        },
        [load, localId, message, onRefresh],
    );

    const reload = useCallback(() => void load(), [load]);

    const act = useCallback(
        async (kind: Exclude<BusyKind, null>, run: () => Promise<void>, okText?: string) => {
            if (busy) return;
            setBusy(kind);
            try {
                await run();
                if (okText) message.success(okText);
                await load();
                onRefresh();
            } catch (error) {
                message.error(errorText(error));
                // AT-16 B8：动作失败也要回到后端事实——主状态条不许停在旧文案上
                void load();
            } finally {
                setBusy(null);
            }
        },
        [busy, load, message, onRefresh],
    );

    // 确认置换：成功 → 立即关 Drawer（渲染由后端推进），列表刷新接住新状态。
    const handleConfirmed = useCallback(() => {
        onClose();
        onRefresh();
    }, [onClose, onRefresh]);

    const titleName = summary?.task_name || summary?.local_id || "任务详情";

    return (
        <AssetStateContext.Provider value={assetStates}>
        <Drawer
            open={open}
            onClose={onClose}
            width="100%"
            rootClassName="hub-detail-drawer"
            title={
                <div className="flex min-w-0 flex-wrap items-center gap-2 pr-8">
                    <span className="truncate text-base font-semibold">{titleName}</span>
                    <HubStateTag state={state} submit={submitView} />
                    {/* AT-16 B5a-2：顶部快捷入口——重提 / 续查一眼可及（判定=hubQuickAction，执行=runHubQuickAction） */}
                    {quick.kind !== "none" ? (
                        <Tooltip title={quick.enabled ? quick.hint : quick.reason}>
                            <span>
                                <Button
                                    size="small"
                                    type={quick.kind === "submit" ? "primary" : "default"}
                                    ghost
                                    loading={quickBusy}
                                    disabled={!quick.enabled || quickBusy}
                                    data-hub-drawer-quick-action={quick.kind}
                                    data-hub-drawer-quick-enabled={quick.enabled ? "1" : "0"}
                                    data-hub-drawer-quick-source={quick.source}
                                    data-hub-drawer-quick-needs-config={quick.needsConfig ? "1" : "0"}
                                    onClick={(event) => {
                                        event.stopPropagation();
                                        void fireQuick(quick);
                                    }}
                                >
                                    {quick.label}
                                </Button>
                            </span>
                        </Tooltip>
                    ) : null}
                </div>
            }
        >
            {!summary ? (
                <div className="flex min-h-[240px] items-center justify-center">
                    <Empty description="任务不存在或已被删除" />
                </div>
            ) : (
                <div className="mx-auto flex w-full max-w-[1100px] flex-col gap-5 pb-16">
                    {/* 失联：错误 + 上次更新时间（§五轮询治理） */}
                    {loadError ? (
                        <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-700 dark:border-amber-950 dark:bg-amber-950/20 dark:text-amber-300">
                            <span className="min-w-0 break-words">详情刷新失败（显示的是上次数据）：{loadError}</span>
                            <span className="text-xs text-amber-600/80 dark:text-amber-300/80">{lastUpdated ? `上次更新 ${new Date(lastUpdated).toLocaleTimeString("zh-CN", { hour12: false })}` : "尚未成功拉取"}</span>
                        </div>
                    ) : null}

                    {/* ① 头部：任务名 / 状态 / 意图原文完整展示 */}
                    <section className="rounded-lg border border-stone-200 bg-card p-4 dark:border-stone-800">
                        <div className="flex flex-wrap items-start justify-between gap-3">
                            <div className="min-w-0 flex-1">
                                <div className="flex flex-wrap items-center gap-2">
                                    <span className="font-mono text-sm font-semibold">{summary.local_id}</span>
                                    <HubStateTag state={state} submit={submitView} />
                                    <span className="text-xs text-stone-400 dark:text-stone-500">{summary.video_name || "参考视频"}</span>
                                    {summary.created_at ? <span className="text-xs text-stone-400 dark:text-stone-500">建于 {formatTime(summary.created_at)}</span> : null}
                                </div>
                                {/* AT-16 B5a-1：提交结果就地可见（结论 + 短错误码 + 可展开的可能原因/脱敏原文），
                                    与主表行内同一个组件、同一份摘要字段（三处同源）。 */}
                                <div className="mt-2">
                                    <SubmitResultLine submit={submitView} />
                                </div>
                                <div className="mt-2 whitespace-pre-wrap break-words text-sm leading-6 text-stone-700 dark:text-stone-200">
                                    <span className="mr-1 font-medium text-stone-500 dark:text-stone-400">改造意图：</span>
                                    {summary.intent || "（未填写意图）"}
                                </div>
                                {/* AT-18 B3b：抽屉内动作（强制结束）失败 → 原文留在头部，不靠一闪而过的 toast */}
                                {actionError ? (
                                    <div className="mt-2 flex items-start gap-2 rounded-md border border-red-200 bg-red-50 px-2 py-1.5 text-xs text-red-700 dark:border-red-950 dark:bg-red-950/20 dark:text-red-300" data-hub-drawer-action-error={summary.local_id}>
                                        <CircleAlert className="mt-0.5 size-3.5 shrink-0" />
                                        <span className="min-w-0 flex-1 break-words">{actionError}</span>
                                        <button type="button" className="shrink-0 text-red-500 underline decoration-dotted dark:text-red-400" onClick={() => setActionError("")}>
                                            知道了
                                        </button>
                                    </div>
                                ) : null}
                            </div>
                            <HeaderActions
                                summary={summary}
                                busy={busy}
                                onDeleted={onDeleted}
                                onStateChange={onStateChange}
                                onRefresh={onRefresh}
                                onError={setActionError}
                            />
                        </div>
                    </section>

                    {/* ② Agent 建议区（拆解后才有） */}
                    {POST_DECOMPOSE_STATES.includes(state) && detail ? <SuggestionSection detail={detail} /> : null}

                    {/* ③ 资产置换区（仅 awaiting_assets，§四：传图/清空/全沿用/成组/解组/确认置换） */}
                    {state === "awaiting_assets" && detail ? (
                        <AssetSection
                            detail={detail}
                            busy={busy}
                            setBusy={setBusy}
                            onWritten={reload}
                            onConfirmed={handleConfirmed}
                        />
                    ) : null}

                    {/* 状态进度（ingesting/discussing/rendering：只读+进度；discussing 显 stage+log_tail） */}
                    {(state === "discussing" || state === "ingesting" || state === "rendering") && detail ? <ProgressSection detail={detail} state={state} /> : null}

                    {/* ingested：发起拆解（§四：ingested → 发起拆解(含意图) → discussing） */}
                    {state === "ingested" ? (
                        <section className="rounded-lg border border-stone-200 bg-card p-4 dark:border-stone-800">
                            <div className="flex flex-wrap items-center justify-between gap-3">
                                <span className="text-sm text-stone-500 dark:text-stone-400">任务已建立但还没开始拆解（可能触发失败）。</span>
                                <Button
                                    type="primary"
                                    icon={<Sparkles className="size-4" />}
                                    loading={busy === "discuss"}
                                    onClick={() =>
                                        void act(
                                            "discuss",
                                            async () => {
                                                await startLapianRun(summary.local_id, summary.intent || "");
                                            },
                                            "已开始拆解（1–3 分钟）",
                                        )
                                    }
                                >
                                    发起拆解
                                </Button>
                            </div>
                        </section>
                    ) : null}

                    {/* ④ 提示词段（AT-16 B2：标题一行 + 正文默认展开可收起 + 自检/门禁独立整宽区） */}
                    {detail && POST_DECOMPOSE_STATES.includes(state) ? <PromptSection detail={detail} /> : null}

                    {/* ⑤ 审批/生成/成片段（§四各状态动作） */}
                    {detail ? <OutcomeSection detail={detail} busy={busy} act={act} /> : null}

                    {!detail && !loadError ? (
                        <div className="flex min-h-[200px] flex-col items-center justify-center gap-2 text-sm text-stone-500 dark:text-stone-400">
                            <LoaderCircle className="size-5 animate-spin" />
                            正在读取任务详情…
                        </div>
                    ) : null}
                </div>
            )}
        </Drawer>
        </AssetStateContext.Provider>
    );
}

/**
 * 头部动作：强制结束（AT-18 §5.1 登记的后续项）+ 删除（AT-16 B6）。
 *
 * 两个入口都复用既有件，不新造第二套：
 *  · 强制结束 = `ForceFinishButton` + `FORCE_FINISH_CONFIRM_TITLE` + `ForceFinishConfirmBody`
 *    （与卡片流/表格行**同一颗按钮、同一段确认文案**，可见性仍是 `hub-status.forceFinishGuard`）；
 *  · 删除 = `TaskDeleteButton`（Popconfirm 二键确认，运行中由 deleteGuard 禁用）。
 * 写操作在跑（busy）时把强制结束一并置忙态，避免与传图/确认置换互踩。
 */
function HeaderActions({
    summary,
    busy,
    onDeleted,
    onStateChange,
    onRefresh,
    onError,
}: {
    summary: HubTaskSummary;
    busy: BusyKind;
    onDeleted: (localId: string) => void;
    onStateChange: (localId: string, state: string) => void;
    onRefresh: () => void;
    onError: (text: string) => void;
}) {
    return (
        <div className="flex flex-wrap items-center gap-1.5">
            <DrawerForceFinishEntry task={summary} busy={busy !== null} onStateChange={onStateChange} onRefresh={onRefresh} onError={onError} />
            <TaskDeleteButton task={summary} label="删除任务" disabled={busy !== null} onDeleted={onDeleted} />
        </div>
    );
}

/**
 * 抽屉内的「强制结束」入口（AT-18 B3b；方案 §5.1 把抽屉内的强制结束登记为后续独立项）。
 *
 * 语义与卡片流逐字一致：只有 discussing/rendering/generating 三种真实运行态渲染按钮；
 * 二次确认正文写死「不删任何文件 / 不杀进程」——那些字面量**只从 task-card 导入**，这里不复制；
 * 取消 = 只关弹层（零请求）；成功后立刻 `onStateChange(localId,"failed")` + `onRefresh()`（不删产物，可重试/删除）。
 * 接口拒绝原文进抽屉头部行内（onError），不吞、不白屏。
 */
export function DrawerForceFinishEntry({
    task,
    busy = false,
    onStateChange,
    onRefresh,
    onError,
}: {
    task: HubTaskSummary;
    busy?: boolean;
    onStateChange: (localId: string, state: string) => void;
    onRefresh: () => void;
    onError: (text: string) => void;
}) {
    const { message } = App.useApp();
    const [forceOpen, setForceOpen] = useState(false);
    const [forceBusy, setForceBusy] = useState(false);
    const guard = forceFinishGuard(task);
    if (!guard.visible) return null;

    const forceFinish = async () => {
        if (forceBusy) return;
        setForceBusy(true);
        onError("");
        try {
            const data = await forceFinishHubTask(task.local_id);
            setForceOpen(false);
            onStateChange(task.local_id, data.state || "failed");
            message.success(data.already ? `任务 ${task.local_id} 已是失败状态` : `已强制结束：${task.local_id} 置为失败（产物全保留，可重试或删除）`);
            onRefresh();
        } catch (error) {
            // 409「任务当前不是运行态…」/400「需二次确认…」原样透出到头部行内
            const text = errorText(error);
            setForceOpen(false);
            onError(`强制结束失败：${text}`);
            message.error(`强制结束失败：${text}`);
        } finally {
            setForceBusy(false);
        }
    };

    return (
        <>
            <ForceFinishButton task={task} busy={busy || forceBusy} onAsk={() => setForceOpen(true)} />
            <Modal
                open={forceOpen}
                title={FORCE_FINISH_CONFIRM_TITLE}
                okText="确认强制结束"
                cancelText="取消"
                okButtonProps={{ danger: true, "data-hub-force-confirm-ok": task.local_id }}
                cancelButtonProps={{ "data-hub-force-confirm-cancel": task.local_id }}
                confirmLoading={forceBusy}
                onOk={() => void forceFinish()}
                onCancel={() => setForceOpen(false)}
                destroyOnHidden
            >
                <ForceFinishConfirmBody task={task} />
            </Modal>
        </>
    );
}

/**
 * Agent 建议区：必须换/建议准备主展示带理由与镜次；可沿用全折叠；待核对显式标「尚无建议·待核对」，禁归可沿用。
 * AT-16 B3：每行挂画面锚点——复用 FrameWithPosition / GroupFrame（既有框与比例处理，红线 25 不另画一套框），
 * 数据源=asset_cards 已透出的代表帧；建议行 #n 与资产卡编号同源（同一份清单行号）→ 无卡/无帧的行诚实占位不造图。
 */
export function SuggestionSection({ detail }: { detail: HubTaskDetail }) {
    const suggestions = detail.suggestions;
    // 行号 → 资产卡：建议行 #n 就是清单行号，卡按同一行号索引（同源，不做二次编号）。
    const cardMap = new Map(detail.asset_cards.map((card) => [card.n, card]));

    if (!suggestions.length) {
        return (
            <section className="rounded-lg border border-amber-200 bg-amber-50 p-4 dark:border-amber-950 dark:bg-amber-950/20">
                <h2 className="text-sm font-semibold">Agent 建议</h2>
                <div className="mt-1.5 text-sm text-amber-700 dark:text-amber-300">尚无建议·待核对（意图建议缺失或解析失败，各行不自动归「可沿用」）。</div>
            </section>
        );
    }

    const must = suggestions.filter((item) => !item.待核对 && item.档位 === "必须换");
    const prepare = suggestions.filter((item) => !item.待核对 && item.档位 === "建议准备");
    const keep = suggestions.filter((item) => !item.待核对 && item.档位 === "可沿用");
    const unverified = suggestions.filter((item) => item.待核对 || !item.档位);

    return (
        <section className="rounded-lg border border-stone-200 bg-card p-4 dark:border-stone-800">
            <h2 className="text-sm font-semibold">Agent 建议</h2>
            <div className="mt-1 text-xs text-stone-500 dark:text-stone-400">结合你填的改造意图逐行给出；「必须换 / 建议准备」优先处理，其余折叠。</div>

            <div className="mt-3 space-y-2">
                {must.map((item) => (
                    <SuggestionRow key={item.n} item={item} card={cardMap.get(item.n)} tone="must" />
                ))}
                {prepare.map((item) => (
                    <SuggestionRow key={item.n} item={item} card={cardMap.get(item.n)} tone="prepare" />
                ))}
                {unverified.map((item) => (
                    <SuggestionRow key={item.n} item={item} card={cardMap.get(item.n)} tone="unverified" />
                ))}
            </div>

            {keep.length ? (
                <details className="mt-3 rounded-md border border-stone-200 dark:border-stone-800">
                    <summary className="flex cursor-pointer list-none items-center justify-between gap-2 px-3 py-2 text-sm [&::-webkit-details-marker]:hidden">
                        <span className="text-stone-600 dark:text-stone-300">{keep.length} 项不需要动(展开看)</span>
                        <span className="text-xs text-stone-400 dark:text-stone-500">展开 / 收起</span>
                    </summary>
                    <div className="space-y-2 border-t border-stone-200 px-3 py-2.5 dark:border-stone-800">
                        {keep.map((item) => (
                            <SuggestionRow key={item.n} item={item} card={cardMap.get(item.n)} tone="keep" />
                        ))}
                    </div>
                </details>
            ) : null}
        </section>
    );
}

/** 建议行锚点缩放：只缩「显示」（几何仍由 frame-overlay 同一套函数算，不另画框）；缩略图里图例 rail 不可读，
 *  故建议行关图例——框内 #行号 角标 + 行文本里的 #编号 承担识别。 */
const SUGGEST_ANCHOR_SCALE = 0.5;

/**
 * AT-16 B3 建议行视觉锚点（四类行同款）：
 *  组成员 → GroupFrame（组卡=一张代表帧+主行一框，与置换区口径一致）；
 *  有帧   → FrameWithPosition（本行帧+框，他人框淡显，三态照旧诚实）；
 *  无帧/无卡 → 诚实占位（不造图，不搬别的行的帧）。
 */
function SuggestionAnchor({ item, card }: { item: HubSuggestion; card?: HubAssetCard }) {
    const frame = "w-[168px] shrink-0";
    if (!card) {
        return (
            <div className={frame} data-hub-suggest-anchor={item.n} data-hub-suggest-anchor-source="none" data-hub-suggest-no-frame="missing-card">
                <NoFrameBox title="无对应资产卡" note="建议行没有对应的资产卡，取不到画面帧" />
            </div>
        );
    }
    const group = card.组;
    if (group) {
        if (!(group.代表帧.url || "").trim()) {
            return (
                <div className={frame} data-hub-suggest-anchor={item.n} data-hub-suggest-anchor-source="none" data-hub-suggest-no-frame="group-no-frame">
                    <NoFrameBox title="无对应帧" note={`组成员 #${group.成员.join("、#")} 没有可用画面帧`} />
                </div>
            );
        }
        return (
            <div className={frame} data-hub-suggest-anchor={item.n} data-hub-suggest-anchor-source="group" data-hub-suggest-group-key={group.键}>
                <GroupFrame group={group} scale={SUGGEST_ANCHOR_SCALE} />
            </div>
        );
    }
    if (!frameUrlOf(card)) {
        return (
            <div className={frame} data-hub-suggest-anchor={item.n} data-hub-suggest-anchor-source="none" data-hub-suggest-no-frame="no-frame">
                <NoFrameBox title="无对应帧" note="该行没有可定位的画面帧（不造图）" />
            </div>
        );
    }
    return (
        <div className={frame} data-hub-suggest-anchor={item.n} data-hub-suggest-anchor-source="frame">
            <FrameWithPosition card={card} scale={SUGGEST_ANCHOR_SCALE} showLegend={false} />
        </div>
    );
}

/** 无帧占位（建议区专用）：虚线空框 + 明说取不到帧，绝不用别的行/别的镜的帧顶替。 */
function NoFrameBox({ title, note }: { title: string; note: string }) {
    return (
        <div
            className="flex h-[104px] w-full flex-col items-center justify-center gap-1 rounded-md border border-dashed border-stone-300 bg-stone-50 px-2 text-center text-[11px] leading-tight text-stone-500 dark:border-stone-700 dark:bg-stone-900 dark:text-stone-400"
            data-hub-suggest-placeholder
        >
            <ImageOff className="size-4" />
            <span className="font-medium">{title}</span>
            <span className="text-[10px] text-stone-400 dark:text-stone-500">{note}</span>
        </div>
    );
}

function SuggestionRow({ item, card, tone }: { item: HubSuggestion; card?: HubAssetCard; tone: "must" | "prepare" | "keep" | "unverified" }) {
    const shots = item.镜次.length ? `第 ${item.镜次.join("、")} 镜` : "";
    const name = (card?.短名 || "").trim();
    const badge =
        tone === "must" ? (
            <Tag color="red" className="m-0">必须换</Tag>
        ) : tone === "prepare" ? (
            <Tag color="blue" className="m-0">建议准备</Tag>
        ) : tone === "unverified" ? (
            <Tag color="warning" className="m-0">尚无建议·待核对</Tag>
        ) : (
            <Tag className="m-0">可沿用</Tag>
        );
    return (
        <div
            className={`flex items-start gap-2.5 rounded-md px-2 py-1.5 text-sm ${tone === "must" ? "bg-red-50/70 dark:bg-red-950/20" : tone === "prepare" ? "bg-blue-50/60 dark:bg-blue-950/20" : tone === "unverified" ? "bg-amber-50/70 dark:bg-amber-950/20" : ""}`}
            data-hub-suggest-row={item.n}
            data-hub-suggest-tone={tone}
        >
            <SuggestionAnchor item={item} card={card} />
            <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                    {badge}
                    <span className="font-mono text-xs text-stone-400 dark:text-stone-500">#{item.n}</span>
                    {name ? <span className="font-medium">{name}</span> : null}
                    {shots ? <span className="text-xs text-stone-400 dark:text-stone-500">{shots}</span> : null}
                </div>
                {item.理由 ? <div className="mt-0.5 break-words text-xs text-stone-500 dark:text-stone-400">理由：{item.理由}</div> : null}
            </div>
        </div>
    );
}

/**
 * 拆解/入库进度（只读）：stage + 双锚计时 + log_tail 尾部（拆解进度必须界面可见）。
 *
 * AT-16 B4a：计时值全部来自 ./lapian-clock（与主表同一份口径/同一个 1 秒展示钟）——
 * 总耗时 = now − 拆解启动锚，本阶段已等待 = now − 阶段锚；缺锚诚实显示「计时不可用」，
 * 终态（成功/失败）按 ended_at 停表。日志行里的「已 Xs」是 job 总耗时快照，不当阶段起点。
 */
export function ProgressSection({ detail, state }: { detail: HubTaskDetail; state: string }) {
    const stage = lapianStageName(detail.lapian_stage || detail.stage || "");
    const logLines = (detail.log_tail || "").split("\n").filter(Boolean);
    const clock = useLapianClock(detail.lapian);
    const texts = lapianClockTexts(clock);
    return (
        <section className="rounded-lg border border-stone-200 bg-card p-4 dark:border-stone-800" data-hub-progress-section>
            <div className="flex flex-wrap items-center gap-2 text-sm">
                <LoaderCircle className="size-4 animate-spin text-stone-400" />
                <span className="font-medium">{state === "rendering" ? "正在渲染提示词（由后台推进，几秒完成）" : state === "ingesting" ? "参考片入库中…" : "拆解中（通常 1–3 分钟），完成后自动进入待置换"}</span>
                {stage ? <Tag color="blue" className="m-0">{stage}</Tag> : null}
            </div>
            <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-xs tabular-nums text-stone-600 dark:text-stone-300" data-hub-lapian-clock data-hub-lapian-frozen={clock.frozen ? "1" : "0"}>
                <span data-hub-lapian-total>{texts.total}</span>
                <span data-hub-lapian-stage-wait>{texts.stage}</span>
                {texts.note ? <span className="font-sans text-[11px] text-stone-400 dark:text-stone-500" data-hub-lapian-note>{texts.note}</span> : null}
            </div>
            {logLines.length ? (
                <pre className="thin-scrollbar mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded-md bg-stone-50 px-3 py-2 text-xs leading-5 text-stone-600 dark:bg-stone-900 dark:text-stone-300">
                    {logLines.slice(-10).join("\n")}
                </pre>
            ) : null}
        </section>
    );
}

// ---------------------------------------------------------------- 资产置换区（awaiting_assets）

/**
 * 资产置换区（§六层1层2层3）：确认组一卡联动、疑似组只提示不联动、九宫格粗标 overlay、
 * 传图/清空并排、必换未换锁点清单 + 全沿用（empty_confirm）。
 */
function AssetSection({
    detail,
    busy,
    setBusy,
    onWritten,
    onConfirmed,
}: {
    detail: HubTaskDetail;
    busy: BusyKind;
    setBusy: (kind: BusyKind) => void;
    onWritten: () => void;
    onConfirmed: () => void;
}) {
    const { message, modal } = App.useApp();
    const taskId = detail.local_id;
    const allCards = [...detail.asset_cards].sort((a, b) => a.n - b.n);
    // 影响镜次（AT-10 C05 信息下沉）：意图建议的镜次，纯透出，不新增识别调用。
    const shotMap = new Map<number, number[]>(detail.suggestions.map((item) => [item.n, [...item.镜次].sort((a, b) => a - b)]));
    const shotsOf = (rows: number[]) => [...new Set(rows.flatMap((n) => shotMap.get(n) || []))].sort((a, b) => a - b);

    // ---- AT-12 N1 按原片折叠（§2.2）：dismissed（已移除）与旧 keep_origin 都移出置换区，
    // 归顶部「已移除 · 按原片 N 项」折叠条（展开可恢复）。服务端集合优先，旧 bridge 缺字段回落卡决策派生。
    const originNs = detail.按原片行?.length ? [...detail.按原片行] : originRowNumbers(allCards);
    const originSet = new Set(originNs);
    const originRows = allCards.filter((card) => originSet.has(card.n));
    const cards = activeCards(allCards);

    // 确认组：同实体确认 ≥2 行的成员归到一组；一卡展示，一传全组联动（后端单点写）。
    const groupMap = new Map<string, HubAssetCard[]>();
    for (const card of cards) {
        if (card.同实体确认.length >= 2) {
            const key = [...card.同实体确认].sort((a, b) => a - b).join("-");
            const bucket = groupMap.get(key) || [];
            bucket.push(card);
            groupMap.set(key, bucket);
        }
    }
    const groupedNs = new Set<number>();
    for (const bucket of groupMap.values()) for (const card of bucket) groupedNs.add(card.n);
    const groups = [...groupMap.values()].map((bucket) => [...bucket].sort((a, b) => a.n - b.n));
    const singles = cards.filter((card) => !groupedNs.has(card.n));

    const mustCards = allCards.filter((card) => card.建议 === "必须换");
    // ---- AT-11 M3 锁点（§3.2 按原片豁免）：列表与计数走纯判定（与后端 lock_remaining 同口径，
    // dismissed 与旧 keep_origin 同豁免），移除/恢复成功后 onWritten() 回刷 detail 派生重算——
    // 无本地影子状态，轮询不闪回。
    const counters = lockCounters(allCards);
    const lockNs = lockRows(allCards);
    const unfilledMust = mustCards.filter((card) => lockNs.includes(card.n));

    // ---- AT-12 N1 勾选式合并（替换 AT-11 模式切换）：每张非组卡左上常驻 checkbox；勾选态=行号 Set
    // （禁存卡对象引用）。勾≥2 → 底部浮条「已勾选 N 项·[合并][移除][清空]」（N3 增批量移除）。
    const [selectedRows, setSelectedRows] = useState<Set<number>>(new Set());
    const [confirmOpen, setConfirmOpen] = useState(false);
    const [confirmRows, setConfirmRows] = useState<HubAssetCard[]>([]);
    const [originOpen, setOriginOpen] = useState(false);
    // taskId 切换 → 勾选态与确认窗一并清理（I2）；同任务 5 秒轮询不清（勾选跨轮询保留）。
    useEffect(() => {
        setSelectedRows(new Set());
        setConfirmOpen(false);
        setConfirmRows([]);
        setOriginOpen(false);
    }, [taskId]);
    // 轮询收口（I2）：被成组 / 被移除 / 行消失的勾选自动剔出，浮条计数诚实；
    // pruneSelection 无变化时返回原 Set 引用 → setState 直接 bail，不引发重渲染抖动。
    useEffect(() => {
        setSelectedRows((prev) => pruneSelection(prev, detail.asset_cards));
    }, [detail.asset_cards]);

    // 逐卡可选性（按原片行已折叠不在列；品牌视觉行与普通行同权，判定全在 mergeSelectable）。
    const selectableMap = new Map<number, { selectable: boolean; reason: string }>();
    for (const card of cards) selectableMap.set(card.n, mergeSelectable(card));
    // 合并候选=勾选且仍可选的行（轮询中变为不可选的行不计入，浮条数字诚实）。
    const mergeableSelected = cards.filter((card) => selectedRows.has(card.n) && selectableMap.get(card.n)?.selectable);
    const toggleRow = (card: HubAssetCard) => {
        const verdict = selectableMap.get(card.n);
        if (verdict && !verdict.selectable) {
            message.warning(verdict.reason);
            return;
        }
        setSelectedRows((prev) => toggleMergeRow(prev, card.n));
    };
    const clearSelection = () => setSelectedRows(new Set());
    // AT-12 N3 批量移除（所有者反馈）：浮条「移除」=勾选行逐行写 dismissed（与「…」菜单同一后端通道），
    // 顺序单飞（busy 锁）；部分失败如实报行号并保留失败行勾选，全成清空勾选。
    const dismissSelection = async () => {
        if (busy) return;
        const rows = mergeableSelected;
        if (!rows.length) return;
        setBusy("dismiss-batch");
        const failed: number[] = [];
        try {
            for (const row of rows) {
                try {
                    await postHubAsset(taskId, { n: row.n, decision: "dismissed" });
                } catch {
                    failed.push(row.n);
                }
            }
            if (failed.length) {
                setSelectedRows(new Set(failed));
                message.warning(`已移除 ${rows.length - failed.length} 项；失败行保留勾选：${failed.map((n) => `#${n}`).join("、")}`);
            } else {
                setSelectedRows(new Set());
                message.success(`已移除 ${rows.length} 项（按原片处理，可在顶部折叠条恢复）`);
            }
            onWritten();
        } finally {
            setBusy(null);
        }
    };
    const openConfirm = () => {
        // 打开确认小窗时拍快照（提交时复核组成员未变，§3.6）。
        setConfirmRows(mergeableSelected);
        setConfirmOpen(true);
    };

    // 确认合并提交：先复核成员未变（变了报错可重选）→ 多图分支先清空落选行（既有单行清空路径，
    // 必须在成组前做——组内清空会整组联动）→ 既有 groupRows + 主行/组名落盘（『组元数据』）。
    // 成功清空选中退出多选（I2）；失败保留选中可重选；撞他组（409 文案）点名「先拆开」。
    const submitMerge = async (opts: { rows: number[]; primary: number; name: string; keepN: number | null }): Promise<boolean> => {
        if (busy) return false;
        const changed = mergeMembershipChanged(opts.rows, detail.asset_cards);
        if (changed) {
            message.error(`合并失败：${changed}`);
            return false;
        }
        setBusy("group");
        try {
            if (opts.keepN !== null) {
                for (const card of detail.asset_cards) {
                    if (opts.rows.includes(card.n) && card.n !== opts.keepN && card.已传图.length) {
                        await postHubAsset(taskId, { n: card.n, clear: true });
                    }
                }
            }
            await groupHubRows(taskId, opts.rows, { primary: opts.primary, name: opts.name });
            message.success(`已合并：#${opts.rows.join("、#")}（主行 #${opts.primary}，此后传一张全组生效）`);
            setSelectedRows(new Set());
            setConfirmOpen(false);
            setConfirmRows([]);
            onWritten();
            return true;
        } catch (error) {
            const text = errorText(error);
            message.error(/已在他组|先解组|先拆开/.test(text) ? `其中一行已在他组，先拆开（${text}）` : `合并失败：${text}`);
            return false;
        } finally {
            setBusy(null);
        }
    };

    const [acked, setAcked] = useState<Set<number>>(new Set());
    const toggleAck = (n: number, on: boolean) =>
        setAcked((prev) => {
            const next = new Set(prev);
            if (on) next.add(n);
            else next.delete(n);
            return next;
        });

    const anyUploaded = allCards.some((card) => card.已传图.length) || Boolean(detail.has_pending_replace);
    const lockOk = unfilledMust.every((card) => acked.has(card.n));

    const confirmReplace = async () => {
        if (busy) return;
        // 全部行都没传图 = 空置换，后端硬拒 → 自动走 empty_confirm（合同放行通道）。
        const emptyConfirm = !anyUploaded;
        setBusy("confirm");
        try {
            const data = await confirmHubReplace(taskId, emptyConfirm, [...acked].sort((a, b) => a - b));
            message.success(data.全沿用 ? "已按全沿用确认，后台推进渲染" : "置换已确认，后台推进渲染；可关页等待");
            onConfirmed();
        } catch (error) {
            message.error(`确认置换失败：${errorText(error)}`);
        } finally {
            setBusy(null);
        }
    };

    const emptyConfirmAll = () => {
        modal.confirm({
            title: "全部按原片沿用？",
            content: `${allCards.length} 项资产全部按原片沿用，不换任何图；确认后直接进入渲染（提示词由后台生成）。`,
            okText: "确认全沿用",
            onOk: async () => {
                try {
                    await confirmHubReplace(taskId, true);
                    message.success("已确认全沿用，后台推进渲染");
                    onConfirmed();
                } catch (error) {
                    message.error(`全沿用确认失败：${errorText(error)}`);
                }
            },
        });
    };

    const doGroup = async (rows: number[]) => {
        if (busy) return;
        setBusy("group");
        try {
            await groupHubRows(taskId, rows);
            message.success(`已成组：#${rows.join("、#")}（此后一传全组联动）`);
            onWritten();
        } catch (error) {
            message.error(`成组失败：${errorText(error)}`);
        } finally {
            setBusy(null);
        }
    };

    const doUngroup = async (rows: number[]) => {
        if (busy) return;
        setBusy("ungroup");
        try {
            await ungroupHubRows(taskId, rows);
            // I1 文案收尾：拆开合并 = 解除联动，各行保留当前传图（不是历史撤销）。
            message.success(`已解除联动，各行保留当前传图（#${rows.join("、#")}）`);
            onWritten();
        } catch (error) {
            message.error(`解组失败：${errorText(error)}`);
        } finally {
            setBusy(null);
        }
    };

    // ---- AT-12 N1 单行资产决策（移除 dismissed / 旧按原片 keep_origin / 恢复 restore）：调 M1 set_asset
    // decision（后端拒绝组内行/非 awaiting_assets，报错原文如实透出）。成功 onWritten() 回刷——折叠条/
    // 锁点全部派生自服务端 detail，5 秒轮询回刷不闪回；busy="decision" 在途防重复点击。
    const doDecide = async (card: HubAssetCard, decision: "dismissed" | "keep_origin" | "restore") => {
        if (busy) return;
        setBusy("decision");
        try {
            await postHubAsset(taskId, { n: card.n, decision });
            message.success(decisionFeedback(card.n, decision));
            onWritten();
        } catch (error) {
            message.error(`#${card.n} 操作失败：${decisionErrorText(errorText(error))}`);
        } finally {
            setBusy(null);
        }
    };

    return (
        <section className="rounded-lg border border-stone-200 bg-card p-4 dark:border-stone-800">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
                <h2 className="text-sm font-semibold">资产置换</h2>
                <div className="flex items-center gap-2">
                    <span className="text-xs text-stone-500 dark:text-stone-400">看原片帧 → 传我们的图（换图走图床公网直链）；没传的按原片沿用</span>
                    {/* AT-12 N1 勾选式合并：勾选状态常驻卡上可见，无模式切换 */}
                    <span className="text-xs text-stone-400 dark:text-stone-500">勾选同一件资产的 ≥2 行 → 合并为一项</span>
                </div>
            </div>

            {/* AT-12 N1 已移除 · 按原片折叠条（§2.2）：dismissed 与旧 keep_origin 同归此处，展开可恢复 */}
            {originRows.length ? (
                <div className="mt-2 rounded-md border border-stone-200 bg-stone-50/70 px-2 py-1.5 dark:border-stone-800 dark:bg-stone-900/40" data-hub-origin-strip>
                    <button
                        type="button"
                        className="flex w-full items-center gap-2 text-left text-xs text-stone-500 transition hover:text-stone-700 dark:text-stone-400 dark:hover:text-stone-200"
                        onClick={() => setOriginOpen((prev) => !prev)}
                        data-hub-origin-toggle
                    >
                        <span className="font-medium">{originStripLabel(originRows.length)}</span>
                        <span className="text-stone-400 dark:text-stone-500">{originOpen ? "（收起）" : "（展开）"}</span>
                    </button>
                    {originOpen ? (
                        <div className="mt-1.5 flex flex-col gap-1">
                            {originRows.map((card) => (
                                <div
                                    key={card.n}
                                    className="flex flex-wrap items-center gap-2 rounded border border-dashed border-stone-300 bg-card px-2 py-1 text-xs dark:border-stone-700"
                                    data-hub-origin-row={card.n}
                                    data-hub-origin-decision={card.决策}
                                >
                                    <span className="font-mono text-stone-400 dark:text-stone-500">#{card.n}</span>
                                    <span className="text-stone-600 dark:text-stone-300">{card.短名 || card.描述.slice(0, 24)}</span>
                                    <Tag className="m-0" style={{ color: "rgb(120,113,108)", borderColor: "rgb(168,162,158)", borderStyle: "dashed", background: "transparent" }}>
                                        {card.决策 === "dismissed" ? "已移除 · 按原片" : "按原片"}
                                    </Tag>
                                    <Button
                                        size="small"
                                        type="link"
                                        className="ml-auto !px-1"
                                        loading={busy === "decision"}
                                        disabled={busy !== null}
                                        onClick={() => doDecide(card, "restore")}
                                        data-hub-origin-restore
                                    >
                                        恢复
                                    </Button>
                                </div>
                            ))}
                        </div>
                    ) : null}
                </div>
            ) : null}

            {/* 确认组：同一件东西·共N行（一卡展示，一传全组联动） */}
            {groups.map((bucket, index) => (
                <GroupCard
                    key={`group-${bucket.map((c) => c.n).join("-")}`}
                    bucket={bucket}
                    color={OVERLAY_COLORS[index % OVERLAY_COLORS.length]}
                    shots={shotsOf(bucket.map((card) => card.n))}
                    busy={busy}
                    setBusy={setBusy}
                    taskId={taskId}
                    onWritten={onWritten}
                    onUngroup={() => void doUngroup(bucket.map((card) => card.n))}
                />
            ))}

            {/* 非组卡：逐卡置换 + 疑似组「可能是同一件？[成组]」（只提示不联动） */}
            <div className="mt-3 space-y-3">
                {singles.length ? null : (
                    <div className="text-xs text-stone-400 dark:text-stone-500">
                        {cards.length ? "所有行都已确认成组。" : "置换区没有待处理的行（都已移除或成组）。"}
                    </div>
                )}
                {singles.map((card, index) => (
                    <AssetCardUnit
                        key={card.n}
                        card={card}
                        color={OVERLAY_COLORS[(groups.length + index) % OVERLAY_COLORS.length]}
                        shots={shotsOf([card.n])}
                        busy={busy}
                        setBusy={setBusy}
                        taskId={taskId}
                        onWritten={onWritten}
                        onGroup={() => void doGroup([card.n, ...card.同实体疑似])}
                        onDecide={doDecide}
                        checked={selectedRows.has(card.n) && Boolean(selectableMap.get(card.n)?.selectable)}
                        checkDisabledReason={selectableMap.get(card.n)?.reason || ""}
                        onToggleCheck={() => toggleRow(card)}
                    />
                ))}
            </div>

            {/* 勾选底部浮条（AT-12 N3：勾≥2 →「已勾选 N 项·[合并][移除][清空]」） */}
            {mergeableSelected.length >= 2 ? (
                <div
                    className="fixed bottom-6 left-1/2 z-30 flex -translate-x-1/2 items-center gap-3 rounded-full border border-stone-200 bg-card px-5 py-2.5 shadow-lg dark:border-stone-700"
                    data-hub-merge-bar
                >
                    <span className="text-sm font-medium">{mergeBarText(mergeableSelected.length)}</span>
                    <Button size="small" type="primary" disabled={busy !== null} loading={busy === "group"} onClick={openConfirm} data-hub-merge-submit>
                        合并
                    </Button>
                    <Button size="small" danger disabled={busy !== null} loading={busy === "dismiss-batch"} onClick={dismissSelection} data-hub-merge-dismiss>
                        移除
                    </Button>
                    <Button size="small" onClick={clearSelection} data-hub-merge-clear>
                        清空
                    </Button>
                </div>
            ) : null}

            {/* 确认小窗（§3.1：名称/主行可改落盘 + 影响预览 + 已传图三分支） */}
            <MergeConfirmModal
                open={confirmOpen}
                rows={confirmRows}
                shotMap={shotMap}
                submitting={busy === "group"}
                onCancel={() => setConfirmOpen(false)}
                onSubmit={submitMerge}
            />

            {/* 锁点清单 + 确认动作（§六层3） */}
            <div className="mt-4 space-y-2 border-t border-stone-100 pt-3 dark:border-stone-800">
                {mustCards.length ? (
                    <div className={`text-sm ${unfilledMust.length ? "text-amber-700 dark:text-amber-300" : "text-stone-500 dark:text-stone-400"}`} data-hub-lock-counters>
                        必换 {counters.must} 行：已换 {counters.replaced}·未换 {counters.unfilled}
                        {counters.keptOrigin ? `·按原片 ${counters.keptOrigin}` : ""}
                        {counters.unfilled ? "（未换行将按原片沿用，逐行勾选确认）" : ""}
                    </div>
                ) : (
                    <div className="text-sm text-stone-500 dark:text-stone-400">清单里没有标「必须换」的行；不传图即全部按原片沿用。</div>
                )}
                {unfilledMust.length ? (
                    <div className="flex flex-col gap-1.5">
                        {unfilledMust.map((card) => (
                            <Checkbox key={card.n} checked={acked.has(card.n)} onChange={(event) => toggleAck(card.n, event.target.checked)}>
                                <span className="text-sm">
                                    <span className="font-mono text-xs text-stone-400">#{card.n}</span> {card.短名 || card.描述.slice(0, 24)} — 知道，按原样
                                </span>
                            </Checkbox>
                        ))}
                    </div>
                ) : null}

                <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
                    <Tooltip title={lockOk ? "" : "必换行还有未换也未勾「按原样」的，先处理再确认"}>
                        <Button
                            type="primary"
                            icon={<CheckCircle2 className="size-4" />}
                            loading={busy === "confirm"}
                            disabled={!lockOk || busy !== null}
                            onClick={() => void confirmReplace()}
                        >
                            确认置换
                        </Button>
                    </Tooltip>
                    <Button loading={busy === "empty"} disabled={busy !== null} onClick={emptyConfirmAll}>
                        全部按原样沿用
                    </Button>
                </div>
                <div className="text-xs text-stone-400 dark:text-stone-500">确认后由后台推进渲染（前端不再触发渲染调用），可关闭本页等待。</div>
            </div>
        </section>
    );
}

/**
 * 确认组一卡通（AT-11 M2 合并卡）：徽标「#1+#3+#5 已合并」+组名+镜次并集；
 * 代表帧与唯一框**同源自主行**（组.代表帧/组.单框，单帧单框）——主行无框 → 诚实文案「主行未定位」，
 * 绝不搬组内其他行的框；旧 bridge 无组投影 → 回退 AT-10 共享帧渲染。
 * 「拆开合并」=解除联动，各行保留当前图与流水（§3.7，不是历史撤销）。
 */
function GroupCard({
    bucket,
    color,
    shots,
    busy,
    setBusy,
    taskId,
    onWritten,
    onUngroup,
}: {
    bucket: HubAssetCard[];
    color: string;
    shots: number[];
    busy: BusyKind;
    setBusy: (kind: BusyKind) => void;
    taskId: string;
    onWritten: () => void;
    onUngroup: () => void;
}) {
    const { modal } = App.useApp();
    // 组卡投影（AT-11 M1）：成员卡挂同一『组』对象；代表卡=主行（投影缺省=最小行）。
    const group = bucket.find((card) => card.组)?.组 ?? null;
    const members = group ? group.成员 : bucket.map((card) => card.n);
    const representative = (group ? bucket.find((card) => card.n === group.主行) : undefined) ?? bucket[0];
    const uploadedAll = [...new Set(bucket.flatMap((card) => card.已传图))];
    const unionShots = group && group.镜次并集.length ? group.镜次并集 : shots;
    const groupName = group?.组名 || "";
    const badge = group ? mergeBadge(members) : `同一件东西·共${bucket.length}行`;

    const confirmUngroup = () =>
        modal.confirm({
            title: `拆开合并 #${members.join("、#")}？`,
            content: "解除联动，各行保留当前传图（不是恢复合并前的历史值）。",
            okText: "拆开合并",
            onOk: onUngroup,
        });

    return (
        <div
            className="mt-3 rounded-lg border-2 border-dashed border-emerald-300 bg-emerald-50/40 p-3 dark:border-emerald-700 dark:bg-emerald-950/20"
            style={{ borderLeft: `4px solid ${color.replace(/0\.30\)$/, "0.85)")}` }}
            data-hub-group-card={group ? group.键 : ""}
        >
            <div className="flex flex-wrap items-center gap-2">
                <Tag color="success" className="m-0" data-hub-group-badge>
                    {badge}
                </Tag>
                <span className="text-sm font-semibold" data-hub-group-name>
                    {groupName || representative.短名 || `#${representative.n}`}
                </span>
                {representative.建议 === "必须换" ? <Tag color="red" className="m-0">必须换</Tag> : null}
                <span className="font-mono text-xs text-stone-400 dark:text-stone-500">#{members.map((n) => (n === representative.n ? `${n}（主行）` : n)).join("、#")}</span>
                {unionShots.length ? (
                    <span className="text-xs text-stone-400 dark:text-stone-500" data-hub-group-shots>
                        影响镜次：第{unionShots.join("、")}镜
                    </span>
                ) : null}
                <Button size="small" className="ml-auto" loading={busy === "ungroup"} disabled={busy !== null} onClick={confirmUngroup} data-hub-group-ungroup>
                    拆开合并
                </Button>
            </div>
            <div className="mt-1 text-xs text-emerald-700 dark:text-emerald-400">
                {group ? `合并后一体：传一张图 · 一个图号替换 #${members.join("、#")} 全部（解除联动各行保留当前传图）。` : "组内一行换图 = 全组生效（清单、流水、提交次序三处同步，由后端单点完成）。"}
            </div>
            <div className="mt-2 flex flex-wrap items-stretch gap-2">
                {group ? <GroupFrame group={group} className="shrink-0" /> : <FrameWithPosition card={representative} className="shrink-0" />}

                {uploadedAll.length ? <ArrowRight className="mx-0.5 size-5 shrink-0 self-center text-stone-300 dark:text-stone-600" /> : null}
                <AssetUploadZone
                    taskId={taskId}
                    n={representative.n}
                    uploaded={uploadedAll}
                    busy={busy}
                    setBusy={setBusy}
                    onWritten={onWritten}
                    hint={`传一张，#${members.join("、#")} 全组生效`}
                />
            </div>
            {/* 组内逐行回显（真源=清单，刷新后与组内其他行一致） */}
            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-stone-500 dark:text-stone-400">
                {bucket.map((card) => (
                    <span key={card.n}>
                        #{card.n} {card.短名}：{card.已传图.length ? `已换 ${card.已传图.length} 图` : "未换（随组联动）"}
                    </span>
                ))}
            </div>
        </div>
    );
}

/**
 * AT-11 M2 合并卡代表帧（§3.1.2 单帧单框）：帧与唯一框同源自主行投影（组.代表帧/组.单框），
 * 帧上**恰一枚框**（data-hub-box-row=主行）；主行无自身框 → 不画任何框+「主行未定位」诚实文案
 * （绝不搬组内其他行的框充数、不跨镜拼框）。几何口径与 AT-10 相同：contain 内容盒 + 百分比框。
 * AT-16 B3：scale（默认 1）只缩显示——建议区组成员行用 0.5 缩略图，框仍同源自主行投影。
 */
function GroupFrame({ group, className = "", scale = 1 }: { group: HubCardGroup; className?: string; scale?: number }) {
    const url = (group.代表帧.url || "").trim();
    const [ratio, setRatio] = useState<number | null>(null);
    const stageRef = useRef<HTMLDivElement | null>(null);

    // 比例探针同 FrameWithPosition（antd Image 不透传 onLoad + 缓存早挂载，挂载期主动查一次）。
    useEffect(() => {
        setRatio(null);
        if (!url) return;
        let alive = true;
        const apply = (img: HTMLImageElement | null) => {
            const next = ratioFromImage(img);
            if (alive && next !== null) setRatio(next);
        };
        apply(stageRef.current?.querySelector("img") ?? null);
        const probe = new Image();
        probe.onload = () => apply(probe);
        probe.onerror = () => apply(null);
        probe.src = hubAssetDisplayUrl(url); // AT-16 B1a：取图同样走显示层换算点（与 <img src> 同源）
        apply(probe);
        return () => {
            alive = false;
            probe.onload = null;
            probe.onerror = null;
        };
    }, [url]);

    const stage = stageSize(ratio);
    const content = containRect(ratio, stage);
    const boxRow = groupBoxRow(group); // 恰一枚框（行号=主行）；null=主行未定位（诚实降级）
    const rect = boxRow && group.单框 ? boxRect(group.单框, content) : null;
    const located = Boolean(url) && (group.代表帧.定位 || "").trim() === "精确";
    const shot = group.代表帧.镜次;
    const caption = located
        ? [typeof shot === "number" && shot > 0 ? `第${shot}镜` : "", formatSeconds(group.代表帧.秒), "AI粗标"].filter(Boolean).join(" · ")
        : "未能定位到该资产出现的镜头 · 暂用整片画面";
    const scaled = scale !== 1;

    return (
        <div className={className} data-hub-group-frame data-hub-frame-state={boxRow ? "precise" : "unlocated"} data-hub-frame-scale={scale}>
            <div className="relative" style={scaled ? { width: Math.round(stage.w * scale), height: Math.round(stage.h * scale) } : undefined}>
                <div
                    ref={stageRef}
                    className="relative shrink-0 overflow-hidden rounded-md bg-stone-100 dark:bg-stone-900"
                    style={{
                        width: stage.w,
                        height: stage.h,
                        ...(scaled ? { transform: `scale(${scale})`, transformOrigin: "top left" } : null),
                    }}
                    data-hub-frame-stage
                    data-hub-frame-state={boxRow ? "precise" : "unlocated"}
                >
                    {url ? (
                        <AntdImage src={hubAssetDisplayUrl(url)} width={stage.w} height={stage.h} style={{ objectFit: "contain", display: "block" }} alt={`#${group.主行} 主行代表帧`} />
                    ) : (
                        <div className="flex size-full flex-col items-center justify-center gap-1 px-2 text-center text-xs text-stone-400 dark:text-stone-500">
                            <ImageOff className="size-4" />
                            <span>未定位（无可用帧）</span>
                        </div>
                    )}

                    {/* 内容盒：与 contain 后图片实际内容区重合；组卡只放主行那一枚框 */}
                    {url ? (
                        <div className="pointer-events-none absolute" style={{ left: content.x, top: content.y, width: content.w, height: content.h }} data-hub-frame-content-box>
                            {boxRow && rect ? (
                                <div
                                    data-hub-box-row={boxRow.row}
                                    data-hub-box-own="1"
                                    className="absolute rounded-[2px]"
                                    style={{
                                        left: rect.x,
                                        top: rect.y,
                                        width: rect.w,
                                        height: rect.h,
                                        border: "2px solid rgb(225,29,72)",
                                        backgroundColor: "rgba(244,63,94,0.14)",
                                    }}
                                >
                                    {badgeFits(rect) ? (
                                        <span className="absolute left-0.5 top-0.5 rounded px-1 text-[10px] leading-4" data-hub-box-badge style={{ backgroundColor: "rgb(225,29,72)", color: "#fff" }}>
                                            #{boxRow.row}
                                        </span>
                                    ) : null}
                                </div>
                            ) : null}
                        </div>
                    ) : null}
                </div>
            </div>
            <div className="mt-1 max-w-full px-0.5 text-left text-[11px] text-stone-500 dark:text-stone-400" data-hub-frame-caption>
                <div>{caption}{located ? "（点图放大）" : ""}</div>
                {url && !boxRow ? (
                    <Tag color="warning" className="m-0 mt-0.5 !text-[10px]" data-hub-group-degraded>
                        主行未定位
                    </Tag>
                ) : null}
            </div>
        </div>
    );
}

/** 单卡置换单元：帧+overlay、传图/清空并排、疑似组提示按钮、品牌文字（品牌视觉卡）。
 *  AT-12 N1：左上常驻 checkbox（勾选状态直接可见，无模式切换）；品牌行置灰并注明原因。 */
function AssetCardUnit({
    card,
    color,
    shots,
    busy,
    setBusy,
    taskId,
    onWritten,
    onGroup,
    onDecide,
    checked = false,
    checkDisabledReason = "",
    onToggleCheck,
}: {
    card: HubAssetCard;
    color: string;
    shots: number[];
    busy: BusyKind;
    setBusy: (kind: BusyKind) => void;
    taskId: string;
    onWritten: () => void;
    onGroup: () => void;
    /** AT-12 N1 单行资产决策（dismissed/restore）；组卡不挂（组内行入口隐藏）。 */
    onDecide?: (card: HubAssetCard, decision: "dismissed" | "keep_origin" | "restore") => void;
    /** 勾选式合并：本卡 checkbox 勾选态（行号 Set 派生，轮询不丢）。 */
    checked?: boolean;
    /** checkbox 置灰原因（品牌行等）；非空 → 置灰+tooltip。 */
    checkDisabledReason?: string;
    onToggleCheck?: () => void;
}) {
    const { message } = App.useApp();
    const [brandText, setBrandText] = useState("");
    const brand = card.类别.includes("品牌视觉");
    // AT-12 N1 决策菜单项（§2.2）：仅必换/建议准备行显示「移除（不需要替换，按原片处理）」；
    // 已按原片行显示「恢复替换」；组内行 null（组卡上不出现）。
    const decision: RowDecisionAction = rowDecisionAction(card);
    const decisionMenu = decision && onDecide
        ? [
              {
                  key: decision.action,
                  label: decision.label,
                  disabled: busy !== null,
                  onClick: () => onDecide(card, decision.action),
              },
          ]
        : [];
    const checkbox = (
        <Tooltip title={checkDisabledReason}>
            <span className="inline-flex items-center" onClick={(event) => event.stopPropagation()}>
                <Checkbox
                    checked={checked}
                    disabled={Boolean(checkDisabledReason) || busy !== null}
                    onChange={() => onToggleCheck?.()}
                    data-hub-check-row={card.n}
                    data-hub-check-state={checkDisabledReason ? "disabled" : checked ? "checked" : "idle"}
                >
                    <span className="text-xs text-stone-500 dark:text-stone-400">选</span>
                </Checkbox>
            </span>
        </Tooltip>
    );

    const saveText = async () => {
        const text = brandText.trim();
        if (!text || busy) return;
        setBusy("upload");
        try {
            await postHubAsset(taskId, { n: card.n, text });
            message.success(`#${card.n} 品牌文字已写入清单`);
            onWritten();
        } catch (error) {
            message.error(`#${card.n} 写入失败：${errorText(error)}`);
        } finally {
            setBusy(null);
        }
    };

    return (
        <div
            className={`rounded-lg border p-3 dark:border-stone-800 ${checked ? "!border-indigo-500 ring-2 ring-indigo-200 dark:ring-indigo-900" : ""}`}
            style={{ borderLeft: `4px solid ${color.replace(/0\.30\)$/, "0.85)")}` }}
            data-hub-card-row={card.n}
            data-hub-merge-selected={checked ? "1" : "0"}
        >
            <header className="flex flex-wrap items-center gap-2">
                {checkbox}
                {card.建议 === "必须换" ? <Tag color="red" className="m-0">必须换</Tag> : card.建议 === "建议准备" ? <Tag color="blue" className="m-0">建议准备</Tag> : null}
                {decisionBadgeText(card) ? (
                    // 降级行徽标（§3.2）：灰·虚线，与必换红/建议蓝区分；文字=「按原片」，非空恒显。
                    <Tag className="m-0" data-hub-decision-badge style={{ color: "rgb(120,113,108)", borderColor: "rgb(168,162,158)", borderStyle: "dashed", background: "transparent" }}>
                        {decisionBadgeText(card)}
                    </Tag>
                ) : null}
                <span className="text-sm font-semibold">{card.短名 || `#${card.n}`}</span>
                <span className="font-mono text-xs text-stone-400 dark:text-stone-500">#{card.n}</span>
                <span className="text-xs text-stone-400 dark:text-stone-500">{card.类别}</span>
                {card.已传图.length ? <Tag color="success" className="m-0">已换图</Tag> : <Tag className="m-0">未换·沿用原片</Tag>}
                {card.同实体确认.length >= 2 ? (
                    <Tag color="cyan" className="m-0">确认组 #{card.同实体确认.join("、#")}</Tag>
                ) : null}
                {shots.length ? <span className="text-xs text-stone-400 dark:text-stone-500">影响镜次：第{shots.join("、")}镜</span> : null}
                {/* AT-11 M3 单行降级入口（§3.2）：非组卡「…」菜单；可沿用行/组内行/多选态不渲染 */}
                {decisionMenu.length ? (
                    <span className="ml-auto" data-hub-decision-menu>
                        <Dropdown
                            trigger={["click"]}
                            menu={{ items: decisionMenu }}
                        >
                            <Button size="small" type="text" icon={<MoreHorizontal className="size-4" />} aria-label="更多操作" loading={busy === "decision"} disabled={busy !== null} />
                        </Dropdown>
                    </span>
                ) : null}
            </header>

            <div className="mt-2 flex flex-wrap items-stretch gap-2">
                <FrameWithPosition card={card} className="shrink-0" />
                {card.已传图.length ? <ArrowRight className="mx-0.5 size-5 shrink-0 self-center text-stone-300 dark:text-stone-600" /> : null}
                <AssetUploadZone taskId={taskId} n={card.n} uploaded={card.已传图} busy={busy} setBusy={setBusy} onWritten={onWritten} />
            </div>

            {/* 疑似组：只提示 + 一键成组，绝不自动联动（#3↔#5 误连实证反例）；要多行合一走勾选合并 */}
            {card.同实体疑似.length ? (
                <div className="mt-2 flex flex-wrap items-center gap-2 rounded-md bg-amber-50/70 px-2 py-1.5 text-xs text-amber-700 dark:bg-amber-950/20 dark:text-amber-300">
                    <span>可能是同一件？与 #{card.同实体疑似.join("、#")} 疑似同实体（现不联动，各自独立换图）</span>
                    <Button size="small" loading={busy === "group"} disabled={busy !== null} onClick={onGroup}>
                        成组
                    </Button>
                </div>
            ) : null}

            {brand ? (
                <div className="mt-2 flex flex-wrap items-center gap-2">
                    <Type className="size-4 shrink-0 text-stone-400" />
                    <Input size="small" value={brandText} placeholder="品牌文字（品牌名 / 品类名 / slogan）" className="max-w-xs" disabled={busy !== null} onChange={(event) => setBrandText(event.target.value)} onPressEnter={() => void saveText()} />
                    <Button size="small" type="primary" ghost loading={busy === "upload"} onClick={() => void saveText()}>
                        写入品牌文字
                    </Button>
                </div>
            ) : null}

            {card.描述 ? (
                <details className="mt-2">
                    <summary className="cursor-pointer list-none text-xs text-stone-400 transition hover:text-stone-600 dark:text-stone-500 dark:hover:text-stone-300 [&::-webkit-details-marker]:hidden">查看描述</summary>
                    <div className="thin-scrollbar mt-1 max-h-32 overflow-auto rounded bg-stone-50 px-2 py-1.5 text-xs leading-5 text-stone-600 dark:bg-stone-900 dark:text-stone-300">
                        {card.描述}
                        {card.时段来源 ? <div className="mt-1 text-[11px] text-stone-400 dark:text-stone-500">时段依据：{card.时段来源}</div> : null}
                    </div>
                </details>
            ) : null}
        </div>
    );
}

/**
 * AT-11 M2 确认小窗（§3.1）：合并后名称（默认主行短名可改）、主行（默认行号最小可改——落盘『组元数据』）、
 * 影响预览（镜次并集 + 传 1 张图 1 个图号替换全部）、已传图三分支（B3）：
 * 零图=直接组；一张唯一图=提示「沿用 #N 已传图统一到全组」；多张不同图=列出点名保留一张（其余行图
 * 清空走既有清空路径，流水如实记录）或取消合并不悄悄任选。
 * 提交失败不关窗（可重选）；父层负责提交编排与成员复核。
 */
function MergeConfirmModal({
    open,
    rows,
    shotMap,
    submitting,
    onCancel,
    onSubmit,
}: {
    open: boolean;
    rows: HubAssetCard[];
    shotMap: Map<number, number[]>;
    submitting: boolean;
    onCancel: () => void;
    onSubmit: (opts: { rows: number[]; primary: number; name: string; keepN: number | null }) => Promise<boolean>;
}) {
    // rows=打开确认窗时的快照（父层拍定），此处排序仅展示用。
    const sortedRows = useMemo(() => [...rows].sort((a, b) => a.n - b.n), [rows]);
    const branch = useMemo(() => mergeImageBranch(sortedRows), [sortedRows]);
    const [primary, setPrimary] = useState<number>(sortedRows[0]?.n ?? 0);
    const [name, setName] = useState(sortedRows[0]?.短名 || "");
    const [keepN, setKeepN] = useState<number | null>(branch.branch === "multi" ? branch.candidates[0].n : null);
    const [error, setError] = useState("");
    const { message } = App.useApp();

    // AT-11 M5 缺陷修复：本组件在 AssetSection 里常驻挂载（只靠 open 控显隐），
    // Modal 的 destroyOnHidden 只销毁子 DOM、不重置组件 state——上面的 useState
    // 初值只在首次挂载时算一次，而首挂载时 rows=[]（primary=0、name=""、keepN=null）。
    // openConfirm 之后填入快照也不会重算 → 确认合并被 `!primary` 校验拦下（M4 ego 实测
    // 3 秒后 sidecar 无组键）。这里在 open 变 true（或快照变化）时重派生全部初值。
    useEffect(() => {
        if (!open) return;
        setPrimary(sortedRows[0]?.n ?? 0);
        setName(sortedRows[0]?.短名 || "");
        setKeepN(branch.branch === "multi" ? branch.candidates[0].n : null);
        setError("");
    }, [open, sortedRows, branch]);

    const shotUnion = mergeShotUnion(sortedRows.map((card) => shotMap.get(card.n) || []));

    const submit = async () => {
        if (!sortedRows.length) return;
        if (!primary || !sortedRows.some((card) => card.n === primary)) {
            setError("请选主行（主行必须是本次合并的行之一）");
            message.warning("请选主行（主行必须是本次合并的行之一）");
            return;
        }
        if (branch.branch === "multi" && keepN === null) {
            setError("多张不同图：请点名保留哪一张，或取消合并");
            message.warning("多张不同图：请点名保留哪一张，或取消合并");
            return;
        }
        setError("");
        await onSubmit({ rows: sortedRows.map((card) => card.n), primary, name: name.trim(), keepN: branch.branch === "multi" ? keepN : null });
    };

    return (
        <Modal
            title={`合并为一项（已选 ${sortedRows.length} 行）`}
            open={open}
            onCancel={onCancel}
            footer={null}
            width={560}
            destroyOnHidden
        >
            <div data-hub-merge-modal>
            {sortedRows.length ? (
                <div className="space-y-4 py-1">
                    {/* 影响预览（§3.1）：镜次并集 + 一图一号替换全部 */}
                    <div className="rounded-md bg-stone-50 px-3 py-2 text-sm dark:bg-stone-900" data-hub-merge-preview>
                        <div className="font-medium">影响预览</div>
                        <div className="mt-1 text-xs text-stone-500 dark:text-stone-400">
                            合并 #{sortedRows.map((card) => card.n).join("、#")}
                            {shotUnion.length ? ` · 镜次并集：第 ${shotUnion.join("、")} 镜` : ""}
                            {branch.branch === "single" ? " · 沿用既有图" : " · 传 1 张图 · 1 个图号替换全部"}
                        </div>
                    </div>

                    {/* 已传图三分支（B3） */}
                    {branch.branch === "empty" ? (
                        <div className="rounded-md border border-stone-200 px-3 py-2 text-xs text-stone-500 dark:border-stone-800 dark:text-stone-400" data-hub-merge-branch="empty">
                            组内暂无已传图：直接合并，之后传一张全组生效。
                        </div>
                    ) : branch.branch === "single" ? (
                        <div className="rounded-md border border-emerald-200 bg-emerald-50/60 px-3 py-2 text-xs text-emerald-700 dark:border-emerald-800 dark:bg-emerald-950/20 dark:text-emerald-300" data-hub-merge-branch="single">
                            沿用 #{branch.keepN} 已传图统一到全组（合并后全组同图同图号）。
                        </div>
                    ) : (
                        <div className="rounded-md border border-amber-200 bg-amber-50/60 px-3 py-2 text-xs text-amber-700 dark:border-amber-800 dark:bg-amber-950/20 dark:text-amber-300" data-hub-merge-branch="multi">
                            <div className="font-medium">所选行有多张不同图，保留哪一张？</div>
                            <Radio.Group className="mt-1.5 flex flex-col gap-1" value={keepN} onChange={(event) => setKeepN(event.target.value)}>
                                {branch.candidates.map((pair) => (
                                    <Radio key={`${pair.n}-${pair.url}`} value={pair.n}>
                                        保留 #{pair.n} 的图
                                    </Radio>
                                ))}
                            </Radio.Group>
                            <div className="mt-1 text-[11px] text-amber-600/80 dark:text-amber-300/80">其余行已传图将清空（流水如实记录）；也可取消合并不动任何图。</div>
                        </div>
                    )}

                    {/* 主行（默认行号最小可改——落盘『组元数据』，代表帧/单框/图号锚定它） */}
                    <div className="flex flex-wrap items-center gap-2 text-sm">
                        <span className="shrink-0">主行</span>
                        <span data-hub-merge-primary className="inline-flex">
                            <Select
                                size="small"
                                className="min-w-40"
                                value={primary}
                                onChange={(value) => {
                                    setPrimary(value);
                                    setName(sortedRows.find((card) => card.n === value)?.短名 || name);
                                }}
                                options={sortedRows.map((card) => ({ value: card.n, label: `#${card.n} ${card.短名 || card.描述.slice(0, 16)}` }))}
                            />
                        </span>
                        <span className="text-xs text-stone-400 dark:text-stone-500">代表帧与合并卡取主行</span>
                    </div>

                    {/* 合并后名称（默认主行短名可改） */}
                    <div className="flex flex-wrap items-center gap-2 text-sm">
                        <span className="shrink-0">合并后名称</span>
                        <Input size="small" className="min-w-60 flex-1" value={name} onChange={(event) => setName(event.target.value)} placeholder="合并后叫什么（默认主行短名）" data-hub-merge-name />
                    </div>

                    {error ? <div className="text-xs text-red-600 dark:text-red-400">{error}</div> : null}

                    <div className="flex justify-end gap-2">
                        <Button size="small" onClick={onCancel}>
                            取消
                        </Button>
                        <Button size="small" type="primary" loading={submitting} onClick={() => void submit()} data-hub-merge-confirm>
                            确认合并
                        </Button>
                    </div>
                </div>
            ) : (
                <div className="py-6 text-center text-sm text-stone-500 dark:text-stone-400">没有可合并的行（请先在多选态选中 ≥2 行）。</div>
            )}
            </div>
        </Modal>
    );
}

/** 角标（建议列原文）配色：与 AT-8 卡片角标同源，禁新增识别调用。 */
const BOX_ACCENT: Record<string, string> = {
    必须换: "rgb(225,29,72)",
    建议准备: "rgb(37,99,235)",
    可沿用: "rgb(120,113,108)",
};
const accentOf = (label: string) => BOX_ACCENT[label] || BOX_ACCENT["可沿用"];

/** 帧取图口径（代表帧优先、旧字段兜底）——FrameWithPosition 与建议区锚点共用同一份解析，禁第二套。 */
function frameUrlOf(card: HubAssetCard): string {
    return (card.代表帧.url || "").trim() || card.帧.map((frame) => (frame.url || "").trim()).find(Boolean) || "";
}

/**
 * 共享帧 + 同帧多红框（AT-10 C3）：overlay 与图片共用 contain 后**实际内容盒**（不是外框），
 * 九宫格百分比直接落内容盒；全部框保留红色边界，本卡加粗实红、其他行红半透明；
 * 图例 rail 逐行号排布（行号·短名·角标），引线汇聚到各自框——同格多框也逐行号可见可分。
 * 三态诚实：未定位→不画框不标镜次+「未能定位到该资产出现的镜头·暂用整片画面」；
 * 无自身位置→帧与镜次照给、他人框照画、描述首句+「未能标注位置」。旧字段（帧[]/画面位置）仍驱动 legacy 分支。
 * AT-16 B3：scale（默认 1）只缩显示不动几何——建议区锚点用 0.5 缩略图，置换区仍 1:1；
 * showLegend=false 供缩略图关掉不可读的图例 rail（框与角标照画）。
 */
function FrameWithPosition({
    card,
    className = "",
    scale = 1,
    showLegend = true,
}: {
    card: HubAssetCard;
    className?: string;
    /** 显示缩放（1=原样）；几何仍由 frame-overlay 同一套函数算，不另画一套框。 */
    scale?: number;
    /** 是否渲染右侧图例 rail（缩略图里不可读 → 建议区关掉）。 */
    showLegend?: boolean;
}) {
    const state = frameState(card);
    const url = frameUrlOf(card);
    const [ratio, setRatio] = useState<number | null>(null);
    const [hoverRow, setHoverRow] = useState<number | null>(null);
    const stageRef = useRef<HTMLDivElement | null>(null);

    /**
     * 帧真实宽高比（C4 修 C3 真机缺陷）：只靠 onLoad 必挂——antd Image（@rc-component/image）内部 `<img>`
     * 只吃 COMMON_PROPS 白名单（无 onLoad）＋自己的 srcAndOnload，外部 onLoad 根本不透传；命中缓存时
     * load 事件又早于 React 挂载。两种情形 ratio 恒 null → 竖屏帧按 16:9 画，框落到 letterbox 黑边。
     * 所以挂载/换图时主动查一次：① DOM 里已渲染的 img（缓存命中 immediate complete）；
     * ② 自带 Image 探针（未命中时由 probe.onload 回来，命中时同 URL 亦走缓存）。
     */
    useEffect(() => {
        setRatio(null);
        if (!url) return;
        let alive = true;
        const apply = (img: HTMLImageElement | null) => {
            const next = ratioFromImage(img);
            if (alive && next !== null) setRatio(next);
        };
        apply(stageRef.current?.querySelector("img") ?? null);
        const probe = new Image();
        probe.onload = () => apply(probe);
        probe.onerror = () => apply(null);
        probe.src = hubAssetDisplayUrl(url); // AT-16 B1a：取图同样走显示层换算点（与 <img src> 同源）
        apply(probe);
        return () => {
            alive = false;
            probe.onload = null;
            probe.onerror = null;
        };
    }, [url]);

    const stage = stageSize(ratio);
    const content = containRect(ratio, stage);
    // 未定位态：不画任何框（合同：帧内框=[] 时也不画）；legacy 只画旧字段那一格。
    const boxes = state === "precise" || state === "no-own-box" ? card.帧内框 : [];
    const laid = boxes
        .map((box) => ({ box, rect: boxRect(box, content) }))
        .sort((a, b) => Number(a.box.本卡) - Number(b.box.本卡));
    const legend = layoutLegend(showLegend ? laid.map(({ box, rect }) => ({ 行: box.行, box: rect })) : [], stage);

    const legacyPosition = state === "legacy" ? card.画面位置 : null;
    const legacyRect =
        legacyPosition && legacyPosition.格 >= 1 && legacyPosition.格 <= 9
            ? boxRect(
                  {
                      "x%": ((legacyPosition.格 - 1) % 3) * (100 / 3),
                      "y%": Math.floor((legacyPosition.格 - 1) / 3) * (100 / 3),
                      "w%": 100 / 3,
                      "h%": 100 / 3,
                  },
                  content,
              )
            : null;

    const caption = frameCaption(card, state);
    const firstDesc = firstSentence(card.描述);
    const others = otherBoxCount(laid.map(({ box }) => box));
    const wrapperW = legend.chips.length ? legend.x + legend.w : stage.w;
    const wrapperH = Math.max(stage.h, legend.height);
    // AT-16 B3：scale≠1 时用一层定尺寸壳子占位（transform 不改布局），壳内仍按 1:1 几何渲染后整体缩放——
    // 框/内容盒/图例全部同源缩放，不存在第二套坐标；**说明文字留在外层**（按容器宽排，不跟着缩成一条）。
    const scaled = scale !== 1;

    return (
        <div className={className} data-hub-frame-state={state} data-hub-frame-scale={scale}>
            <div
                className="relative"
                style={scaled ? { width: Math.round(wrapperW * scale), height: Math.round(wrapperH * scale) } : undefined}
            >
                <div
                    className="relative flex items-start"
                    style={{
                        width: wrapperW,
                        height: wrapperH,
                        ...(scaled ? { transform: `scale(${scale})`, transformOrigin: "top left" } : null),
                    }}
                >
                {/* 舞台：尺寸按帧真实宽高比定（竖屏窄、横屏宽）；overflow 只裁图片圆角，标签全在舞台之外 */}
                <div
                    ref={stageRef}
                    className="relative shrink-0 overflow-hidden rounded-md bg-stone-100 dark:bg-stone-900"
                    style={{ width: stage.w, height: stage.h }}
                    data-hub-frame-stage
                    data-hub-frame-state={state}
                >
                    {url ? (
                        <AntdImage
                            src={hubAssetDisplayUrl(url)}
                            width={stage.w}
                            height={stage.h}
                            style={{ objectFit: "contain", display: "block" }}
                            alt={`#${card.n} 原片帧`}
                        />
                    ) : (
                        <div className="flex size-full flex-col items-center justify-center gap-1 px-2 text-center text-xs text-stone-400 dark:text-stone-500">
                            <ImageOff className="size-4" />
                            <span>未定位（无可用帧）</span>
                        </div>
                    )}

                    {/* 内容盒：与 contain 后图片实际内容区重合，框百分比只落在它里面 */}
                    {url ? (
                        <div
                            className="pointer-events-none absolute"
                            style={{ left: content.x, top: content.y, width: content.w, height: content.h }}
                            data-hub-frame-content-box
                        >
                            {/* 九宫格参考线（极淡，帮助读格） */}
                            <div aria-hidden className="absolute inset-0">
                                <div className="absolute inset-y-0 left-1/3 w-px bg-white/25" />
                                <div className="absolute inset-y-0 left-2/3 w-px bg-white/25" />
                                <div className="absolute inset-x-0 top-1/3 h-px bg-white/25" />
                                <div className="absolute inset-x-0 top-2/3 h-px bg-white/25" />
                            </div>

                            {laid.map(({ box, rect }) => {
                                const active = hoverRow === box.行;
                                return (
                                    <div
                                        key={box.行}
                                        data-hub-box-row={box.行}
                                        data-hub-box-own={box.本卡 ? "1" : "0"}
                                        className="absolute rounded-[2px]"
                                        style={{
                                            left: rect.x,
                                            top: rect.y,
                                            width: rect.w,
                                            height: rect.h,
                                            border: box.本卡 ? "2px solid rgb(225,29,72)" : "1px solid rgba(244,63,94,0.45)",
                                            backgroundColor: box.本卡 ? "rgba(244,63,94,0.14)" : "rgba(244,63,94,0.06)",
                                            outline: active ? "2px solid rgba(225,29,72,0.6)" : undefined,
                                            opacity: active || box.本卡 ? 1 : 0.8,
                                            zIndex: active ? 30 : box.本卡 ? 20 : 10,
                                        }}
                                    >
                                        {badgeFits(rect) ? (
                                            <span
                                                className="absolute left-0.5 top-0.5 rounded px-1 text-[10px] leading-4"
                                                style={
                                                    box.本卡
                                                        ? { backgroundColor: "rgb(225,29,72)", color: "#fff" }
                                                        : { backgroundColor: "rgba(255,255,255,0.9)", color: "rgb(190,18,60)" }
                                                }
                                            >
                                                #{box.行}
                                            </span>
                                        ) : null}
                                    </div>
                                );
                            })}

                            {legacyRect ? (
                                <div
                                    aria-hidden
                                    className="absolute border-2"
                                    style={{
                                        left: legacyRect.x,
                                        top: legacyRect.y,
                                        width: legacyRect.w,
                                        height: legacyRect.h,
                                        borderColor: "rgba(244,63,94,0.85)",
                                        backgroundColor: "rgba(244,63,94,0.30)",
                                    }}
                                />
                            ) : null}
                        </div>
                    ) : null}
                </div>

                {/* 图例 rail + 引线：同一格的多行各占一条，不按格去重身份 */}
                {legend.chips.length ? (
                    <>
                        <svg className="pointer-events-none absolute inset-0" width={wrapperW} height={wrapperH} aria-hidden data-hub-frame-links>
                            {legend.links.map((link) => (
                                <line
                                    key={link.行}
                                    x1={link.x1}
                                    y1={link.y1}
                                    x2={link.x2}
                                    y2={link.y2}
                                    stroke="rgba(225,29,72,0.45)"
                                    strokeWidth={1}
                                    strokeDasharray="3 2"
                                />
                            ))}
                        </svg>
                        <ul className="absolute flex flex-col gap-1" style={{ left: legend.x, top: 0, width: legend.w }} data-hub-frame-legend>
                            {legend.chips.map((chip) => {
                                const item = laid.find(({ box }) => box.行 === chip.行);
                                if (!item) return null;
                                const { box } = item;
                                return (
                                    <li
                                        key={chip.行}
                                        data-hub-box-row={chip.行}
                                        data-hub-box-own={box.本卡 ? "1" : "0"}
                                        onMouseEnter={() => setHoverRow(box.行)}
                                        onMouseLeave={() => setHoverRow(null)}
                                        className={`flex cursor-default flex-col justify-center overflow-hidden rounded border px-1 leading-tight ${
                                            box.本卡
                                                ? "border-rose-500 bg-rose-50 text-rose-700 dark:border-rose-400 dark:bg-rose-950/40 dark:text-rose-200"
                                                : "border-stone-200 bg-white/85 text-stone-500 dark:border-stone-700 dark:bg-stone-900/70 dark:text-stone-400"
                                        }`}
                                        style={{ height: chip.rect.h, opacity: box.本卡 ? 1 : 0.85 }}
                                        title={`#${box.行} ${box.短名}｜${box.角标}`}
                                    >
                                        <span className="flex items-center gap-1">
                                            <span className={`font-mono text-[10px] ${box.本卡 ? "font-bold" : ""}`}>#{box.行}</span>
                                            <span className={`truncate ${chip.compact ? "text-[10px]" : "text-[11px]"} ${box.本卡 ? "font-semibold" : ""}`}>
                                                {box.短名 || "（未命名）"}
                                            </span>
                                            <span
                                                aria-hidden
                                                className="ml-auto size-1.5 shrink-0 rounded-full"
                                                style={{ backgroundColor: accentOf(box.角标) }}
                                            />
                                        </span>
                                        {chip.compact ? null : (
                                            <span className="truncate text-[10px]" style={{ color: accentOf(box.角标) }}>
                                                {box.角标}
                                            </span>
                                        )}
                                    </li>
                                );
                            })}
                        </ul>
                    </>
                ) : null}
                </div>
            </div>

            <div className="mt-1 max-w-full px-0.5 text-left text-[11px] text-stone-500 dark:text-stone-400" data-hub-frame-caption>
                {state === "unlocated" ? (
                    <span className="text-amber-700 dark:text-amber-300">{caption.main}</span>
                ) : (
                    <>
                        <div>
                            {caption.main}
                            {caption.hint ? <span className="text-stone-400 dark:text-stone-500">（{caption.hint}）</span> : null}
                        </div>
                        {caption.honest ? (
                            <Tag color="warning" className="m-0 mt-0.5 !text-[10px]">
                                {caption.honest}
                            </Tag>
                        ) : null}
                        {state === "no-own-box" && firstDesc ? (
                            <div className="line-clamp-2 text-stone-400 dark:text-stone-500">{firstDesc}</div>
                        ) : null}
                        {others ? <div className="text-stone-400 dark:text-stone-500">同画面其他资产 {others} 项（图上红框，非本行换图对象）</div> : null}
                    </>
                )}
            </div>
        </div>
    );
}

/** 资产引用健康态（AT-14 B3）：ref 原串 → local|ok|expired|unknown。由 Drawer 顶层注入，
 *  卡片深处（AssetUploadZone）直接取——避免把一份只读映射逐层穿过七个子组件。 */
const AssetStateContext = createContext<Record<string, string>>({});

/* hub-asset-exit:begin —— 受控资产出口（AT-16 B1a）。
   全 drawer **唯一**允许把资产引用赋给 <img src> / <a href> 的地方：只收 ref 原串，
   内部过 hubAssetRefView()（= hub-api 显示层唯一换算点 hubAssetDisplayUrl）出 src/href。
   外部防再漏检查（web/scripts/probe-at16-b1a.mjs）按这对标记划白名单——标记之外任何
   「.url / ref 来源表达式（含 props 间接传入）直接赋给 src=/href=」一律判红。 */
export function UploadedThumb({
    assetRef,
    state,
    alt,
    thumbClass,
}: {
    /** 数据层原串（local:任务号/asset-uploads/xxx.png 或旧公网直链）；健康态查键也用它。 */
    assetRef: string;
    /** local|ok|expired|unknown；缺省=未探测，照常出图。 */
    state?: string;
    alt: string;
    thumbClass?: string;
}) {
    if (state === "expired" || state === "unknown") {
        const expired = state === "expired";
        return (
            <div
                data-hub-asset-state={state}
                className="flex aspect-video w-40 flex-col items-center justify-center gap-1 rounded-md border border-dashed border-red-300 bg-red-50/60 px-2 text-center text-[11px] leading-tight text-red-600 dark:border-red-700 dark:bg-red-950/30 dark:text-red-400"
            >
                <span className="font-medium">{expired ? "图已过期，请重传" : "图片不可用，请重试"}</span>
                <span className="text-[10px] text-stone-400 dark:text-stone-500">{expired ? "图床直链已失效（403/404）" : "连不上/超时（未证实失效）"}</span>
            </div>
        );
    }
    const view = hubAssetRefView(assetRef);
    return (
        <a href={view.href} target="_blank" rel="noreferrer" className="block" data-hub-asset-exit="link">
            <img
                src={view.src}
                alt={alt}
                data-hub-asset-exit="img"
                className={thumbClass || "aspect-video w-40 rounded-md border border-emerald-300 bg-stone-100 object-contain dark:border-emerald-700 dark:bg-stone-900"}
            />
        </a>
    );
}
/* hub-asset-exit:end */

/** 传图/清空并排的上传区（本地资产引用 → 定向写清单；组卡传代表行=全组联动）。 */
function AssetUploadZone({
    taskId,
    n,
    uploaded,
    assetStates,
    busy,
    setBusy,
    onWritten,
    hint,
}: {
    taskId: string;
    n: number;
    uploaded: string[];
    /** 显示 URL → 引用健康态（AT-14 B3 旧 URL 三态）；缺键/正常值一律照常出图。 */
    assetStates?: Record<string, string>;
    busy: BusyKind;
    setBusy: (kind: BusyKind) => void;
    onWritten: () => void;
    hint?: string;
}) {
    const { message } = App.useApp();
    const fileInputRef = useRef<HTMLInputElement>(null);
    const dragDepthRef = useRef(0);
    const [dragActive, setDragActive] = useState(false);
    const ctxAssetStates = useContext(AssetStateContext);
    const states = assetStates ?? ctxAssetStates;

    const uploadOne = async (file: File) => {
        if (busy) return;
        setBusy("upload");
        try {
            const uploadedRef = await uploadHubImage(file, taskId);
            const result = await postHubAsset(taskId, { n, image_url: uploadedRef.ref });
            message.success(result.组?.length ? `已换图，#${result.组.join("、#")} 全组生效` : `#${n} 已换图`);
            onWritten();
        } catch (error) {
            message.error(`#${n} 换图失败：${errorText(error)}`);
        } finally {
            setBusy(null);
            if (fileInputRef.current) fileInputRef.current.value = "";
        }
    };

    const pickFile = (files: FileList | null) => {
        const image = Array.from(files || []).find((item) => item.type.startsWith("image/"));
        if (!image) {
            message.warning("请选图片文件");
            return;
        }
        void uploadOne(image);
    };

    const clearRow = async () => {
        if (busy) return;
        setBusy("upload");
        try {
            const result = await postHubAsset(taskId, { n, clear: true });
            message.success(result.组?.length ? `已清空，#${result.组.join("、#")} 全组按原片` : `#${n} 已按原片沿用`);
            onWritten();
        } catch (error) {
            message.error(`#${n} 清空失败：${errorText(error)}`);
        } finally {
            setBusy(null);
        }
    };

    const dropzoneClass = `flex h-16 min-w-40 flex-1 cursor-pointer items-center justify-center gap-1.5 rounded-md border border-dashed px-2 text-center text-xs transition ${
        dragActive ? "border-stone-900 bg-stone-100/80 dark:border-stone-100 dark:bg-stone-900/80" : "border-stone-300 bg-stone-50/60 hover:border-stone-400 dark:border-stone-700 dark:bg-stone-900/40 dark:hover:border-stone-500"
    }`;

    return (
        <div className="flex min-w-0 flex-1 flex-col justify-center gap-1.5">
            {uploaded.length ? (
                <div className="flex flex-wrap items-stretch gap-2">
                    {uploaded.map((assetRef) => (
                        <UploadedThumb key={assetRef} assetRef={assetRef} state={states[assetRef] || ""} alt={`#${n} 换图`} />
                    ))}
                </div>
            ) : null}
            <div className="flex flex-wrap items-center gap-2">
                <div
                    role="button"
                    tabIndex={0}
                    className={dropzoneClass}
                    onClick={() => fileInputRef.current?.click()}
                    onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                            event.preventDefault();
                            fileInputRef.current?.click();
                        }
                    }}
                    onDragEnter={(event: DragEvent<HTMLDivElement>) => {
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
                    {busy === "upload" ? (
                        <>
                            <LoaderCircle className="size-4 animate-spin text-stone-400" />
                            <span className="text-stone-500 dark:text-stone-400">上传中…</span>
                        </>
                    ) : (
                        <>
                            <Upload className="size-4 shrink-0 text-stone-400" />
                            <span className="text-stone-600 dark:text-stone-300">{uploaded.length ? "重传（覆盖）" : dragActive ? "松开上传" : "拖图到这里 / 点击传图"}</span>
                        </>
                    )}
                </div>
                <Button size="small" disabled={busy !== null || !uploaded.length} onClick={() => void clearRow()}>
                    清空
                </Button>
            </div>
            <div className="text-[11px] text-stone-400 dark:text-stone-500">{hint || (uploaded.length ? "换图走图床公网直链；清空=按原片沿用" : "没传就按原片沿用")}</div>
            <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                className="hidden"
                onChange={(event) => {
                    pickFile(event.target.files);
                    event.target.value = "";
                }}
            />
        </div>
    );
}

// ---------------------------------------------------------------- 提示词段 + 审批/生成/成片段

/** 拆解门禁条目名（与 automation/selfcheck.GATE_ITEM_NAME 同值；数据由服务端读路径汇总进 自检 束）。 */
const GATE_ITEM_NAME = "拆解门禁";

/** 提示词段（AT-16 B2 布局）：标题行只放「提示词」+ 操作按钮（一行收住）；正文默认展开、
 *  可用按钮收起；自检/门禁的异常详情走下方**独立整宽区域**（不进标题行 flex，展开时标题与
 *  正文边界不互挤）。 */
export function PromptSection({ detail }: { detail: HubTaskDetail }) {
    const prompt = detail.prompt_md || "";
    const [collapsed, setCollapsed] = useState(false);          // 正文默认展开（折叠改成可选收起）
    // AT-15 R2：拆解门禁条目从自检束里拆出来单独渲染（绿Tag=绿项数摘要；FAIL/WARN 红条=缺项+重拆轮次）。
    const gateItem = detail.自检?.find((item) => item.项 === GATE_ITEM_NAME) ?? null;
    const checkItems = detail.自检?.filter((item) => item.项 !== GATE_ITEM_NAME) ?? null;
    return (
        <section className="rounded-lg border border-stone-200 bg-card p-4 dark:border-stone-800" data-hub-prompt-section>
            <div className="flex min-w-0 flex-wrap items-center gap-2" data-hub-prompt-head>
                <h2 className="flex min-w-0 items-center gap-1.5 text-sm font-semibold">
                    <FileText className="size-4" />
                    提示词
                </h2>
                {prompt ? (
                    <Button
                        size="small"
                        type="text"
                        className="ml-auto shrink-0"
                        data-hub-prompt-toggle
                        onClick={() => setCollapsed((value) => !value)}
                    >
                        {collapsed ? "展开正文" : "收起正文"}
                    </Button>
                ) : null}
            </div>
            {/* 自检/门禁：独立整宽区（块级流，与标题行并列，不嵌进标题行 flex） */}
            <SelfCheckPanel items={checkItems} gateItem={gateItem} />
            {prompt ? (
                collapsed ? null : (
                    <div
                        className="agent-streamdown thin-scrollbar mt-3 max-h-96 w-full overflow-auto rounded-md border border-stone-200 px-3 py-2 text-sm dark:border-stone-800"
                        data-hub-prompt-body
                        data-expanded="1"
                    >
                        <Streamdown>{prompt}</Streamdown>
                    </div>
                )
            ) : (
                <div className="mt-3 w-full rounded-md border border-dashed border-stone-300 px-3 py-4 text-sm text-stone-500 dark:border-stone-700 dark:text-stone-400" data-hub-prompt-empty>
                    提示词尚未生成（确认置换后由后台渲染产出）。
                </div>
            )}
        </section>
    );
}

/** 自检面板（AT-9 B5 + AT-16 B2）：独立整宽区域，有异常默认展开。
 *  用户话（结论 + 原因 + 怎么处理 + 发现的行号/图号）全部由自检生产者产出（
 *  automation/selfcheck.py「说明」字段）——前端只渲染，**不解析证据文字重判**（红线 25）。
 *  旧任务没有话术字段（auto-0042 那批）→ 降级显示原始证据，并明说没有处理指引，不改历史文件。 */
function SelfCheckPanel({ items, gateItem }: { items: HubSelfCheckItem[] | null; gateItem: HubSelfCheckItem | null }) {
    const all = items ?? [];
    const failed = all.filter((item) => item.结论 !== "PASS");
    const passed = all.filter((item) => item.结论 === "PASS");
    return (
        <div className="mt-2 w-full space-y-2" data-hub-selfcheck>
            <div className="flex flex-wrap items-center gap-2" data-hub-selfcheck-head>
                {!all.length ? (
                    <Tooltip title="这条任务渲染于自检上线前，没有自检结果文件（自检.json）。">
                        <Tag className="m-0" data-hub-selfcheck-unchecked>
                            自检结论：未检查
                        </Tag>
                    </Tooltip>
                ) : (
                    <Tag
                        color={failed.length ? "red" : "green"}
                        className="m-0"
                        data-hub-selfcheck-badge
                        data-hub-selfcheck-state={failed.length ? "fail" : "pass"}
                    >
                        {failed.length ? `自检 ⚠ ${failed.length}/${all.length} 项异常` : `自检 ✓ ${all.length} 项通过`}
                    </Tag>
                )}
            </div>
            {failed.length ? (
                <div className="w-full space-y-2.5 rounded-md border border-red-200 bg-red-50/60 p-2.5 dark:border-red-950 dark:bg-red-950/20" data-hub-selfcheck-detail>
                    {failed.map((item) => (
                        <SelfCheckRow key={item.项} item={item} />
                    ))}
                    {passed.length ? (
                        <details className="w-full min-w-0" data-hub-selfcheck-passed>
                            <summary className="cursor-pointer list-none text-xs text-stone-500 [&::-webkit-details-marker]:hidden dark:text-stone-400">
                                {passed.length} 项通过（展开看证据）
                            </summary>
                            <div className="mt-1.5 space-y-2.5">
                                {passed.map((item) => (
                                    <SelfCheckRow key={item.项} item={item} />
                                ))}
                            </div>
                        </details>
                    ) : null}
                </div>
            ) : null}
            {gateItem ? <GateBadge item={gateItem} /> : null}
        </div>
    );
}

/** 一条自检：生产者话术优先（结论人话/原因/怎么处理/发现）；旧结构（无说明）只显原始证据。 */
function SelfCheckRow({ item }: { item: HubSelfCheckItem }) {
    const say = item.说明;
    const failed = item.结论 !== "PASS";
    return (
        <div
            className="w-full text-xs leading-5"
            data-hub-selfcheck-item
            data-hub-selfcheck-state={failed ? "fail" : "pass"}
            data-hub-selfcheck-legacy={say ? "0" : "1"}
        >
            <div className="flex flex-wrap items-center gap-1.5">
                {failed ? <CircleAlert className="size-3.5 shrink-0 text-red-600" /> : <CheckCircle2 className="size-3.5 shrink-0 text-emerald-600" />}
                <span className="font-medium">{item.项}</span>
                <Tag color={failed ? "red" : "green"} className="mr-0 px-1 py-0 text-[10px]">
                    {failed ? "异常" : "通过"}
                </Tag>
            </div>
            {say ? (
                <div className="mt-1 space-y-0.5 pl-5">
                    {say.结论人话 ? (
                        <div className="break-words text-stone-700 dark:text-stone-200" data-hub-selfcheck-conclusion>
                            {say.结论人话}
                        </div>
                    ) : null}
                    {say.原因 ? (
                        <div className="break-words whitespace-pre-wrap text-stone-600 dark:text-stone-300" data-hub-selfcheck-reason>
                            <span className="text-stone-400 dark:text-stone-500">原因：</span>
                            {say.原因}
                        </div>
                    ) : null}
                    {say.处理 ? (
                        <div
                            className={`break-words ${failed ? "text-red-700 dark:text-red-300" : "text-stone-600 dark:text-stone-300"}`}
                            data-hub-selfcheck-how
                        >
                            <span className="font-medium">怎么处理：</span>
                            {say.处理}
                        </div>
                    ) : null}
                    {say.发现.length ? (
                        <ul className="space-y-0.5 text-stone-500 dark:text-stone-400" data-hub-selfcheck-findings>
                            {say.发现.map((finding, index) => (
                                <li key={index} className="break-words">
                                    {finding.行号 !== null ? <span className="mr-1 font-mono">#{finding.行号}</span> : null}
                                    {finding.图号 !== null ? <span className="mr-1 font-mono">图{finding.图号}</span> : null}
                                    {finding.文字}
                                </li>
                            ))}
                        </ul>
                    ) : null}
                    {item.证据 ? (
                        <details className="w-full min-w-0" data-hub-selfcheck-raw>
                            <summary className="cursor-pointer list-none text-[11px] text-stone-400 [&::-webkit-details-marker]:hidden dark:text-stone-500">
                                原始证据
                            </summary>
                            <div className="mt-0.5 break-words whitespace-pre-wrap text-stone-500 dark:text-stone-400">{item.证据}</div>
                        </details>
                    ) : null}
                </div>
            ) : (
                <div className="mt-1 space-y-0.5 pl-5" data-hub-selfcheck-legacy-note>
                    <div className="text-stone-500 dark:text-stone-400">
                        这条任务的检查结果早于「人话说明」上线：只有当时的原始证据，没有处理指引。
                    </div>
                    <div className="break-words whitespace-pre-wrap text-stone-500 dark:text-stone-400" data-hub-selfcheck-evidence>
                        {item.证据}
                    </div>
                </div>
            )}
        </div>
    );
}

/** 拆解门禁（AT-15 R2 数据合同）：PASS → 绿 Tag 带摘要；FAIL/WARN → 整宽红条（缺项/告警逐条 +
 *  重拆轮次 + 怎么处理）。摘要与逐条明细都来自 selfcheck.load_gate_item 的「说明」，
 *  这里只渲染不解释（红线 25：不拆证据字符串重判）。 */
function GateBadge({ item }: { item: HubSelfCheckItem }) {
    const say = item.说明;
    const failed = item.结论 !== "PASS";
    const summary = say?.结论人话 || "拆解门禁结果";
    const details = say?.发现.map((finding) => finding.文字) ?? [];
    if (!failed && item.结论 !== "WARN") {
        return (
            <div className="w-full" data-hub-gate data-hub-gate-state="pass">
                <Tag color="green" className="m-0">
                    拆解门禁 ✓ {summary}
                </Tag>
            </div>
        );
    }
    return (
        <div className="w-full" data-hub-gate data-hub-gate-state={failed ? "fail" : "warn"}>
            <details className="w-full min-w-0" open>
                <summary className="flex cursor-pointer list-none flex-wrap items-center gap-1.5 [&::-webkit-details-marker]:hidden">
                    <Tag color={failed ? "red" : "gold"} className="m-0">
                        {failed ? `拆解门禁 ✗ ${summary}` : `拆解门禁 ⚠ ${summary}`}
                    </Tag>
                </summary>
                <div className="mt-2 w-full space-y-1 rounded-md border border-red-300 bg-red-50 p-2 dark:border-red-900 dark:bg-red-950" data-hub-gate-detail>
                    {details.map((line, index) => (
                        <div key={index} className="break-words whitespace-pre-wrap text-xs leading-5 text-stone-600 dark:text-stone-300">
                            {line}
                        </div>
                    ))}
                    {say?.处理 ? (
                        <div className="break-words text-xs leading-5 text-red-700 dark:text-red-300" data-hub-gate-how>
                            <span className="font-medium">怎么处理：</span>
                            {say.处理}
                        </div>
                    ) : null}
                </div>
            </details>
        </div>
    );
}

/** 审批/生成/成片段（§四）：批准出片/退回置换；生成进度；成片预览+下载；失败重试。 */
function OutcomeSection({
    detail,
    busy,
    act,
}: {
    detail: HubTaskDetail;
    busy: BusyKind;
    act: (kind: Exclude<BusyKind, null>, run: () => Promise<void>, okText?: string) => Promise<void>;
}) {
    const state = detail.state;
    const localId = detail.local_id;

    // AT-13 B3：待批准 / 已批准 / 生成中一律走「生成设置」段（配置面板 + 提交链阻断 UI）
    if (state === "awaiting_approval" || state === "approved" || state === "generating") {
        return <GenSettingsSection detail={detail} busy={busy} act={act} />;
    }

    if (state === "done") {
        return (
            <section className="rounded-lg border border-stone-200 bg-card p-4 dark:border-stone-800">
                <h2 className="text-sm font-semibold">成片</h2>
                <video src={hubOutputUrl(localId)} controls preload="metadata" className="mt-2 aspect-video w-full rounded-md border border-stone-200 bg-black object-contain dark:border-stone-800" />
                <div className="mt-2 flex flex-wrap justify-end">
                    <a className="inline-flex h-8 items-center gap-1.5 rounded-md bg-stone-900 px-3 text-sm font-medium text-white transition hover:bg-stone-700 dark:bg-stone-100 dark:text-stone-900 dark:hover:bg-stone-300" href={hubOutputUrl(localId)} download={`${localId}.mp4`}>
                        <Download className="size-4" />
                        下载成片
                    </a>
                </div>
            </section>
        );
    }

    if (state === "failed") {
        const issue = genIssue(detail);
        return (
            <>
                {/* 失败红条 + 阻断面板（原文由 GenIssueSection 出，避免与本段重复） */}
                <GenIssueSection detail={detail} busy={busy} act={act} />
                <section className="rounded-lg border border-stone-200 bg-card p-4 dark:border-stone-800">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="min-w-0 break-words text-xs text-stone-500 dark:text-stone-400">
                            {detail.gen_error ? "失败原文见上（已脱敏）。" : detail.error ? "" : "任务失败，错误原文未返回。"}重试=重跑拆解与渲染（产物优先保留）。
                        </span>
                        <div className="flex flex-wrap items-center justify-end gap-2">
                            {/* 阻断态（未知收单/在途）下不许重试：会重复建单扣费（§2.3） */}
                            {issue.blocksRetry ? (
                                <Tooltip title={`${issue.title}——先按上方指引核对 / 续查，解除阻断后才能重试`}>
                                    <span>
                                        <RetryButton localId={localId} busy={busy} act={act} disabled />
                                    </span>
                                </Tooltip>
                            ) : (
                                <RetryButton localId={localId} busy={busy} act={act} />
                            )}
                        </div>
                    </div>
                </section>
            </>
        );
    }

    return null;
}

/** 重试（§四 failed 动作）：POST /api/hub/task/<id>/retry（AT-9 B5 上桥）——failed → discussing
 *  续跑，产物优先；非 failed 桥端 409，错误原文透出（不再吞错降级）。
 *  AT-13 B3：阻断态（未知收单 / 在途）一律 disabled——重试会再次触发创建，重复扣费。 */
function RetryButton({ localId, busy, act, disabled = false }: { localId: string; busy: BusyKind; act: (kind: Exclude<BusyKind, null>, run: () => Promise<void>, okText?: string) => Promise<void>; disabled?: boolean }) {
    return (
        <Button
            danger
            size="small"
            icon={<RefreshCw className="size-3.5" />}
            loading={busy === "retry"}
            disabled={disabled || busy !== null}
            data-hub-gen-retry
            onClick={() =>
                void act("retry", async () => {
                    await retryHubTask(localId);
                }, "已触发重试（后台续跑，可关页等待）")
            }
        >
            重试
        </Button>
    );
}

// ---------------------------------------------------------------- AT-13 B3：生成设置 + 提交链 UI

/** 画布 AiConfig.channels → 纯渠道描述（lib 不吃 store 状态，探针直接断言纯函数）。 */
function genChannelsOf(config: AiConfig): GenChannelDesc[] {
    return (config.channels || []).map((channel) => ({
        id: channel.id,
        name: channel.name,
        baseUrl: channel.baseUrl,
        apiKey: channel.apiKey,
        apiFormat: channel.apiFormat,
        // 模型候选不在这里派生（AT-14 §3.2）：hub 直接用画布共享函数 selectableModelsByCapability，
        // 渠道描述只留渠道身份；带模型脚本的判定也由共享数据（AiConfig.channels[].models[].script）给出。
        models: (channel.models || []).map((model) => ({
            name: model.name,
            capability: model.capability,
        })),
    }));
}

/** 生成渠道/模型的默认种子（只读画布全局默认，不回写 videoModel / videoSeconds）。 */
function genSeedOf(config: AiConfig) {
    return {
        videoSeconds: config.videoSeconds,
        vquality: config.vquality,
        size: config.size,
        videoMode: config.videoMode,
        videoGenerateAudio: config.videoGenerateAudio,
    };
}

/**
 * 提交链提示段（§2.3/§2.4）：失败红条（gen_error 已脱敏原文）+ 阻断/在途/孤儿引导。
 * 三种块各有 data-hub-gen-* 钩子，供 B5 真机 DOM 断言。
 */
function GenIssueSection({ detail, busy, act }: { detail: HubTaskDetail; busy: BusyKind; act: (kind: Exclude<BusyKind, null>, run: () => Promise<void>, okText?: string) => Promise<void> }) {
    const config = useConfigStore((state) => state.config);
    const issue = genIssue(detail);
    const channel = findGenChannel(genChannelsOf(config), readGenDraft(detail.local_id)?.channelId || "");
    const genError = String(detail.gen_error || "").trim();

    if (!genError && issue.kind === "none") return null;

    const resume = () => {
        const draft = readGenDraft(detail.local_id);
        const target = findGenChannel(genChannelsOf(config), draft?.channelId || "") || channel;
        const key = genFamily(target?.apiFormat) === "wan" ? "" : String(target?.apiKey || "").trim();
        void act("submit", async () => {
            await submitHubTask(detail.local_id, key);
        }, "已按云端 taskId 续查（只查不重提）");
    };

    return (
        <div className="space-y-2">
            {genError ? (
                <div className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-4 py-2.5 text-sm text-red-700 dark:border-red-950 dark:bg-red-950/20 dark:text-red-300" data-hub-gen-error>
                    <CircleAlert className="mt-0.5 size-4 shrink-0" />
                    <div className="min-w-0 break-words">{genError}</div>
                </div>
            ) : null}

            {issue.kind !== "none" ? (
                <div
                    className={
                        issue.kind === "active"
                            ? "rounded-lg border border-indigo-200 bg-indigo-50 p-4 dark:border-indigo-950 dark:bg-indigo-950/20"
                            : "rounded-lg border border-amber-200 bg-amber-50 p-4 dark:border-amber-950 dark:bg-amber-950/20"
                    }
                    data-hub-gen-issue={issue.kind}
                >
                    <div className="flex items-start gap-2">
                        <CircleAlert className="mt-0.5 size-4 shrink-0 text-amber-600 dark:text-amber-400" />
                        <div className="min-w-0 flex-1">
                            <div className="text-sm font-semibold text-stone-800 dark:text-stone-100">{issue.title}</div>
                            {issue.detail ? <div className="mt-1 break-words text-sm text-stone-600 dark:text-stone-300">{issue.detail}</div> : null}
                            <ol className="mt-2 list-decimal space-y-1 pl-5 text-xs text-stone-600 dark:text-stone-300">
                                {issue.steps.map((step) => (
                                    <li key={step} className="break-words">{step}</li>
                                ))}
                            </ol>
                            {issue.cloudTaskId ? (
                                <div className="mt-2 font-mono text-xs text-stone-500 dark:text-stone-400" data-hub-gen-cloud-id>云端 taskId：{issue.cloudTaskId}</div>
                            ) : null}
                            {issue.canResume ? (
                                <div className="mt-2 flex justify-end">
                                    <Button size="small" loading={busy === "submit"} disabled={busy !== null} onClick={resume} data-hub-gen-resume>
                                        按云端 taskId 续查（只查不重提）
                                    </Button>
                                </div>
                            ) : null}
                        </div>
                    </div>
                </div>
            ) : null}

            {genError ? (
                <div className="text-xs text-stone-500 dark:text-stone-400" data-hub-gen-hint>
                    {genFailureHint(channel, detail.state)}
                </div>
            ) : null}
        </div>
    );
}

/**
 * 音频三态区（AT-17 B3，方案 §4 B3 + 附注 14）：三态控件 + B2c 待核对 WARN + B2b 门禁红条。
 *
 * 落点 = 确认流按钮区（原本只有 B2b 红条的那一块），**不开新面板**：三态控件与红条上下相邻，
 * 改选后门禁结果就在下面一行更新——「改了三态红条还在骂旧三态」在视觉上也无处藏。
 *
 * 切换行为（服务端一处收口，前端只发一个 POST）：
 *   改选 → POST /api/hub/task/<id>/audio-mode → 落盘（任务.json + 画布旁路同值）
 *   → 按新三态重渲染（B2a 同一条 GET render 链）→ 门禁重判（B2b 同一条判定）
 *   → act() 回刷 detail：提示词正文（PromptSection）与红条同时换新。
 * 只有「待批准」态可改：已批准要先「撤销批准」（既有受控回退边），这里如实说明、不放宽。
 */
export function AudioModeSection({
    detail,
    busy,
    act,
}: {
    detail: HubTaskDetail;
    busy: BusyKind;
    act: (kind: Exclude<BusyKind, null>, run: () => Promise<void>, okText?: string) => Promise<void>;
}) {
    const mode = detail.audio_mode || "";
    const editable = detail.state === "awaiting_approval";
    const gate = detail.audio_gate;
    return (
        <div className="mt-3 w-full space-y-2" data-hub-audio-mode-section data-hub-audio-mode={mode}>
            <AudioModePicker
                value={mode}
                basis={detail.audio_mode_依据 || ""}
                pendingCheck={Boolean(detail.audio_mode_待核对)}
                busy={busy === "audio-mode"}
                disabled={!editable || busy !== null}
                onChange={(next) => {
                    void act(
                        "audio-mode",
                        async () => {
                            await setHubAudioMode(detail.local_id, next);
                        },
                        `音频三态已改为「${next}」：提示词声音段按新三态重渲染，门禁已重判`,
                    );
                }}
            />
            {!editable ? (
                <div className="text-[11px] leading-4 text-stone-500 dark:text-stone-400" data-hub-audio-mode-locked>
                    三态只在「待批准」态可改；已批准的先「撤销批准」回退，再改。
                </div>
            ) : null}
            {editable && gate?.verdict === "FAIL" ? (
                <div
                    className="w-full space-y-1 rounded-md border border-red-300 bg-red-50 p-2 dark:border-red-900 dark:bg-red-950"
                    data-hub-audio-gate="fail"
                    data-hub-audio-gate-mode={gate.audio_mode || ""}
                >
                    <div className="flex flex-wrap items-center gap-1.5">
                        <Tag color="red" className="m-0">
                            音频门禁 ✗ 批不了
                        </Tag>
                        <span className="text-xs text-stone-600 dark:text-stone-300">
                            提示词的声音段与任务声明的音频三态不一致（只拒绝批准，渲染产物保持原状）
                        </span>
                    </div>
                    {(gate.发现 ?? []).map((line, index) => (
                        <div key={index} className="break-words whitespace-pre-wrap text-xs leading-5 text-stone-600 dark:text-stone-300">
                            {line}
                        </div>
                    ))}
                    {gate.处理 ? (
                        <div className="break-words text-xs leading-5 text-red-700 dark:text-red-300" data-hub-audio-gate-how>
                            <span className="font-medium">怎么处理：</span>
                            {gate.处理}
                        </div>
                    ) : null}
                </div>
            ) : null}
        </div>
    );
}

/**
 * 生成设置（§2.1/§2.2/§2.10）：
 *  - 渠道 + 模型（复用 ModelPicker(capability=video)）→ 按「渠道+模型引用」存任务草稿（不写全局 videoModel）
 *  - 模型候选＝画布视频模型全集（AT-14 §3.2 档位 a，共享函数 selectableModelsByCapability 派生）；
 *    执行范围层三态：SDGF/wan 可选、openai/gemini 禁用标「画布可用·工作台暂不承接」、模型脚本禁用标「画布专用（模型脚本）」
 *  - wan 如实 UI：固定 wan3.0 · 自动多图参考，model / mode 控件不渲染
 *  - 参数面板枚举与画布同源（media-size.ts）；渠道不接的值照列 + 显式提示
 *  - 批准带 config（无 key）；提交带 key（一次性）；改配置＝快照失效需重批（撤销批准）
 */
function GenSettingsSection({ detail, busy, act }: { detail: HubTaskDetail; busy: BusyKind; act: (kind: Exclude<BusyKind, null>, run: () => Promise<void>, okText?: string) => Promise<void> }) {
    const { message, modal } = App.useApp();
    const config = useConfigStore((state) => state.config);
    const localId = detail.local_id;
    const state = detail.state;
    const readOnly = state !== "awaiting_approval";
    const promptReady = Boolean(detail.prompt_md);

    const channels = useMemo(() => genChannelsOf(config), [config]);
    const configRef = useRef(config);
    configRef.current = config;

    const [draft, setDraft] = useState<GenDraft | null>(null);
    const initRef = useRef("");

    /**
     * AT-16 B5b ④ 无本地草稿边界：这份面板里的配置到底是从哪来的？
     *   draft    = 本机落盘的任务草稿（真·待改草稿）
     *   approved = 草稿丢了，退回「批准时记下的配置基线」（已批准配置，**不是**待改草稿）
     *   none     = 两者都没有（换过浏览器 / 在命令行批的）→ 只是画布默认占位，必须明说
     * 老实现一律 `stored ?? defaultGenDraft(...)`：草稿一丢就把画布全局默认当真配置显示，
     * 等于假装读到了快照——本条修正的就是这个。
     */
    const [draftOrigin, setDraftOrigin] = useState<"draft" | "approved" | "none">("draft");

    // 草稿按任务加载（切任务重读；轮询不重置输入）。全局 videoModel 只作默认值参考，从不回写。
    useEffect(() => {
        if (!localId || initRef.current === localId) return;
        initRef.current = localId;
        const configNow = configRef.current;
        const list = genChannelsOf(configNow);
        const stored = readGenDraft(localId);
        const approved = stored ? null : readApprovedDraft(localId);
        const preferred = configNow.videoModel ? configNow.videoModel.split("::")[0] : "";
        const channel = stored
            ? findGenChannel(list, stored.channelId)
            : approved
              ? findGenChannel(list, approved.channelId)
              : findGenChannel(list, genDefaultChannelId(list, preferred));
        setDraftOrigin(stored ? "draft" : approved ? "approved" : "none");
        setDraft(stored ?? approved?.draft ?? defaultGenDraft(channel, genSeedOf(configNow), configNow));
    }, [localId]);

    const channel = draft ? findGenChannel(channels, draft.channelId) : null;
    const ui = genUiFlags(channel);
    const staleNote = genDraftStaleNote(draft, channels);
    const needsReapproval = draft && readOnly ? genNeedsReapproval(localId, draft, channel) : false;

    /**
     * AT-16 B5b ①/②/③：撤销重批起态门。判据只有一份（hub-api.hubReworkGate）：
     * 只在允许态（approved 且无活跃提交 / 无 submit_unknown / dispatch 已完结）才引导撤销；
     * 未知态改走「已核对 → 解除阻断（既有 release 端点）+ 续查」，**绝不**引导撤销。
     */
    const reworkGate: HubReworkGate = hubReworkGate({
        state,
        submit: detail.submit ?? null,
        cloudTaskId: detail.cloud_task_id || "",
        activeSubmit: detail.active_submit,
        submitUnknown: Boolean(detail.submit_unknown),
        dispatchPhase: genDispatchPhase(detail),
    });

    /**
     * AT-16 B9 / D10：解除阻断（dispatch.phase=cleared）后的换配置直达。
     * 只读同一份事实，判定复用上面的起态门（hubClearedDirect 内部先跑 hubReworkGate）——
     * cleared 态在起态门里本来就是 allowed，缺的只是「让所有者看得见 + 一键」，不是新状态。
     */
    const clearedDirect = hubClearedDirect({
        state,
        submit: detail.submit ?? null,
        cloudTaskId: detail.cloud_task_id || "",
        activeSubmit: detail.active_submit,
        submitUnknown: Boolean(detail.submit_unknown),
        dispatchPhase: genDispatchPhase(detail),
    });

    const update = (patch: Partial<GenDraft>) => {
        setDraft((prev) => {
            const next = { ...(prev as GenDraft), ...patch };
            saveGenDraft(localId, next);
            return next;
        });
    };

    const pickChannel = (channelId: string) => {
        const next = findGenChannel(channels, channelId);
        // 默认模型只从该渠道内的候选里挑（同源共享函数；绝不跨渠道抓别人的模型）
        const rows = genChannelModelRows(config, channelId);
        update({
            channelId,
            model: genFamily(next?.apiFormat) === "sdgf" ? rows.find((row) => row.selectable)?.name || "" : "",
            mode: genFamily(next?.apiFormat) === "sdgf" ? draft?.mode || "frames" : "",
        });
    };

    /**
     * 选模型（AT-14 §3.2 档位 a）：列表是画布视频模型全集，模型值自带渠道身份 —— 选中别家渠道的模型
     * 必须同步切渠道，否则会拿 A 渠道的 baseUrl/apiFormat 派 B 渠道的模型（渠道身份与模型错配）。
     */
    const pickModel = (value: string) => {
        const picked = genModelPick(value);
        if (!picked.channelId) {
            update({ model: picked.model });
            return;
        }
        const family = genFamily(findGenChannel(channels, picked.channelId)?.apiFormat);
        update({
            channelId: picked.channelId,
            model: family === "sdgf" ? picked.model : "",
            mode: family === "sdgf" ? draft?.mode || "frames" : "",
        });
    };

    const apiKey = String(channel?.apiKey || "").trim();
    const ratioRows = genRatioRows(channel);
    // 模型区行：候选全集＝画布视频模型全集（同源共享函数），禁用差异只来自执行范围层
    const modelRows = genModelRows(channel, config);
    const resolutionRows = genResolutionRows();
    // 画幅规约（AT-14 §3.1）：自定义 W×H 走画布 inferVideoRatio 就地规约，禁像素串外传
    const sizeInfo = draft ? genResolveSize(draft.size, channel) : genResolveSize("auto", channel);
    const sizeBlocked = !sizeInfo.valid || !sizeInfo.accepted;
    const pixels = draft ? parsePixelSizeOf(draft.rawSize || draft.size) : null;

    /** 自定义 W×H 输入 → 立即规约并落草稿（size=规约结果，rawSize=用户原始输入）。 */
    const applyPixels = (width: number, height: number) => {
        const raw = `${width}x${height}`;
        const resolved = genResolveSize(raw, channel);
        update({ size: resolved.valid ? resolved.size : raw, rawSize: raw });
    };

    // 批准 = 固化配置（无 key）+ 算 v2 快照 → 立刻提交（key 一次性透传）
    const approveAndSubmit = () => {
        if (!draft) return;
        const issues = genApproveIssues(draft, channel);
        if (issues.length) {
            message.error(issues[0]);
            return;
        }
        void act(
            "approve",
            async () => {
                const result = await approveHubTask(localId, buildApproveConfig(draft, channel));
                saveApprovedSignature(localId, genDraftSignature(draft, channel));
                try {
                    await submitHubTask(localId, apiKey);
                    message.success(`已批准并提交生成（快照 v${result.snapshot_version ?? 2}），等待成片`);
                } catch (error) {
                    message.warning(`已批准，但提交生成失败：${errorText(error)}。可在「提交生成」重试`);
                }
            },
        );
    };

    /**
     * 提交生成（抽屉底部按钮）：AT-16 B5a-2 起与列表行内、抽屉顶部**同一条链**——
     * 准入=hubQuickAction（按批准渠道取 key；active / blocksRetry / sizeBlocked 同一套守卫），
     * 执行=runHubQuickAction（HTTP 受理只报「提交处理中」，轮询确认 generating/done 才报云端已收单）。
     */
    const submitAction = hubQuickAction({
        localId,
        state,
        submit: detail.submit ?? null,
        cloudTaskId: detail.cloud_task_id || "",
        channels: hubQuickChannels(config),
        approvedSignature: readApprovedSignature(localId),
        // 凭据只认「批准快照 → 落盘草稿」；面板里那份从全局默认派生的初始草稿**不算凭据**
        // （否则会拿画布全局默认渠道的 key 顶批准渠道——卡片明确禁止）。
        draft: readGenDraft(localId),
        busy: busy !== null,
    });

    /**
     * 唯一执行点（底部「提交生成」与未知态「续查」共用）：runHubQuickAction 只在 HTTP 受理时报
     * 「提交处理中」，轮询到 generating/done 才报「云端已收单」——两处不各写一套 toast。
     */
    const runSubmitAction = () => {
        void act("submit", async () => {
            await runHubQuickAction(localId, submitAction, {
                onPhase: (phase, phaseState, note) => {
                    // AT-16 B8：主状态条由父级抽屉驱动——act 先把 busy 置 submit，父级轮询随即
                    // 加密到 2s 并立刻重取一次，所以这里只报人话，不另起一套刷新。
                    const line = hubQuickPhaseText(phase, phaseState);
                    const text = note || line.text;
                    if (!text) return;
                    if (line.level === "success") message.success(text);
                    else if (line.level === "warning") message.warning(text);
                    else message.info(text);
                },
            });
        });
    };

    const submitOnly = () => {
        if (!draft) return;
        if (submitAction.kind !== "submit" || !submitAction.enabled) {
            message.error(submitAction.reason || "当前不能提交生成");
            return;
        }
        runSubmitAction();
    };

    /** 未知态指引段里的「续查」：同一个 hubQuickAction / runHubQuickAction（只查不重提）。 */
    const resumeFromGate = () => {
        if (submitAction.kind !== "resume" || !submitAction.enabled) {
            message.error(submitAction.reason || "现在没有可续查的云端任务");
            return;
        }
        runSubmitAction();
    };

    /** 底部按钮的悬挂说明：先说「必须先做什么」（重批 / 守卫 / 缺配置），再做渠道提示。 */
    const submitBlockReason = needsReapproval
        ? "批准后配置变过（渠道身份 / 模型 / 模式 / 参数）——先「撤销批准」，改完再批准一次"
        : !submitAction.enabled
          ? submitAction.reason
          : "";

    /**
     * 撤销批准（起态门允许态才可点）：先确认，确认文案把「心血不作废」讲清楚——
     * 只作废配置快照，资产确认 / 上传流水 / 置换确认 / 提示词一动不动。
     */
    const revoke = () => {
        if (!reworkGate.allowed) {
            message.warning(reworkGate.reason || "当前状态不能撤销批准——先按上面的指引核对 / 解除阻断");
            return;
        }
        modal.confirm({
            title: "撤销批准？（改配置后重批）",
            content: "撤销后回到待批准，改完配置再批准一次（新配置会重算快照 v2）。资产确认 / 上传流水 / 置换确认 / 提示词都原样保留，不会被作废。",
            okText: "撤销批准",
            onOk: () =>
                act("revoke", async () => {
                    await revokeHubTask(localId);
                    clearApprovedSignature(localId);
                }, "已撤销批准，改配置后再批准一次"),
        });
    };

    /**
     * 未知态的人工解除（AT-16 B5b ①）：走既有 POST /api/hub/task/<id>/release（confirm:true）。
     * 前端不猜结果、不自己清标记——核对是人的动作，解除后由后端记 history。
     */
    const releaseUnknown = () => {
        modal.confirm({
            title: "已在渠道控制台核对过「没有这一单」？",
            content: "解除会清掉「未知收单」标记并写入任务历史，之后才能重新提交——重提会再扣一次费。确认渠道确实没有这一单再解。",
            okText: "已核对，解除阻断",
            okButtonProps: { danger: true },
            onOk: () =>
                act("release", async () => {
                    await releaseHubTask(localId);
                }, "已解除阻断：可以重新批准 / 提交了"),
        });
    };

    const rollback = () => {
        modal.confirm({
            title: "退回置换？",
            content: "丢弃当前提示词，回到待置换状态重新传图/确认（原提示词不保留）。",
            okText: "退回置换",
            onOk: () =>
                act("rollback", async () => {
                    await rollbackHubTask(localId);
                }),
        });
    };

    if (!draft) {
        return (
            <section className="rounded-lg border border-stone-200 bg-card p-4 dark:border-stone-800">
                <div className="flex items-center gap-2 text-sm text-stone-500 dark:text-stone-400">
                    <LoaderCircle className="size-4 animate-spin" /> 正在读取生成设置…
                </div>
            </section>
        );
    }

    const blocked = genIssue(detail);

    /**
     * 无本地草稿边界文案（④）：既要如实说清「手上这份是什么」，又不能在实际改不了的态里
     * 引导撤销（未知态撤销会清掉未知标记）——所以按起态门分岔。
     */
    const draftBoundaryText =
        draftOrigin === "approved"
            ? reworkGate.allowed
                ? "本机没有这条任务的待改草稿——下面这份是「已批准配置」（批准时记下的基线，不是草稿）。要改就按顺序：撤销批准 → 换模型 → 批准出片；资产与提示词不受影响。"
                : "本机没有这条任务的待改草稿——下面这份是「已批准配置」（批准时记下的基线，不是草稿）。现在结果待核对，先按上面的指引核对 / 解除阻断，解除之后才能改配置；资产与提示词不受影响。"
            : readOnly
              ? reworkGate.allowed
                  ? "本机既没有待改草稿、也没有批准时的配置基线（换过浏览器 / 在命令行批的）——下面只是画布默认占位，不代表这条任务已固化的配置。要改配置请先「撤销批准」，再重新挑渠道 / 模型。"
                  : "本机既没有待改草稿、也没有批准时的配置基线（换过浏览器 / 在命令行批的）——下面只是画布默认占位，不代表这条任务已固化的配置。现在结果待核对：先按上面的指引核对 / 解除阻断，解除之后才能改配置。"
              : "本机没有这条任务的草稿——下面是从画布默认派生的初始值（还没落盘）；改任一字段就会存成这条任务自己的草稿。";

    return (
        <>
            <GenIssueSection detail={detail} busy={busy} act={act} />

            <section className="rounded-lg border border-stone-200 bg-card p-4 dark:border-stone-800" data-hub-gen-settings data-hub-gen-readonly={readOnly ? "1" : "0"}>
                <div className="flex flex-wrap items-center justify-between gap-2">
                    <h2 className="text-sm font-semibold">生成设置</h2>
                    <span className="text-xs text-stone-500 dark:text-stone-400">
                        {readOnly
                            ? clearedDirect.visible
                                ? "已随批准固化（只读）——上次的阻断已解除：原配置可重交，换配置先撤销批准"
                                : reworkGate.mode === "unknown" || reworkGate.mode === "blocked"
                                  ? "已随批准固化（只读）——结果待核对，先别撤销"
                                  : "已随批准固化（快照 v2）；要改先撤销批准"
                            : "与会话画布同源；批准时固化本次配置"}
                    </span>
                </div>
                <div className="mt-1 text-xs text-stone-500 dark:text-stone-400">
                    {state === "generating"
                        ? `已提交的配置由服务端快照固化；本地草稿口径：${genDraftSummary(draft, channel)}`
                        : "改任一有效字段（渠道身份 / 模型 / 模式 / 参数）＝快照失效，需重新批准；只换 key 不失效。"}
                </div>

                {/* AT-16 B9 / D10 直达条：解除阻断（cleared）后把「换模型 / 渠道」这条路摆到最上面 ——
                    判定只读起态门，一键接既有撤销批准链（revoke → 回 awaiting_approval → 配置解锁 →
                    改完走同一条「批准出片」）；非 cleared 态这个组件返回 null。 */}
                <GenClearedBar direct={clearedDirect} busy={busy} onRevoke={revoke} />

                {/* AT-16 B5b ④ 无本地草稿边界：草稿丢了就说清手上这份是什么，不假装读到快照 */}
                {draftOrigin !== "draft" ? (
                    <div
                        className="mt-2 rounded-md border border-sky-200 bg-sky-50 px-3 py-2 text-xs text-sky-800 dark:border-sky-950 dark:bg-sky-950/20 dark:text-sky-300"
                        data-hub-gen-draft-boundary={draftOrigin}
                        data-hub-gen-draft-boundary-readonly={readOnly ? "1" : "0"}
                    >
                        {draftBoundaryText}
                    </div>
                ) : null}

                {staleNote ? (
                    <div className="mt-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700 dark:border-amber-950 dark:bg-amber-950/20 dark:text-amber-300" data-hub-gen-stale>
                        {staleNote}
                    </div>
                ) : null}

                {/* 渠道（协议矩阵：SDGF / 万相可选，其余「画布专用」禁选） */}
                <div className="mt-3 grid gap-3 md:grid-cols-2">
                    <label className="space-y-1.5">
                        <span className="text-xs font-medium text-stone-500 dark:text-stone-400">渠道</span>
                        <Select
                            className="w-full"
                            disabled={readOnly}
                            value={draft.channelId || undefined}
                            placeholder="选 SDGF / 万相渠道"
                            onChange={pickChannel}
                            data-hub-gen-channel
                            options={channels.map((row) => ({
                                value: row.id,
                                disabled: genFamily(row.apiFormat) === "canvas-only",
                                label: (
                                    <span className="flex min-w-0 items-center gap-2">
                                        <span className="truncate">{row.name || row.id}</span>
                                        <span className="shrink-0 text-[10px] text-stone-400">{row.apiFormat}</span>
                                        {genFamily(row.apiFormat) === "canvas-only" ? <Tag className="m-0">{GEN_CANVAS_ONLY_TAG}</Tag> : null}
                                    </span>
                                ),
                            }))}
                        />
                    </label>

                    {/* 模型：wan 族不渲染（固定 wan3.0）；SDGF 族＝画布视频模型全集（档位 a：三态标注） */}
                    {ui.showModel ? (
                        <div className="space-y-1.5" data-hub-gen-models data-hub-gen-model-count={modelRows.length} data-hub-gen-model-blocked={modelRows.filter((row) => !row.selectable).length}>
                            <span className="text-xs font-medium text-stone-500 dark:text-stone-400">模型</span>
                            {readOnly ? (
                                <div className="flex h-8 items-center rounded-full border border-input px-3 text-sm">{draft.model || "未选模型"}</div>
                            ) : (
                                <ModelPicker
                                    config={config}
                                    value={channel && draft.model ? `${channel.id}::${draft.model}` : ""}
                                    onChange={pickModel}
                                    capability="video"
                                    fullWidth
                                    placeholder={modelRows.length ? "选视频模型（含画布同款）" : "画布还没配视频模型"}
                                    options={modelRows.map((row) => row.value)}
                                    optionDisabled={(value) => !(modelRows.find((row) => row.value === value)?.selectable ?? true)}
                                    optionSuffix={(value) => modelRows.find((row) => row.value === value)?.note || ""}
                                />
                            )}
                        </div>
                    ) : (
                        <div className="space-y-1.5">
                            <span className="text-xs font-medium text-stone-500 dark:text-stone-400">模型</span>
                            <div className="flex h-8 items-center rounded-full border border-dashed border-stone-300 px-3 text-sm text-stone-600 dark:border-stone-700 dark:text-stone-300" data-hub-gen-fixed>
                                {ui.fixedNote}
                            </div>
                        </div>
                    )}
                </div>

                {/* 画幅（枚举同源 media-size.videoRatioOptions：含 21:9 / 自适应 / 自定义 W×H） */}
                <div className="mt-3 space-y-1.5">
                    <span className="text-xs font-medium text-stone-500 dark:text-stone-400">画幅</span>
                    <div className="flex flex-wrap gap-1.5">
                        {ratioRows.map((row) => (
                            <button
                                key={row.value}
                                type="button"
                                disabled={readOnly}
                                data-hub-gen-ratio={row.value}
                                data-hub-gen-ratio-accepted={row.accepted ? "1" : "0"}
                                className={`h-8 rounded-full border px-3 text-sm transition disabled:cursor-not-allowed disabled:opacity-50 ${
                                    draft.size === row.value ? "border-stone-900 text-stone-900 dark:border-stone-100 dark:text-stone-100" : "border-stone-300 text-stone-600 dark:border-stone-700 dark:text-stone-300"
                                }`}
                                onClick={() => update({ size: row.value, rawSize: "" })}
                            >
                                {row.label}
                                {row.accepted ? null : <span className="ml-1 text-[10px] text-amber-600 dark:text-amber-400">渠道不生效</span>}
                            </button>
                        ))}
                        <label className="flex h-8 items-center gap-1.5 rounded-full border border-stone-300 px-2 text-xs text-stone-600 dark:border-stone-700 dark:text-stone-300">
                            <Input
                                size="small"
                                type="number"
                                className="w-16"
                                disabled={readOnly}
                                value={pixels?.width ?? ""}
                                placeholder="W"
                                data-hub-gen-width
                                onChange={(event) => {
                                    const width = Math.floor(Number(event.target.value) || 0);
                                    const height = pixels?.height || 0;
                                    if (width > 0 && height > 0) applyPixels(width, height);
                                }}
                            />
                            ×
                            <Input
                                size="small"
                                type="number"
                                className="w-16"
                                disabled={readOnly}
                                value={pixels?.height ?? ""}
                                placeholder="H"
                                data-hub-gen-height
                                onChange={(event) => {
                                    const height = Math.floor(Number(event.target.value) || 0);
                                    const width = pixels?.width || 0;
                                    if (width > 0 && height > 0) applyPixels(width, height);
                                }}
                            />
                        </label>
                    </div>
                    {sizeInfo.note ? (
                        <div className="text-xs text-amber-600 dark:text-amber-400" data-hub-gen-size-note
                            data-hub-gen-size-resolved={sizeInfo.size} data-hub-gen-size-accepted={sizeInfo.accepted ? "1" : "0"}>
                            {sizeInfo.note}
                        </div>
                    ) : sizeInfo.custom ? (
                        <div className="text-xs text-stone-500 dark:text-stone-400" data-hub-gen-size-note
                            data-hub-gen-size-resolved={sizeInfo.size} data-hub-gen-size-accepted={sizeInfo.accepted ? "1" : "0"}>
                            将按 {sizeInfo.size} 生成
                        </div>
                    ) : null}
                </div>

                {/* 清晰度 / 时长 / 声音（与画布同源） */}
                <div className="mt-3 grid gap-3 md:grid-cols-3">
                    <div className="space-y-1.5">
                        <span className="text-xs font-medium text-stone-500 dark:text-stone-400">清晰度</span>
                        <div className="flex flex-wrap gap-1.5">
                            {resolutionRows.map((row) => (
                                <button
                                    key={row.value}
                                    type="button"
                                    disabled={readOnly}
                                    data-hub-gen-resolution={row.value}
                                    className={`h-8 rounded-full border px-3 text-sm transition disabled:cursor-not-allowed disabled:opacity-50 ${
                                        draft.vquality === row.value ? "border-stone-900 text-stone-900 dark:border-stone-100 dark:text-stone-100" : "border-stone-300 text-stone-600 dark:border-stone-700 dark:text-stone-300"
                                    }`}
                                    onClick={() => update({ vquality: row.value })}
                                >
                                    {row.label}
                                </button>
                            ))}
                        </div>
                    </div>
                    <div className="space-y-1.5">
                        <span className="text-xs font-medium text-stone-500 dark:text-stone-400">时长</span>
                        <div className="flex items-center gap-2">
                            <Slider
                                className="min-w-0 flex-1"
                                min={GEN_DURATION_MIN}
                                max={GEN_DURATION_MAX}
                                step={1}
                                value={draft.duration}
                                disabled={readOnly}
                                onChange={(value) => update({ duration: Array.isArray(value) ? value[0] : value })}
                                data-hub-gen-duration
                            />
                            <span className="w-8 shrink-0 text-sm">{draft.duration}s</span>
                        </div>
                    </div>
                    <div className="space-y-1.5">
                        <span className="text-xs font-medium text-stone-500 dark:text-stone-400">声音</span>
                        <label className="flex h-8 items-center gap-2 text-sm">
                            <Checkbox checked={draft.audio} disabled={readOnly} data-hub-gen-audio onChange={(event) => update({ audio: event.target.checked })}>
                                带声音（渠道支持才生效）
                            </Checkbox>
                        </label>
                    </div>
                </div>

                {/* 模式：仅 SDGF 族（wan 自动多图参考，无 mode） */}
                {ui.showMode ? (
                    <div className="mt-3 space-y-1.5">
                        <span className="text-xs font-medium text-stone-500 dark:text-stone-400">模式</span>
                        <Radio.Group
                            value={draft.mode || "frames"}
                            disabled={readOnly}
                            onChange={(event) => update({ mode: event.target.value === "reference" ? "reference" : "frames" })}
                        >
                            <Radio.Button value="frames" data-hub-gen-mode="frames">
                                首尾帧
                            </Radio.Button>
                            <Radio.Button value="reference" data-hub-gen-mode="reference">
                                多图参考
                            </Radio.Button>
                        </Radio.Group>
                        <div className="text-xs text-stone-400 dark:text-stone-500">两图时切首尾帧 / 多图参考会随提交体 mode 字段变化。</div>
                    </div>
                ) : null}

                {needsReapproval ? (
                    <div className="mt-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700 dark:border-amber-950 dark:bg-amber-950/20 dark:text-amber-300" data-hub-gen-reapproval>
                        批准后配置变过（渠道身份 / 模型 / 模式 / 参数）——快照已作废：先「撤销批准」，改完再批准一次。
                    </div>
                ) : null}

                {/* AT-16 B5b ①②：撤销重批引导（判据=起态门 hubReworkGate）。允许态给三步顺序 + 心血不作废；
                    未知态只给「核对 → 解除阻断（既有 release 端点）→ 续查」，**不引导撤销**。 */}
                {reworkGate.mode !== "none" ? (
                    <div
                        className={
                            reworkGate.allowed
                                ? "mt-3 rounded-md border border-stone-300 bg-stone-50 px-3 py-2 text-xs text-stone-700 dark:border-stone-700 dark:bg-stone-900/40 dark:text-stone-300"
                                : "mt-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700 dark:border-amber-950 dark:bg-amber-950/20 dark:text-amber-300"
                        }
                        data-hub-gen-rework={reworkGate.mode}
                        data-hub-gen-rework-allowed={reworkGate.allowed ? "1" : "0"}
                        data-hub-gen-rework-block={reworkGate.blockKind}
                    >
                        <div className="font-medium">{reworkGate.title}</div>
                        {reworkGate.reason ? <div className="mt-1 break-words">{reworkGate.reason}</div> : null}
                        <ol className="mt-1 list-decimal space-y-0.5 pl-5">
                            {(reworkGate.allowed ? reworkGate.steps : hubReworkUnknownSteps(reworkGate)).map((step) => (
                                <li key={step} className="break-words">
                                    {step}
                                </li>
                            ))}
                        </ol>
                        {reworkGate.keep ? (
                            <div className="mt-1 break-words" data-hub-gen-rework-keep>
                                {reworkGate.keep}
                            </div>
                        ) : null}
                        {reworkGate.showRelease || reworkGate.canResume ? (
                            <div className="mt-2 flex flex-wrap justify-end gap-2">
                                {reworkGate.canResume ? (
                                    <Button size="small" loading={busy === "submit"} disabled={busy !== null} onClick={resumeFromGate} data-hub-gen-rework-resume>
                                        按云端 taskId 续查（只查不重提）
                                    </Button>
                                ) : null}
                                {reworkGate.showRelease ? (
                                    <Button size="small" danger loading={busy === "release"} disabled={busy !== null} onClick={releaseUnknown} data-hub-gen-release>
                                        已核对，解除阻断
                                    </Button>
                                ) : null}
                            </div>
                        ) : null}
                    </div>
                ) : null}

                {/* AT-17 B3：音频三态控件（本区，与 B2b 红条同一处）——改选 → 落盘 → 重渲染 → 门禁重判 */}
                <AudioModeSection detail={detail} busy={busy} act={act} />

                <div className="mt-3 flex flex-wrap items-center justify-end gap-2">
                    {state === "awaiting_approval" ? (
                        <>
                            <Button danger ghost loading={busy === "rollback"} disabled={busy !== null} onClick={rollback}>
                                退回置换
                            </Button>
                            <Tooltip title={!promptReady ? "提示词还没出（等渲染完成）" : sizeBlocked ? sizeInfo.note || "当前画幅渠道不生效" : ""}>
                                <span>
                                    <Button
                                        type="primary"
                                        icon={<Sparkles className="size-4" />}
                                        loading={busy === "approve"}
                                        disabled={!promptReady || busy !== null || blocked.blocksRetry || sizeBlocked}
                                        onClick={approveAndSubmit}
                                        data-hub-gen-approve
                                        data-hub-gen-approve-size-blocked={sizeBlocked ? "1" : "0"}
                                    >
                                        批准出片（固化配置并提交）
                                    </Button>
                                </span>
                            </Tooltip>
                        </>
                    ) : null}

                    {state === "approved" ? (
                        <>
                            {/* 起态门：只有允许态（无活跃提交 / 无未知收单 / dispatch 已完结）才给撤销入口。
                                未知态不给按钮——上面指引段直接说「先核对渠道」，撤销会清掉未知标记。 */}
                            {reworkGate.allowed ? (
                                <Button ghost loading={busy === "revoke"} disabled={busy !== null} onClick={revoke} data-hub-gen-revoke data-hub-gen-revoke-allowed="1">
                                    撤销批准（改配置）
                                </Button>
                            ) : null}
                            <Tooltip title={submitBlockReason || submitAction.hint}>
                                <span>
                                    <Button
                                        type="primary"
                                        loading={busy === "submit"}
                                        disabled={busy !== null || needsReapproval || blocked.blocksRetry || sizeBlocked || !submitAction.enabled}
                                        onClick={submitOnly}
                                        data-hub-gen-submit
                                        data-hub-gen-submit-size-blocked={sizeBlocked ? "1" : "0"}
                                        data-hub-gen-submit-enabled={submitAction.enabled ? "1" : "0"}
                                        data-hub-gen-submit-source={submitAction.source}
                                    >
                                        提交生成
                                    </Button>
                                </span>
                            </Tooltip>
                        </>
                    ) : null}

                    {state === "generating" ? (
                        <span className="flex items-center gap-1.5 text-xs text-stone-500 dark:text-stone-400">
                            <LoaderCircle className="size-3.5 animate-spin" />
                            生成中：云端通常 5 分钟以上；前端超时 ≠ 云端失败，按 taskId 可续查找回成片。
                        </span>
                    ) : null}
                </div>
            </section>
        </>
    );
}

/** 画幅里的自定义 W×H（"1920x1080"）；比例字面值 → null。 */
function parsePixelSizeOf(size: string): { width: number; height: number } | null {
    const match = /^(\d+)\s*[x×*]\s*(\d+)$/.exec(String(size || "").trim());
    if (!match) return null;
    const width = Number(match[1]);
    const height = Number(match[2]);
    return width > 0 && height > 0 ? { width, height } : null;
}

/** 描述首句（位置缺失时常驻展示）。 */
function firstSentence(text: string) {
    const head = (text || "").split(/[。；;.!！?？\n]/).find((part) => part.trim()) || "";
    return head.trim().slice(0, 42);
}

function formatTime(value: string) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}
