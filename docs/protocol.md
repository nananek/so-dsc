# Sony Camera Remote API (PlayMemories Mobile 経路) 通信メモ

> **第三者注記** — 本ドキュメントは Sony Group Corporation / Sony Imaging
> Products & Solutions Inc. による公式資料ではありません。Sony が公開して
> いた **Camera Remote API ベータ SDK** (developer.sony.com、現在 EOL) の
> 公開仕様および筆者の動作観察に基づくサードパーティの整理です。Sony 公式
> アプリ (PlayMemories Mobile / Imaging Edge Mobile) のソースコードや
> リバースエンジニアリング成果物は含まれていません。暗号化・認証・DRM な
> ど保護機構を回避する内容も一切含みません。"Sony"、"PlayMemories"、
> "Imaging Edge"、"Camera Remote API"、"DSC"、"RX100" は Sony Group
> Corporation の商標です。

旧 PlayMemories Mobile (PMM) アプリが対応カメラと話す **Camera Remote API**
の要点まとめ。Sony が公式に SDK / API リファレンスを公開していたもの (現在は
EOL で developer.sony.com 上は到達不能だが、SDK ZIP やサンプル / API リファ
レンス HTML はアーカイブで参照可能)。本書は公開仕様の整理であり、暗号化や
保護機構を回避するものは含まない。

対象機: **DSC-RX100M5A** (本書の動作確認機)。同 API は α (ILCE-7M2 等) /
RX0 / QX10 / 一部 HX/WX-V 系も同形式で話す。

## ネットワーク構成

| 項目 | 値 |
|---|---|
| カメラ AP SSID | `DIRECT-XXXX:DSC-RX100M5A` (WPA2-PSK、PW は本体表示) |
| カメラ IP | `192.168.122.1` (固定。機種により稀に異なる) |
| クライアントに割り当てられる IP | `192.168.122.0/24` の DHCP |
| Wi-Fi 制限 | 同時接続クライアント 1 台のみ (純正アプリと同時利用不可) |

## カメラ側のモード

RX100M5A の場合、**目的により「カメラのメニューでどのモードを選ぶか」が違う**。
これを切り替えないと該当 API は応答しない:

| 目的 | カメラ側メニュー | API 側の挙動 |
|---|---|---|
| ライブビュー / リモート撮影 | アプリケーション → 「スマートリモコン」 (PlayMemories Camera App) | `camera` サービスが完全に有効。**`avContent` は登場しない** |
| カメラ内画像の閲覧/取り込み | MENU → ネットワーク → スマートフォン操作 → 「スマートフォンに送る」 | `avContent` サービスが有効。`camera` 側は撮影系が止まる |

`setCameraFunction` 経由のソフト切替は **RX100M5A の Smart Remote Control __SAK__
v2.1.7 では未提供** (`getAvailableApiList` に出てこず、`12 No Such Method`)。
**カメラ本体メニューで切り替える前提**。α 等の機種では `setCameraFunction` で動く。

### 「常時接続」(persistent connection) オプション

カメラ本体: ネットワーク → Bluetooth リモートコントロール → 「常時接続」を ON。
- カメラ電源 ON ですぐに API サーバが立ち上がる (Smart Remote を毎回手起動する必要なし)
- スリープ復帰後も接続維持される
- 開発・常用に強く推奨

## ポート構成

すべて TCP/HTTP。**ポートは機種・ファーム依存** (DD.xml の `X_ScalarWebAPI_ActionList_URL`
が真の値)。RX100M5A 実測:

| Port | Path | 用途 |
|---|---|---|
| 64321 | `/dd.xml` | UPnP デバイス記述 (動作確認の入口、固定っぽい) |
| 10000 | `/sony/camera` | リモート撮影・ライブビュー制御・カメラ設定 (JSON-RPC) |
| 10000 | `/sony/system` | デバイス情報 (JSON-RPC) |
| 10000 | `/sony/guide` | API ガイド (JSON-RPC、機種依存) |
| 10000 | `/sony/accessControl` | `actEnableMethods` (機種依存。RX100M5A は呼ばなくても動く) |
| 10000 | `/sony/avContent` | カメラ内画像/動画ブラウズ・転送 (JSON-RPC、**Send to Smartphone モードのみ**) |
| 60152 | `/liveviewstream` | ライブビューバイナリストリーム (HTTP、`startLiveview` で動的発行) |

