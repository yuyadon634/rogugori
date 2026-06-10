# ログゴリ

Garmin・Google Fit・LINE・Gemini を連携したヘルスケア自動通知システム。

## アーキテクチャ

- **GitHub Actions** - データ取得・通知（1時間ごと）/ LLM分析（22:00自動 or 手動）
- **Google Sheets** - 履歴データ・実行状態・Garminセッションの永続化
- **Render.com** - LINE Webhookを受け取りGitHub Actionsを起動する中継サーバー

## 通知の種類

| 通知 | タイミング |
|------|-----------|
| 睡眠レポート | 毎朝6:00〜8:00 |
| アクティビティ通知 | 運動データ検出時に即時 |
| 休養日通知 | 23:00以降・その日に運動なしの場合 |
| 体重通知 | 体重データ検出時に即時 |
| LLM分析 | 手動（LINEボタン）or 22:00自動 |

## セットアップ

### 1. 依存ライブラリのインストール

```bash
py -m pip install -r requirements.txt
```

### 2. 環境変数の設定

`.env.example` をコピーして `.env` を作成し、各キーを設定する。

```bash
cp .env.example .env
```

### 3. Google Sheets の準備

スプレッドシートを新規作成し、以下の3シートを用意する。

- `daily_summary`
- `status`
- `session`

シートIDを `.env` の `GOOGLE_SHEETS_ID` に設定する。

### 4. Google Fit OAuth の初回認証

`credentials.json`（Google Cloud Consoleからダウンロード）をプロジェクトルートに配置して実行する。初回のみブラウザで認証が必要。

### 5. GitHub Secrets の設定

`.env` の全キーを GitHub リポジトリの Secrets に登録する。

### 6. Render.com デプロイ

`webhook/` ディレクトリを Render.com の Web Service としてデプロイする。
環境変数に `GITHUB_TOKEN` と `GITHUB_REPO` を設定する。

LINE Webhook URL に Render.com の URL を設定する。

### 7. LINE リッチメニューの設定

ローカルで 1 回だけ実行する。Eufy スケール × Garmin × 睡眠を総括分析する「分析開始」ボタンが LINE に表示される。

```bash
py scripts/setup_rich_menu.py
```

| ボタン | アクション |
|--------|-----------|
| 🔍 分析開始 | Postback → GitHub Actions → Gemini 分析 → LINE 返信 |
| 📊 今日のデータ確認 | 「今日の分析」テキストを送信 |
