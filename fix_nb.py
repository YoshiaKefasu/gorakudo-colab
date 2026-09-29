"""Patch the Colab notebook:
1) replace indented `!` magics (inside if-blocks) with subprocess/os equivalents
2) add a cell that writes the GoRakuDo Main Workflow (Cache-free) into the GUI list
3) re-verify every code cell
"""
import ast
import json

NB = 'GoRakuDo_ComfyUI_Colab.ipynb'
MAIN_WF = 'GoRakuDo_Main_Workflow_cachefree.json'

nb = json.load(open(NB, encoding='utf-8'))


def cell_source(i):
    return ''.join(nb['cells'][i]['source'])


def set_source(i, text):
    nb['cells'][i]['source'] = text.splitlines(keepends=True)


# ---------- 1) indented magics ----------
fixes = {
    2: [
        ("    !git clone --depth 1 --branch {COMFY_TAG} "
         "https://github.com/comfyanonymous/ComfyUI.git",
         "    subprocess.run(['git', 'clone', '--depth', '1', '--branch', COMFY_TAG,\n"
         "                    'https://github.com/comfyanonymous/ComfyUI.git'], check=True)"),
    ],
    6: [
        ("    !wget -q -O /content/cloudflared "
         "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
         "    subprocess.run(['wget', '-q', '-O', '/content/cloudflared',\n"
         "                    'https://github.com/cloudflare/cloudflared/releases/latest/download/"
         "cloudflared-linux-amd64'], check=True)"),
        ("    !chmod +x /content/cloudflared",
         "    os.chmod('/content/cloudflared', 0o755)"),
    ],
}

for idx, pairs in fixes.items():
    src = cell_source(idx)
    for old, new in pairs:
        if old in src:
            src = src.replace(old, new)
        else:
            # tolerate a slightly different wget URL by matching the magic line prefix
            if old.strip().startswith('!wget'):
                import re
                src = re.sub(r'(?m)^\s*!wget .*cloudflared.*$',
                             new.replace('\n', '\n    ').lstrip(), src)
            elif old.strip().startswith('!chmod'):
                import re
                src = re.sub(r'(?m)^\s*!chmod \+x /content/cloudflared\s*$',
                             new.lstrip(), src)
            else:
                print('  !! not found in cell', idx, ':', old[:60])
    # make sure subprocess / os are importable in that cell
    if 'subprocess.run' in src and 'import subprocess' not in src:
        src = src.replace('import os', 'import os, subprocess', 1)
    if "os.chmod(" in src and 'import subprocess' not in src:
        src = src.replace('import os', 'import os, subprocess', 1)
    set_source(idx, src)
    print('cell', idx, 'patched')

# ---------- 2) add the Main Workflow cell ----------
wf = json.load(open(MAIN_WF, encoding='utf-8'))
wf_text = json.dumps(wf, ensure_ascii=True, separators=(',', ':'))
assert "'''" not in wf_text

main_cell = f"""# ===== セル5b: GoRakuDo Main Workflow をGUI一覧に置く（Cacheノード除去済み） =====
# ローカルの「GoRakuDo Main Workflow」をそのまま持ってきたもの。
#   * QwenImage21Cache を2つとも除去して配線し直してある（aimdo memory compile error 回避）
#   * 実行時に MODE に合わせてモデル名を差し替える（bf16 / int8）
import json, os

MAIN_WF = r'''{wf_text}'''

wf = json.loads(MAIN_WF)
UNET_NAME = 'qwen_image_2.1_bf16.safetensors' if MODE == 'bf16' else 'qwen_image_2.1_int8_convrot.safetensors'
for n in wf['nodes']:
    t = n.get('type')
    if t == 'UNETLoader' and n.get('widgets_values'):
        n['widgets_values'][0] = UNET_NAME
    elif t == 'CLIPLoader' and n.get('widgets_values'):
        n['widgets_values'][0] = TE_FILE

paths = [
    '/content/ComfyUI/user/default/workflows/GoRakuDo Main Workflow.json',
    '/content/ComfyUI/user/default/workflows/GoRakuDo - T2I int8+Pruna.json',
]
os.makedirs(os.path.dirname(paths[0]), exist_ok=True)
json.dump(wf, open(paths[0], 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
for p in paths:
    print('wrote:', p)
print()
print('GUIの左サイドバー「ワークフロー」から開けます。')
print('  * モデル名はこのランタイム用に差し替え済み（' + UNET_NAME + '）')
print('  * 触らない設定: cfg=1.0 / ネガティブ空 / 1024 / ManualSigmasのPruna 8step値 / LoRA 1.0')
"""

cell = {'cell_type': 'code', 'metadata': {}, 'execution_count': None,
        'outputs': [], 'source': main_cell.splitlines(keepends=True)}

# insert right after the "write workflows" cell (index 4)
idx = 5
nb['cells'].insert(idx, cell)
print('inserted Main Workflow cell at index', idx)

json.dump(nb, open(NB, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('notebook written')

# ---------- 3) verify ----------
nb2 = json.load(open(NB, encoding='utf-8'))
print('cells:', len(nb2['cells']),
      '| code:', sum(1 for c in nb2['cells'] if c['cell_type'] == 'code'))

bad = []
for i, c in enumerate(nb2['cells']):
    if c['cell_type'] != 'code':
        continue
    src = ''.join(c['source'])
    # strip top-level magics only (indented ones must already be gone)
    stripped = '\n'.join(l for l in src.split('\n') if not l.lstrip().startswith(('%', '!')))
    if any(l.lstrip().startswith('!') for l in src.split('\n')):
        bad.append((i, 'magic still present'))
    try:
        ast.parse(stripped)
    except SyntaxError as e:
        bad.append((i, f'SyntaxError: {e}'))
print('issues:', bad if bad else 'none')

txt = json.dumps(nb2, ensure_ascii=False)
print('QwenImage21Cache as class_type:', txt.count('"class_type\\": \\"QwenImage21Cache'))
print('contains Main Workflow embed:', 'GoRakuDo Main Workflow.json' in txt)