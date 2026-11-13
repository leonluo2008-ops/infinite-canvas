/**
 * Wan 成片映射单测（S2；node:test，经 `scripts/probe-p6-batch-queue.mjs` 打包后真跑）。
 *
 * 覆盖：
 *  A. 老 payload 解析（`{job: taskId}` 仍可读；坏载荷当不存在）
 *  B. 老 payload 回写仍是 200 条（兼容回归不能破）
 *  C. 项目登记表不截断（S2 核心：>200 条也不丢）
 *  D. 合并优先级（项目记录压过老 localStorage）
 *  E. 导入校验 / 查找 / 摘要
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
    WAN_FILE_TASK_LEGACY_KEY,
    WAN_FILE_TASK_LEGACY_LIMIT,
    acceptWanFileTasks,
    lookupWanFileTask,
    mergeWanFileTasks,
    normalizeWanFileTaskRecord,
    parseLegacyWanFileTasks,
    toLegacyPayload,
    upsertWanFileTask,
    wanFileTasksDigest,
    type WanFileTaskRecord,
} from "@/lib/canvas/wan-file-task-map";

const AT = "2026-11-13T00:00:00.000Z";

describe("A. 老 localStorage 载荷（兼容读取）", () => {
    it("旧键与旧上限保持不变", () => {
        assert.equal(WAN_FILE_TASK_LEGACY_KEY, "wan_video_file_tasks");
        assert.equal(WAN_FILE_TASK_LEGACY_LIMIT, 200);
    });

    it("{job: taskId} 老载荷可解析成登记表记录", () => {
        const records = parseLegacyWanFileTasks(JSON.stringify({ jobA: "task-A", jobB: "task-B" }), AT);
        assert.equal(records.length, 2);
        assert.deepEqual(records.map((item) => [item.job, item.fileTaskId, item.provider, item.recordedAt]), [
            ["jobA", "task-A", "wan", AT],
            ["jobB", "task-B", "wan", AT],
        ]);
    });

    it("坏载荷/空值当不存在（不抛）", () => {
        assert.deepEqual(parseLegacyWanFileTasks("not-json"), []);
        assert.deepEqual(parseLegacyWanFileTasks(null), []);
        assert.deepEqual(parseLegacyWanFileTasks(JSON.stringify([1, 2, 3])), []);
    });

    it("坏条目被丢弃（缺 job / 缺 taskId）", () => {
        assert.equal(normalizeWanFileTaskRecord({ job: "", fileTaskId: "x" }), null);
        assert.equal(normalizeWanFileTaskRecord({ job: "j" }), null);
        assert.equal(normalizeWanFileTaskRecord({ job: "j", taskId: "t" })?.fileTaskId, "t");
    });
});

describe("B. 老载荷回写（仍按 200 条）", () => {
    it("250 条只回写最后 200 条（旧行为一致）", () => {
        const pairs: [string, string][] = Array.from({ length: 250 }, (_, index) => [`job-${index}`, `task-${index}`]);
        const payload = toLegacyPayload(pairs);
        const keys = Object.keys(payload);
        assert.equal(keys.length, 200);
        assert.equal(keys[0], "job-50");
        assert.equal(keys.at(-1), "job-249");
    });

    it("登记表记录同样能转老载荷", () => {
        const records: WanFileTaskRecord[] = [
            { job: "j1", fileTaskId: "t1", provider: "wan", recordedAt: AT },
            { job: "j2", fileTaskId: "t2", provider: "wan", recordedAt: AT },
        ];
        assert.deepEqual(toLegacyPayload(records), { j1: "t1", j2: "t2" });
    });
});

describe("C. 项目登记表（S2：不截断）", () => {
    it(">200 条全部保留（这是相对 localStorage 的核心差异）", () => {
        let records: WanFileTaskRecord[] = [];
        for (let index = 0; index < 260; index += 1) records = upsertWanFileTask(records, { job: `job-${index}`, fileTaskId: `task-${index}`, provider: "wan", recordedAt: AT });
        assert.equal(records.length, 260);
        assert.equal(lookupWanFileTask(records, "job-0"), "task-0");
        assert.equal(toLegacyPayload(records)["job-0"], undefined, "老载荷按 200 截断：老键里确实没有最早的记录");
    });

    it("同一 job 只留最新一条（覆盖，不重复）", () => {
        const first = upsertWanFileTask([], { job: "j", fileTaskId: "t-old", provider: "wan", recordedAt: AT });
        const second = upsertWanFileTask(first, { job: "j", fileTaskId: "t-new", provider: "wan", recordedAt: AT });
        assert.equal(second.length, 1);
        assert.equal(lookupWanFileTask(second, "j"), "t-new");
    });
});

describe("D. 合并优先级与查找", () => {
    it("项目记录压过老 localStorage（同 job 取项目值）", () => {
        const project: WanFileTaskRecord[] = [{ job: "j1", fileTaskId: "project-t1", provider: "wan", recordedAt: AT }];
        const legacy = parseLegacyWanFileTasks(JSON.stringify({ j1: "legacy-t1", j2: "legacy-t2" }), AT);
        const merged = mergeWanFileTasks(legacy, project);
        assert.equal(merged.length, 2);
        assert.equal(lookupWanFileTask(merged, "j1"), "project-t1");
        assert.equal(lookupWanFileTask(merged, "j2"), "legacy-t2");
        assert.equal(lookupWanFileTask(merged, "nope"), "");
    });

    it("摘要可复算且与顺序无关", () => {
        const a: WanFileTaskRecord[] = [
            { job: "j1", fileTaskId: "t1", provider: "wan", recordedAt: AT },
            { job: "j2", fileTaskId: "t2", provider: "wan", recordedAt: AT },
        ];
        assert.equal(wanFileTasksDigest(a), wanFileTasksDigest([...a].reverse()));
        assert.notEqual(wanFileTasksDigest(a), wanFileTasksDigest([a[0]]));
    });
});

describe("E. 导入校验", () => {
    it("形如登记表就透传；坏条目丢弃；空表返回 undefined", () => {
        const accepted = acceptWanFileTasks([{ job: "j", fileTaskId: "t" }, { job: "" }, null]);
        assert.equal(accepted?.length, 1);
        assert.equal(acceptWanFileTasks([]), undefined);
        assert.equal(acceptWanFileTasks("nope"), undefined);
        assert.equal(acceptWanFileTasks(undefined), undefined);
    });
});
