"""Full verification of the Colab notebook + helpers (offline checks + URL reachability).

Run:  python verify_full.py
Exit code 0 = all good.
"""
import ast
import json
import re
import subprocess
import sys
import urllib.request

NB = 'GoRakuDo_ComfyUI_Colab.ipynb'
MAIN = 'GoRakuDo_Main_Workflow_cachefree.json'

fails, warns, notes = [], [], []


def fail(m):
    fails.append(m)


def warn(m):
    warns.append(m)


def ok(m):
    notes.append('OK  ' + m)


# ---------------------------------------------------------------- notebook
try:
    nb = json.load(open(NB, encoding='utf-8'))
    ok(f'notebook parses as JSON: {len(nb["cells"])} cells '
       f'({sum(1 for c in nb["cells"] if c["cell_type"] == "code")} code)')
except Exception as e:
    fail(f'notebook JSON: {e}')
    sys.exit(1)

code_cells = [(i, c) for i, c in enumerate(nb['cells']) if c['cell_type'] == 'code']

# 1) syntax + magics placement
for i, c in code_cells:
    src = ''.join(c['source'])
    for n, line in enumerate(src.split('\n')):
        stripped = line.lstrip()
        if stripped.startswith(('!', '%')):
            indent = len(line) - len(stripped)
            if indent:
                fail(f'cell {i} line {n}: INDENTED magic "{stripped[:40]}" (breaks in if-blocks)')
    code = '\n'.join(l for l in src.split('\n') if not l.lstrip().startswith(('%', '!')))
    try:
        ast.parse(code)
    except SyntaxError as e:
        fail(f'cell {i}: SyntaxError: {e}')
ok('all code cells parse (top-level magics stripped)')

# 2) escaping smells
for i, c in code_cells:
    src = ''.join(c['source'])
    for bad, why in [
        (r'([\\d.]+)', 'double-escaped regex class'),
        (r'\\.trycloudflare', 'double-escaped dot'),
        (r"print('\\n", 'double-escaped newline in string'),
    ]:
        if bad in src:
            fail(f'cell {i}: {why} -> {bad}')

# 3) required features present
whole = json.dumps(nb, ensure_ascii=False)
expect = {
    'stdout fix': 'text=True).stdout.strip()',
    'gpu name parse': 'rpartition',
    'bf16 unet url': 'qwen_image_2.1_bf16.safetensors',
    'int8 unet url': 'qwen_image_2.1_int8_convrot.safetensors',
    'bf16 TE url': 'qwen3vl_8b_bf16.safetensors',
    'int8 TE url': 'qwen3vl_8b_int8_convrot.safetensors',
    'vae': 'qwen_image_2.1_vae_bf16.safetensors',
    'pruna lora': 'p_qwen_image_2.1_8step_v0.1.safetensors',
    'cloudflared': 'cloudflared',
    'tailscale default': "TUNNEL_PROVIDER = 'tailscale'",
    'tailscale authkey': 'TAILSCALE_AUTHKEY',
    'tailscale hostname': "TAILSCALE_HOSTNAME = 'colab-g4'",
    'tailscale serve': "'tailscale', 'serve'",
    'tailscale ip': "'tailscale', 'ip', '-4'",
    'tailscale tun': '--tun=userspace-networking',
    'stay alive': 'STAY_ALIVE = True',
    'api prompt endpoint': '/prompt',
    'api history endpoint': '/history',
    'view endpoint': '/view',
    'files.download': 'files.download',
    'main workflow embed': 'GoRakuDo Main Workflow.json',
}
for label, needle in expect.items():
    if needle in whole:
        ok(f'contains {label}')
    else:
        fail(f'MISSING {label} ({needle})')

if '"class_type\\": \\"QwenImage21Cache' in whole or '"class_type": "QwenImage21Cache' in whole:
    fail('embedded workflow still contains QwenImage21Cache')
else:
    ok('no QwenImage21Cache in embedded workflows')

# 4) embedded workflows: valid JSON + link integrity
#    + UI形式は必須入力の未接続も検出 / API形式6本はノード参照の整合を展開検証
UI_REQUIRED = {
    'SamplerCustom': ['model', 'positive', 'negative', 'latent_image', 'sampler', 'sigmas'],
    'KSamplerAdvanced': ['model', 'positive', 'negative', 'latent_image'],
}
API_REQUIRED = {
    'SamplerCustom': ['model', 'positive', 'negative', 'latent_image', 'sampler', 'sigmas'],
    'KSamplerAdvanced': ['model', 'positive', 'negative', 'latent_image'],
}


