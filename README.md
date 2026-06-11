# ログゴリ

Garmin・Google Fit・LINE・Gemini を連携したヘルスケア自動通知システム。

## アーキテクチャ

- **GitHub Actions** - データ取得・通知（1時間ごと）/ LLM分析（22:00自動 or 手動）
- **Google Sheets** - 履歴データ・実行状態・Garminセッション・分析ログの永続化
- **Render.com** - LINE Webhookを受け取りGitHub Actionsを起動する中継サーバー

### 分析の品質担保（サブエージェント）

LLM分析は `AnalysisAgent`（`src/analysis_agent.py`）が「生成 → 批評 → 再生成」を制御する:

1. **生成**: `GeminiClient.generate()` で分析を作成する
2. **批評**: `GeminiClient.critique()` が元データと照合し、`{pass, issues}` で品質を監査する
   - action_plan/menu に具体的な数値があるか
   - 最重要課題が前回の使い回しでないか
   - 各記述が実データの数値を根拠にしているか
   - （日次のみ）コーチの戯言が前回と重複していないか
3. **再生成**: 不合格なら指摘事項を修正指示として付加し、1回だけ作り直す（最大3コール）

再生成回数と検出された不備は `analysis_log` シートの `retry_count` / `critic_issues` に記録される。

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

スプレッドシートを新規作成し、以下のシートを用意する。

- `daily_summary`
- `status`
- `session`

シートIDを `.env` の `GOOGLE_SHEETS_ID` に設定する。
（`analysis_log` シートは LLM 分析結果の履歴保存用で、初回実行時に自動作成される）

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