旧来のドキュメントには `8080` 固定とあるが、これは α 等の旧ファーム値。RX100M5A
を含む 2018+ ファームでは **10000** + 別ポート (60152 等) の構成が標準。
**コードのデフォルトは DD.xml ベースで動的取得** すること。

## 1. ディスカバリ (SSDP)

UDP ブロードキャスト `239.255.255.250:1900` に M-SEARCH:

```
M-SEARCH * HTTP/1.1
HOST: 239.255.255.250:1900
MAN: "ssdp:discover"
MX: 1
ST: urn:schemas-sony-com:service:ScalarWebAPI:1
```

カメラはユニキャストで HTTP 風レスポンス (LOCATION ヘッダ付) を返す:

```
HTTP/1.1 200 OK
LOCATION: http://192.168.122.1:64321/dd.xml
ST: urn:schemas-sony-com:service:ScalarWebAPI:1
USN: uuid:00000000-0005-0010-8000-XXXXXXXXXXXX::urn:schemas-sony-com:service:ScalarWebAPI:1
...
```

`LOCATION` を HTTP GET → UPnP デバイス記述 XML が返る。重要なのは
Sony 拡張ネームスペース `av:` の中:

```xml
<av:X_ScalarWebAPI_DeviceInfo>
  <av:X_ScalarWebAPI_Version>1.0</av:X_ScalarWebAPI_Version>
  <av:X_ScalarWebAPI_ServiceList>
    <av:X_ScalarWebAPI_Service>
      <av:X_ScalarWebAPI_ServiceType>camera</av:X_ScalarWebAPI_ServiceType>
      <av:X_ScalarWebAPI_ActionList_URL>http://192.168.122.1:8080/sony</av:X_ScalarWebAPI_ActionList_URL>
      <av:X_ScalarWebAPI_AccessType></av:X_ScalarWebAPI_AccessType>
    </av:X_ScalarWebAPI_Service>
    <!-- system / avContent / guide も同形式 -->
  </av:X_ScalarWebAPI_ServiceList>
</av:X_ScalarWebAPI_DeviceInfo>
```

`ActionList_URL + "/" + ServiceType` が該当サービスのエンドポイント。
RX100M5A は IP/ポート固定なので、IP が既知なら SSDP は省略してよい
(`http://192.168.122.1:8080/sony/{service}` 直撃)。

## 2. JSON-RPC 2.0

各サービスエンドポイントに `POST` で JSON-RPC を投げる。`Content-Type` は
任意 (`application/json` で OK)。

リクエスト:

```json
{
  "method": "startLiveview",
  "params": [],
  "id": 1,
  "version": "1.0"
}
```

レスポンス (成功):

```json
{"result":["http://192.168.122.1:8080/liveview/liveviewstream"],"id":1}
```

レスポンス (エラー):

```json
{"error":[1,"Not Available Now"],"id":1}
```

`error` は `[code, message]` の配列。代表的なコード:

| code | 意味 |
|---|---|
| 1 | `Any` (汎用) |
| 2 | Timeout |
| 3 | Illegal Argument |
| 5 | Illegal Request |
| 12 | No Such Method |
| 14 | Illegal State |
| 40400 | Already Polling (`getEvent` が既に走っている) |
| 40402 | Polling Cancelled |

複雑なメソッド (`setExposureCompensation` など) では `version` が `1.1`
以上を要求するものがある。`getMethodTypes` で API バージョンを確認可能。

## 3. 主なメソッド

### 3.1 camera サービス (`/sony/camera`)

セットアップ系:

| method | 概要 |
|---|---|
| `getApplicationInfo` | アプリ名・API バージョン |
| `getAvailableApiList` | 現在の状態で呼べる API 一覧 (モード切替直後は要再確認) |
| `getVersions` | サービスのバージョン一覧 |
| `setCameraFunction` | `"Remote Shooting"` / `"Contents Transfer"` の切替 |
| `getCameraFunction` | 現モード取得 |
| `startRecMode` | リモート撮影モード開始 (古い機種で必要、RX100M5A では基本不要だが冪等で打ってよい) |
| `stopRecMode` | 上記停止 |

ライブビュー:

