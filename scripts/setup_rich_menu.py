"""
LINE リッチメニューを登録するセットアップスクリプト。
初回 1 回だけローカルで実行すれば、以後は自動的にメニューが表示される。

必要な環境変数 (.env):
  - LINE_CHANNEL_ACCESS_TOKEN

使い方:
  python scripts/setup_rich_menu.py

メニューレイアウト (2500 x 1686 px / 2行×2列):
  ┌──────────────────────┬──────────────────────┐
  │   🔍 分析開始          │ 🏃 明日のメニュー       │
  │  (Postback)           │  (Postback)           │
  ├──────────────────────┼──────────────────────┤
  │  📊 今週の傾向          │  ⚖️ 体重同期            │
  │  (Postback)           │  (Postback)           │
  └──────────────────────┴──────────────────────┘
"""

import os
import sys
import json
import logging
from io import BytesIO

import requests
from dotenv import load_dotenv

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False
    print("[WARN] Pillow が未インストールです。画像生成をスキップし、別途手動で画像をアップロードしてください。")

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 設定
# ------------------------------------------------------------------

TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
}
BASE_URL = "https://api.line.me"

# リッチメニューの寸法（2行×2列レイアウト）
W, H = 2500, 1686
ROW_H = H // 2       # 1行の高さ: 843px
COL_W = W // 2       # 1列の幅: 1250px

# 各セルの左上座標
TOP_LEFT_X,    TOP_LEFT_Y    = 0,     0
TOP_RIGHT_X,   TOP_RIGHT_Y   = COL_W, 0
BOT_LEFT_X,    BOT_LEFT_Y    = 0,     ROW_H
BOT_RIGHT_X,   BOT_RIGHT_Y   = COL_W, ROW_H

# カラーパレット
COLOR_BG       = (30, 30, 30)        # ダークグレー背景
COLOR_COL1     = (46, 125, 50)       # 濃い緑（分析開始）
COLOR_COL2     = (21, 101, 192)      # 濃い青（明日のメニュー）
COLOR_COL3     = (106, 27, 154)      # 紫（今週の傾向）
COLOR_COL4     = (183, 28, 28)       # 深紅（体重同期）
COLOR_DIVIDER  = (255, 255, 255)     # 白い区切り線
COLOR_TEXT     = (255, 255, 255)     # 白テキスト

# リッチメニュー JSON 定義
RICH_MENU_PAYLOAD = {
    "size": {"width": W, "height": H},
    "selected": True,
    "name": "ゴリラコーチメニュー",
    "chatBarText": "🦍 ゴリラコーチ",
    "areas": [
        {
            "bounds": {"x": TOP_LEFT_X, "y": TOP_LEFT_Y, "width": COL_W, "height": ROW_H},
            "action": {
                "type": "postback",
                "label": "分析開始",
                "data": "action=llm_analysis",
                "displayText": "🔍 分析開始！",
            },
        },
        {
            "bounds": {"x": TOP_RIGHT_X, "y": TOP_RIGHT_Y, "width": COL_W, "height": ROW_H},
            "action": {
                "type": "postback",
                "label": "明日のメニューを詳しく",
                "data": "action=tomorrow_plan",
                "displayText": "🏃 明日のメニューを詳しく！",
            },
        },
        {
            "bounds": {"x": BOT_LEFT_X, "y": BOT_LEFT_Y, "width": COL_W, "height": ROW_H},
            "action": {
                "type": "postback",
                "label": "今週の傾向を見る",
                "data": "action=weekly_trend",
                "displayText": "📊 今週の傾向を見る！",
            },
        },
        {
            "bounds": {"x": BOT_RIGHT_X, "y": BOT_RIGHT_Y, "width": COL_W, "height": ROW_H},
            "action": {
                "type": "postback",
                "label": "体重を今すぐ同期",
                "data": "action=weight_sync",
                "displayText": "⚖️ 体重を今すぐ同期！",
            },
        },
    ],
}


# ------------------------------------------------------------------
# 画像生成
# ------------------------------------------------------------------

def _load_font(size: int) -> "ImageFont.FreeTypeFont | ImageFont.ImageFont":
    """日本語フォントを優先して読み込む。なければデフォルト。"""
    font_candidates = [
        # Windows
        r"C:\Windows\Fonts\YuGothB.ttc",
        r"C:\Windows\Fonts\msgothic.ttc",
        r"C:\Windows\Fonts\meiryo.ttc",
        # Linux (GitHub Actions)
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for path in font_candidates:
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError):
            continue
    return ImageFont.load_default()


def _draw_button(draw: "ImageDraw.ImageDraw", x0: int, y0: int, x1: int, y1: int,
                 bg_color: tuple, icon: str, label: str, sub_label: str) -> None:
    """ボタン領域を描画する。"""
    # 背景塗りつぶし
    draw.rectangle([x0, y0, x1, y1], fill=bg_color)

    cx = (x0 + x1) // 2
    cy = (y0 + y1) // 2

    # アイコン（大きめ絵文字風テキスト）
    font_icon = _load_font(200)
    draw.text((cx, cy - 120), icon, font=font_icon, fill=COLOR_TEXT, anchor="mm")

    # メインラベル
    font_main = _load_font(90)
    draw.text((cx, cy + 100), label, font=font_main, fill=COLOR_TEXT, anchor="mm")

    # サブラベル
    font_sub = _load_font(55)
    draw.text((cx, cy + 210), sub_label, font=font_sub, fill=(200, 200, 200), anchor="mm")


