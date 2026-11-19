/**
 * S3.12 服务探针：媒体托管首选（media-host）登录门 / 清单 / 首选读写 / 校验 / 连通测试冷却 / 走首选上传。
 *
 * 直打目标服务（默认 http://100.126.167.88:8780，可用 S312_BASE 或首个命令行参数覆盖），
 * 零第三方依赖（仅 node 内置 fetch 与 node:process）：
 *   node web/scripts/probe-s3-12-media-host.mjs
 * 有 FAIL → 退出码 1。
 *
 * 断言：
 *  未登录  GET /api/user/media-host            → 401
 *  未登录  POST /api/user/media-host/test      → 401
 *  自注册一次性账号（s312probe + Date.now()），取 Set-Cookie 首 对：
 *  P1 GET sources：4 源，id 顺序 tencent/local/litterbox/uguu，selected=null，每源 id/label/configured/default 四键
 *  P2 PUT selected=tencent → 200 且回读 selected=tencent
 *  P3 PUT selected=nonsense → 400 且 body 有 error
 *  P4 PUT selected=null → 200 且回读 selected=null
 *  P5 POST test id=tencent → 200，ok=true，ms>0，url 以 https://img.boostbite.cn/ 开头
 *  P6 紧接着再发一次 → 429（冷却）
 *  P7 首选 local → POST /api/asset/upload 67 字节 1x1 PNG → 200，source=local，url 以 https://img.aistar.work/vc/ 开头；收尾改回 null
 */
import { Buffer } from "node:buffer";
import process from "node:process";

const BASE = (process.argv[2] || process.env.S312_BASE || "http://100.126.167.88:8780").replace(/\/+$/, "");

let passed = 0;
let failed = 0;
function pass(name) {
    passed += 1;
    console.log(`PASS | ${name}`);
}
function fail(name, actual) {
    failed += 1;
    console.log(`FAIL | ${name} => ${actual}`);
}

/** 网络异常不炸脚本：包一层返回 { res, body, error } */
async function req(method, urlPath, { cookie, body, headers = {} } = {}) {
    try {
        const res = await fetch(`${BASE}${urlPath}`, {
            method,
            headers: {
                connection: "close",
                ...(cookie ? { cookie } : {}),
                ...(body !== undefined ? { "content-type": "application/json" } : {}),
                ...headers,
            },
            body: body === undefined ? undefined : typeof body === "string" ? body : JSON.stringify(body),
            redirect: "manual",
        });
        const text = await res.text();
        let json = null;
        try {
            json = JSON.parse(text);
        } catch {
            /* 非 JSON 响应保留 null */
        }
        return { res, text, json, error: null };
    } catch (error) {
        return { res: null, text: "", json: null, error: String(error && error.message) };
    }
}

function firstCookie(res) {
    const setCookie = res.headers.getSetCookie ? res.headers.getSetCookie() : [res.headers.get("set-cookie")].filter(Boolean);
    const first = setCookie.find((c) => /^[^=]+=[^;]*/.test(c));
    return first ? first.split(";")[0].trim() : null;
}

// ---- 未登录断言 ----
{
    const g = await req("GET", "/api/user/media-host");
    if (g.error) fail("未登录 GET /api/user/media-host 401", g.error);
    else if (g.res.status === 401) pass("未登录 GET /api/user/media-host 401");
    else fail("未登录 GET /api/user/media-host 401", `status=${g.res.status}`);

    const p = await req("POST", "/api/user/media-host/test", { body: { id: "tencent" } });
    if (p.error) fail("未登录 POST /api/user/media-host/test 401", p.error);
    else if (p.res.status === 401) pass("未登录 POST /api/user/media-host/test 401");
    else fail("未登录 POST /api/user/media-host/test 401", `status=${p.res.status}`);
}

// ---- 自注册 ----
const username = `s312probe${Date.now()}`;
const password = "probe-pass-123";
const reg = await req("POST", "/api/auth/register", { body: { username, password } });
if (reg.error) {
    console.log(`FAIL | 自注册 ${username} => 网络异常 ${reg.error}`);
    process.exit(1);
}
const cookie = firstCookie(reg.res);
if (reg.res.status !== 200 || !cookie) {
    console.log(`FAIL | 自注册 ${username} => status=${reg.res.status} body=${reg.text.slice(0, 200)} cookie=${cookie}`);
    process.exit(1);
}
console.log(`PASS | 自注册 ${username}（cookie 已取得）`);
passed += 1;

