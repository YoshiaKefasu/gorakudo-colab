"""Build GoRakuDo_ComfyUI_Colab.ipynb (G4/bf16 main line)."""
import json
import copy

WDIR = 'D:/OSS_ProgramFiles/ComfyUI/workflows'
OUT = 'D:/OSS_ProgramFiles/ComfyUI/tools/colab/GoRakuDo_ComfyUI_Colab.ipynb'

BF16_UNET = 'qwen_image_2.1_bf16.safetensors'
BF16_TE = 'qwen3vl_8b_bf16.safetensors'

t2i = json.load(open(WDIR + '/t2i_pruna_int8_api.json', encoding='utf-8'))
edit = json.load(open(WDIR + '/edit_pruna_int8_api.json', encoding='utf-8'))
inp = json.load(open(WDIR + '/inpaint_int8_api.json', encoding='utf-8'))


def strip_cache_edit(wf):
    w = copy.deepcopy(wf)
    assert w['12']['class_type'] == 'QwenImage21Cache', 'edit cache node id changed!'
    del w['12']
    w['15']['inputs']['model'] = ['11', 0]  # Cache -> LoRA直結
    return w


def strip_cache_inpaint(wf):
    w = copy.deepcopy(wf)
    assert w['11']['class_type'] == 'QwenImage21Cache', 'inpaint cache node id changed!'
    del w['11']
    w['6']['inputs']['model'] = ['1', 0]  # Cache -> UNET直結
    return w


def to_bf16(wf, prefix):
    w = copy.deepcopy(wf)
    w['1']['inputs']['unet_name'] = BF16_UNET
    w['2']['inputs']['clip_name'] = BF16_TE
    w['8']['inputs']['filename_prefix'] = prefix
    return w


WF = {
    't2i_pruna_int8_api.json': t2i,
    'edit_pruna_int8_api.json': strip_cache_edit(edit),
    'inpaint_int8_api.json': strip_cache_inpaint(inp),
}
WF['t2i_bf16_api.json'] = to_bf16(t2i, 'gorakudo/t2i_bf16_pruna_s8')
WF['edit_bf16_api.json'] = to_bf16(WF['edit_pruna_int8_api.json'], 'gorakudo/edit_bf16')
WF['inpaint_bf16_api.json'] = to_bf16(WF['inpaint_int8_api.json'], 'gorakudo/inpaint_bf16')

for name, w in WF.items():
    assert 'QwenImage21Cache' not in json.dumps(w), 'Cache残存: ' + name
    print(name + ': ' + str(len(w)) + ' nodes, Cache除去OK')


def md(src):
    return {'cell_type': 'markdown', 'metadata': {}, 'source': src.splitlines(True)}


def code(src):
    return {'cell_type': 'code', 'metadata': {}, 'execution_count': None,
            'outputs': [], 'source': src.splitlines(True)}


TITLE = (
    '# GoRakuDo ComfyUI on Colab（G4 / bf16 本線）\n'
    'Qwen-Image-2.1 を Google Colab 上の ComfyUI で回すノートブック。'
    '**主経路=G4（RTX PRO 6000 Blackwell 96GB）+ bf16フル精度 + Pruna 8step**。\n'
    'T4/L4/A100 では int8 に自動フォールバックする。\n'
    '上から順に実行するだけ。詳細は `README-colab.md`。\n'
    '> ※ Colab実機での実行は未検証（JSON構造・構文・URL到達性のみ検証済み）。\n'
)

