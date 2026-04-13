import os
import json
import time
import requests
import argparse
from datetime import datetime
import pytz
import gspread
from google.oauth2.service_account import Credentials

# ==========================================
# CẤU HÌNH BIẾN MÔI TRƯỜNG
# ==========================================
TRACK17_API_KEY = os.getenv("TRACK17_API_KEY")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
GCP_JSON_STR = os.getenv("GCP_JSON")

REGISTER_URL = "https://api.17track.net/track/v2.4/register"
TRACK_INFO_URL = "https://api.17track.net/track/v2.4/gettrackinfo"
USPS_CARRIER_CODE = 21051
VN_TZ = pytz.timezone('Asia/Ho_Chi_Minh')

def get_google_sheet():
    creds_dict = json.loads(GCP_JSON_STR)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    )
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).worksheet("Data")

def to_vn_time(iso_str):
    """Chuyển đổi thời gian từ 17Track (UTC) sang GMT+7"""
    if not iso_str: return None
    try:
        # 17Track trả về dạng 2024-04-12T05:14:00Z hoặc tương tự
        dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        return dt.astimezone(VN_TZ)
    except:
        return None

def calculate_sla_hours(label_at_str, transit_at_str):
    """Tính SLA theo giờ (Số nguyên), quy đổi về GMT+7"""
    if not label_at_str: return ""
    
    try:
        # Parse thời gian từ Sheet (giả định đã lưu dạng GMT+7)
        fmt = "%Y-%m-%d %H:%M"
        t_label = datetime.strptime(label_at_str[:16], fmt).replace(tzinfo=VN_TZ)
        
        if transit_at_str:
            t_end = datetime.strptime(transit_at_str[:16], fmt).replace(tzinfo=VN_TZ)
        else:
            t_end = datetime.now(VN_TZ)
            
        diff = t_end - t_label
        return int(diff.total_seconds() / 3600)
    except:
        return ""

def run_register(sheet, records):
    """Chế độ I: Chỉ register mã mới (Cột C trống)"""
    print("🎬 [MODE: REGISTER] Đang quét mã mới...")
    to_reg = []
    updates = []
    
    for idx, row in enumerate(records, start=2):
        reg_status = str(row.get('Register_Track', '')).strip().lower()
        num = str(row.get('Tracking_Number', '')).strip()
        
        if not reg_status and num and len(num) > 10:
            to_reg.append({"number": num, "carrier": USPS_CARRIER_CODE})
            # Đánh dấu "done" và "Pending" vào hàng đợi update
            updates.append({
                'range': f'C{idx}:D{idx}',
                'values': [["done", "Pending"]]
            })
            if len(to_reg) >= 40: break # Giới hạn batch 40

    if to_reg:
        print(f"🚀 Đang register {len(to_reg)} mã...")
        headers = {"Content-Type": "application/json", "17token": TRACK17_API_KEY}
        requests.post(REGISTER_URL, json=to_reg, headers=headers)
        sheet.batch_update(updates)
        print("✅ Đã Register xong.")
    else:
        print("ℹ️ Không có mã mới cần register.")

def run_track(sheet, records):
    """Chế độ II: GetTrackInfo cho mã đã Register và chưa InTransit"""
    print("🎬 [MODE: TRACK] Đang cập nhật dữ liệu hành trình...")
    to_track = []
    row_map = {}
    
    for idx, row in enumerate(records, start=2):
        reg_status = str(row.get('Register_Track', '')).strip().lower()
        curr_stt = str(row.get('17Track_Status', '')).strip()
        num = str(row.get('Tracking_Number', '')).strip()
        
        # Chỉ chạy nếu: Register=done AND Status in [Pending, InfoReceived]
        # Sẽ tự động bỏ qua nếu Status là InTransit
        if reg_status == "done" and curr_stt in ["Pending", "InfoReceived", "NotFound"]:
            to_track.append({"number": num, "carrier": USPS_CARRIER_CODE})
            row_map[num] = idx
            if len(to_track) >= 40: break

    if not to_track:
        print("ℹ️ Không có mã nào cần track (tất cả đã InTransit hoặc chưa Register).")
        return

    headers = {"Content-Type": "application/json", "17token": TRACK17_API_KEY}
    resp = requests.post(TRACK_INFO_URL, json=to_track, headers=headers)
    data = resp.json()
    
    accepted = data.get("data", {}).get("accepted", [])
    updates = []
    now_vn = datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M")

    for item in accepted:
        num = item.get("number")
        info = item.get("track_info") or {}
        latest = info.get("latest_status") or {}
        status = latest.get("status", "NotFound")
        
        # Lấy mốc thời gian và chuyển sang GMT+7
        label_at, transit_at = "", ""
        providers = info.get("tracking", {}).get("providers", [])
        events = providers[0].get("events", []) if providers else []

        for ev in sorted(events, key=lambda x: x.get("time_utc", "")):
            desc = ev.get("description", "").lower()
            t_vn = to_vn_time(ev.get("time_utc"))
            if not t_vn: continue
            t_str = t_vn.strftime("%Y-%m-%d %H:%M")
            
            if ("label created" in desc or "info received" in desc) and not label_at:
                label_at = t_str
            if ("in transit" in desc or "accepted" in desc or "picked up" in desc) and not transit_at:
                transit_at = t_str

        # Tính SLA thuần số
        # Nếu chưa có label_at trên API, lấy tạm từ sheet để tính cho InfoReceived
        ridx = row_map.get(num)
        sheet_label = str(records[ridx-2].get('Label_Created_At', ''))
        effective_label = label_at if label_at else sheet_label
        
        sla_hours = calculate_sla_hours(effective_label, transit_at)

        if ridx:
            # Thứ tự: D:Status, E:Label, F:Transit, G:SLA, H:LastUpdated
            updates.append({
                'range': f'D{ridx}:H{ridx}',
                'values': [[status, label_at if label_at else sheet_label, transit_at, sla_hours, now_vn]]
            })

    if updates:
        sheet.batch_update(updates)
        # Bôi màu SLA (Sử dụng định dạng có điều kiện của Google Sheet là tốt nhất, 
        # nhưng ở đây ta chỉ gửi dữ liệu, bôi màu Dashboard sẽ làm ở bước Dashboard)
        print(f"✅ Đã cập nhật {len(updates)} dòng.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", help="register or track")
    args = parser.parse_args()
    
    sheet_obj = get_google_sheet()
    all_records = sheet_obj.get_all_records()
    
    if args.mode == "register":
        run_register(sheet_obj, all_records)
    elif args.mode == "track":
        run_track(sheet_obj, all_records)