// ---- P1 清单 ----
{
    const r = await req("GET", "/api/user/media-host", { cookie });
    const j = r.json;
    const sources = j && Array.isArray(j.sources) ? j.sources : null;
    const ids = sources ? sources.map((s) => s.id) : null;
    const expectedIds = ["tencent", "local", "litterbox", "uguu"];
    const keysOk =
        sources &&
        sources.every((s) => ["id", "label", "configured", "default"].every((k) => k in s));
    if (
        !r.error &&
        r.res.status === 200 &&
        sources &&
        sources.length === 4 &&
        JSON.stringify(ids) === JSON.stringify(expectedIds) &&
        j.selected === null &&
        keysOk
    ) {
        pass("P1 GET sources 4源 顺序 tencent/local/litterbox/uguu selected=null 四键齐全");
    } else {
        fail(
            "P1 GET sources 4源 顺序 tencent/local/litterbox/uguu selected=null 四键齐全",
            r.error || `status=${r.res && r.res.status} body=${r.text.slice(0, 300)}`,
        );
    }
}

// ---- P2/P3/P4 首选写 ----
async function putSelected(value) {
    return req("PUT", "/api/user/media-host", { cookie, body: { selected: value } });
}
async function getSelected() {
    const r = await req("GET", "/api/user/media-host", { cookie });
    return r.json ? r.json.selected : null;
}

{
    const r = await putSelected("tencent");
    const back = r.error ? null : await getSelected();
    if (!r.error && r.res.status === 200 && back === "tencent") pass("P2 PUT selected=tencent → 200 回读 tencent");
    else fail("P2 PUT selected=tencent → 200 回读 tencent", r.error || `status=${r.res.status} 回读=${back}`);
}
{
    const r = await putSelected("nonsense");
    if (!r.error && r.res.status === 400 && r.json && "error" in r.json) pass("P3 PUT selected=nonsense → 400 且有 error 字段");
    else fail("P3 PUT selected=nonsense → 400 且有 error 字段", r.error || `status=${r.res.status} body=${r.text.slice(0, 200)}`);
}
{
    const r = await putSelected(null);
    const back = r.error ? "<err>" : await getSelected();
    if (!r.error && r.res.status === 200 && back === null) pass("P4 PUT selected=null → 200 回读 null");
    else fail("P4 PUT selected=null → 200 回读 null", r.error || `status=${r.res.status} 回读=${JSON.stringify(back)}`);
}

// ---- P5/P6 连通测试与冷却 ----
{
    const r = await req("POST", "/api/user/media-host/test", { cookie, body: { id: "tencent" } });
    const j = r.json;
    if (
        !r.error &&
        r.res.status === 200 &&
        j && j.ok === true && typeof j.ms === "number" && j.ms > 0 &&
        typeof j.url === "string" && j.url.startsWith("https://img.boostbite.cn/")
    ) {
        pass(`P5 POST test tencent → 200 ok=true ms=${j.ms} url=${j.url}`);
    } else {
        fail("P5 POST test tencent → 200 ok=true ms>0 url 前缀 boostbite", r.error || `status=${r.res.status} body=${r.text.slice(0, 200)}`);
    }

    const r2 = await req("POST", "/api/user/media-host/test", { cookie, body: { id: "tencent" } });
    if (!r2.error && r2.res.status === 429) pass("P6 紧接着再发一次 test → 429 冷却生效");
    else fail("P6 紧接着再发一次 test → 429 冷却生效", r2.error || `status=${r2.res.status}`);
}

// ---- P7 local 上传 ----
{
    const put = await putSelected("local");
    if (put.error || put.res.status !== 200) {
        fail("P7 local 上传", `前置 PUT local 失败 status=${put.res && put.res.status} ${put.error || ""}`);
    } else {
        const png = Buffer.from(
            "89504e470d0a1a0a0000000d49484452000000010000000108000000003a7e9b550000000a4944415478da6360000000020001e527defc0000000049454e44ae426082",
            "hex",
        );
        let upload = null;
        let text = "";
        let j = null;
        try {
            upload = await fetch(`${BASE}/api/asset/upload`, {
                method: "POST",
                headers: { connection: "close", cookie, "content-type": "application/octet-stream", "x-filename": "probe.png" },
                body: png,
            });
            text = await upload.text();
            j = JSON.parse(text);
        } catch {
            upload = null;
        }
        if (
            upload && upload.status === 200 &&
            j && j.source === "local" && typeof j.url === "string" && j.url.startsWith("https://img.aistar.work/vc/")
        ) {
            pass(`P7 local 上传 1x1 PNG → 200 source=local url=${j.url}`);
        } else {
            fail("P7 local 上传 1x1 PNG → 200 source=local url 前缀 img.aistar.work/vc/", upload ? `status=${upload.status} body=${text.slice(0, 200)}` : "网络/解析异常");
        }
    }
    // 收尾：改回 null
    const reset = await putSelected(null);
    if (reset.error || reset.res.status !== 200) {
        console.log(`WARN | 收尾把首选改回 null 失败 status=${reset.res && reset.res.status} ${reset.error || ""}`);
    }
}

console.log(`\nS3.12 探针：${passed} passed / ${failed} failed`);
process.exit(failed ? 1 : 0);