C1 = (
    '# ===== セル1: GPU確認 + モード自動選択 =====\n'
    'import subprocess, re\n'
    'def detect_gpu():\n'
    "    out = subprocess.run(['nvidia-smi', '--query-gpu=name,memory.total',\n"
    "                         '--format=csv,noheader,nounits'],\n"
    '                         capture_output=True, text=True).text.strip()\n'
    "    print('nvidia-smi:', out)\n"
    '    line = out.splitlines()[0]\n'
    "    m = re.search(r'([\\\\d.]+)\\\\s*$', line)\n"
    '    vram_gb = float(m.group(1)) / 1024\n'
    '    name = line[:m.start()].strip().rstrip(",")\n'
    '    return name, vram_gb\n'
    '\n'
    'GPU_NAME, VRAM_GB = detect_gpu()\n'
    "print(f'GPU: {GPU_NAME} / VRAM: {VRAM_GB:.1f} GB')\n"
    '\n'
    "MODE_OVERRIDE = ''  # 強制したい時だけ 'bf16' / 'int8' を入れる\n"
    "if MODE_OVERRIDE in ('bf16', 'int8'):\n"
    '    MODE = MODE_OVERRIDE\n'
    'elif VRAM_GB >= 48:   # G4 96GB / A100 80GB -> bf16フル精度\n'
    "    MODE = 'bf16'\n"
    'else:                 # T4/L4/A100-40GB等 -> int8\n'
    "    MODE = 'int8'\n"
    '# TEはbf16モード&48GB以上でのみbf16版、それ以外はint8版にフォールバック\n'
    "TE_FILE = 'qwen3vl_8b_bf16.safetensors' if (MODE == 'bf16' and VRAM_GB >= 48) else 'qwen3vl_8b_int8_convrot.safetensors'\n"
    "COMFY_FLAGS = '--lowvram' if VRAM_GB < 20 else ''\n"
    'print(f\'MODE={MODE} / TE={TE_FILE} / COMFY_FLAGS="{COMFY_FLAGS}"\')\n'
)

C2 = (
    '# ===== セル2: ComfyUI インストール（安定版タグ） =====\n'
    'import os\n'
    "COMFY_TAG = 'v0.37.4'  # 2026-09 時点の安定版。最新に追う場合はタグを更新\n"
    '%cd /content\n'
    "if not os.path.isdir('/content/ComfyUI'):\n"
    '    !git clone --depth 1 --branch {COMFY_TAG} https://github.com/comfyanonymous/ComfyUI.git\n'
    'else:\n'
    "    print('ComfyUI は取得済み（スキップ）')\n"
    '%cd /content/ComfyUI\n'
    '!pip install -q -r requirements.txt\n'
    "print('install done')\n"
)

C2B = (
    '# ===== セル2b（任意）: ComfyUI-GGUF（Q8 GGUFを使う場合のみ実行） =====\n'
    '# bf16/int8 の safetensors 経路では不要。このセルは飛ばしてよい。\n'
    '%cd /content/ComfyUI/custom_nodes\n'
    '!git clone --depth 1 https://github.com/city96/ComfyUI-GGUF.git\n'
    "print('GGUF node done（ComfyUI再起動後に有効）')\n"
)

C3 = (
    '# ===== セル3: モデルDL（直列・レジューム・スキップ付き、所要時間表示） =====\n'
    'import os, time, subprocess\n'
    '%cd /content/ComfyUI\n'
    'try:\n'
    '    MODE, TE_FILE\n'
    'except NameError:\n'
    "    MODE, TE_FILE = 'bf16', 'qwen3vl_8b_bf16.safetensors'  # セル1未実行時の既定（G4想定）\n"
    "    print('セル1未実行のため既定 MODE=bf16 を使用')\n"
    '\n'
    "os.environ['HF_HUB_ENABLE_HF_TRANSFER'] = '1'\n"
    '!pip install -q hf_transfer  # 高速DL。失敗しても下のcurl/wgetが動く\n'
    '\n'
    "BASE = 'https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main'\n"
    "UNET = 'qwen_image_2.1_bf16.safetensors' if MODE == 'bf16' else 'qwen_image_2.1_int8_convrot.safetensors'\n"
    'FILES = [\n'
    "    (f'{BASE}/diffusion_models/{UNET}', f'models/diffusion_models/{UNET}'),\n"
    "    (f'{BASE}/text_encoders/{TE_FILE}', f'models/text_encoders/{TE_FILE}'),\n"
    "    (f'{BASE}/vae/qwen_image_2.1_vae_bf16.safetensors', 'models/vae/qwen_image_2.1_vae_bf16.safetensors'),\n"
    "    ('https://huggingface.co/PrunaAI/Pruna-Qwen-Image-2.1/resolve/main/p_qwen_image_2.1_8step_v0.1.safetensors',\n"
    "     'models/loras/p_qwen_image_2.1_8step_v0.1.safetensors'),\n"
    ']\n'
    't0 = time.time()\n'
    'for url, dst in FILES:\n'
    '    os.makedirs(os.path.dirname(dst), exist_ok=True)\n'
    '    if os.path.exists(dst) and os.path.getsize(dst) > 0:\n'
    "        print(f'skip (exists): {dst} ({os.path.getsize(dst)/1e9:.2f} GB)')\n"
    '        continue\n'
    "    print(f'dl: {url}')\n"
    '    s = time.time()\n'
    "    r = subprocess.run(['curl', '-L', '--continue-at', '-', '-o', dst, url])\n"
    '    if r.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) == 0:\n'
    "        print('  curl失敗 -> wget -c でフォールバック')\n"
    "        subprocess.run(['wget', '-c', '-O', dst, url], check=True)\n"
    "    print(f'  done {os.path.getsize(dst)/1e9:.2f} GB / {time.time()-s:.0f}s')\n"
    "print(f'モデルDL合計: {time.time()-t0:.0f}s')\n"
)

