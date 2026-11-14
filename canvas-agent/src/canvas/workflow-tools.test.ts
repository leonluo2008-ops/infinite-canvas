import { EventEmitter } from "node:events";
import type { ServerResponse } from "node:http";
import assert from "node:assert/strict";
import test from "node:test";

import { collectPaidActions } from "./paid-actions.js";
import { toolDescriptions, toolInputSchemas, toolNames } from "./schemas.js";
import { CanvasSession } from "./session.js";

const WORKFLOW_TOOLS = ["extract_workflow_template", "run_workflow_batch", "run_workflow_instance"] as const;

test("P9 三工具已注册：名称/入参 schema/中文描述齐全（注册面不漏）", () => {
    for (const name of WORKFLOW_TOOLS) {
        assert.ok((toolNames as readonly string[]).includes(name), `${name} 不在 toolNames`);
        assert.ok(toolInputSchemas[name], `${name} 没有入参 schema`);
        const description = toolDescriptions[name];
        assert.ok(description && description.length > 40, `${name} 描述过短`);
        // 卡面要求 5：描述里必须写明「终点=待审批 + 付费视频生成不在本工具内发生」
        assert.match(description, /待审批/, `${name} 描述缺「待审批」口径`);
        if (name !== "extract_workflow_template") assert.match(description, /付费视频生成不在本工具内发生/, `${name} 描述缺付费口径`);
    }
});

test("P9 extract_workflow_template 走页面通道（当前激活网页执行并回执）", async (t) => {
    const session = new CanvasSession();
    const first = connect(session, "first");
    const second = connect(session, "second");
    t.after(() => {
        first.close();
        second.close();
    });
    session.activateClient("second");

    const result = session.callTool("extract_workflow_template", { sourceNodeIds: ["v1", "lap-1"], name: "夹具模板" });
    const call = second.event("tool_call");
    assert.equal(first.event("tool_call"), undefined);
    assert.equal(field(call, "name"), "extract_workflow_template");
    assert.deepEqual(field(call, "input"), { sourceNodeIds: ["v1", "lap-1"], name: "夹具模板" });

    const pageResult = { ok: true, templateId: "wabc123", nodeCount: 2, slots: [], externalReferences: [], frozenOk: true };
    session.resolveResult("second", { requestId: String(field(call, "requestId")), result: pageResult });
    assert.deepEqual(await result, pageResult);
});

test("P9 run_workflow_batch 入参经 zod 校验后原样下发（素材组不被改写）", async (t) => {
    const session = new CanvasSession();
    const first = connect(session, "first");
    t.after(() => first.close());
    session.activateClient("first");

    const input = { templateId: "wabc123", materials: [{ sourceVideoNodeId: "v1", title: "素材一" }, { storageKey: "video:k2", optionalSegments: "A" }], concurrency: 2 };
    const result = session.callTool("run_workflow_batch", input);
    const call = first.event("tool_call");
    assert.equal(field(call, "name"), "run_workflow_batch");
    assert.deepEqual(field(call, "input"), input);

    const pageResult = { ok: true, batchId: "batch-1", instances: [{ instanceId: "i1", status: "awaiting_approval" }], paidVideoCreations: 0 };
    session.resolveResult("first", { requestId: String(field(call, "requestId")), result: pageResult });
    assert.deepEqual(await result, pageResult);
});

test("P10 具名工作流直达：描述写死「视频拆解」话术 + 零参即可自动解析（不要求 templateId/materials）", () => {
    const description = toolDescriptions.run_workflow_batch;
    // 卡面要求 2：用户说「用视频拆解工作流处理这些/这 N 个视频」时直接调本工具
    assert.match(description, /用视频拆解工作流处理/, "描述缺「视频拆解」话术触发句");
    assert.match(description, /templateId 与 materials 都不用传/, "描述缺「零参直达」口径");
    assert.match(description, /源视频节点/, "描述缺「自动解析源视频」口径");
    assert.match(description, /逐条列出/, "描述缺「回执逐条列素材供核对」口径");
    assert.match(description, /选中/, "描述缺「选中优先」口径");
    // 卡面要求 3/4：具名登记 + 内置模板开箱即用（P12：不再引导「先抽模板」）
    assert.match(description, /模板名/, "描述缺「回执报模板名」口径");
    assert.match(description, /系统内置工作流/, "描述缺「视频拆解是系统内置工作流」口径");
    assert.match(description, /开箱即用/, "描述缺「开箱即用」口径");
    assert.doesNotMatch(description, /抽一次模板/, "描述仍在引导「先抽模板」（P12 已把抽取降级为自定义能力）");
    assert.match(toolDescriptions.extract_workflow_template, /具名/, "抽模板工具描述缺「name 即具名」引导");
    assert.match(toolDescriptions.extract_workflow_template, /高级能力|自定义/, "抽模板工具描述缺「抽取=自定义能力」定位");
    assert.match(toolDescriptions.extract_workflow_template, /不需要先抽模板/, "抽模板工具描述缺「不需要先抽模板」口径");

    // zod：templateId / materials / 两者都不传 均合法（materials 空数组也按自动解析处理）
    assert.equal(toolInputSchemas.run_workflow_batch.safeParse({}).success, true);
    assert.equal(toolInputSchemas.run_workflow_batch.safeParse({ templateId: "wabc123" }).success, true);
    assert.equal(toolInputSchemas.run_workflow_batch.safeParse({ materials: [] }).success, true);
    assert.equal(toolInputSchemas.run_workflow_batch.safeParse({ materials: [{ sourceVideoNodeId: "v1" }] }).success, true);
    // 素材结构仍然校验（坏素材不下发）
    assert.equal(toolInputSchemas.run_workflow_batch.safeParse({ materials: [{ sourceVideoNodeId: 1 }] }).success, false);
});

