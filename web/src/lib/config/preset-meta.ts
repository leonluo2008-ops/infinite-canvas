/**
 * S3.5-B2：预置模板版本化元数据（`infinite-canvas:preset_meta`）。
 *
 * 硬约束（任务书 v1.2 §2.1）：
 *  * **独立 localStorage 键，不进 config store 的 persist 信封**——`probe-bootstrap-config`
 *    断言信封顶层键严格等于 ["state","version"]，元数据一旦塞进 state 就破该约束。
 *  * 记 `{ lastPresetVersion, tombstones, aliases }` 三件：
 *    - `lastPresetVersion`：已真正落地的模板版本（sha1 前 8 位，字符串比较）；
 *    - `tombstones`：用户删过的模板渠道 id（仅本浏览器记忆，不进服务端）；
 *    - `aliases`：升格派生 id 映射 `templateId -> mintedId`（mint 恰一次落盘的凭据）。
 *
 * 本模块不引用 store / i18n / React，只碰 localStorage（带 window 守卫），
 * 因此 store 的 decode 层与 node 探针都能直接 import。缓存只为「decode 处 lazy 自愈」
 * 少读 localStorage，写操作一律先落盘再更新缓存。
 */
export const PRESET_META_KEY = "infinite-canvas:preset_meta";

export type PresetMeta = {
    lastPresetVersion: string;
    tombstones: string[];
    aliases: Record<string, string>;
};

function emptyMeta(): PresetMeta {
    return { lastPresetVersion: "", tombstones: [], aliases: {} };
}

function readStorage(): Storage | null {
    try {
        if (typeof window === "undefined" || !window.localStorage) return null;
        return window.localStorage;
    } catch {
        // 隐私模式/被禁的 localStorage：元数据功能整体降级为不记忆（渠道功能不受影响）
        return null;
    }
}

// 缓存按「原始串」命中（不是一次性）：同进程内 localStorage 被改写（探针/其它标签页语义）也不会读到陈旧值
let cachedRaw: string | null = null;
let cachedMeta: PresetMeta | null = null;

function normalizeMeta(raw: unknown): PresetMeta {
    const meta = emptyMeta();
    if (!raw || typeof raw !== "object") return meta;
    const value = raw as Partial<PresetMeta>;
    meta.lastPresetVersion = typeof value.lastPresetVersion === "string" ? value.lastPresetVersion : "";
    meta.tombstones = Array.isArray(value.tombstones) ? value.tombstones.filter((id): id is string => typeof id === "string" && Boolean(id)) : [];
    if (value.aliases && typeof value.aliases === "object" && !Array.isArray(value.aliases)) {
        for (const [from, to] of Object.entries(value.aliases)) {
            if (typeof from === "string" && from && typeof to === "string" && to) meta.aliases[from] = to;
        }
    }
    return meta;
}

/** 读元数据（坏 JSON / 缺失一律回空结构，绝不让启动路径抛）。 */
export function readPresetMeta(): PresetMeta {
    const storage = readStorage();
    if (!storage) return emptyMeta();
    let raw: string | null = null;
    try {
        raw = storage.getItem(PRESET_META_KEY);
    } catch {
        return emptyMeta();
    }
    if (cachedMeta && raw === cachedRaw) return cachedMeta;
    let parsed: unknown = null;
    try {
        parsed = raw ? JSON.parse(raw) : null;
    } catch {
        parsed = null;
    }
    cachedRaw = raw;
    cachedMeta = normalizeMeta(parsed);
    return cachedMeta;
}

/** 探针/测试用：丢掉内存缓存，强制下次从 localStorage 重读。 */
export function resetPresetMetaCache() {
    cachedRaw = null;
    cachedMeta = null;
}

/**
 * 写元数据（浅合并）。
 * **抛异常语义由调用方决定**：单人模式「写失败不写版本不 reload」依赖它把 setItem 的
 * 配额异常透出来，所以这里不吞。
 */
export function writePresetMeta(patch: Partial<PresetMeta>): PresetMeta {
    const next = normalizeMeta({ ...readPresetMeta(), ...patch });
    if (patch.aliases) next.aliases = { ...readPresetMeta().aliases, ...patch.aliases };
    const storage = readStorage();
    const raw = JSON.stringify(next);
    if (storage) storage.setItem(PRESET_META_KEY, raw);
    cachedRaw = raw;
    cachedMeta = next;
    return next;
}

/** 模板版本真正落地后调用（合并/首启写盘成功后）。 */
export function setLastPresetVersion(version: string) {
    writePresetMeta({ lastPresetVersion: version || "" });
}

export function lastPresetVersion(): string {
    return readPresetMeta().lastPresetVersion;
}

/** 记墓碑（用户删了 id ∈ 当前模板 id 集合的渠道）。重复记=幂等。 */
export function recordTombstone(id: string) {
    const value = (id || "").trim();
    if (!value) return;
    const current = readPresetMeta().tombstones;
    if (current.includes(value)) return;
    writePresetMeta({ tombstones: [...current, value] });
}

export function presetTombstones(): string[] {
    return readPresetMeta().tombstones;
}

/** 记 alias（mint 恰一次落盘的凭据）。已存在则原样返回旧值，保证「连续两次 flush id 不变」。 */
export function recordAlias(templateId: string, mintedId: string): string {
    const from = (templateId || "").trim();
    const to = (mintedId || "").trim();
    if (!from || !to) return from;
    const aliases = readPresetMeta().aliases;
    if (aliases[from]) return aliases[from];
    writePresetMeta({ aliases: { [from]: to } });
    return to;
}

export function presetAliases(): Record<string, string> {
    return readPresetMeta().aliases;
}

/** 模板 id → 已 mint 的渠道 id（无映射原样返回）。 */
export function resolvePresetAliasId(channelId: string): string {
    const id = (channelId || "").trim();
    if (!id) return channelId;
    return readPresetMeta().aliases[id] || channelId;
}

/** 反查：mintedId → templateId（删升格渠道记墓碑用）。 */
export function templateIdOfMinted(mintedId: string): string {
    const id = (mintedId || "").trim();
    if (!id) return "";
    for (const [from, to] of Object.entries(readPresetMeta().aliases)) {
        if (to === id) return from;
    }
    return "";
}

// ------------------------------------------------------------------ 当前模板 id 集合
//
// 单人模式与登录模式都要判「这个 id 属不属于当前模板」（删渠道记墓碑 / 填 key 升格换 id），
// 而两条启动路径各自拉 bootstrap 文件——真源只能有一份，放在这里由拉起方写入。

let knownTemplateIds: string[] = [];

/** 拉起方（bootstrap-config / channel-sync 启动门）拿到预置文件后写入。 */
export function setPresetTemplateIdList(ids: string[] | undefined | null) {
    knownTemplateIds = Array.from(new Set((ids || []).map((id) => (id || "").trim()).filter(Boolean)));
}

export function presetTemplateIdList(): string[] {
    return [...knownTemplateIds];
}

export function isPresetTemplateId(id: string | undefined | null): boolean {
    if (!id) return false;
    return knownTemplateIds.includes(String(id));
}