| method | 概要 |
|---|---|
| `startLiveview` | ライブビュー URL を返す |
| `stopLiveview` | 停止 |
| `startLiveviewWithSize` | サイズ指定あり (`"L"` / `"M"`) |
| `getLiveviewSize` | 現在のサイズ |
| `getSupportedLiveviewSize` / `getAvailableLiveviewSize` | 可否 |

撮影:

| method | 概要 |
|---|---|
| `actTakePicture` | 1 枚撮影。`result` に postview JPEG URL (1 枚) |
| `awaitTakePicture` | 連写などで遅延がある場合の待機 |
| `actHalfPressShutter` / `cancelHalfPressShutter` | 半押し (AF) |
| `startMovieRec` / `stopMovieRec` | 動画録画開始/停止 (本体カードに保存) |
| `startContShooting` / `stopContShooting` | 連写 |

設定 (一部):

| method | 概要 |
|---|---|
| `setShootMode` | `"still"`, `"movie"`, `"audio"`, `"intervalstill"` |
| `getShootMode` / `getSupportedShootMode` | |
| `setIsoSpeedRate` / `getIsoSpeedRate` | `"AUTO"` or `"100"` 等の文字列 |
| `setShutterSpeed` / `getShutterSpeed` | `"1/250"`, `"BULB"` 等 |
| `setFNumber` / `getFNumber` | `"5.6"` 等 |
| `setExposureCompensation` | `±N` (ステップは `getExposureCompensation` 系で確認) |
| `setFocusMode` | `"AF-S"`, `"MF"` 等 |
| `setWhiteBalance` | |
| `setStillSize` | `"3:2","20M"` 等 |
| `setSelfTimer` | 秒 |
| `setFlashMode` | |
| `actZoom` | `("in"|"out", "1shot"|"start"|"stop")` |
| `setProgramShift` | |
| `setBeepMode` | |

イベント (状態取得):

| method | 概要 |
|---|---|
| `getEvent` | 引数 `[longPoll(bool)]`。`longPoll=true` だと変化があるまで待つ。`result` は state スロット配列 (バッテリ/シャッタースピード/ストレージ情報など) |

`getEvent` は `version` で返す slot 数が変わる **数少ないメソッド**。RX100M5A 実測:

| version | 全 slot 位置数 | 中身が来る slot |
|---|---|---|
| 1.0 | 35 | 14 (availableApiList, cameraStatus, zoomInformation, liveviewStatus, exposureMode, selfTimer, shootMode, exposureCompensation, flashMode, fNumber, isoSpeedRate, programShift, shutterSpeed, whiteBalance) |
| 1.1 | 36 | 15 (上記 + **focusStatus**) |
| 1.2 | 60 | 16 (上記 + **contShootingMode**) |
| 1.3 | 63 | 16 (1.2 と同じ。空 slot が 3 本増えてるが、本機ファームでは中身は来ない — 別機種/将来ファームの予約枠と思われる) |

本実装は **v1.2 を採用**。RX100M5A では v1.3 で実中身が増えないため、保
守的に最小バージョンを選択。別機種で v1.3 の追加 slot に意味があるなら
個別に上げる。

**`focusStatus`** の値遷移 (実測): 半押し前 `Not Focusing` → 半押し直後 `Focusing` (~50ms) → 合焦 `Focused` (~150ms) / 失敗 `Failed`。`actTakePicture` に進む前に `Focused` を待つことで、暗所/低コントラストの被写体での "ぼけ撮影" を排除できる。

### 3.2 avContent サービス (`/sony/avContent`)

階層: scheme → source → contents。

| method | 概要 |
|---|---|
| `getSchemeList` | `[{"scheme":"storage"}]` |
| `getSourceList` | `[{"scheme":"storage"}]` → `[{"source":"storage:memoryCard1"}]` |
| `getContentCount` | 指定 source / view (`"date"`/`"flat"`) のコンテンツ数 |
| `getContentList` | コンテンツ列挙。各要素に `uri`, `content.original[].url`, `content.thumbnailUrl`, `content.smallUrl`, `createdTime`, `contentKind` |
| `setStreamingContent` | 動画ストリーミング URL 発行 |
| `startStreaming` / `stopStreaming` | |
| `deleteContent` | 削除 |

`getContentList` 引数は v1.2 以降で `[{ "uri": "...", "stIdx": 0, "cnt": 100, "view": "date", "sort": "descending" }]` の形。

