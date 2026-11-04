import assert from "node:assert/strict";
import test from "node:test";

import { dataUrlBytes, dataUrlMime, guardTurnAttachments, MAX_ATTACHMENT_BYTES } from "./attachments.js";

/** 造一个解码后正好 `bytes` 字节的 base64 图片 dataUrl。 */
function imageDataUrl(bytes: number, mime = "image/png") {
    const payload = Buffer.alloc(bytes, 1).toString("base64");
    return `data:${mime};base64,${payload}`;
}

test("图片 dataUrl：MIME 与解码字节数算得准", () => {
    assert.equal(dataUrlMime("data:image/png;base64,AAAA"), "image/png");
    assert.equal(dataUrlMime("data:IMAGE/JPEG;charset=utf-8;base64,AAAA"), "image/jpeg");
    assert.equal(dataUrlMime("https://x/a.png"), "");
    assert.equal(dataUrlBytes("data:image/png;base64,AAAA"), 3);
    assert.equal(dataUrlBytes(imageDataUrl(1024)), 1024);
    assert.equal(dataUrlBytes("not-a-data-url"), 0);
});

test("8MB 以内的图片附件放行（含边界）", () => {
    assert.deepEqual(guardTurnAttachments([]), { ok: true });
    assert.deepEqual(guardTurnAttachments([{ name: "a.png", type: "image/png", dataUrl: imageDataUrl(2048) }]), { ok: true });
    // 边界：正好 8MB 放行，多 1 字节拒收。
    assert.deepEqual(guardTurnAttachments([{ name: "edge.png", dataUrl: imageDataUrl(MAX_ATTACHMENT_BYTES) }]), { ok: true });
    const over = guardTurnAttachments([{ name: "edge.png", dataUrl: imageDataUrl(MAX_ATTACHMENT_BYTES + 1) }]);
    assert.equal(over.ok, false);
    assert.match(over.ok ? "" : over.error, /8MB/);
});

test("超 8MB 的图片附件拒收，文案给出压缩或 @素材引用两条路", () => {
    const result = guardTurnAttachments([{ name: "big.png", dataUrl: imageDataUrl(MAX_ATTACHMENT_BYTES + 1024 * 1024) }]);
    assert.equal(result.ok, false);
    const error = result.ok ? "" : result.error;
    assert.match(error, /big\.png/);
    assert.match(error, /9\.0MB/);
    assert.match(error, /超过单张图片 8MB 上限/);
    assert.match(error, /@素材引用/);
});

test("非图片附件（视频/音频）拒收，文案引导改用 @画布素材引用", () => {
    const video = guardTurnAttachments([{ name: "clip.mp4", type: "video/mp4", dataUrl: "data:video/mp4;base64,AAAA" }]);
    assert.equal(video.ok, false);
    assert.match(video.ok ? "" : video.error, /video\/mp4/);
    assert.match(video.ok ? "" : video.error, /只支持图片/);
    assert.match(video.ok ? "" : video.error, /@画布素材引用/);

    const audio = guardTurnAttachments([{ name: "voice.mp3", type: "audio/mpeg", dataUrl: "data:audio/mpeg;base64,AAAA" }]);
    assert.equal(audio.ok, false);
    assert.match(audio.ok ? "" : audio.error, /只支持图片/);
});

test("无数据或非 base64 的附件拒收；多张里只要有一张不合格就整体拒收", () => {
    const missing = guardTurnAttachments([{ name: "空" }]);
    assert.equal(missing.ok, false);
    assert.match(missing.ok ? "" : missing.error, /缺少图片数据/);

    const plain = guardTurnAttachments([{ name: "a.png", dataUrl: "data:image/png,raw-bytes" }]);
    assert.equal(plain.ok, false);
    assert.match(plain.ok ? "" : plain.error, /base64/);

    const mixed = guardTurnAttachments([
        { name: "ok.png", dataUrl: imageDataUrl(128) },
        { name: "clip.mp4", dataUrl: "data:video/mp4;base64,AAAA" },
    ]);
    assert.equal(mixed.ok, false);
    assert.match(mixed.ok ? "" : mixed.error, /clip\.mp4/);
});
