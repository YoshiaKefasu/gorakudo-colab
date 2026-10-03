# remote-bridge — ブラウザはローカル・実行はColab・保存はローカル

ローカルPCのブラウザ (`http://127.0.0.1:8188`) でComfyUIのGUIを触り、
実行だけColab上のComfyUIに流し、生成画像をローカルフォルダに自動保存する
小さな透過プロキシ。ローカルGPUは使わない。

**推奨接続は Tailscale（WireGuard P2P直結・完全無料・通信量無制限・最速）**。
Colabセル5が出す `http://100.x.y.z:8188` を `--remote` にそのまま渡すだけ。

- 本体: `remote-bridge.py`（標準ライブラリ + `aiohttp` のみ。ComfyUIのvenvに入っている）
- GoRakuDoリポジトリは触らない。コミット不要。

## 前提: Colab側で実行しておくセル

ノートブック `tools/colab/GoRakuDo_ComfyUI_Colab.ipynb` を開き、ランタイムをGPUにして：

1. **セル1〜5を上から実行**（必須）。セル5がComfyUI起動＋Tailscale公開URLを発行する。
   出力の `公開URL: http://100.x.y.z:8188` をコピーする（PCも同じTailscaleアカウントでログイン必須）。
   フォールバック（cloudflared/ngrok）の場合は `公開URL: https://...` の方をコピーする。
2. **セル8は任意**（ブラウザのキープアライブ。アイドル切断を減らしたい時だけ）。
3. **セル9は任意**（監視ループ。15秒ごとに状態表示＋トンネル自動復帰。実行中は他セル不可）。
4. トンネルが落ちるとセル5のウォッチドッグが自動で作り直し、
   `[watchdog] 新しい公開URL: https://...` を出す。そのURLに切り替える（下記）。

## 起動

```bat
:: 推奨・Tailscale（P2P直結・無制限・最速）
D:\OSS_ProgramFiles\ComfyUI\.venv\Scripts\python.exe D:\OSS_ProgramFiles\ComfyUI\tools\remote-bridge.py --remote http://100.x.y.z:8188

:: フォールバック・cloudflared
D:\OSS_ProgramFiles\ComfyUI\.venv\Scripts\python.exe D:\OSS_ProgramFiles\ComfyUI\tools\remote-bridge.py --remote https://xxxx.trycloudflare.com
```

ブラウザで `http://127.0.0.1:8188` を開く。生成物は
`D:\OSS_ProgramFiles\ComfyUI\output\remote` に落ちる。

オプション:

```bat
:: 保存先とポートを変える
python tools\remote-bridge.py --remote https://xxxx.trycloudflare.com --save-dir D:\MyOutputs --port 8189

:: URLファイル追従あり（下記）
python tools\remote-bridge.py --remote https://xxxx.trycloudflare.com --url-file D:\OSS_ProgramFiles\ComfyUI\tools\tunnel_url.txt
```

## トンネルURLが変わった時（切替は2方式。併用可）

**TailscaleのIPは同一ホスト名なら再取得でも同じことが多く、切替はほぼ不要。**
Colabのquick tunnel（cloudflared/ngrok）は再作成されるとURLが変わる。切替方法：

- **A. 標準入力**: 起動したコンソールで `url <新しいURL>` + Enter。即時切替。
- **B. `--url-file`**: URLだけ書いたテキストファイルを指定。5秒ごとに読み直して自動追従。
  watchdogが `[watchdog] 新しい公開URL: ...` を出したら、そのURLをファイルに貼り直すだけ。

## Tailscaleで使う（推奨・本線）

セル5既定の `'tailscale'` で出た `http://100.x.y.z:8188` をそのまま渡す：

```bat
python tools\remote-bridge.py --remote http://100.x.y.z:8188
```

- https化不要・ngrok警告ヘッダ不要（ブリッジが自動判定。`http://100.x` を見ると起動ログに `[tailscale]` と出る）。
- URLが変わらないので `--url-file` の更新も不要。`url <新URL>` での差し替えもそのまま使える。
- 条件は **PCとColabが同じTailnet** に入っていることだけ（PC側Tailscaleアプリでログイン）。

## ngrokで使う（フォールバック。月間1GB上限に注意）

ngrok無料枠は**月間1GB上限で、画像を数枚流すとすぐ死ぬ**。Tailscaleが使えない時だけの選択肢。
cloudflaredのquick tunnelは読み込みが遅いことがある。速くしたい時はngrokに切替える。

**Colab側（セル5先頭）**:

```python
TUNNEL_PROVIDER = 'ngrok'
NGROK_AUTHTOKEN = 'あなたのauthtoken'
NGROK_DOMAIN    = 'grkd-colab.ngrok-free.app'  # 無料の固定ドメイン。空なら毎回ランダム
```