wf_lines = []
for _name, _w in WF.items():
    wf_lines.append('    ' + repr(_name) + ': json.loads(' + repr(json.dumps(_w, ensure_ascii=False)) + '),')
WF_EMBED = '\n'.join(wf_lines)

C4 = (
    '# ===== セル4: ワークフロー書き出し（埋め込みJSON -> workflows/） =====\n'
    'import json, os\n'
    'try:\n'
    '    TE_FILE\n'
    'except NameError:\n'
    "    TE_FILE = 'qwen3vl_8b_bf16.safetensors'  # セル1未実行時の既定（G4想定）\n"
    '%cd /content/ComfyUI\n'
    '# Note: edit/inpaint系は QwenImage21Cache ノード除去済み（aimdo memory compile error回避）。\n'
    '# bf16系は UNETLoader=bf16 + CLIPLoader=bf16-TE。TEがint8フォールバック時は下で差し替える。\n'
    'WF_EMBED = {\n'
    + WF_EMBED + '\n'
    '}\n'
    "os.makedirs('workflows', exist_ok=True)\n"
    'for name, wf in WF_EMBED.items():\n'
    "    if 'bf16' in name and TE_FILE == 'qwen3vl_8b_int8_convrot.safetensors':\n"
    "        wf['2']['inputs']['clip_name'] = TE_FILE  # TEフォールバック差し替え\n"
    "    open(f'workflows/{name}', 'w').write(json.dumps(wf, indent=1))\n"
    "    print('wrote workflows/' + name)\n"
)

C5 = (
    '# ===== セル5: ComfyUI起動 + cloudflared公開URL発行 =====\n'
    'import subprocess, time, re, os, urllib.request\n'
    'try:\n'
    '    COMFY_FLAGS\n'
    'except NameError:\n'
    "    COMFY_FLAGS = ''  # セル1未実行時の既定（G4想定: フラグなし）\n"
    '%cd /content/ComfyUI\n'
    "if not os.path.exists('/content/cloudflared'):\n"
    '    !wget -q -O /content/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64\n'
    '    !chmod +x /content/cloudflared\n'
    "    print('cloudflared取得done')\n"
    "log = open('/content/comfy.log', 'w')\n"
    "subprocess.Popen(['python', 'main.py', '--listen', '127.0.0.1', '--port', '8188', *COMFY_FLAGS.split()],\n"
    "                 cwd='/content/ComfyUI', stdout=log, stderr=subprocess.STDOUT)\n"
    'print(f\'ComfyUI起動中... FLAGS="{COMFY_FLAGS}" 起動待ち:\')\n'
    'for i in range(60):\n'
    "    try:\n"
    "        urllib.request.urlopen('http://127.0.0.1:8188/system_stats', timeout=5)\n"
    "        print('ComfyUI up!')\n"
    '        break\n'
    '    except Exception:\n'
    '        time.sleep(5)\n'
    'else:\n'
    "    raise RuntimeError('ComfyUIが起動しません。/content/comfy.log を確認')\n"
    "cf = subprocess.Popen(['/content/cloudflared', 'tunnel', '--url', 'http://127.0.0.1:8188'],\n"
    '                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)\n'
    'url = None\n'
    'for _ in range(60):\n'
    '    line = cf.stdout.readline()\n'
    '    print(line, end="")\n'
    '    m = re.search(r"https://[a-z0-9-]+\\\\.trycloudflare\\\\.com", line)\n'
    '    if m:\n'
    '        url = m.group(0)\n'
    '        break\n'
    "print('\\\\n公開URL:', url)\n"
    "print('ブラウザで開いてComfyUI GUIが使える（workflows/ のJSONを読み込んで実行可）')\n"
)

