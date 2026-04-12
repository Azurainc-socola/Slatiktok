import os
import json
import time
import requests
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

# ==========================================
# CẤU HÌNH HỆ THỐNG
# ==========================================
TRACK17_API_KEY = os.getenv("TRACK17_API_KEY")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
GCP_JSON_STR = os.getenv("GCP_JSON")

# 2 Endpoint cần thiết cho v2.4
REGISTER_URL = "https://api.17track.net/track/v2.4/register"
TRACK_INFO_URL = "https://api.17track.net/track/v2.4/gettrackinfo"
USPS_CARRIER_CODE = 21051  

def get_google_sheet():
    if not GCP_JSON_STR:
        raise ValueError("❌ Lỗi: Thiếu GCP_JSON trong Env Var.")
    creds_dict = json.loads(GCP_JSON_STR)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    )
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).worksheet("Data")

def calculate_sla(label_at, transit_at):
    if not label_at or not transit_at: return ""
    try:
        t1 = datetime.fromisoformat(label_at.replace('Z', '+00:00'))
        t2 = datetime.fromisoformat(transit_at.replace('Z', '+00:00'))
        hours = (t2 - t1).total_seconds() / 3600
        if hours <= 24: return "✅ EXCELLENT (<24h)"
        if hours <= 48: return "⚡ GOOD (<48h)"
        return f"⚠️ DELAY ({int(hours)}h)"
    except: return "N/A"

def run_sync():
    print(f"🚀 [USPS Auto-Register] Bắt đầu quét lúc: {datetime.now()}")
    
    try:
        sheet = get_google_sheet()
        records = sheet.get_all_records()
    except Exception as e:
        print(f"❌ Lỗi truy cập Sheet: {e}")
        return

    tracking_list = []
    row_mapping = {}

    for idx, row in enumerate(records, start=2):
        num = str(row.get('Tracking_Number', '')).strip()
        if num and len(num) > 10:
            tracking_list.append({"number": num, "carrier": USPS_CARRIER_CODE})
            row_mapping[num] = idx

    if not tracking_list:
        print("📭 Không có mã nào để xử lý.")
        return

    headers = {"Content-Type": "application/json", "17token": TRACK17_API_KEY}
    updates = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Chia batch 40 số để xử lý
    for i in range(0, len(tracking_list), 40):
        batch = tracking_list[i:i+40]
        print(f"📦 Batch {i//40 + 1}: Xử lý {len(batch)} mã...")
        
        try:
            # BƯỚC 1: ĐĂNG KÝ (REGISTER)
            # 17Track yêu cầu mã phải được đăng ký trước khi lấy thông tin
            reg_resp = requests.post(REGISTER_URL, json=batch, headers=headers)
            reg_data = reg_resp.json()
            if reg_data.get("code") == 0:
                print(f"   ✅ Đăng ký thành công {len(batch)} mã với 17Track.")
            else:
                print(f"   ⚠️ Lưu ý đăng ký: {reg_data.get('msg')}")

            # Đợi 1 chút để hệ thống 17Track ghi nhận
            time.sleep(1)

            # BƯỚC 2: LẤY THÔNG TIN (GET INFO)
            info_resp = requests.post(TRACK_INFO_URL, json=batch, headers=headers)
            res_data = info_resp.json()
            
            if res_data.get("code") != 0:
                print(f"   ❌ Lỗi GetInfo: {res_data.get('msg')}")
                continue

            accepted = res_data.get("data", {}).get("accepted", [])
            for item in accepted:
                num = item.get("number")
                info = item.get("track_info") or {}
                stt_obj = info.get("latest_status") or {}
                
                # Trạng thái thô từ 17Track
                status_raw = stt_obj.get("status", "Pending")
                
                # Tìm mốc thời gian Events
                label_at, transit_at = "", ""
                providers = info.get("tracking", {}).get("providers", []) if info else []
                events = providers[0].get("events", []) if providers else []

                for ev in sorted(events, key=lambda x: x.get("time_utc", "")):
                    desc = ev.get("description", "").lower()
                    t_utc = ev.get("time_utc", "")
                    if not t_utc: continue
                    if ("label created" in desc or "info received" in desc) and not label_at:
                        label_at = t_utc
                    if ("in transit" in desc or "accepted" in desc or "picked up" in desc) and not transit_at:
                        transit_at = t_utc

                sla_val = calculate_sla(label_at, transit_at)
                ridx = row_mapping.get(num)
                if ridx:
                    updates.append({
                        'range': f'C{ridx}:G{ridx}',
                        'values': [[status_raw, label_at, transit_at, sla_val, now_str]]
                    })

            # Tránh spam API
            time.sleep(1)

        except Exception as e:
            print(f"⚠️ Lỗi xử lý batch: {e}")

    # BƯỚC 3: CẬP NHẬT SHEET
    if updates:
        print(f"📝 Đang ghi {len(updates)} kết quả lên Sheet...")
        sheet.batch_update(updates)
        print("✅ Done!")
    else:
        print("ℹ️ Đã đăng ký mã nhưng 17Track chưa có dữ liệu ngay. Hãy đợi 15-30p rồi chạy lại.")

if __name__ == "__main__":
    run_sync()
