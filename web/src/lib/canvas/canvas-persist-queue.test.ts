/**
 * P5 落盘队列单测（node:test；经 `scripts/probe-p5-batch-status.mjs` 打包后真跑）。
 *
 * 覆盖：防抖合并 / flush 立即落盘 / 无待写值时 flush 不假写 / 写失败可见且不丢待写值 / prime 恢复基线。
 * 全部用**真定时器**（短窗口），不 mock 时间——探针要证的正是「等 vs 不等」的差别。
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { createPersistQueue } from "@/lib/canvas/canvas-persist-queue";

type FakeState = { projects: { id: string }[] };

function memoryStorage(options: { fail?: () => boolean } = {}) {
    const writes: { name: string; payload: string }[] = [];
    return {
        writes,
        storage: {
            setItem: async (name: string, value: string) => {
                if (options.fail?.()) throw new Error("磁盘满了（探针伪造写盘失败）");
                writes.push({ name, payload: value });
            },
        },
    };
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

describe("落盘队列", () => {
    it("防抖窗口内不写盘，窗口后写一次（合并多次 set）", async () => {
        const disk = memoryStorage();
        const queue = createPersistQueue<FakeState>({ name: "k", storage: disk.storage, delayMs: 30 });
        queue.setItem({ projects: [{ id: "a" }] });
        assert.equal(queue.pending(), true);
        queue.setItem({ projects: [{ id: "b" }] });
        assert.equal(disk.writes.length, 0, "防抖窗口内不该写盘");
        await sleep(80);
        assert.equal(disk.writes.length, 1, "多次 set 合并成一次写");
        assert.ok(disk.writes[0].payload.includes("\"b\""), "写的是最新一份");
        assert.equal(queue.pending(), false);
    });

    it("flush 立即落盘（不等防抖窗口），字节数如实回传", async () => {
        const disk = memoryStorage();
        const queue = createPersistQueue<FakeState>({ name: "k", storage: disk.storage, delayMs: 5000 });
        queue.setItem({ projects: [{ id: "flush-now" }] });
        const startedAt = Date.now();
        const result = await queue.flush("probe");
        const elapsed = Date.now() - startedAt;
        assert.equal(result.ok, true);
        assert.equal(result.written, true);
        assert.ok(result.bytes > 0);
        assert.ok(elapsed < 1000, `flush 必须立刻返回，实际 ${elapsed}ms`);
        assert.equal(disk.writes.length, 1, "flush 自己写盘，不必等 5s 防抖");
        assert.ok(disk.writes[0].payload.includes("flush-now"));
        assert.equal(queue.flushCount(), 1);
        // 之后的防抖计时器已被取消：再等一会儿不会重复写
        await sleep(60);
        assert.equal(disk.writes.length, 1, "flush 之后不该再补一次防抖写");
    });

    it("没有待写值时 flush 不算失败、也不假写", async () => {
        const disk = memoryStorage();
        const queue = createPersistQueue<FakeState>({ name: "k", storage: disk.storage, delayMs: 10 });
        const result = await queue.flush();
        assert.equal(result.ok, true);
        assert.equal(result.written, false);
        assert.equal(disk.writes.length, 0);
    });

    it("写盘失败：flush 返回错误 + onError 可见 + 待写值保留（下次仍能落盘）", async () => {
        let broken = true;
        const disk = memoryStorage({ fail: () => broken });
        const errors: string[] = [];
        const queue = createPersistQueue<FakeState>({ name: "k", storage: disk.storage, delayMs: 10, onError: (error) => errors.push(error.message) });
        queue.setItem({ projects: [{ id: "dup" }] });
        const failed = await queue.flush("boom");
        assert.equal(failed.ok, false);
        assert.ok(String(failed.error).includes("磁盘满了"));
        assert.deepEqual(errors, ["磁盘满了（探针伪造写盘失败）"]);
        assert.equal(queue.pending(), true, "失败后待写值必须保留（不静默丢数据）");
        assert.ok(String(queue.lastError()).includes("磁盘满了"));
        broken = false;
        const retried = await queue.flush("retry");
        assert.equal(retried.ok, true);
        assert.equal(retried.written, true);
        assert.equal(queue.lastError(), null, "成功后错误清空");
        assert.equal(disk.writes.length, 1);
    });

    it("prime 恢复基线：同一份值不再回写；引用变了的才写", async () => {
        const disk = memoryStorage();
        const queue = createPersistQueue<FakeState>({ name: "k", storage: disk.storage, delayMs: 10, isEqual: (previous, next) => previous.projects === next.projects });
        const restored: FakeState = { projects: [{ id: "from-disk" }] };
        queue.prime(restored);
        queue.setItem(restored);
        assert.equal(queue.pending(), false, "刚恢复的同一份不该入队");
        queue.setItem({ projects: [...restored.projects, { id: "new" }] });
        assert.equal(queue.pending(), true);
        await queue.flush("after-prime");
        assert.equal(disk.writes.length, 1);
        assert.ok(disk.writes[0].payload.includes("new"));
    });
});
