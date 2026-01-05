# Discord Match Manager Bot

即席マッチ用に、ボイスチャンネルを自動生成し、チーム別VCへメンバーを自動移動するDiscord Botです。 [`/startmatch` `/endmatch` `/move` `/match status` `/match lock` `/match transfer` `/swap` `/match timer`] を提供します。 

## 機能

- `/startmatch team1:@A @B team2:@C @D move:allow|deny`
  - `Match-1234` の親VCカテゴリ（またはカテゴリ）と、チーム別VCを作成
  - 指定ユーザーを各チームVCへ移動
  - `move` 設定を保存
- `/endmatch`
  - 作成者なら全員を元VCへ戻してMatch用VCを削除
  - 作成者以外なら作成者にDMで承認リクエスト（ボタン付き）
- `/move`
  - `move:allow`：自分をチームへ移動
  - `move:deny`：作成者のみ、指定ユーザーをチームへ移動
- `/match status`：状態確認（作成者/チーム/設定/経過時間）
- `/match lock` `/match unlock`：ロック中はVC入室・移動不可
- `/match transfer user:@User`：作成者権限の譲渡
- `/swap user1:@A user2:@B`：チーム間の入れ替え
- `/match timer start 20m` `/match timer stop`：試合タイマー（5分前通知、終了時自動終了はオプション）

## セットアップ

### 1. Bot作成

Discord Developer PortalでBotを作成し、トークンを取得します。

### 2. 権限

必要な権限（招待リンク作成時）：
- `applications.commands`
- `bot` スコープ
- Bot権限: `Manage Channels`, `Move Members`, `View Channels`, `Connect`, `Send Messages`

### 3. 環境変数

`.env` を作成:

```env
DISCORD_TOKEN=xxxxxxxx
GUILD_ID=123456789012345678
```

### 4. 起動

```bash
python -m venv .venv
source .venv/bin/activate  # Windowsは .venv\\Scripts\\activate
pip install -r requirements.txt
python main.py
```

## 注意

- このBotは「1サーバー内で1つのMatchを同時進行」という前提で実装しています（複数同時進行にしたい場合はIssue歓迎）。
- 永続化はSQLiteに差し替えやすいように、現状はメモリ内保存です。
