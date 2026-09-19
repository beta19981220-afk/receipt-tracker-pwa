import os 
import json 
from datetime import datetime

#Googleサービスとの連携やAI呼び出しに使う外部ライブラリ
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread
from google import genai
from google.genai import types

import io
from googleapiclient.http import MediaIoBaseDownload

#GitHub Secretsに登録した情報を、Pythonの中に取り込み
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
RECEIPT_FOLDER_ID = "1ZLlpLJFJFMMZt3hszoCJBPGwO3BfmW3i"

"""
実際にGoogleへ接続するための「認証情報」と、
Drive・Sheets・Geminiそれぞれの「窓口(クライアント)」を作ります
"""
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

#認証情報の作成
service_account_info = json.loads(SERVICE_ACCOUNT_JSON)
credentials = Credentials.from_service_account_info(service_account_info, 
                                                    scopes=SCOPES)
#上記で作った認証情報でアクセス
drive_service = build("drive", "v3", credentials=credentials)
gspread_client = gspread.authorize(credentials)
sheet = gspread_client.open_by_key(SPREADSHEET_ID).sheet1
#GeminiはAPIキーでアクセスしている
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

#、Driveの中に「処理済み」というフォルダを自動で探して、なければ作る処理
def get_or_create_processed_folder():
    query = (
        f"'{RECEIPT_FOLDER_ID}' in parents "
        "and name = '処理済み' "
        "and mimeType = 'application/vnd.google-apps.folder' "
        "and trashed = false"
    )
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    folders = results.get("files", [])

    if folders:
        return folders[0]["id"]

    folder_metadata = {
        "name": "処理済み",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [RECEIPT_FOLDER_ID],
    }
    folder = drive_service.files().create(body=folder_metadata, fields="id").execute()
    return folder["id"]



#レシートフォルダの中にある「未処理の画像ファイル一覧」を取得する関数
def get_unprocessed_files():
    query = (
        f"'{RECEIPT_FOLDER_ID}' in parents "
        "and mimeType contains 'image/' "
        "and trashed = false"
    )
    results = drive_service.files().list(
        q=query, fields="files(id, name, mimeType)"
    ).execute()
    return results.get("files", [])

#ファイルの中身(画像データ本体)をダウンロードする処理
def download_file(file_id):
    request = drive_service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)

    done = False
    while not done:
        status, done = downloader.next_chunk()

    buffer.seek(0)
    return buffer.read()


#画像をGeminiに送って構造化データを取り出す関数
def extract_receipt_data(image_bytes, mime_type):
    prompt = """
あなたはレシート画像を解析する会計アシスタントです。
画像から以下の情報を読み取り、JSON形式だけで出力してください。
説明文や前置きは不要です。

{
  "date": "YYYY-MM-DD形式の日付。読み取れなければnull",
  "store": "店名",
  "items": [
    {
      "name": "商品名",
      "amount": 金額(数値のみ。カンマや円マークは含めない),
      "category": "食費・日用品・交通費・交際費・その他 のいずれか"
    }
  ]
}

itemsには、レシートに書かれている商品を1つずつ全て含めてください。
"""

    response = gemini_client.models.generate_content(
        model="gemini-3.6-flash",
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            prompt,
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )

    return json.loads(response.text)


#抽出したデータをスプレッドシートに書き込む
def append_to_sheet(data, file_name):
    date = data.get("date")
    store = data.get("store")
    items = data.get("items", [])

    rows = []
    for item in items:
        rows.append([
            date,
            store,
            item.get("name"),
            item.get("amount"),
            item.get("category"),
            file_name,
        ])

    if rows:
        sheet.append_rows(rows)


#処理が終わった画像を「処理済み」フォルダへ移動する
def move_to_processed(file_id, processed_folder_id):
    file = drive_service.files().get(fileId=file_id, fields="parents").execute()
    previous_parents = ",".join(file.get("parents", []))

    drive_service.files().update(
        fileId=file_id,
        addParents=processed_folder_id,
        removeParents=previous_parents,
        fields="id, parents",
    ).execute()

#デバック
def debug_list_visible_files():
    results = drive_service.files().list(
        pageSize=20, fields="files(id, name, mimeType, parents, trashed)"
    ).execute()
    files = results.get("files", [])
    print(f"サービスアカウントから見えるファイル数: {len(files)}")
    for f in files:
        print(f"  - 名前: {f['name']} / 種類: {f['mimeType']} / 親フォルダ: {f.get('parents')} / ゴミ箱: {f.get('trashed')}")
#本体
def main():
    debug_list_visible_files()  # ← 一時的に追加

    processed_folder_id = get_or_create_processed_folder()
    files = get_unprocessed_files()

    if not files:
        print("未処理のファイルはありません")
        return

    print(f"{len(files)}件の未処理ファイルを検出しました")

    for file in files:
        file_id = file["id"]
        file_name = file["name"]
        mime_type = file["mimeType"]

        print(f"処理中: {file_name}")

        try:
            image_bytes = download_file(file_id)
            data = extract_receipt_data(image_bytes, mime_type)
            append_to_sheet(data, file_name)
            move_to_processed(file_id, processed_folder_id)
            print(f"  → 完了: {file_name}")

        except Exception as e:
            print(f"  → エラー発生、スキップします: {file_name} ({e})")
            continue


if __name__ == "__main__":
    main()
