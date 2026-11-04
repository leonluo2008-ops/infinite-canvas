import {
    CONFIG_STORE_KEY,
    createModelChannel,
    defaultConfig,
    defaultWebdavSyncConfig,
    encodeChannelModel,
    modelOptionsFromChannels,
    type AiConfig,
    type ModelCapability,
    type ModelChannel,
} from "@/stores/use-config-store";

/**
 * First-launch preset import for bundled deployments (bridge serving the
 * static bundle on one origin): when the persisted AI config is absent (fresh
 * browser profile, empty localStorage), fetch the same-origin preset endpoint
 * and seed the zustand persist entry, then reload once so the store hydrates
 * the preset channels.
 *
 * Idempotent: nothing is fetched or written when the storage key already
 * exists. Any failure (404, offline, malformed payload) is silently ignored —
 * the app just keeps its default config.
 */
const BOOTSTRAP_CONFIG_ENDPOINT = "/api/bootstrap-config";
// use-config-store's persist declares no version → zustand default is 0.
const PERSIST_VERSION = 0;

function firstModelOptionOf(channels: ModelChannel[], capability: ModelCapability) {
    for (const channel of channels) {
        const model = channel.models.find((item) => item.capability === capability);
        if (model) return encodeChannelModel(channel.id, model.name);
    }
    return "";
}

export async function maybeImportBootstrapConfig() {
    try {
        if (typeof window === "undefined") return;
        if (window.localStorage.getItem(CONFIG_STORE_KEY)) return;

        const response = await fetch(BOOTSTRAP_CONFIG_ENDPOINT);
        if (!response.ok) return;
        const payload = (await response.json()) as { channels?: unknown } | null;
        if (!payload || !Array.isArray(payload.channels)) return;
        // Drop malformed entries (null/string) so a sloppy preset file cannot
        // produce placeholder channels; a normalized empty list imports nothing.
        const channels = payload.channels.filter((item) => Boolean(item) && typeof item === "object").map((channel) => createModelChannel(channel as Partial<ModelChannel>));
        if (!channels.length) return;
        const models = modelOptionsFromChannels(channels);
        const config: AiConfig = {
            ...defaultConfig,
            channels,
            models,
            model: firstModelOptionOf(channels, "image") || models[0] || "",
            imageModel: firstModelOptionOf(channels, "image"),
            videoModel: firstModelOptionOf(channels, "video"),
            textModel: firstModelOptionOf(channels, "text"),
            audioModel: firstModelOptionOf(channels, "audio"),
        };
        window.localStorage.setItem(
            CONFIG_STORE_KEY,
            JSON.stringify({
                state: { config, webdav: { ...defaultWebdavSyncConfig } },
                version: PERSIST_VERSION,
            }),
        );
        console.info(`[bootstrap] imported ${channels.length} channel(s) from ${BOOTSTRAP_CONFIG_ENDPOINT}`);
        window.location.reload();
    } catch {
        // Missing endpoint or offline — keep the default config, stay silent.
    }
}