def generate_rich_menu_image() -> bytes:
    """リッチメニュー画像を生成して PNG バイト列で返す。（2行×2列レイアウト）"""
    img = Image.new("RGB", (W, H), color=COLOR_BG)
    draw = ImageDraw.Draw(img)

    # 上段左: 分析開始（緑）
    _draw_button(
        draw, TOP_LEFT_X, TOP_LEFT_Y, COL_W - 2, ROW_H - 2,
        COLOR_COL1,
        "🔍", "分析開始", "今日の全データを総括",
    )

    # 上段右: 明日のメニュー（青）
    _draw_button(
        draw, TOP_RIGHT_X + 2, TOP_RIGHT_Y, W, ROW_H - 2,
        COLOR_COL2,
        "🏃", "明日のメニュー", "翌日のトレーニング計画",
    )

    # 下段左: 今週の傾向（紫）
    _draw_button(
        draw, BOT_LEFT_X, BOT_LEFT_Y + 2, COL_W - 2, H,
        COLOR_COL3,
        "📊", "今週の傾向", "直近7日の運動・体重・睡眠",
    )

    # 下段右: 体重同期（深紅）
    _draw_button(
        draw, BOT_RIGHT_X + 2, BOT_RIGHT_Y + 2, W, H,
        COLOR_COL4,
        "⚖️", "体重同期", "Eufyから今すぐ取得",
    )

    # 縦の区切り線（中央）
    draw.rectangle([COL_W - 2, 0, COL_W + 2, H], fill=COLOR_DIVIDER)
    # 横の区切り線（中央）
    draw.rectangle([0, ROW_H - 2, W, ROW_H + 2], fill=COLOR_DIVIDER)

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ------------------------------------------------------------------
# LINE API 操作
# ------------------------------------------------------------------

def create_rich_menu() -> str:
    """リッチメニューを作成して richMenuId を返す。"""
    resp = requests.post(
        f"{BASE_URL}/v2/bot/richmenu",
        headers=HEADERS,
        json=RICH_MENU_PAYLOAD,
        timeout=15,
    )
    resp.raise_for_status()
    rich_menu_id = resp.json()["richMenuId"]
    logger.info("リッチメニュー作成完了: %s", rich_menu_id)
    return rich_menu_id


def upload_rich_menu_image(rich_menu_id: str, image_bytes: bytes) -> None:
    """リッチメニューに画像をアップロードする。"""
    resp = requests.post(
        f"https://api-data.line.me/v2/bot/richmenu/{rich_menu_id}/content",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "image/png",
        },
        data=image_bytes,
        timeout=30,
    )
    resp.raise_for_status()
    logger.info("リッチメニュー画像アップロード完了")


def set_default_rich_menu(rich_menu_id: str) -> None:
    """リッチメニューをデフォルトとして設定する。"""
    resp = requests.post(
        f"{BASE_URL}/v2/bot/user/all/richmenu/{rich_menu_id}",
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=15,
    )
    resp.raise_for_status()
    logger.info("デフォルトリッチメニューを設定しました: %s", rich_menu_id)


def delete_existing_rich_menus() -> None:
    """既存のリッチメニューをすべて削除する（重複防止）。"""
    resp = requests.get(
        f"{BASE_URL}/v2/bot/richmenu/list",
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=15,
    )
    resp.raise_for_status()
    menus = resp.json().get("richmenus", [])
    for menu in menus:
        mid = menu.get("richMenuId", "")
        del_resp = requests.delete(
            f"{BASE_URL}/v2/bot/richmenu/{mid}",
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=15,
        )
        if del_resp.status_code == 200:
            logger.info("既存リッチメニューを削除: %s", mid)
        else:
            logger.warning("削除失敗 (%s): %s %s", mid, del_resp.status_code, del_resp.text)


# ------------------------------------------------------------------
# エントリーポイント
# ------------------------------------------------------------------

def main() -> None:
    if not TOKEN:
        logger.error("LINE_CHANNEL_ACCESS_TOKEN が未設定です。.env を確認してください。")
        sys.exit(1)

    logger.info("===== LINE リッチメニュー設定開始 =====")

    # 既存メニューを削除
    delete_existing_rich_menus()

    # リッチメニューを作成
    rich_menu_id = create_rich_menu()

    # 画像を生成してアップロード
    if HAS_PILLOW:
        logger.info("リッチメニュー画像を生成中...")
        image_bytes = generate_rich_menu_image()
        upload_rich_menu_image(rich_menu_id, image_bytes)
    else:
        logger.warning(
            "Pillow 未インストールのため画像生成をスキップしました。\n"
            "手動で 2500×843 の PNG 画像を以下の URL にアップロードしてください:\n"
            "  POST https://api-data.line.me/v2/bot/richmenu/%s/content",
            rich_menu_id,
        )

    # デフォルトメニューに設定
    set_default_rich_menu(rich_menu_id)

    logger.info("===== 設定完了 =====")
    logger.info("richMenuId: %s", rich_menu_id)
    logger.info("LINE Official Account を開いてメニューが表示されることを確認してください。")


if __name__ == "__main__":
    main()