C6 = (
    '# ===== セル6: APIで1枚生成 -> PNG保存 -> ダウンロード（GUI不要） =====\n'
    'import json, time, urllib.request\n'
    'from google.colab import files\n'
    'try:\n'
    '    MODE\n'
    'except NameError:\n'
    "    MODE = 'bf16'  # セル1未実行時の既定（G4想定）\n"
    '\n'
    "PROMPT = 'a cozy fantasy tavern interior, warm lantern light, anime style, highly detailed'\n"
    "NEGATIVE = ''  # Pruna 8stepはCFG=1・ネガティブ空が推奨\n"
    'SEED = 12345\n'
    'RESOLUTION = 1024  # 1024/1280/1536/2048。G4+bf16なら1536〜2048も実用的\n'
    "OUTNAME = 'gorakudo_g4_01.png'\n"
    '\n'
    "WF_NAME = 't2i_bf16_api.json' if MODE == 'bf16' else 't2i_pruna_int8_api.json'\n"
    "wf = json.load(open(f'/content/ComfyUI/workflows/{WF_NAME}'))\n"
    "wf['4']['inputs']['prompt'] = PROMPT\n"
    "wf['4']['inputs']['negative_prompt'] = NEGATIVE\n"
    "wf['4']['inputs']['resolution'] = RESOLUTION\n"
    "wf['5']['inputs']['width'] = RESOLUTION\n"
    "wf['5']['inputs']['height'] = RESOLUTION\n"
    "for k, n in wf.items():\n"
    "    if n['class_type'] in ('SamplerCustom', 'KSamplerAdvanced', 'RandomNoise'):\n"
    "        n['inputs']['noise_seed'] = SEED\n"
    '\n'
    't0 = time.time()\n'
    "req = urllib.request.Request('http://127.0.0.1:8188/prompt',\n"
    "                             data=json.dumps({'prompt': wf}).encode(),\n"
    "                             headers={'Content-Type': 'application/json'})\n"
    "pid = json.load(urllib.request.urlopen(req))['prompt_id']\n"
    "print('prompt_id:', pid)\n"
    'while True:\n'
    "    h = json.load(urllib.request.urlopen(f'http://127.0.0.1:8188/history/{pid}'))\n"
    "    if pid in h and h[pid].get('status', {}).get('completed'):\n"
    '        break\n'
    "    print('  generating...')\n"
    '    time.sleep(5)\n'
    "print(f'生成時間: {time.time()-t0:.0f}s')\n"
    "for nid, nd in h[pid]['outputs'].items():\n"
    "    for img in nd.get('images', []):\n"
    '        q = f"filename={img[\'filename\']}&subfolder={img[\'subfolder\']}&type={img[\'type\']}"\n'
    "        data = urllib.request.urlopen(f'http://127.0.0.1:8188/view?{q}').read()\n"
    "        open('/content/' + OUTNAME, 'wb').write(data)\n"
    "        print('saved', OUTNAME, f'{len(data)/1e6:.1f} MB')\n"
    '        break\n'
    '    break\n'
    "files.download('/content/' + OUTNAME)\n"
)

