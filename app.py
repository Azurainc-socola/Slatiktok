import os
import json
import time
import requests
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

# ==========================================
# CẤU HÌNH BIẾN MÔI TRƯỜNG (Từ GitHub Secrets)
# ==========================================
TRACK17_API_KEY = os.getenv("TRACK17_API_KEY")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
GCP_JSON_STR = os.getenv("GCP_JSON")

TRACK17_URL = "https://api.17track.net/track/v2.2/gettrackinfo"

def get_google_sheet():
    """Đăng nhập và lấy bảng tính Google Sheet bằng JSON credentials"""
    if not GCP_JSON_STR:
        raise ValueError("❌ Lỗi: Không tìm thấy biến môi trường GCP_JSON.")
        
    # Parse chuỗi JSON từ biến môi trường thành Dictionary
    creds_dict = json.loads(GCP_JSON_STR)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    )
    client = gspread.authorize(creds)
    # Mở file bằng ID và chọn Tab tên là 'Data'
    return client.open_by_key(SPREADSHEET_ID).worksheet("Data")

def calculate_sla(label_created_at, in_transit_at):
    """Tính toán cảnh báo SLA dựa trên thời gian chênh lệch"""
    if in_transit_at:
        return "🟢 OK"
    if not label_created_at:
        return "⚪ WAITING"

    # Xử lý tính toán thời gian (Giả định format 17Track trả về: YYYY-MM-DD HH:MM)
    try:
        label_time = datetime.strptime(label_created_at[:16], "%Y-%m-%d %H:%M")
        now = datetime.utcnow() # Dùng UTC vì Github Actions server chạy múi giờ UTC
        
        # Tính số giờ chênh lệch
        delta_hours = (now - label_time).total_seconds() / 3600
        
        if delta_hours >= 72: return "🔴 LATE_72H"
        if delta_hours >= 60: return "🟠 LATE_60H"
        if delta_hours >= 48: return "🟡 LATE_48H"
        if delta_hours >= 24: return "🔵 LATE_24H"
        return "⚪ WAITING"
    except Exception as e:
        return "⚪ ERROR DATE"

def main():
    print("🚀 Bắt đầu chạy TikTok SLA Bot...")
    
    try:
        sheet = get_google_sheet()
    except Exception as e:
        print(f"❌ Lỗi kết nối Google Sheet: {e}")
        return
    
    # Lấy toàn bộ dữ liệu từ Sheet
    records = sheet.get_all_records()
    trackings_to_query = []
    row_mapping = {}

    # BƯỚC 1: LỌC DỮ LIỆU
    for i, row in enumerate(records):
        row_idx = i + 2 # Do gspread đếm từ 1 và trừ đi dòng header ở dòng 1
        tracking = str(row.get("Tracking_Number", "")).strip()
        status_17 = str(row.get("17Track_Status", "")).strip()
        sla_status = str(row.get("SLA_Status", "")).strip()

        # Chỉ lấy tracking bắt đầu bằng 920019, chưa Delivered và chưa có nhãn Xanh (OK)
        if tracking.startswith("920019") and "🟢 OK" not in sla_status and "Delivered" not in status_17:
            trackings_to_query.append({"number": tracking, "carrier": 3011}) # 3011 là code của USPS
            row_mapping[tracking] = row_idx

    if not trackings_to_query:
        print("✅ Không có đơn hàng nào cần kiểm tra. Kết thúc luồng!")
        return

    print(f"🔍 Tìm thấy {len(trackings_to_query)} mã vận đơn cần gọi API...")

    # BƯỚC 2: GỌI API 17TRACK (Chia batch 40 mã/lần để tránh bị chặn)
    batch_size = 40
    updates = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    headers = {
        "17token": TRACK17_API_KEY,
        "Content-Type": "application/json"
    }

    for i in range(0, len(trackings_to_query), batch_size):
        batch = trackings_to_query[i:i + batch_size]
        
        try:
            response = requests.post(TRACK17_URL, headers=headers, json=batch)
            data = response.json()

            if data.get("code") != 0:
                print(f"❌ Lỗi từ 17Track cho batch này: {data}")
                continue

            for result in data.get("data", {}).get("accepted", []):
                tracking_num = result.get("number")
                track_info = result.get("track", {})
                if not track_info:
                    continue

                events = track_info.get("z1", [])
                events_sorted = sorted(events, key=lambda x: x.get("a", "")) # Cũ nhất xếp trước

                label_at = ""
                transit_at = ""
                current_status = "Info Received"

                # Xác định status khái quát từ 17Track
                status_code = track_info.get("e", 0)
                if status_code == 10: current_status = "Not Found"
                elif status_code == 20: current_status = "In Transit"
                elif status_code == 40: current_status = "Delivered"

                # Bóc tách sự kiện tìm thời gian tạo Label và In Transit
                for ev in events_sorted:
                    desc = ev.get("z", "").lower()
                    time_str = ev.get("a", "")
                    
                    if ("label created" in desc or "info received" in desc) and not label_at:
                        label_at = time_str
                    
                    if ("in transit" in desc or "accepted at usps" in desc or "arrived at usps" in desc) and not transit_at:
                        transit_at = time_str

                # Tính nhãn phân loại SLA
                sla = calculate_sla(label_at, transit_at)

                # Lưu vào danh sách chuẩn bị update lên Sheet
                row_idx = row_mapping.get(tracking_num)
                if row_idx:
                    # Ghi nhận thay đổi từ Cột C đến Cột G
                    updates.append({
                        'range': f'C{row_idx}:G{row_idx}',
                        'values': [[current_status, label_at, transit_at, sla, now_str]]
                    })

            time.sleep(0.5) # Nghỉ nửa giây giữa các batch để giữ an toàn cho API

        except Exception as e:
            print(f"❌ Lỗi Exception khi xử lý API 17Track: {e}")

    # BƯỚC 3: GHI DỮ LIỆU LÊN GOOGLE SHEET (Batch Update)
    if updates:
        print(f"📝 Đang cập nhật đồng loạt {len(updates)} dòng lên Google Sheet...")
        sheet.batch_update(updates)
        print("✅ Hoàn tất quy trình thành công!")
    else:
        print("✅ Đã gọi API xong nhưng không có dữ liệu trạng thái nào mới để cập nhật lên Sheet.")

if __name__ == "__main__":
    main()