def _check_ui_consistency(wf, label):
    # Note: outputs[].links <-> links[] <-> inputs[].link の三者整合＋slot重複＋last_link_id
    nodes = {n['id']: n for n in wf['nodes']}
    link_by_id = {l[0]: l for l in wf.get('links', [])}
    in_count, out_count = {}, {}
    for n in wf['nodes']:
        seen_in, seen_out = set(), set()
        for inp in n.get('inputs', []):
            lid = inp.get('link')
            if lid is None:
                continue
            in_count[lid] = in_count.get(lid, 0) + 1
            if lid in seen_in:
                fail(f'{label}: node#{n["id"]} input slot duplication (link {lid})')
        for oi, o in enumerate(n.get('outputs', [])):
            for lid in (o.get('links') or []):
                out_count[lid] = out_count.get(lid, 0) + 1
                if lid in seen_out:
                    fail(f'{label}: node#{n["id"]} output slot duplication (link {lid})')
                seen_out.add(lid)
        for inp in n.get('inputs', []):
            if inp.get('link') is not None:
                seen_in.add(inp['link'])
    ok(f'{label}: no input/output slot duplication')
    for lid in set(in_count) | set(out_count):
        if in_count.get(lid, 0) != 1 or out_count.get(lid, 0) != 1:
            fail(f'{label}: link {lid} referenced in={in_count.get(lid, 0)} out={out_count.get(lid, 0)} (want 1/1)')
    if set(in_count) | set(out_count) == set(link_by_id):
        ok(f'{label}: links[] <-> node slots two-way match')
    else:
        fail(f'{label}: links[] vs slots mismatch '
             f'(orphan={sorted(set(link_by_id) - set(in_count) - set(out_count))} '
             f'ref-missing={sorted((set(in_count) | set(out_count)) - set(link_by_id))})')
    for lid, l in link_by_id.items():
        _, sn, so, dn, di = l[0], l[1], l[2], l[3], l[4]
        try:
            if lid not in (nodes[sn]['outputs'][so].get('links') or []):
                fail(f'{label}: link {lid} not in outputs[{so}].links of node#{sn}')
            if nodes[dn]['inputs'][di].get('link') != lid:
                fail(f'{label}: link {lid} != inputs[{di}].link of node#{dn}')
        except (KeyError, IndexError):
            fail(f'{label}: link {lid} endpoint out of range ({sn}:{so} -> {dn}:{di})')
    else:
        ok(f'{label}: all link endpoints resolve to node slots')
    if wf.get('links'):
        if wf.get('last_link_id') == max(l[0] for l in wf['links']):
            ok(f'{label}: last_link_id == max link id ({wf.get("last_link_id")})')
        else:
            fail(f'{label}: last_link_id={wf.get("last_link_id")} != max link id '
                 f'{max(l[0] for l in wf["links"])}')

embedded_api_count = 0


def _iter_embedded(src):
    # Note: セル4は json.loads('...') 単一引用符形式、セル4bは r'''...''' 形式。両方拾う
    yield from (m.group(1) for m in re.finditer(r"json\.loads\('(\{.*?\})'\)", src, re.S))
    yield from (m.group(1) for m in re.finditer(r"r'''(.+?)'''", src, re.S))


for i, c in code_cells:
    src = ''.join(c['source'])
    for blob in _iter_embedded(src):
        try:
            wf = json.loads(blob)
        except Exception as e:
            fail(f'cell {i}: embedded JSON unparsable: {e}')
            continue
        if 'nodes' in wf:  # UI format
            ids = {n['id'] for n in wf['nodes']}
            bad = [l for l in wf.get('links', []) if l[1] not in ids or l[3] not in ids]
            if bad:
                fail(f'cell {i}: UI workflow has {len(bad)} dangling links')
            else:
                ok(f'cell {i}: UI workflow {len(wf["nodes"])} nodes / {len(wf.get("links", []))} links, links intact')
            _check_ui_consistency(wf, f'cell {i} UI')
            # 必須入力の未接続を inputs[].link から検出
            for n in wf['nodes']:
                req = UI_REQUIRED.get(n.get('type'))
                if not req:
                    continue
                links = {inp.get('name'): inp.get('link') for inp in n.get('inputs', [])}
                missing = [k for k in req if links.get(k) is None]
                if missing:
                    fail(f'cell {i}: UI {n.get("type")}#{n.get("id")} missing inputs: {missing}')
                else:
                    ok(f'cell {i}: UI {n.get("type")}#{n.get("id")} required inputs connected')
        else:  # API format
            embedded_api_count += 1
            node_ids = set(wf.keys())
            bad = [k for k, v in wf.items()
                   for val in v.get('inputs', {}).values()
                   if isinstance(val, list) and len(val) == 2 and str(val[0]) not in node_ids]
            if bad:
                fail(f'cell {i}: API workflow references missing nodes: {bad}')
            else:
                ok(f'cell {i}: API workflow {len(wf)} nodes, references intact')
            for k, v in wf.items():
                req = API_REQUIRED.get(v.get('class_type'))
                if not req:
                    continue
                missing = [r for r in req if r not in v.get('inputs', {})]
                if missing:
                    fail(f'cell {i}: API {v.get("class_type")} node {k} missing inputs: {missing}')
                else:
                    ok(f'cell {i}: API {v.get("class_type")} node {k} required inputs present')