test("P10 一句话直达：零参调用原样下发给页面（页面侧自动解析模板与素材）", async (t) => {
    const session = new CanvasSession();
    const first = connect(session, "first");
    t.after(() => first.close());
    session.activateClient("first");

    const pending = session.callTool("run_workflow_batch", {});
    const call = first.event("tool_call");
    assert.equal(field(call, "name"), "run_workflow_batch");
    assert.deepEqual(field(call, "input"), {});
    session.resolveResult("first", { requestId: String(field(call, "requestId")), result: { ok: true, materialsFrom: "canvas", materials: [] } });
    assert.deepEqual(await pending, { ok: true, materialsFrom: "canvas", materials: [] });
});

test("P11 画布替换图→槽：描述写明「自动识别替换素材 + 显式优先」且 zod 接受 assetBindings", () => {
    const batch = toolDescriptions.run_workflow_batch;
    assert.match(batch, /产品图\/人物图/, "run_workflow_batch 描述缺「画布产品图/人物图」口径");
    assert.match(batch, /自动识别为替换素材并绑定到对应槽位/, "run_workflow_batch 描述缺「自动识别并绑定到槽位」口径");
    assert.match(batch, /绑定理由/, "run_workflow_batch 描述缺「回执给绑定理由」口径");
    assert.match(batch, /显式指定的优先于自动匹配/, "run_workflow_batch 描述缺「显式优先于自动匹配」口径");
    assert.match(batch, /留空交人/, "run_workflow_batch 描述缺「判不准留空交人」口径");
    const instance = toolDescriptions.run_workflow_instance;
    assert.match(instance, /自动识别为替换素材并绑定到对应槽位/, "run_workflow_instance 描述缺绑定口径");
    assert.match(instance, /显式指定的优先于自动匹配/, "run_workflow_instance 描述缺显式优先口径");

    // zod：一条合法绑定（按槽名指代）与一条最简绑定（只给图）都合法；缺 imageNodeId 不合法
    assert.equal(toolInputSchemas.run_workflow_batch.safeParse({ assetBindings: [{ slotName: "女性A", imageNodeId: "img-1", reason: "把人物换成这张" }] }).success, true);
    assert.equal(toolInputSchemas.run_workflow_instance.safeParse({ material: { sourceVideoNodeId: "v1" }, assetBindings: [{ imageNodeId: "img-1" }] }).success, true);
    assert.equal(toolInputSchemas.run_workflow_batch.safeParse({ assetBindings: [{ slotName: "女性A" }] }).success, false);
});

test("P11 绑定入参原样下发页面（显式指定不被 Agent 侧改写）", async (t) => {
    const session = new CanvasSession();
    const first = connect(session, "first");
    t.after(() => first.close());
    session.activateClient("first");

    const input = { materials: [{ sourceVideoNodeId: "v1" }], assetBindings: [{ slotNodeId: "slot-female", imageNodeId: "img-1", reason: "这张人物" }] };
    const pending = session.callTool("run_workflow_batch", input);
    const call = first.event("tool_call");
    assert.deepEqual(field(call, "input"), input);
    session.resolveResult("first", { requestId: String(field(call, "requestId")), result: { ok: true, assetBindings: [], paidVideoCreations: 0 } });
    assert.deepEqual(await pending, { ok: true, assetBindings: [], paidVideoCreations: 0 });
});

