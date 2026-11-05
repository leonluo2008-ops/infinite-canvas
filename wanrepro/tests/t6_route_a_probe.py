"""T6 路 A 离线对抗探针；只写 --out-dir，不改被审文件、不访问模型。
运行：py -3 tests/t6_route_a_probe.py --out-dir <仓外证据目录>
退出 0=全部行为契约通过，1=至少一条验收失败；不是把当前缺陷当成功。
"""
from __future__ import annotations
import argparse
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'pipeline/reverse'), str(ROOT / 'pipeline'), str(ROOT / 'tests')]
import confirm as C
import extract as E
import frame_facts as F
import render as R
import recheck as RC
import lapian_tenth_layer_check as FX


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out-dir', required=True)
    args = ap.parse_args()
    out = Path(args.out_dir).resolve()
    if out == ROOT or ROOT in out.parents:
        ap.error('--out-dir 必须在仓库外，保护真实任务与夹具')
    out.mkdir(parents=True, exist_ok=True)
    checks = []
    data = {}

    def record(name, ok, detail):
        checks.append({'name': name, 'ok': bool(ok), 'detail': detail})

    def task(name, tail):
        d = out / name
        d.mkdir(exist_ok=True)
        (d / '拆解.md').write_text(FX.DISSECTION_TEMPLATE.format(tail_layer=tail), encoding='utf-8')
        (d / '待确认清单.md').write_text(FX.CHECKLIST, encoding='utf-8')
        return d

    def run(name, script, *argv):
        p = subprocess.run([sys.executable, str(ROOT / script), *map(str, argv)], cwd=ROOT,
                           capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90,
                           env={**os.environ, 'PYTHONIOENCODING': 'utf-8', 'TMPDIR': str(out),
                                'TMP': str(out), 'TEMP': str(out)})
        (out / (name + '.log')).write_text(p.stdout + '\nSTDERR:\n' + p.stderr, encoding='utf-8')
        return p.returncode

    # 正文中的 | 是 OCR 原字，不是结构分隔符；部分可辨必须保留不确定性。
    closing = '* **收束**：类型=产品与品牌字同框；版式=产品居中；动效时序=先定格后淡入'
    layer = '\n'.join(['## 十 图文与收束',
        '* **品牌字（出现于：全片）**：「SPARK!」｜粗体｜白色｜顶部｜淡入',
        '* **包装字（出现于：全片）**：「SOURCE-MARK」｜细体｜白色｜右侧｜无',
        '* **字幕（出现于：全片）**：「全场 5 折」｜粗体｜黄色｜底部｜弹出',
        '* **水印（出现于：全片）**：「部分可辨：EAU | DE …」｜细体｜灰色｜右上｜无', closing])
    d = task('canonical', layer)
    blocks = R.parse_blocks(layer.split('\n', 1)[1], '图文与收束')
    data['blocks'] = [{'layer': b.layer, 'label': b.label, 'dim': b.dim, 'lines': b.lines} for b in blocks]
    wm = next(b for b in blocks if '水印' in b.label)
    record('Q1 OCR 文字中的半角竖线不被改写', 'EAU | DE …' in ''.join(wm.lines), wm.lines)
    record('Q1 confirm 十层正常', run('confirm', 'pipeline/reverse/confirm.py', d / '拆解.md') == 0, '')
    data['confirm_rows'] = C.parse_checklist(d / '待确认清单.md')
    data['recheck'] = RC.detect(str(d))
    record('Q1 部分可辨进入人工复检', any('部分可辨' in x.get('original', '') for x in data['recheck']), data['recheck'])
    (d / '待确认清单.md').write_text(FX.CHECKLIST, encoding='utf-8')
    renders = []
    for template, tag in [('lumi-4section', 'C'), ('employee-checklist', 'A'),
                          ('timeline-narrative', 'A'), ('seedance-flow', 'A'), ('seven-section', 'A')]:
        od = out / ('render-' + template)
        code = run('render-' + template, 'pipeline/reverse/render.py', '--id', d, '--template', template,
                   '--optional', tag, '--out-dir', od)
        text = (od / ('提示词-' + template + '.md')).read_text(encoding='utf-8') if code == 0 else ''
        body = text.split('# 附注')[0]
        assets = (od / '资产清单.md').read_text(encoding='utf-8') if code == 0 else ''
        renders.append({'template': template, 'exit': code, 'body': body, 'assets': assets})
        record('Q5 ' + template + ' 显式开启可渲染', code == 0, code)
        record('Q1 ' + template + ' 字幕逐条进入正文', '字幕（出现于' in body, body)
        record('Q4 ' + template + ' 包装原字不外泄', 'SOURCE-MARK' not in body, body)
        record('Q5 ' + template + ' 品牌视觉置换位进入资产表', '品牌视觉' in assets, assets)
    data['renders'] = renders
    mixed = '收束：版式=字幕「全场 5 折」在下、品牌「SPARK!」在上'
    replaced = R._replace_brand_glyphs(mixed)
    record('Q4 非品牌字幕不被置换为品牌', '全场 5 折' in replaced, replaced)
    placeholder = '品牌字：「客户品牌字样」'
    record('Q4 占位字面幂等', R._replace_brand_glyphs(placeholder) == placeholder, R._replace_brand_glyphs(placeholder))
    long = '品牌字：「' + 'A' * 61 + '」'
    record('Q4 长字串不泄露原字', 'A' * 61 not in R._replace_brand_glyphs(long), R._replace_brand_glyphs(long))
    sec = R.Section(1, '段', '声音', False)
    sec.fields = {'写什么': '声音与台词', '数据来源': '十层 八（声音）', '字样置换': '是'}
    text = R.render_prompt({'name': 'audit-scope'}, [sec], {'八': '* 台词：「全场 5 折」'}, [], {}, 'scope', 'scope')
    record('Q4 非收束声音层未被替换', '全场 5 折' in text, text)

    old = task('nine', '')
    code = run('nine-lapian', 'pipeline/reverse/render_lapian.py', '--id', old, '--spec', 'jimeng-lapian')
    lp = (old / '拉片-jimeng-lapian.md').read_text(encoding='utf-8') if code == 0 else ''
    record('Q3 旧九层投影明确未取证', code == 0 and '片尾收束：〔未取证〕' in lp, lp)
    record('Q3 旧九层默认模板继续渲染', run('nine-render', 'pipeline/reverse/render.py', '--id', old, '--template',
           'lumi-4section', '--out-dir', out / 'nine-render') == 0, '')
    record('Q3 旧九层误开收束明确拒绝', run('nine-optional', 'pipeline/reverse/render.py', '--id', old, '--template',
           'lumi-4section', '--optional', 'C', '--out-dir', out / 'nine-optional') == 4, '')
    record('Q3 旧九层复检不炸', isinstance(RC.detect(str(old)), list), RC.detect(str(old)))

    # 真 ffmpeg 解码；模型判定是明确本地夹具，绝不宣称识别了测试图案。
    windows_data = []
    for duration in [0.04, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 6.0]:
        fd = out / ('frames-' + str(duration)); fd.mkdir(exist_ok=True)
        video = fd / 'source.mp4'
        # 每次复跑本探针都做冷启动，再测第二次缓存复用。
        facts_file = fd / F.FACTS_NAME
        if facts_file.exists():
            facts_file.unlink()
        p = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                            'testsrc2=size=64x96:rate=100', '-t', str(duration), '-c:v', 'libx264',
                            '-pix_fmt', 'yuv420p', '-y', str(video)], capture_output=True, text=True, encoding='utf-8')
        if p.returncode:
            raise RuntimeError(p.stderr)
        windows, _note = E.frames_of('', fd, {'duration': duration})
        calls = []
        def judge(_prompt, path):
            calls.append(float(path.stem))
            return json.dumps(FX.TAIL_JUDGMENT, ensure_ascii=False), {}, 0.0
        with contextlib.redirect_stdout(io.StringIO()):
            facts, first = F.ensure_facts(fd, video, {'duration': duration, 'width': 64, 'height': 96}, windows, judge)
            first_calls = list(calls); calls.clear()
            _facts2, second = F.ensure_facts(fd, video, {'duration': duration, 'width': 64, 'height': 96}, windows, judge)
        item = {'duration': duration, 'windows': windows, 'first': first, 'second': second,
                'calls_first': first_calls, 'calls_second': list(calls)}
        windows_data.append(item)
        record('Q2 ' + str(duration) + 's 真抽帧不失败', first['ok'], item)
        record('Q2 ' + str(duration) + 's 不重复判同一秒', len(first_calls) == len(set(first_calls)), item)
        record('Q2 ' + str(duration) + 's 二次运行零调用', second['calls'] == 0, item)
    data['windows'] = windows_data
    fd = out / 'frames-6.0'
    with patch.object(F, 'ensure_facts', side_effect=AssertionError('off sampled')):
        facts, warning = E.ensure_frame_facts(fd / 'source.mp4', fd, {'duration': 6}, 'AUDIT-DUMMY', 1, None)
    record('Q2 off 不采样并可读旧事实', facts is not None and not warning, warning)
    record('Q2 inject off 禁尾锚点', '## 尾帧判定' not in E.build_prompt(E.read_prompt(), '', {'duration': 6}, facts, inject='off'), '')
    record('Q2 filler 禁尾锚点', '## 尾帧判定' not in E.build_prompt(E.read_prompt(), '', {'duration': 6}, facts, inject='filler'), '')
    legacy = copy.deepcopy(FX.TAIL_JUDGMENT); legacy.pop('text_elements')
    parsed = F.parse_judgment(json.dumps(legacy, ensure_ascii=False))
    agg = F.finalize(F.aggregate([(5.95, parsed)]))
    payload = {'窗口': [{'窗口': '4.5:6', '起': 4.5, '止': 6, '帧': [{'秒': 5.95, '判定': parsed}], '聚合': agg}]}
    injection = E.build_tail_brand_block(payload, 6)
    record('Q3 漏答不伪造无字事实', '没有持久性文字/图形标识' not in injection, injection)
    data['missing_text'] = {'raw': legacy, 'parsed': parsed, 'aggregate': agg, 'injection': injection}

    # 直接调用现行 handler，真子进程渲染；非浏览器验收，元数据辅助函数均明确隔离。
    sp = importlib.util.spec_from_file_location('t6_audit_ui', ROOT / 'ui/server.py')
    ui = importlib.util.module_from_spec(sp)
    with patch.dict(os.environ, {'WAN_TOKEN': 'AUDIT-DUMMY-NOT-A-SECRET'}):
        sp.loader.exec_module(ui)
    captured, responses = [], []
    def runner(argv, timeout):
        captured.append(list(argv)); argv = list(argv); argv[argv.index('--id') + 1] = str(d)
        p = subprocess.run([sys.executable, *argv], cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    def latest(_):
        p = d / '提示词-lumi-4section.md'
        return p.read_text(encoding='utf-8') if p.exists() else '', 'lumi-4section'
    class Request:
        def read_json(self):
            return {'template': 'lumi-4section', 'optional': 'C'}
        def send_json(self, value):
            responses.append(value)
    with patch.object(ui, 'task_dir', lambda _: d), patch.object(ui, 'run_py', runner), \
         patch.object(ui, 'latest_prompt', latest), patch.object(ui, 'build_assets', lambda _: []), \
         patch.object(ui, 'dialogue_map', lambda _: {}), patch.object(ui, 'intent_receipt', lambda _: {}):
        ui.Handler.do_render(Request(), 'canonical')
    body = responses[0]['prompt']['text'].split('# 附注')[0]
    record('Q5 UI 显式开启传到渲染器且正文生效', '--optional' in captured[0] and '## 品牌收束与图文' in body,
           {'argv': captured, 'body': body})
    data['checks'] = checks
    passed = sum(1 for x in checks if x['ok'])
    failed = len(checks) - passed
    data['summary'] = {'total': len(checks), 'passed': passed, 'failed': failed}
    (out / 'evidence.json').write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for item in checks:
        print(('PASS ' if item['ok'] else 'FAIL ') + item['name'])
    print(json.dumps(data['summary'], ensure_ascii=False))
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    with patch.object(socket, 'create_connection', side_effect=RuntimeError('AUDIT_NETWORK_DISABLED')):
        raise SystemExit(main())
