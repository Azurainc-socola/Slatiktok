import os
import json
import time
import requests
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

# ==========================================
# CẤU HÌNH API & HỆ THỐNG
# ==========================================
TRACK17_API_KEY = os.getenv("TRACK17_API_KEY")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
GCP_JSON_STR = os.getenv("GCP_JSON")

TRACK17_URL = "https://api.17track.net/track/v2.4/gettrackinfo"
USPS_CARRIER_CODE = 21051  # ID chuẩn của USPS

def get_google_sheet():
    """Đăng nhập Google Sheet"""
    if not GCP_JSON_STR:
        raise ValueError("❌ Lỗi: Thiếu GCP_JSON trong Env Var.")
    creds_dict = json.loads(GCP_JSON_STR)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    )
    client = gspread.authorize(creds)
    
    # LƯU Ý: Đảm bảo Tab trong file Google Sheet của bạn tên là 'Data'
    # Nếu tên tab là 'Sheet1' thì bạn sửa chữ 'Data' thành 'Sheet1' nhé
    return client.open_by_key(SPREADSHEET_ID).worksheet("Data")

def calculate_sla(label_at, transit_at):
    """Tính toán cảnh báo SLA"""
    if not label_at or not transit_at:
        return ""
    try:
        t1 = datetime.fromisoformat(label_at.replace('Z', '+00:00'))
        t2 = datetime.fromisoformat(transit_at.replace('Z', '+00:00'))
        
        diff = t2 - t1
        hours = diff.total_seconds() / 3600
        
        if hours <= 24: return "✅ EXCELLENT (<24h)"
        if hours <= 48: return "⚡ GOOD (<48h)"
        return f"⚠️ DELAY ({int(hours)}h)"
    except:
        return "N/A"

def run_sync():
    print(f"🚀 [USPS Mode] Bắt đầu quét lúc: {datetime.now()}")
    
    try:
        sheet = get_google_sheet()
        records = sheet.get_all_records()
    except Exception as e:
        print(f"❌ Lỗi truy cập Sheet: {e}")
        return

    tracking_list = []
    row_mapping = {}

    # Bước 1: Thu thập mã từ Sheet
    for idx, row in enumerate(records, start=2):
        # 🟢 ĐÃ FIX: Lấy đúng tên cột Tracking_Number theo file CSV
        num = str(row.get('Tracking_Number', '')).strip()
        
        # Lọc các mã hợp lệ (mã bạn gửi là USPS bắt đầu bằng 92, dài 22 số)
        if num and len(num) > 10:
            tracking_list.append({"number": num, "carrier": USPS_CARRIER_CODE})
            row_mapping[num] = idx

    if not tracking_list:
        print("📭 Không có mã nào hợp lệ để quét. Hãy kiểm tra lại tên cột trong file Sheet.")
        return

    # Bước 2: Gọi API 17Track v2.4 (Batch 40)
    headers = {"Content-Type": "application/json", "17token": TRACK17_API_KEY}
    updates = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    for i in range(0, len(tracking_list), 40):
        batch = tracking_list[i:i+40]
        print(f"📦 Batch {i//40 + 1}: Đang check {len(batch)} mã...")
        
        try:
            resp = requests.post(TRACK17_URL, json=batch, headers=headers)
            res_data = resp.json()
            
            if res_data.get("code") != 0:
                print(f"❌ Lỗi API: {res_data.get('msg')}")
                continue

            accepted = res_data.get("data", {}).get("accepted", [])
            for item in accepted:
                num = item.get("number")
                info = item.get("track_info", {})
                if not info: continue

                # Lấy trạng thái mới nhất
                stt_obj = info.get("latest_status", {})
                current_stt = stt_obj.get("status", "NotFound")

                # Tìm mốc thời gian trong Events (v2.4)
                label_at = ""
                transit_at = ""
                providers = info.get("tracking", {}).get("providers", [])
                events = providers[0].get("events", []) if providers else []

                for ev in sorted(events, key=lambda x: x.get("time_utc", "")):
                    desc = ev.get("description", "").lower()
                    time_utc = ev.get("time_utc", "")
                    
                    if not time_utc: continue

                    if ("label created" in desc or "info received" in desc or "shipping info received" in desc) and not label_at:
                        label_at = time_utc
                    if ("in transit" in desc or "accepted" in desc or "picked up" in desc) and not transit_at:
                        transit_at = time_utc

                # Tính SLA
                sla_val = calculate_sla(label_at, transit_at)

                # Chuẩn bị update lên Sheet (Khớp hoàn toàn với Cột C -> G trong file của bạn)
                ridx = row_mapping.get(num)
                if ridx:
                    updates.append({
                        'range': f'C{ridx}:G{ridx}',
                        'values': [[current_stt, label_at, transit_at, sla_val, now_str]]
                    })

            time.sleep(0.5)

        except Exception as e:
            print(f"⚠️ Lỗi Batch: {e}")

    # Bước 3: Đổ dữ liệu về Sheet
    if updates:
        print(f"📝 Đang ghi {len(updates)} dòng...")
        sheet.batch_update(updates)
        print("✅ Done!")
    else:
        print("ℹ️ Không có gì để cập nhật.")

if __name__ == "__main__":
    run_sync()