1. [ngrokのダッシュボード](https://dashboard.ngrok.com/)で無料登録 → **Your Authtoken**をコピーして
   `NGROK_AUTHTOKEN`に入れる（ユーザー操作。トークンは他人に見せない）。
2. 固定ドメインがほしい時はダッシュボードの **Domains**で無料枠の1つを作成 → `NGROK_DOMAIN`に入れる。
   固定ドメインならURLが変わらないので、`--url-file`の更新も不要になる。
3. ブリッジ側はそのまま：`--remote https://<固定ドメインか発行URL>`で起動するだけ。
   リモートが `*.ngrok-free.app` / `*.ngrok.io` / `*.ngrok.app`なら、ブリッジが
   `ngrok-skip-browser-warning: true`を**自動で付与**する（無料枠の訪問警告ページ対策。設定不要）。

注意：

- 無料枠の制限（同時トンネル数・帯域・セッション時間など）は変わることがあるので**公式ページで確認**すること。
- cloudflaredに戻したい時は `TUNNEL_PROVIDER = 'cloudflared'`に戻すだけ（既定。壊していない）。

## pinggyで使う（フォールバック。UDPが壊れた環境向け）

TailscaleのUDP経路が遅い環境向けの選択肢。**SSHリモートフォワード（TCP）・帯域無制限・登録不要**。
無料枠は**60分で切れる**（URLが変わる）。Colabセル5のウォッチドッグが自動で張り直すので、
新しいURLを `url <新しいURL>` で切替えるか `--url-file` を使う。

**Colab側（セル5先頭）**:

```python
TUNNEL_PROVIDER = 'pinggy'
```

ブリッジ側はそのまま：`--remote https://<pinggyのURL>`で起動するだけ。
リモートが `*.pinggy.link` / `*.pinggy-free.link`なら、ブリッジが
`X-Pinggy-No-Screen: 1`を**自動で付与**する（無料枠のscreeningページ対策。設定不要）。

注意：

- ComfyUIのWebSocket（`/ws`）がHTTPトンネルを通るかは未実測。進捗表示が出ない時は報告すること（TCPモード検討）。

## キャッシュ（初回は遅い・2回目以降は速い）

トンネル経由は1リクエスト数秒かかる。GUIは静的アセットを数百回取るので、ブリッジがローカルに覚える：

- **静的アセット**（`/assets/`, `/extensions/`, `/static/`, `/favicon*`, `/templates/`, `/user.css`, `/scripts/`のGET・200）
  → ディスク（既定 `<save-dirの親>/bridge-cache`、`--cache-dir`で変更）に保存。2回目以降はローカルから返す。
- **重い読み取りAPI**（`/object_info`, `/embeddings`, `/extensions`, `/models`, `/view_metadata/*`）
  → メモリにTTL保持（既定300秒、`--api-cache-ttl`で変更。0で無効）。
- `/prompt`, `/queue`, `/interrupt`, `/history`, `/view`, `/upload/image`, `/ws`は**常に転送**（キャッシュしない）。
- キャッシュから返した応答には `X-Bridge-Cache: hit`ヘッダが付く。条件付きリクエスト（ETag/Last-Modified）には304を返す。
- 起動ログに `キャッシュ: <dir> / TTL=<n>s / ...`と1行出る。
- **キャッシュを消したい時はディレクトリ（`bridge-cache`）を削除**するだけ。API側は再起動で消える。

## 保存仕様

- リモートの `/history?max_items=20` を2秒ごとにポーリング。`SaveImage`/`PreviewImage` の
  `output`/`temp` 画像を `/view` で取得して保存する。
- ファイル名: `<prompt_id先頭8>_<元ファイル名>`。`subfolder` は維持。
- 同じ画像は二度保存しない（`prompt_id`+subfolder+filename+typeで管理）。
  保存のたびに `[saved] <path>` を表示する。
- 再起動後は直近20件を拾い直すが、**同名ファイルが既にあれば黙ってスキップ**するので実害なし。

## セキュリティ注意

- TailscaleのIPは**あなたのTailnet内からのみ到達可能**（Tailnet外には見えない）。
  それでも使い終わったらColabのランタイムを停止する（Ephemeralキーはノードを自動削除）。
- quick tunnelのURLは**認証なし**。URLを知っている人は誰でもColabのComfyUIを使える。
  **他人に渡さない**。使い終わったらColabのランタイムを停止する。
- `--host` は既定の `127.0.0.1` のまま使う。`--host 0.0.0.0` はLAN全体に認証なし公開するので**非推奨**。

## トラブルシュート

| 症状 | 原因・対処 |
|---|---|
| 起動時に `[error] リモートに接続できません` | URLが古い可能性大。Colabでセル5を再実行し、新しいURLに切替 |
| Tailscaleの `http://100.x` に繋がらない | PCがTailnetに入っていない可能性大。PC側Tailscaleアプリで同じアカウントにログインし、`100.x.y.z` にpingが通るか確認 |
| ブラウザで403（Cloudflareの画面） | ランタイム切れかcloudflared死亡。セル1〜5を再実行。watchdogの新URLが出てないか確認 |
| `/history 取得失敗` が出る | 一時的なトンネル瞬断。復帰すれば `[ok] リモートに再接続しました` と出る。続く場合はURL切替 |
| 進捗バーが動かない | WS切断。ブラウザが自動再接続する。ダメならF5 |
| `待ち受けできません`（ポート使用中） | ローカルのComfyUIが8188を掴んでいる。止めるか `--port 8189` で避ける |

## 制限

- ポーリングは2秒間隔＋直近20件まで。終わった直後の古い生成は拾わないことがある。
- 再起動すると重複管理集合はリセットされる（既存ファイルのスキップで吸収）。
- 生成中（キュー待ち・実行中）の途中経過は保存しない。完成した`outputs`のみ。
- Tailscale直結なら画像取得も高速。cloudflared経由だと直結より遅い。巨大バッチはセル6/7のAPI経路＋zip取得が速い。
- GUI側の再接続はブラウザ任せ（ComfyUI標準の自動再接続）。プロキシ側で特別な再送はしない。