if embedded_api_count >= 6:
    ok(f'embedded API workflows found: {embedded_api_count} (>= 6)')
else:
    fail(f'embedded API workflows found: {embedded_api_count} (expected >= 6)')

# 4b) ipynbセル4bの埋め込み == cachefree JSON
try:
    _mw_file = json.load(open(MAIN, encoding='utf-8'))
    _found = False
    for i, c in code_cells:
        for m in re.finditer(r'r\'\'\'(\{.*?\})\'\'\'', ''.join(c['source']), re.S):
            try:
                _emb = json.loads(m.group(1))
            except Exception:
                continue
            if isinstance(_emb, dict) and 'nodes' in _emb and _emb.get('last_link_id') == _mw_file.get('last_link_id'):
                _found = True
                if _emb == _mw_file:
                    ok(f'cell {i}: MAIN_WF embed == {MAIN} (json.loads compare True)')
                else:
                    fail(f'cell {i}: MAIN_WF embed != {MAIN}')
    if not _found:
        fail('MAIN_WF embed not found in notebook')
except Exception as e:
    fail(f'embed==file compare: {e}')

# 4c) セル6/7のエラー検出＋タイムアウト / セル3の.part+os.replace / セル6.5の存在
_cell_titles = {i: ''.join(c['source']).splitlines()[0] for i, c in code_cells}
_idx6 = next((i for i, t in _cell_titles.items() if 'セル6:' in t and '6.5' not in t), None)
_idx7 = next((i for i, t in _cell_titles.items() if 'セル7' in t), None)
_idx65 = next((i for i, t in _cell_titles.items() if '6.5' in t), None)
_idx3 = next((i for i, t in _cell_titles.items() if 'セル3' in t), None)
_code_by_idx = {i: ''.join(c['source']) for i, c in code_cells}
for _label, _idx in [('cell6', _idx6), ('cell7', _idx7)]:
    if _idx is None:
        fail(f'{_label} not found')
        continue
    _s = _code_by_idx[_idx]
    for _nd in ('status_str', '1800', 'TimeoutError', 'urllib.error.HTTPError'):
        if _nd in _s:
            ok(f'{_label}: contains {_nd}')
        else:
            fail(f'{_label} MISSING {_nd}')
    # エラー判定が completed の外にあること（死にコード化の回帰検出）
    if "status_str') == 'error' or st.get('completed')" in _s:
        ok(f'{_label}: error check outside completed')
    else:
        fail(f'{_label}: error check still inside completed (dead code)')
    if '生成成功だが出力が見つかりません' in _s:
        ok(f'{_label}: empty-output message present')
    else:
        fail(f'{_label} MISSING empty-output message')
# セル5: pkillパターン＋cloudflared始末
_idx5 = next((i for i, t in _cell_titles.items() if 'セル5' in t), None)
if _idx5 is None:
    fail('cell5 not found')
else:
    _s5 = _code_by_idx[_idx5]
    for _nd in ("main.py --listen", 'cloudflared tunnel'):
        if _nd in _s5:
            ok(f'cell5: contains {_nd}')
        else:
            fail(f'cell5 MISSING {_nd}')
    for _nd in ('tailscale', 'TAILSCALE_AUTHKEY', 'STAY_ALIVE = True',
                'login.tailscale.com/admin/settings/keys',
                '--tun=userspace-networking', "'tailscale', 'ip', '-4'",
                'Tailscale P2P'):
        if _nd in _s5:
            ok(f'cell5: contains {_nd}')
        else:
            fail(f'cell5 MISSING {_nd}')
    if "TUNNEL_PROVIDER = 'tailscale'" in _s5:
        ok('cell5: default provider is tailscale')
    else:
        fail("cell5: default TUNNEL_PROVIDER is not 'tailscale'")
    if 'ComfyUI/main.py' in _s5:
        fail('cell5: stale pkill pattern ComfyUI/main.py still present')
    else:
        ok('cell5: stale pkill pattern removed')
