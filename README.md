# so-dsc

**非公式 / サードパーティ実装 (unofficial third-party)**。Sony DSC シリーズ
(動作確認: **DSC-RX100M5A**) の **Camera Remote API** を再実装した Flask
ビューア + 操作 + コンテンツ取り込み + AI 撮影係用 MCP サーバ。旧
PlayMemories Mobile / Imaging Edge Mobile の代替。

> ⚠️  **本リポジトリは Sony Group Corporation / Sony Imaging Products &
> Solutions Inc. と一切関係ありません。** Sony が公開していた Camera
> Remote API SDK の公開仕様 (`developer.sony.com`、現在は EOL) を参照して、
> サードパーティが独自に実装したクライアントです。Sony 公式アプリのコード
> は含まれておらず、暗号化や保護機構を回避するものでもありません。
> "Sony"、"PlayMemories"、"Imaging Edge"、"Creators' App"、"DSC"、
> "RX100" は Sony Group Corporation の商標です。

プロトコル仕様: [docs/protocol.md](docs/protocol.md)
最小再現 (依存なし): [scripts/grab_frame.py](scripts/grab_frame.py)

## 使い方

```sh
# 1) カメラ本体メニュー: ネットワーク → Bluetooth リモートコントロール →
#    「常時接続」を ON にしておくと、電源 ON で勝手に Smart Remote が立ち上がる。
#    そうでなければ:
#      アプリケーション → スマートリモコン を都度起動 (画面に SSID + PW 表示)
# 2) PC 側で `DIRECT-XXXX:DSC-RX100M5A` に Wi-Fi 接続
# 3)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
SODSC_HOST=192.168.122.1 python run.py
# → http://127.0.0.1:5050 を開く
```

カメラ内画像/動画の取り込みは **カメラ本体側で** 「スマートリモコン」を抜けて
「スマートフォンに送る」を起動する必要がある (RX100M5A は `setCameraFunction`
未対応のためソフト経由でモード切替できない)。その状態で UI の `Camera content`
タブの `Load list` を押す。撮影に戻すにはカメラ側で再度「スマートリモコン」へ。

環境変数:

| 名前 | 既定 | 説明 |
|------|------|------|
| `SODSC_HOST` | (SSDP 自動発見) | カメラ IP。RX100M5A は AP モードで `192.168.122.1` |
| `SODSC_DD_PORT` | `64321` | UPnP DD.xml 取得ポート (各サービスポートは DD.xml から動的に取得) |
| `SODSC_REC_DIR` | `recordings` | ライブビュー録画保存先 (.mjpeg) |
| `SODSC_DL_DIR` | `downloads` | カメラから取り込んだ画像/動画の保存先 |
| `HOST` | `127.0.0.1` | Flask bind host |
| `PORT` | `5050` | Flask port (5000 を避ける: macOS Monterey 以降の AirPlay Receiver と衝突) |

## 機能

- ライブビュー (Sony 独自バイナリストリームをパースして MJPEG として配信)
- リモートシャッター + AF (半押し) + ズーム
- 設定変更: shoot mode / ISO / シャッタースピード / F値 / EV
- カメラモード切替: Remote Shooting ⇔ Contents Transfer
- カメラ内画像/動画一覧 (サムネ + 元データダウンロード)
- ライブビューの録画 (`recordings/<timestamp>.mjpeg` に JPEG を連結保存)

未実装 / 別プロジェクト:
- **仮想カメラデバイス出力** — macOS 13+ 用の CoreMediaIO Camera Extension で
  別途実装予定。本リポジトリの `/stream` を pull する形を想定。
- 動画記録 (本体 SD への記録: `startMovieRec`) — UI から叩けるようにはまだしてない。
  `/api/setting` の `shoot_mode=movie` + 別途 `startMovieRec` を組めば可能。

## 録画ファイル (.mjpeg)

ライブビューの JPEG フレームを単純連結したファイル。ffmpeg で MP4 にエンコードしたい場合:

```sh
# MJPEG をそのまま MP4 コンテナに（再エンコードなし、互換性高）
ffmpeg -framerate 15 -i recordings/20260429_103000.mjpeg -c:v copy out.mp4

# H.264 にトランスコード（ファイル小）
ffmpeg -framerate 15 -i recordings/20260429_103000.mjpeg -c:v libx264 -pix_fmt yuv420p out.mp4
```

カメラ本体カードに残る高解像度動画は Camera content タブからダウンロードしてください。

## 制約

- カメラの Wi-Fi AP は **同時接続クライアント 1 台** のみ受け付ける。純正アプリと
  同時には使えない。
- Flask の reloader は 2 プロセス起動するためカメラ側の 1-client 制限とぶつかる。
  `run.py` では reloader を無効化済み。
- `setCameraFunction` 直後はカメラ側状態がしばらく不安定。Flash モード切替直後に
  `startLiveview` を叩いて Illegal State が返る場合は数秒待って再試行する設計。
- macOS Safari は `multipart/x-mixed-replace` の挙動が貧弱な場合がある。Chrome /
  Firefox 推奨。
