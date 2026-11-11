/**
 * 拆解进度双锚计时（AT-16 B4a）——**全前端唯一走表口径**（红线 25：不做第二套计时）。
 *
 * 缺陷背景：拆解远程等待段进度假冻结——`lapian_stage` 是静态阶段字符串，日志行里的
 * 「已 Xs」是 job 总耗时快照，10 分钟不动。本模块把「读数」和「锚」彻底分开：
 *   · 锚（数据，来自读层任务文件）= lapian.started_at / lapian.stage_at / lapian.ended_at
 *   · 读数（展示，本模块算）= 总耗时 = now − started_at；本阶段已等待 = now − stage_at
 *
 * 三条不可让步的口径：
 *   1) 缺锚就诚实显示「计时不可用」——**禁止**拿 created_at、日志尾行 [HH:mm:ss]、
 *      日志行内「已 Xs」凑锚（摘要日志只回末 15 行、[HH:mm:ss] 无日期跨日不唯一）。
 *   2) 终态（成功/失败）停表：有 ended_at 时读数冻结在结束时刻，不随墙上钟继续走。
 *   3) 秒级走表与轮询解耦：本模块自带 1 秒展示钟，读数不依赖 5 秒列表轮询/详情刷新。
 *      （对应物=画布 lapian-node.tsx 的「1 秒展示钟与轮询解耦」，但那套从组件启动计时、
 *      不提供持久化恢复锚，所以恢复锚另由双锚合同提供，不复用它的计时状态。）
 */
import { useEffect, useState } from "react";

import type { HubLapianAnchors } from "./hub-api";

/** 展示钟步长：1 秒一跳（与轮询周期无关）。 */
export const LAPIAN_TICK_MS = 1000;

export type LapianClock = {
    /** 拆解总耗时（秒）；null = 计时不可用（缺/坏启动锚）。 */
    total: number | null;
    /** 当前阶段已等待（秒）；null = 不可用（缺/坏阶段锚）。 */
    stage: number | null;
    /** 终态冻结：ended_at 已落（成功/失败），读数停表。 */
    frozen: boolean;
    /** 诚实说明（空串 = 两值都可用且未停表）。 */
    note: string;
};

/** ISO 时间戳 → 毫秒；空/非字符串/坏值一律 null（不猜、不兜底成 now）。 */
export function anchorMs(value: string | null | undefined): number | null {
    if (typeof value !== "string" || !value.trim()) return null;
    const ms = Date.parse(value);
    return Number.isFinite(ms) ? ms : null;
}

/**
 * 给定锚与「当前时刻」算读数（纯函数：可控时钟直接喂 now，不用真等）。
 * 语义边界：总耗时与阶段等待是两个独立值——阶段变化只重置 stage，total 继续累积。
 */
export function lapianClockAt(anchors: HubLapianAnchors | null | undefined, nowMs: number): LapianClock {
    const started = anchorMs(anchors?.started_at);
    const stageAt = anchorMs(anchors?.stage_at);
    const ended = anchorMs(anchors?.ended_at);
    const at = ended === null ? nowMs : ended;            // 终态：冻结在结束时刻
    const secondsSince = (from: number) => Math.max(0, Math.round((at - from) / 1000));
    const total = started === null ? null : secondsSince(started);
    const stage = stageAt === null ? null : secondsSince(stageAt);
    const missing: string[] = [];
    if (total === null) missing.push("拆解启动锚");
    if (stage === null) missing.push("阶段锚");
    const note = missing.length
        ? `计时不可用：任务里没有${missing.join("与")}（旧任务，或日志/任务文件被截断）——不从日志时间戳反推`
        : ended === null
            ? ""
            : "已停表：拆解已到终态（成功/失败），读数冻结在结束时刻";
    return { total, stage, frozen: ended !== null, note };
}

/** 秒数 → 人话（35 秒 / 2 分 05 秒 / 1 小时 02 分）。 */
export function formatLapianDuration(seconds: number): string {
    const s = Math.max(0, Math.round(seconds));
    if (s < 60) return `${s} 秒`;
    if (s < 3600) return `${Math.floor(s / 60)} 分 ${String(s % 60).padStart(2, "0")} 秒`;
    return `${Math.floor(s / 3600)} 小时 ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")} 分`;
}

/**
 * 阶段文案 → 界面上该显示的部分（AT-16 B4a）：**去掉末尾的「（已 Xs）」**。
 *
 * 那是 discuss 落日志时的快照（`f"{stage}（已 {elapsed}s）"`，elapsed 是 job 总耗时，
 * 不是阶段起点），拆解远程等待段 10 分钟不动就是这个数字——它在界面上和走表读数并列
 * 会自相矛盾（"已 95s" 对 "总耗时 1 分 54 秒"）。数据层与日志照旧保留原文，只在这里
 * 做显示裁剪；裁剪后为空则返回空串，调用方自己兜「拆解中」。
 */
export function lapianStageName(stage: string | null | undefined): string {
    if (typeof stage !== "string") return "";
    return stage.replace(/\s*[（(]\s*已\s*\d+\s*s\s*[）)]\s*$/u, "").trim();
}

/**
 * 读数的唯一文案口径（列表与详情共用，避免两处各写一套措辞/格式）。
 * 单值为 null 时明说不可用，不写 0 秒冒充。
 */
export function lapianClockTexts(clock: LapianClock): { total: string; stage: string; note: string } {
    return {
        total: clock.total === null ? "总耗时 计时不可用" : `总耗时 ${formatLapianDuration(clock.total)}`,
        stage: clock.stage === null ? "本阶段已等待 计时不可用" : `本阶段已等待 ${formatLapianDuration(clock.stage)}`,
        note: clock.note,
    };
}

/**
 * 1 秒展示钟：锚一变立刻重算（换阶段/重开详情从锚恢复/重拆重置），终态停表不再跳。
 * 组件卸载清定时器；读数与父级轮询周期完全解耦。
 */
export function useLapianClock(
    anchors: HubLapianAnchors | null | undefined,
    tickMs: number = LAPIAN_TICK_MS,
): LapianClock {
    const startedAt = anchors?.started_at || "";
    const stageAt = anchors?.stage_at || "";
    const endedAt = anchors?.ended_at || "";
    const [now, setNow] = useState(() => Date.now());
    useEffect(() => {
        setNow(Date.now());                 // 锚变化：先按新锚校正一次，不等下一跳
        if (endedAt) return;                // 终态：停表（不再重渲染）
        const timer = window.setInterval(() => setNow(Date.now()), tickMs);
        return () => window.clearInterval(timer);
    }, [startedAt, stageAt, endedAt, tickMs]);
    return lapianClockAt({ started_at: startedAt, stage_at: stageAt, ended_at: endedAt }, now);
}
