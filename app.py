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
    """Xác thực và mở Google Sheet"""
    if not GCP_JSON_STR:
        raise ValueError("❌ Lỗi: Thiếu GCP_JSON trong Env Var.")
    creds_dict = json.loads(GCP_JSON_STR)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    )
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).worksheet("Data")

def to_vn_time_str(iso_str):
    """Chuyển đổi ISO UTC từ 17Track sang String GMT+7 (%Y-%m-%d %H:%M)"""
    if not iso_str: return ""
    try:
        # 17Track trả về UTC (Z)
        dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        return dt.astimezone(VN_TZ).strftime("%Y-%m-%d %H:%M")
    except:
        return ""

def calculate_sla_hours(label_at_str, transit_at_str):
    """Tính SLA theo số giờ thuần túy (Integer)"""
    if not label_at_str: return ""
    
    try:
        fmt = "%Y-%m-%d %H:%M"
        # Parse thời gian đã lưu trên sheet (đã là VN Time)
        t_label = VN_TZ.localize(datetime.strptime(label_at_str, fmt))
        
        if transit_at_str:
            t_end = VN_TZ.localize(datetime.strptime(transit_at_str, fmt))
        else:
            # Nếu chưa InTransit, tính đến thời điểm hiện tại (VN)
            t_end = datetime.now(VN_TZ)
            
        diff = t_end - t_label
        return int(diff.total_seconds() / 3600)
    except Exception as e:
        print(f"⚠️ Lỗi tính SLA: {e}")
        return ""

def run_register(sheet, records):
    """MODE: Chỉ đăng ký những mã mới (Cột C chưa có 'done')"""
    print("🎬 [REGISTER MODE] Đang lọc mã mới...")
    to_reg = []
    updates = []
    
    for idx, row in enumerate(records, start=2):
        reg_status = str(row.get('Register_Track', '')).strip().lower()
        num = str(row.get('Tracking_Number', '')).strip()
        
        if reg_status != "done" and num and len(num) > 10:
            to_reg.append({"number": num, "carrier": USPS_CARRIER_CODE})
            # Ghi nhận ngay để không reg lại
            updates.append({
                'range': f'C{idx}:D{idx}',
                'values': [["done", "Pending"]]
            })
            if len(to_reg) >= 40: break

    if to_reg:
        print(f"🚀 Đang đăng ký {len(to_reg)} mã lên 17Track...")
        headers = {"Content-Type": "application/json", "17token": TRACK17_API_KEY}
        requests.post(REGISTER_URL, json=to_reg, headers=headers)
        sheet.batch_update(updates)
        print("✅ Đã đánh dấu 'done' vào cột Register_Track.")
    else:
        print("ℹ️ Không có mã mới cần đăng ký.")

def run_track(sheet, records):
    """MODE: Cập nhật hành trình (Chỉ mã đã Register và chưa InTransit)"""
    print("🎬 [TRACK MODE] Đang cập nhật dữ liệu hành trình...")
    to_track = []
    row_map = {}
    
    for idx, row in enumerate(records, start=2):
        reg_done = str(row.get('Register_Track', '')).strip().lower() == "done"
        curr_stt = str(row.get('17Track_Status', '')).strip()
        num = str(row.get('Tracking_Number', '')).strip()
        
        # SKIP nếu đã InTransit hoặc chưa Register
        if reg_done and curr_stt not in ["InTransit", "Delivered", "Returned"]:
            to_track.append({"number": num, "carrier": USPS_CARRIER_CODE})
            row_map[num] = idx
            if len(to_track) >= 40: break

    if not to_track:
        print("ℹ️ Không có mã nào cần tracking (đã InTransit hoặc chưa Register).")
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
        stt_obj = info.get("latest_status") or {}
        api_status = stt_obj.get("status", "NotFound")
        
        # Bóc tách sự kiện sang giờ VN
        label_at_vn, transit_at_vn = "", ""
        providers = info.get("tracking", {}).get("providers", [])
        events = providers[0].get("events", []) if providers else []

        for ev in sorted(events, key=lambda x: x.get("time_utc", "")):
            desc = ev.get("description", "").lower()
            t_vn_str = to_vn_time_str(ev.get("time_utc"))
            if not t_vn_str: continue
            
            if ("label created" in desc or "info received" in desc) and not label_at_vn:
                label_at_vn = t_vn_str
            if ("in transit" in desc or "accepted" in desc or "picked up" in desc) and not transit_at_vn:
                transit_at_vn = t_vn_str

        # Lấy row index và dữ liệu cũ trên sheet
        ridx = row_map.get(num)
        sheet_label = str(records[ridx-2].get('Label_Created_At', ''))
        
        # Ưu tiên label từ API, nếu ko có thì dùng label cũ trên sheet
        effective_label = label_at_vn if label_at_vn else sheet_label
        
        # Tính SLA thuần số
        sla_val = calculate_sla_hours(effective_label, transit_at_vn)

        if ridx:
            # Ghi đè từ Cột D đến Cột H
            # D:Status, E:Label, F:Transit, G:SLA, H:LastUpdated
            updates.append({
                'range': f'D{ridx}:H{ridx}',
                'values': [[api_status, effective_label, transit_at_vn, sla_val, now_vn]]
            })

    if updates:
        print(f"📝 Đang đẩy {len(updates)} dòng dữ liệu mới lên Sheet...")
        sheet.batch_update(updates)
        print("✅ Hoàn tất cập nhật.")
    else:
        print("ℹ️ Không có dữ liệu mới từ API.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", help="register or track")
    args = parser.parse_args()
    
    try:
        sheet_obj = get_google_sheet()
        all_records = sheet_obj.get_all_records()
        
        if args.mode == "register":
            run_register(sheet_obj, all_records)
        elif args.mode == "track":
            run_track(sheet_obj, all_records)
        else:
            print("❌ Vui lòng cung cấp --mode (register hoặc track)")
    except Exception as e:
        print(f"❌ Lỗi hệ thống: {e}")