- 一部の API (動画記録設定など) は RX100M5A では未対応 (`12 No Such Method`)。
  事前に `getAvailableApiList` で確認するのが安全。

## ネットワーク的な注意

デフォルトで `127.0.0.1` にだけ bind しています。`HOST=0.0.0.0` で公開する場合は
**同一 LAN の誰でもカメラを操作・録画閲覧できる** ことに注意してください。認証は
ありません。自宅 LAN かつ信頼できる範囲でのみ晒すこと。

## MCP サーバ (AI 撮影係)

Flask が立っている前提で、`mcp_server/server.py` が **Claude Code / Claude
Desktop 等の MCP クライアントに stdio で接続できるブリッジ** になります。
カメラを「AI に渡せるツール」として公開する形:

ツール一覧 (18 個):

| カテゴリ | tool | 説明 |
|---|---|---|
| introspect | `get_status` | 現状 (running/idle/available_apis/battery など) |
| introspect | `reconnect` | 手動再接続トリガ |
| vision | `get_liveview_frame` | 最新ライブビュー JPEG を Image で返す (AI が見る) |
| shoot | `take_picture(save=True)` | シャッター。postview を Image で返す + downloads/ に保存 |
| shoot | `half_press(on)` | 半押し AF (位置は本体側で決まる) |
| shoot | `zoom(direction, movement)` | ズーム |
| exposure | `set_iso`, `set_shutter`, `set_fnumber`, `set_exposure_compensation` | 露出 |
| exposure | `set_white_balance_auto`, `set_white_balance_kelvin(K)` | WB |
| burst | `start_burst` / `stop_burst` | 連写制御 (postview URL のリスト返却) |
| bulb | `start_bulb` / `stop_bulb` | バルブ撮影 |
| storage | `list_saved_pictures`, `get_saved_picture(name)` | 保存済を覗く |

### Claude Code に登録する

`.mcp.json.example` をコピーして絶対パスを埋めるか、`claude mcp add` で
プロジェクトスコープに追加します。`.mcp.json` 自体は `.gitignore` 済み
(ホスト依存)。

```sh
# Flask は別タームで起動しておく
SODSC_HOST=192.168.122.1 python run.py

# 方法 A: claude CLI で登録 (推奨)
claude mcp add so-dsc -s project \
  -e SODSC_API_BASE=http://127.0.0.1:5050 \
  -e PYTHONPATH=$(pwd) \
  -- $(pwd)/.venv/bin/python -m mcp_server.server

# 方法 B: 手動コピー
cp .mcp.json.example .mcp.json
# → .mcp.json を開き <ABSOLUTE_PATH_TO_REPO> を実パスに置換
```

`SODSC_API_BASE` を変えれば別ポートの Flask に向けられます (例: dev で
`5051` を使ってる場合)。登録後、Claude Code を再起動するとツールが認識
されます (`mcp__so-dsc__get_status` など)。

### 使用感

Claude に `撮影係になって` と頼むと:

- `get_liveview_frame` で構図を見る
- 必要なら `set_iso`/`set_shutter`/`set_exposure_compensation` で露出調整
- `zoom` で寄り/引き
- `take_picture` で撮る → postview を即見る
- 「もうちょい寄る?」「明るすぎ、EV-1」みたいな自己フィードバックが回る

**AF 位置だけは指定不可** (RX100M5A 制限)。本体側で Focus Area を決めてお
くか、`half_press` 中に画面中央へ被写体が来るよう人間 or AI が構図を寄
せる運用。

## Disclaimer / 免責事項

This project is an independent, third-party reimplementation of the
publicly-documented **Camera Remote API** that Sony released as a beta
SDK (now end-of-life). It is **not affiliated with, endorsed by, or
sponsored by Sony Group Corporation, Sony Imaging Products & Solutions
Inc., or any of their subsidiaries**. No source code from any Sony
application, SDK, or firmware is included or redistributed. The project
does not bypass any authentication, DRM, or content-protection
mechanism.

Trademarks "Sony", "PlayMemories", "Imaging Edge", "Creators' App",
"Camera Remote API", "DSC", "RX100", and any associated logos are
property of Sony Group Corporation. They are referenced here solely
for compatibility documentation.

Use this software at your own risk. The MIT license below disclaims
all warranties.

本リポジトリは Sony 公式の Camera Remote API ベータ SDK (現在 EOL) の
**公開仕様** を参照したサードパーティ独自実装です。**Sony Group Corporation
および関連会社とは一切関係ありません**。Sony 公式アプリ・SDK・ファーム
ウェアのソースコードは含まれていません。認証・DRM・保護機構を回避する
ものでもありません。商標 ("Sony"、"PlayMemories"、"Imaging Edge"、
"Creators' App"、"Camera Remote API"、"DSC"、"RX100" 等) はすべて Sony
Group Corporation に帰属し、互換性を説明する目的でのみ言及しています。
本ソフトウェアは利用者の自己責任でお使いください。

## License

MIT — see [LICENSE](LICENSE).
