"""Verify the Colab notebook: structure, embedded workflows, no cache node, cell syntax."""
import ast
import json

NB = 'D:/OSS_ProgramFiles/ComfyUI/tools/colab/GoRakuDo_ComfyUI_Colab.ipynb'

nb = json.load(open(NB, encoding='utf-8'))
print('1) ipynb json.load: OK, cells=%d' % len(nb['cells']))
assert nb['nbformat'] == 4

WF_NAMES = ['t2i_pruna_int8_api.json', 'edit_pruna_int8_api.json', 'inpaint_int8_api.json',
            't2i_bf16_api.json', 'edit_bf16_api.json', 'inpaint_bf16_api.json']
codes = [c for c in nb['cells'] if c['cell_type'] == 'code']
srcs = [''.join(c['source']) for c in codes]
full = '\n'.join(srcs)
for w in WF_NAMES:
    assert repr(w) in full, 'missing embedded wf: ' + w
print('2) 埋め込みWF 6種: OK')
bad = [ln for ln in full.splitlines()
       if 'class_type' in ln and 'QwenImage21Cache' in ln
       and not ln.strip().startswith(('#', 'assert'))]
assert not bad, 'Cacheノード混入: %s' % bad
print('3) QwenImage21Cache 除去(埋め込みJSON内): OK')
# 除去の正しさ: editはmodel=["11", 0]直結、inpaintはmodel=["1", 0]直結のはず
assert '"model": ["11", 0]' in full and '"model": ["1", 0]' in full
print('4) Cache除去後の再結線 ([11,0]/[1,0]): OK')

def strip_magic(s):
    out = []
    for ln in s.splitlines():
        st = ln.strip()
        if st.startswith(('!', '%')) and not st.startswith(('!=', '%=')):
            indent = ln[:len(ln) - len(ln.lstrip())]
            out.append(indent + 'pass  # colab-magic')
        else:
            out.append(ln)
    return '\n'.join(out)


ok = 0
for i, s in enumerate(srcs):
    try:
        ast.parse(strip_magic(s))
        ok += 1
    except SyntaxError as e:
        print('   cell%d SyntaxError: %s' % (i, e))
        raise
print('5) ast.parse: %d/%d cells OK (magic/!行除外)' % (ok, len(srcs)))
