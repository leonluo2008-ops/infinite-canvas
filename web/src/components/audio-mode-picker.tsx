/**
 * 音频三态选择器（AT-17 B3，方案 `docs/nodes/AT-17.md` §4 B3 + §7 附注 14）。
 *
 * **两处界面增量共用这一个控件**：画布拉片弹层（`pages/canvas/project.tsx`）与 hub 确认流按钮区
 * （`pages/hub/task-detail-drawer.tsx`）。不新开面板、不造第二套样式（红线 25）：hub 侧沿用抽屉里
 * 现有的 stone/amber 配色与字号，画布侧沿用拉片弹层的深色面板。
 *
 * 合同：
 *  · 值恒为后端枚举的三个中文词（`pipeline/reverse/intent.py:AUDIO_MODE_VALUES`）——全链一套命名，
 *    禁止 `audio_mode` / `音频三态` / `沿用·调整·不要` 三套混用（B0 附注 4）；
 *  · 本组件**只展示 + 回调**，不发请求：写盘与重渲染归调用方（hub=POST /api/hub/task/<id>/audio-mode，
 *    画布=POST /api/audio-mode/<id>），两条链各自一处收口；
 *  · `待核对` 是 B2c 的「不猜无 BGM」透传：拆解读不到 BGM 判定时挂 WARN，让人先核对再信这个值。
 */
export const AUDIO_MODE_VALUES = ["沿用原片", "按新主体调整", "不要"] as const;
export type AudioModeValue = (typeof AUDIO_MODE_VALUES)[number];

/** 三态的人话解释（悬停提示，避免「调整」被理解成「不换声」）。 */
export const AUDIO_MODE_HINTS: Record<AudioModeValue, string> = {
    沿用原片: "原片的 BGM 与音效点原样保留",
    按新主体调整: "只留情绪 / 节奏 / 织体骨架，剥离指向旧主体的声源与音效点",
    不要: "显式声明无 BGM，只保留环境声",
};

export function isAudioModeValue(value: string): value is AudioModeValue {
    return (AUDIO_MODE_VALUES as readonly string[]).includes(value);
}

const HUB_BTN_BASE =
    "rounded-md border px-2 py-0.5 text-xs transition disabled:cursor-not-allowed disabled:opacity-50";
const HUB_BTN_ON =
    "border-amber-400 bg-amber-50 font-medium text-amber-700 dark:border-amber-700 dark:bg-amber-950/40 dark:text-amber-300";
const HUB_BTN_OFF =
    "border-stone-200 bg-card text-stone-600 hover:border-stone-300 dark:border-stone-700 dark:text-stone-300 dark:hover:border-stone-600";
const CANVAS_BTN_BASE =
    "rounded-md border px-2 py-0.5 text-[11px] transition disabled:cursor-not-allowed disabled:opacity-40";
const CANVAS_BTN_ON = "border-amber-400/70 bg-amber-500/15 font-medium text-amber-200";
const CANVAS_BTN_OFF = "border-white/10 bg-stone-950 text-stone-300 hover:border-white/25";

export function AudioModePicker({
    value,
    basis = "",
    pendingCheck = false,
    busy = false,
    disabled = false,
    tone = "hub",
    label = "音频三态",
    onChange,
    dataAttr = "audio-mode-picker",
}: {
    /** 当前值；空串/枚举外 = 还没落值（拆解后自动判定），此时不高亮任何一个。 */
    value: string;
    /** 判定依据（B2c 落盘的「依据」；空则不显示）。 */
    basis?: string;
    /** B2c 的待核对标记：true → 显示 WARN，不伪装成已定论。 */
    pendingCheck?: boolean;
    busy?: boolean;
    disabled?: boolean;
    tone?: "hub" | "canvas";
    label?: string;
    onChange: (next: AudioModeValue) => void;
    dataAttr?: string;
}) {
    const canvas = tone === "canvas";
    const btnBase = canvas ? CANVAS_BTN_BASE : HUB_BTN_BASE;
    const btnOn = canvas ? CANVAS_BTN_ON : HUB_BTN_ON;
    const btnOff = canvas ? CANVAS_BTN_OFF : HUB_BTN_OFF;
    const muted = canvas ? "text-stone-400" : "text-stone-500 dark:text-stone-400";
    const faint = canvas ? "text-stone-500" : "text-stone-400 dark:text-stone-500";
    return (
        <div className="flex flex-col gap-1" data-audio-mode-picker={tone} data-audio-mode-value={value}>
            <div className="flex flex-wrap items-center gap-1.5">
                <span className={`text-xs ${muted}`}>{label}</span>
                {AUDIO_MODE_VALUES.map((option) => {
                    const active = option === value;
                    return (
                        <button
                            key={option}
                            type="button"
                            title={AUDIO_MODE_HINTS[option]}
                            aria-pressed={active}
                            disabled={disabled || busy}
                            data-audio-mode-option={option}
                            data-audio-mode-active={active ? "1" : "0"}
                            className={`${btnBase} ${active ? btnOn : btnOff}`}
                            onClick={() => {
                                if (active || disabled || busy) return;
                                onChange(option);
                            }}
                        >
                            {busy && active ? "…" : option}
                        </button>
                    );
                })}
                {value && !isAudioModeValue(value) ? (
                    <span className={`text-[11px] ${faint}`} data-audio-mode-unknown>
                        当前值不在枚举内：{value}
                    </span>
                ) : null}
            </div>
            <div className={`text-[11px] leading-4 ${faint}`} data-audio-mode-basis>
                {value ? `当前：${value}` : "当前未落值（拆解出「八 声音」后按 BGM 自动判定）"}
                {basis ? ` · 依据：${basis}` : ""}
            </div>
            {pendingCheck ? (
                <div
                    className={
                        canvas
                            ? "rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-1 text-[11px] leading-4 text-amber-200"
                            : "rounded-md border border-amber-200 bg-amber-50 px-2 py-1 text-[11px] leading-4 text-amber-700 dark:border-amber-950 dark:bg-amber-950/20 dark:text-amber-300"
                    }
                    data-audio-mode-warn="1"
                >
                    ⚠ 待核对：拆解八 声音读不到 BGM 判定（不是「无 BGM」）——先核对原片有没有背景音乐，再信这个三态。
                </div>
            ) : null}
        </div>
    );
}
