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

# 17Track v2.4 Endpoints
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
    # Mở worksheet tên là "Data"
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
    print(f"🚀 [VibeCoder - Full Action] Bắt đầu lúc: {datetime.now()}")
    
    try:
        sheet = get_google_sheet()
        records = sheet.get_all_records()
        print(f"✅ Đã kết nối Sheet, tìm thấy {len(records)} dòng.")
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
        print("📭 Không tìm thấy mã vận đơn hợp lệ.")
        return

    headers = {"Content-Type": "application/json", "17token": TRACK17_API_KEY}
    updates = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Chia batch 40
    for i in range(0, len(tracking_list), 40):
        batch = tracking_list[i:i+40]
        print(f"📦 Đang xử lý Batch {i//40 + 1} ({len(batch)} mã)...")
        
        try:
            # --- BƯỚC 1: HÀNH ĐỘNG ĐĂNG KÝ (REGISTER) ---
            print(f"   -> Đang đăng ký mã với 17Track...")
            reg_resp = requests.post(REGISTER_URL, json=batch, headers=headers)
            # Chúng ta không cần đợi kết quả register quá lâu, cứ đăng ký rồi đi tiếp
            
            time.sleep(2) # Nghỉ 2s để 17Track kịp khởi tạo dữ liệu

            # --- BƯỚC 2: HÀNH ĐỘNG LẤY THÔNG TIN (GET INFO) ---
            print(f"   -> Đang lấy thông tin hành trình...")
            info_resp = requests.post(TRACK_INFO_URL, json=batch, headers=headers)
            res_data = info_resp.json()
            
            data_body = res_data.get("data", {})
            accepted = data_body.get("accepted", [])
            rejected = data_body.get("rejected", [])

            # Xử lý các mã được nhận (Accepted)
            for item in accepted:
                num = item.get("number")
                info = item.get("track_info") or {}
                stt_obj = info.get("latest_status") or {}
                
                # Nếu mới đăng ký, status thường là "NotFound" hoặc "Pending"
                current_stt = stt_obj.get("status", "Registered/Pending")

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
                        transit_at = transit_at = t_utc

                sla_val = calculate_sla(label_at, transit_at)
                ridx = row_mapping.get(num)
                if ridx:
                    updates.append({
                        'range': f'C{ridx}:G{ridx}',
                        'values': [[current_stt, label_at, transit_at, sla_val, now_str]]
                    })

            # Xử lý các mã bị từ chối (Rejected)
            for item in rejected:
                num = item.get("number")
                msg = item.get("error", {}).get("message", "Rejected")
                ridx = row_mapping.get(num)
                if ridx:
                    # Ghi lý do bị từ chối lên Sheet để người dùng biết
                    updates.append({
                        'range': f'C{ridx}:G{ridx}',
                        'values': [[f"Error: {msg}", "", "", "", now_str]]
                    })

            time.sleep(1)

        except Exception as e:
            print(f"⚠️ Lỗi xử lý batch: {e}")

    # --- BƯỚC 3: HÀNH ĐỘNG GHI LÊN GOOGLE SHEET ---
    if updates:
        print(f"📝 Đang ghi {len(updates)} hành động lên Google Sheet...")
        sheet.batch_update(updates)
        print("✅ Hoàn tất cập nhật file Google Sheet!")
    else:
        print("ℹ️ Không có dữ liệu nào được ghi.")

if __name__ == "__main__":
    run_sync()
