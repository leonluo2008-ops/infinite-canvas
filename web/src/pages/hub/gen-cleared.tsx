import { Button } from "antd";

import { hubReworkGate, type HubReworkGate, type HubReworkInput } from "./hub-api";

/**
 * 解除阻断后的「换配置直达」（AT-16 B9 / 缺陷 D10，所有者 2026-11-12 实测 auto-0043）。
 *
 * 病：人工解除阻断（dispatch.phase=cleared）之后，任务仍是 approved 态 ——
 *   · 模型 / 渠道下拉 `readOnly = state !== "awaiting_approval"` 锁死，配置一个都改不了；
 *   · 主操作还是「提交生成」，原配置重交（上次正是配置问题才撞的 502，必再撞一次）；
 *   · 真正的换配置路（B5b：撤销批准 → 回 awaiting_approval → 改 → 重批）只写在 GenSettings
 *     的说明文字里，所有者撞不到 —— B5a-1 指引（解除后再提交）与 B5b 路径（换配置先撤销）断层。
 *
 * 修（**复用 B5b 全链，不新造草稿态** —— 红线 25）：
 *   · 本模块只做「能不能显示 + 显示什么」，判定仍然只读 `hubReworkGate`（同一个起态门，不造第二套）；
 *     cleared 态在起态门里本来就是 allowed（无 active / 无 submit_unknown / dispatch 已完结）。
 *   · 一键按钮**不改写撤销链**：抽屉直接把它接到既有 `revoke`（modal 确认 + revokeHubTask +
 *     clearApprovedSignature），所以撤销后 readOnly 自然解除、改完走同一条「批准出片」。
 *   · 「心血不作废」口径不在这里另写一份，逐字取起态门的 `keep`（B5b 探针的逐字节断言同源）。
 *   · cleared 态「提交生成」照旧保留：阻断是人工核对后才解的，原配置重交合法。
 */

export type HubClearedDirect = {
    /** cleared 态且起态门允许才 true（其它态一律 false，组件不渲染） */
    visible: boolean;
    /** 判据事实（dispatch 阶段原文，排障对账用） */
    phase: string;
    title: string;
    /** 一句话点明「什么时候该点这个」与「原配置重交的代价」 */
    hint: string;
    /** 指引三条（第三条=换配置先撤销批准；与 B5b 三步同结论，不另造流程） */
    steps: string[];
    /** 一键按钮文案（动作=既有撤销批准链） */
    action: string;
    /** 心血不作废承诺（逐字取起态门 keep，B5b 同源） */
    keep: string;
};

const HUB_CLEARED_NONE: HubClearedDirect = {
    visible: false,
    phase: "",
    title: "",
    hint: "",
    steps: [],
    action: "",
    keep: "",
};

/** 指引里的硬句子（卡片 D10 修法②原话；探针逐字断言，别改成近义句）。 */
export const HUB_CLEARED_RECONFIG_STEP = "要换模型/渠道：先撤销批准（配置解锁、资产与提示词不动）。";

/**
 * 解除阻断后换配置直达（纯函数，探针直测；抽屉只读它）。
 *
 * 判定顺序：先读起态门（唯一判据），再看 dispatch 阶段是不是 cleared ——
 * 两者都成立才显示。所以：
 *   · unknown / blocked（还在未知收单或提交在跑）→ 不显示（那是核对 / 解除的路，不是换配置的路）
 *   · awaiting_approval → 不显示（配置本来就能改，没有要撤销的东西）
 *   · generating / done / failed → 不显示（起态门 mode=none）
 *   · cleared 但另有阻断（比如解除后又提交撞了新未知）→ 不显示（起态门 allowed=false）
 */
export function hubClearedDirect(input: HubReworkInput): HubClearedDirect {
    const gate: HubReworkGate = hubReworkGate(input);
    const phase = String(input.submit?.dispatch_phase || input.dispatchPhase || "").trim();
    if (phase !== "cleared" || !gate.allowed) return { ...HUB_CLEARED_NONE, phase };
    return {
        visible: true,
        phase,
        title: "上次提交已解除阻断——换模型 / 渠道重交",
        hint: "阻断是你人工核对后解除的：原配置重交合法；但上次若是模型 / 渠道的问题，重交会再撞同一个错。",
        steps: [
            "原配置重交：直接点下面「提交生成」——阻断已解除，这条提交合法（不用再核对 / 再解除）。",
            "上次那单是模型 / 渠道的问题？别用原配置重复付费试错。",
            HUB_CLEARED_RECONFIG_STEP,
        ],
        action: "撤销批准，换配置",
        keep: gate.keep,
    };
}

/**
 * 直达条（展示件）：只渲染「标题 + 三条指引 + 心血不作废 + 一键按钮」，不做任何判定。
 *
 * 一键按钮的 onClick 由抽屉传进来（= 既有 `revoke`），所以这里没有第二条撤销实现。
 * data-hub-gen-cleared-* 是真机 / 探针抓手（B9 探针断言可见性与按钮接线）。
 */
export function GenClearedBar({ direct, busy, onRevoke }: { direct: HubClearedDirect; busy: string | null; onRevoke: () => void }) {
    if (!direct.visible) return null;
    return (
        <div
            className="mt-2 rounded-md border border-sky-300 bg-sky-50 px-3 py-2 text-xs text-sky-900 dark:border-sky-900 dark:bg-sky-950/30 dark:text-sky-200"
            data-hub-gen-cleared-direct="1"
            data-hub-gen-cleared-phase={direct.phase}
        >
            <div className="font-medium" data-hub-gen-cleared-title>
                {direct.title}
            </div>
            {direct.hint ? <div className="mt-1 break-words">{direct.hint}</div> : null}
            <ol className="mt-1 list-decimal space-y-0.5 pl-5" data-hub-gen-cleared-steps>
                {direct.steps.map((step, index) => (
                    <li key={step} className="break-words" data-hub-gen-cleared-step={String(index + 1)}>
                        {step}
                    </li>
                ))}
            </ol>
            {direct.keep ? (
                <div className="mt-1 break-words" data-hub-gen-cleared-keep>
                    {direct.keep}
                </div>
            ) : null}
            <div className="mt-2 flex justify-end">
                <Button type="primary" size="small" loading={busy === "revoke"} disabled={busy !== null} onClick={onRevoke} data-hub-gen-cleared-revoke>
                    {direct.action}
                </Button>
            </div>
        </div>
    );
}