# セル1: ValueError捕捉／セル3: 死にHF行の削除＋サイズ再照合
_idx1 = next((i for i, t in _cell_titles.items() if 'セル1' in t), None)
if _idx1 is not None and 'ValueError' in _code_by_idx[_idx1]:
    ok('cell1: contains ValueError')
else:
    fail('cell1 MISSING ValueError')
if 'hf_transfer' in json.dumps(nb, ensure_ascii=False):
    fail('dead hf_transfer reference still present')
else:
    ok('dead hf_transfer reference removed')
if _idx65 is None:
    fail('cell6.5 (参照画像アップロード) not found')
else:
    _s65 = _code_by_idx[_idx65]
    for _nd in ('files.upload()', 'REPLACE_ME_REF', 'REPLACE_ME_BASE', '/content/ComfyUI/input/'):
        if _nd in _s65:
            ok(f'cell6.5: contains {_nd}')
        else:
            fail(f'cell6.5 MISSING {_nd}')
if _idx3 is None:
    fail('cell3 not found')
else:
    _s3 = _code_by_idx[_idx3]
    for _nd in ('.part', 'os.replace', 'size match'):
        if _nd in _s3:
            ok(f'cell3: contains {_nd}')
        else:
            fail(f'cell3 MISSING {_nd}')

# ---------------------------------------------------------------- main workflow file
try:
    mw = json.load(open(MAIN, encoding='utf-8'))
    ids = {n['id'] for n in mw['nodes']}
    bad = [l for l in mw.get('links', []) if l[1] not in ids or l[3] not in ids]
    caches = [n['id'] for n in mw['nodes'] if n.get('type') == 'QwenImage21Cache']
    if bad or caches:
        fail(f'{MAIN}: dangling={len(bad)} cache={caches}')
    else:
        ok(f'{MAIN}: {len(mw["nodes"])} nodes, {len(mw["links"])} links, 0 cache, links intact')
    _check_ui_consistency(mw, MAIN)
except Exception as e:
    fail(f'{MAIN}: {e}')

# ---------------------------------------------------------------- G4 parse simulation
real = 'NVIDIA RTX PRO 6000 Blackwell Server Edition, 97887'
name, _, mb = real.rpartition(',')
try:
    vram_gb = float(mb.strip()) / 1024
    assert name.strip() == 'NVIDIA RTX PRO 6000 Blackwell Server Edition'
    assert abs(vram_gb - 95.6) < 0.5
    ok(f'G4 nvidia-smi line parses -> {name.strip()!r} / {vram_gb:.1f} GB (MODE would be bf16)')
except Exception as e:
    fail(f'G4 parse simulation: {e}')

for line, want_mode in [
    ('NVIDIA A100-SXM4-40GB, 40960', 'int8'),
    ('Tesla T4, 15360', 'int8'),
    ('NVIDIA L4, 23034', 'int8'),
]:
    _, _, mb = line.rpartition(',')
    v = float(mb.strip()) / 1024
    got = 'bf16' if v >= 48 else 'int8'
    if got == want_mode:
        ok(f'{line.split(",")[0]} -> {v:.1f} GB -> {got}')
    else:
        fail(f'{line}: expected {want_mode}, got {got}')

# ---------------------------------------------------------------- URL reachability
URLS = [
    'https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/diffusion_models/qwen_image_2.1_bf16.safetensors',
    'https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/diffusion_models/qwen_image_2.1_int8_convrot.safetensors',
    'https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/text_encoders/qwen3vl_8b_bf16.safetensors',
    'https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/text_encoders/qwen3vl_8b_int8_convrot.safetensors',
    'https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/vae/qwen_image_2.1_vae_bf16.safetensors',
    'https://huggingface.co/PrunaAI/Pruna-Qwen-Image-2.1/resolve/main/p_qwen_image_2.1_8step_v0.1.safetensors',
]
for u in URLS:
    try:
        req = urllib.request.Request(u, method='HEAD')
        with urllib.request.urlopen(req, timeout=30) as r:
            size = int(r.headers.get('Content-Length', 0))
        ok(f'URL {u.split("/")[-1]} -> {r.status} ({size/1e9:.2f} GB)')
    except Exception as e:
        fail(f'URL unreachable: {u} ({e})')

# ---------------------------------------------------------------- report
print('=' * 70)
print(f'PASS {len(notes)}   WARN {len(warns)}   FAIL {len(fails)}')
print('=' * 70)
for m in notes:
    print(' ', m)
for m in warns:
    print(' WARN', m)
for m in fails:
    print(' FAIL', m)
sys.exit(1 if fails else 0)