test("P12 内置模板：描述写明「人物/产品」内置槽位 + 实例工具同样开箱即用", () => {
    const batch = toolDescriptions.run_workflow_batch;
    // 内置模板的资产槽位是语义层的唯一抓手：Agent 必须能按用户话术把它们翻成 assetBindings
    assert.match(batch, /资产槽位是「人物」和「产品」/, "描述缺内置槽位清单");
    assert.match(batch, /角色\/人物\/模特/, "描述缺「角色→人物槽」话术映射");
    assert.match(batch, /产品\/商品/, "描述缺「商品→产品槽」话术映射");
    // 回执要能区分模板来源（builtin/project），否则用户不知道这次用的是哪个模板
    assert.match(batch, /templateFrom=explicit\/project\/builtin/, "描述缺「回执报模板来源」口径");
    // 不完整空壳模板：显式指向时人话报错，不建批
    assert.match(batch, /不完整空壳模板会人话报错/, "描述缺「空壳模板报错不建批」口径");
    const single = toolDescriptions.run_workflow_instance;
    assert.match(single, /系统内置的「视频拆解」/, "run_workflow_instance 描述缺内置模板口径");
    assert.match(single, /内置槽位=「人物」「产品」/, "run_workflow_instance 描述缺内置槽位口径");
});

test("P9 入参校验拒绝缺 material（不把坏调用下发给页面）", async (t) => {
    const session = new CanvasSession();
    const first = connect(session, "first");
    t.after(() => first.close());
    session.activateClient("first");

    await assert.rejects(() => session.callTool("run_workflow_instance", {}));
    await assert.rejects(() => session.callTool("run_workflow_instance", { material: "not-an-object" }));
    // 校验失败必须在页面侧零下发（页面没收到任何 tool_call）
    assert.equal(first.event("tool_call"), undefined);
});

test("P9 三工具不触发付费确认队列（付费闸不被绕过，也不会被误挂起）", async (t) => {
    const session = new CanvasSession();
    const first = connect(session, "first");
    t.after(() => first.close());
    session.activateClient("first");

    const state = { projectId: "p1", title: "p1", nodes: [{ id: "v1", type: "video", position: { x: 0, y: 0 }, width: 100, height: 100, metadata: { generationMode: "video", count: 4 } }], connections: [] } as never;
    assert.deepEqual(collectPaidActions("extract_workflow_template", { sourceNodeIds: ["v1"] }, state), []);
    assert.deepEqual(collectPaidActions("run_workflow_batch", { materials: [{ sourceVideoNodeId: "v1" }], concurrency: 3 }, state), []);
    assert.deepEqual(collectPaidActions("run_workflow_instance", { material: { sourceVideoNodeId: "v1" } }, state), []);

    const pending = session.callTool("run_workflow_batch", { materials: [{ sourceVideoNodeId: "v1" }] });
    const call = first.event("tool_call");
    assert.equal(first.event("paid_confirmation"), undefined, "付费工具才会走 paid_confirmation，本工具不该挂起");
    session.resolveResult("first", { requestId: String(field(call, "requestId")), result: { ok: true } });
    assert.deepEqual(await pending, { ok: true });
});

test("既有画布工具零回归：read_node_text / generation_get_status 仍走页面通道", async (t) => {
    const session = new CanvasSession();
    const first = connect(session, "first");
    t.after(() => first.close());
    session.activateClient("first");

    const text = session.callTool("read_node_text", { nodeId: "text-1" });
    const textCall = first.event("tool_call");
    assert.equal(field(textCall, "name"), "read_node_text");
    session.resolveResult("first", { requestId: String(field(textCall, "requestId")), result: { nodeId: "text-1", length: 3 } });
    assert.deepEqual(await text, { nodeId: "text-1", length: 3 });

    const status = session.callTool("generation_get_status", { scope: "canvas" });
    const statusCall = first.events("tool_call")[1];
    assert.equal(field(statusCall, "name"), "generation_get_status");
    session.resolveResult("first", { requestId: String(field(statusCall, "requestId")), result: { total: 0, tasks: [] } });
    assert.deepEqual(await status, { total: 0, tasks: [] });
});

/** 连接一个模拟网页客户端。 */
function connect(session: CanvasSession, clientId: string) {
    const response = new FakeSseResponse();
    session.openEvents(new URL(`http://127.0.0.1/events?clientId=${clientId}`), response as unknown as ServerResponse, "");
    return response;
}

/** 安全读取测试对象字段。 */
function field(value: unknown, key: string) {
    return value && typeof value === "object" ? (value as Record<string, unknown>)[key] : undefined;
}

/** 模拟 Node SSE 响应并提供事件读取能力。 */
class FakeSseResponse extends EventEmitter {
    private chunks: string[] = [];

    writeHead() {
        return this;
    }

    write(chunk: string) {
        this.chunks.push(chunk);
        return true;
    }

    event(type: string) {
        return this.events(type)[0];
    }

    events(type: string) {
        return this.chunks.flatMap((chunk) => {
            if (!chunk.startsWith(`event: ${type}\n`)) return [];
            const data = chunk.split("\n").find((line) => line.startsWith("data: "))?.slice(6);
            return data ? [JSON.parse(data) as Record<string, unknown>] : [];
        });
    }

    /** 触发连接关闭事件（与 session.test.ts 的假响应同形）。 */
    close() {
        this.emit("close");
    }
}