オリジナルは `content.original[0].url` を **ただ HTTP GET** すればバイナリが返る (`Content-Length` 付き)。サムネ / プレビューは
`content.thumbnailUrl` / `content.smallUrl` から同様に GET。

### 3.3 system サービス (`/sony/system`)

| method | 概要 |
|---|---|
| `getDeviceInfo` | カメラ機種名・ファーム |
| `setCurrentTime` | カメラ時計合わせ |

### 3.4 guide サービス (`/sony/guide`)

API ガイド情報。今回の用途では未使用。

## 4. 接続シーケンス (リモート撮影)

```
1. (任意) SSDP M-SEARCH で IP/ポート/サービス URL 取得
2. POST /sony/camera  startRecMode                        — 冪等
3. POST /sony/camera  setShootMode    {"still"}           — 必要なら
4. POST /sony/camera  startLiveview                        — URL ゲット
5. GET  <liveview URL>                                     — バイナリ受信ループ
6. POST /sony/camera  actTakePicture                       — 撮影
   → postview URL を GET でダウンロード可能
7. POST /sony/camera  stopLiveview                         — 終了処理
8. POST /sony/camera  stopRecMode
```

並行して `getEvent(longPoll=true)` を 1 本回しておくとカメラ側変化
(モード変更・残量・残枚数) を検知できる。

## 5. 接続シーケンス (画像取り込み)

```
1. (カメラ本体メニュー、または以下のソフト経由)
   POST /sony/camera   setCameraFunction {"Contents Transfer"}
2. POST /sony/avContent  getSchemeList
3. POST /sony/avContent  getSourceList   {"scheme":"storage"}
4. POST /sony/avContent  getContentCount {"source":"storage:memoryCard1","view":"date"}
5. POST /sony/avContent  getContentList  {"source":"...","stIdx":0,"cnt":N,"view":"date","sort":"descending"}
6. GET  <content.original[0].url>   — バイナリ DL
```

`Contents Transfer` モード中はライブビューや撮影 API は使えない。
戻すには `setCameraFunction({"Remote Shooting"})`。

## 6. ライブビューバイナリ形式

`startLiveview` が返す URL を `GET` (HTTP/1.0、`Connection: close` でも可)
すると **延々とバイナリが流れてくる** ストリーム。`Content-Type` は
`video/x-mpeg` 風だが MJPEG ではなく **Sony 独自のフレームコンテナ**。

各フレーム = `Common Header (8B) + Payload Header (128B) + JPEG (NB) + Padding (KB)`

### 6.1 Common Header (8 バイト)

| Offset | Size | Field | 説明 |
|---|---|---|---|
| 0 | 1 | `start_byte` | 常に `0xFF` |
| 1 | 1 | `payload_type` | `0x01` = liveview frame, `0x02` = liveview frame info |
| 2 | 2 | `sequence` | BE uint16、フレーム連番 (オーバーフローで巻き戻り) |
| 4 | 4 | `timestamp_ms` | BE uint32、撮影タイムスタンプ (ms) |

### 6.2 Payload Header (128 バイト) — type=0x01

| Offset | Size | Field | 説明 |
|---|---|---|---|
| 0 | 4 | `start_code` | `24 35 68 79` 固定 |
| 4 | 3 | `jpeg_size` | BE uint24、続く JPEG のバイト数 |
| 7 | 1 | `padding_size` | JPEG 後のパディングバイト数 (uint8) |
| 8 | 4 | `reserved` | (機種により利用) |
| 12 | 1 | `flag` | `0x00` 固定 |
| 13 | 115 | `reserved` | 全 0 埋め |

### 6.3 Payload — type=0x01

`jpeg_size` バイトの **完全な単フレーム JPEG** (SOI..EOI)。`BitmapFactory`
にそのまま流し込める。

その後 `padding_size` バイトの捨てバイト。次フレーム (= 次 Common Header)
は `padding_size` の直後から。

### 6.4 type=0x02 (frame info)

顔検出枠などのオーバーレイ情報。本実装では **読み飛ばし** のみ
(`startLiveviewWithSize` で `"L"` 指定時、または `getLiveviewFrameInfo`
で有効化したときに混在)。

レイアウト:
- Payload Header (128B): `start_code`, `frame_count` (uint16), `frame_size` (uint16), 残りパディング
- `frame_count * frame_size` バイトのフレーム情報配列