C7 = (
    '# ===== セル7（任意）: バッチ生成 -> zipでダウンロード =====\n'
    'import json, time, urllib.request, zipfile, glob, os\n'
    'from IPython.display import Image, display\n'
    'try:\n'
    '    MODE\n'
    'except NameError:\n'
    "    MODE = 'bf16'\n"
    '\n'
    'PROMPTS = [\n'
    "    'a cozy fantasy tavern interior, warm lantern light, anime style, highly detailed',\n"
    "    'a quiet library dungeon with floating candles, anime style, highly detailed',\n"
    ']\n'
    'SAME_PROMPT_N = 0  # >0ならPROMPTS[0]をseed連番でN枚（G4なら10枚でも1〜2分）\n'
    'BASE_SEED = 100\n'
    'RESOLUTION = 1024  # 1024/1280/1536/2048\n'
    "OUTDIR = '/content/batch_out'\n"
    '\n'
    'os.makedirs(OUTDIR, exist_ok=True)\n'
    "WF_NAME = 't2i_bf16_api.json' if MODE == 'bf16' else 't2i_pruna_int8_api.json'\n"
    "base_wf = json.load(open(f'/content/ComfyUI/workflows/{WF_NAME}'))\n"
    '\n'
    'def gen_one(prompt, seed, outpath):\n'
    '    wf = json.loads(json.dumps(base_wf))\n'
    "    wf['4']['inputs']['prompt'] = prompt\n"
    "    wf['4']['inputs']['negative_prompt'] = ''\n"
    "    wf['4']['inputs']['resolution'] = RESOLUTION\n"
    "    wf['5']['inputs']['width'] = RESOLUTION\n"
    "    wf['5']['inputs']['height'] = RESOLUTION\n"
    '    for k, n in wf.items():\n'
    "        if n['class_type'] in ('SamplerCustom', 'KSamplerAdvanced', 'RandomNoise'):\n"
    "            n['inputs']['noise_seed'] = seed\n"
    "    req = urllib.request.Request('http://127.0.0.1:8188/prompt',\n"
    "                                 data=json.dumps({'prompt': wf}).encode(),\n"
    "                                 headers={'Content-Type': 'application/json'})\n"
    "    pid = json.load(urllib.request.urlopen(req))['prompt_id']\n"
    '    while True:\n'
    "        h = json.load(urllib.request.urlopen(f'http://127.0.0.1:8188/history/{pid}'))\n"
    "        if pid in h and h[pid].get('status', {}).get('completed'):\n"
    '            break\n'
    '        time.sleep(5)\n'
    '    for nid, nd in h[pid][\'outputs\'].items():\n'
    "        for img in nd.get('images', []):\n"
    '            q = f"filename={img[\'filename\']}&subfolder={img[\'subfolder\']}&type={img[\'type\']}"\n'
    "            open(outpath, 'wb').write(urllib.request.urlopen(f'http://127.0.0.1:8188/view?{q}').read())\n"
    '            return\n'
    '\n'
    'jobs = ([(PROMPTS[0], BASE_SEED + i) for i in range(SAME_PROMPT_N)] if SAME_PROMPT_N > 0\n'
    '        else [(p, BASE_SEED + i) for i, p in enumerate(PROMPTS)])\n'
    't0 = time.time()\n'
    'for i, (p, s) in enumerate(jobs):\n'
    "    out = f'{OUTDIR}/batch_{i:02d}_s{s}.png'\n"
    '    gen_one(p, s, out)\n'
    "    print(f'[{i+1}/{len(jobs)}] saved {out}')\n"
    '    display(Image(out, width=384))\n'
    "print(f'バッチ合計: {time.time()-t0:.0f}s / {len(jobs)}枚')\n"
    "with zipfile.ZipFile('/content/batch_out.zip', 'w') as z:\n"
    "    for f in sorted(glob.glob(f'{OUTDIR}/*.png')):\n"
    '        z.write(f, os.path.basename(f))\n'
    'from google.colab import files\n'
    "files.download('/content/batch_out.zip')\n"
)

C8 = (
    '# ===== セル8（任意）: キープアライブ（アイドル切断避けの小技） =====\n'
    'from IPython.display import Javascript, display\n'
    '# Note: ブラウザタブを開いたままにする事が条件。確実ではないおまじない。\n'
    'display(Javascript(\'setInterval(function(){console.log("grkd-keepalive " + new Date().toISOString());}, 60*1000);\'))\n'
    "print('keepalive timer started（コンソールに60秒毎出力）')\n"
)

nb = {
    'nbformat': 4,
    'nbformat_minor': 5,
    'metadata': {
        'kernelspec': {'display_name': 'Python 3', 'language': 'python', 'name': 'python3'},
        'language_info': {'name': 'python', 'version': '3.10'},
    },
    'cells': [md(TITLE), code(C1), code(C2), code(C2B), code(C3),
              code(C4), code(C5), code(C6), code(C7), code(C8)],
}
json.dump(nb, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('wrote ' + OUT)