通常は無効化されているため到来しない。

### 6.5 HTTP の輸送と Transfer-Encoding: chunked

RX100M5A 実機で `startLiveview` が返す URL は **port 60152** の
`/liveviewstream?...` 形式。`HTTP/1.1` でも `HTTP/1.0` でもサーバは
**Transfer-Encoding: chunked** で返してくる。受信側はチャンクサイズ行
(`<hex>\r\n`) を剥がしながらバイナリ本体を組み立てる必要がある。

### 6.6 受信ループ (擬似コード)

```python
buf = b""
while True:
    head = read_exact(8)         # chunked 解凍済みのバイト列に対する read_exact
    assert head[0] == 0xFF
    payload_type = head[1]
    pheader = read_exact(128)
    if payload_type == 0x01:
        jpeg_size = int.from_bytes(pheader[4:7], "big")
        padding_size = pheader[7]
        jpeg = read_exact(jpeg_size)
        read_exact(padding_size)
        yield jpeg
    elif payload_type == 0x02:
        # frame info — pheader の jpeg_size + padding ぶん読み飛ばし
        ...
    else:
        # unknown — Transfer-Encoding が壊れた or プロトコルずれ。再接続が安全
        ...
```

実機 (RX100M5A、Smart Remote Control __SAK__ v2.1.7): **640×424 / baseline JPEG /
~35KB / 12〜15 fps**。`startLiveviewWithSize("L")` 等のサイズ指定 API は available
リストに含まれないため、現状サイズは固定。

## 7. 撮影レスポンスの postview URL

`actTakePicture` 成功時の `result`:

```json
{"result":[["http://192.168.122.1:8080/postview/...JPG"]],"id":N}
```

URL を GET すると **本体カードに保存された JPEG の縮小版** が返る (オリジナル
ではない、~2MP / ~500KB)。オリジナルは `setCameraFunction("Contents Transfer")`
へ移行して `getContentList` 経由で取得する。

## 8. 既知の制約 / ハマりどころ

- **モード遷移が遅い**: `setCameraFunction` 直後は `getAvailableApiList` の
  返りが間に合わないことがある。`getEvent` で `cameraFunction` が目的値に
  なるまで待つのが堅実。
- **getEvent と他コマンドの直列化**: long-poll `getEvent` が走っている
  間に同じソケットで他コマンドを送ると `40400 Already Polling` が返る。
  long-poll 用と通常コマンド用で **HTTP コネクションを分ける** か、
  `getEvent` を short-poll にする。
- **ライブビュー HTTP コネクションは長時間張りっぱなし**: タイムアウト系
  の例外を握って再接続するのが必須。`stopLiveview` で URL は無効化される。
- **RX100M5A の Smart Remote 対応 API は限定的**: 例えば動画記録設定
  (`setMovieFileFormat` 等) は未対応で `12 No Such Method`。事前に
  `getAvailableApiList` で確認するのが安全。
- **時刻同期**: 起動直後にカメラ側時刻が未設定だと `getContentList` の
  `createdTime` が当てにならない。`setCurrentTime` を 1 度叩いておく。
- **postview URL は短命**: 撮影直後に GET しないと 404 になることがある。
- **`setTouchAFPosition` は RX100M5A の Smart Remote Control __SAK__
  v2.1.7 では未実装**: `getMethodTypes` 全バージョンで該当メソッドが登録
  されておらず、v1.0 で叩くと `[1, '']` (silent reject)、v1.1 で叩くと
  `[12, 'setTouchAFPosition']` (No Such Method)。AF 位置は **本体 Focus
  Area で固定** し、API 側からは `actHalfPressShutter` /
  `cancelHalfPressShutter` で半押し AF させるしかない。
- **同様に `setShootMode` / `setFocusMode` / `setShutterSpeed` /
  `setFNumber` / `setStillSize` 等の "本体設定で決まる系" も
  available_apis に入らない**。露出補正・ISO・WB などは API 側から変更可能。

## 9. 参考 (公開資料)

- Sony Camera Remote API beta SDK / API リファレンス (developer.sony.com、現 EOL)
- Sony 提供のサンプル `CameraRemoteSampleApp` (Android, GitHub mirror が多数)
- 各サービス JSON スキーマは `getMethodTypes` で取得可能
