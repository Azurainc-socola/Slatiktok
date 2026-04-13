import streamlit as st
import os
import json
import time
import requests
from datetime import datetime
import pytz
import gspread
from google.oauth2.service_account import Credentials
import pandas as pd

# ==========================================
# CẤU HÌNH HỆ THỐNG & BIẾN
# ==========================================
VN_TZ = pytz.timezone('Asia/Ho_Chi_Minh')
REGISTER_URL = "https://api.17track.net/track/v2.4/register"
TRACK_INFO_URL = "https://api.17track.net/track/v2.4/gettrackinfo"
USPS_CARRIER_CODE = 21051

# Lấy Secrets từ Streamlit (Cài đặt trong mục Settings > Secrets trên web Streamlit)
TRACK17_API_KEY = st.secrets["TRACK17_API_KEY"]
SPREADSHEET_ID = st.secrets["SPREADSHEET_ID"]
GCP_JSON_STR = st.secrets["GCP_JSON"]

# ==========================================
# CÁC HÀM BỔ TRỢ (HELPER FUNCTIONS)
# ==========================================
def get_google_sheet():
    creds_dict = json.loads(GCP_JSON_STR)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    )
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).worksheet("Data")

def to_vn_time_str(iso_str):
    if not iso_str: return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        return dt.astimezone(VN_TZ).strftime("%Y-%m-%d %H:%M")
    except: return ""

def calculate_sla_hours(label_at_str, transit_at_str):
    if not label_at_str: return ""
    try:
        fmt = "%Y-%m-%d %H:%M"
        t_label = VN_TZ.localize(datetime.strptime(label_at_str, fmt))
        t_end = VN_TZ.localize(datetime.strptime(transit_at_str, fmt)) if transit_at_str else datetime.now(VN_TZ)
        return int((t_end - t_label).total_seconds() / 3600)
    except: return ""

# ==========================================
# GIAO DIỆN STREAMLIT
# ==========================================
st.set_page_config(page_title="Azura SLA Tracker", page_icon="🚚", layout="wide")

st.title("🚚 Azura 17Track Management System")
st.markdown("Hệ thống dành cho nhân viên quản lý Register & Tracking tự động.")

# Kết nối Sheet và hiển thị dữ liệu
try:
    sheet = get_google_sheet()
    records = sheet.get_all_records()
    df = pd.DataFrame(records)
    
    # Hiển thị thống kê nhanh
    col1, col2, col3 = st.columns(3)
    col1.metric("Tổng đơn", len(df))
    col2.metric("Chưa Register", len(df[df['Register_Track'] != 'done']))
    col3.metric("Đang chờ InTransit", len(df[~df['17Track_Status'].isin(['InTransit', 'Delivered', 'Returned'])]))
    
    st.write("### Xem trước dữ liệu từ Google Sheet")
    st.dataframe(df.head(20), use_container_width=True)

except Exception as e:
    st.error(f"Không thể kết nối Google Sheet: {e}")
    st.stop()

# --- KHU VỰC ĐIỀU KHIỂN ---
st.divider()
st.write("### Khu vực xử lý tác vụ")
c1, c2 = st.columns(2)

# --- NÚT 1: REGISTER ---
if c1.button("1️⃣ Bắt đầu REGISTER mã mới", use_container_width=True, type="primary"):
    with st.status("Đang quét mã mới...", expanded=True) as status:
        to_reg = []
        updates = []
        for idx, row in enumerate(records, start=2):
            if str(row.get('Register_Track', '')).lower() != "done" and row.get('Tracking_Number'):
                to_reg.append({"number": str(row['Tracking_Number']), "carrier": USPS_CARRIER_CODE})
                updates.append({'range': f'C{idx}:D{idx}', 'values': [["done", "Pending"]]})
                if len(to_reg) >= 40: break
        
        if to_reg:
            headers = {"Content-Type": "application/json", "17token": TRACK17_API_KEY}
            requests.post(REGISTER_URL, json=to_reg, headers=headers)
            sheet.batch_update(updates)
            st.success(f"Đã đăng ký thành công {len(to_reg)} mã.")
        else:
            st.info("Không tìm thấy mã mới cần Register.")
        status.update(label="Hoàn tất Register!", state="complete")

# --- NÚT 2: UPDATE TRACKING ---
if c2.button("2️⃣ Cập nhật thông tin TRACKING", use_container_width=True, type="primary"):
    with st.status("Đang lấy thông tin từ 17Track...", expanded=True) as status:
        to_track = []
        row_map = {}
        for idx, row in enumerate(records, start=2):
            reg_done = str(row.get('Register_Track', '')).lower() == "done"
            curr_stt = str(row.get('17Track_Status', ''))
            if reg_done and curr_stt not in ["InTransit", "Delivered", "Returned"]:
                to_track.append({"number": str(row['Tracking_Number']), "carrier": USPS_CARRIER_CODE})
                row_map[str(row['Tracking_Number'])] = idx
                if len(to_track) >= 40: break
        
        if to_track:
            headers = {"Content-Type": "application/json", "17token": TRACK17_API_KEY}
            resp = requests.post(TRACK_INFO_URL, json=to_track, headers=headers)
            res_data = resp.json()
            accepted = res_data.get("data", {}).get("accepted", [])
            
            updates = []
            now_vn = datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M")
            
            for item in accepted:
                num = item.get("number")
                info = item.get("track_info") or {}
                api_status = (info.get("latest_status") or {}).get("status", "Pending")
                
                label_at, transit_at = "", ""
                events = (info.get("tracking", {}).get("providers", []) or [{}])[0].get("events", [])
                for ev in sorted(events, key=lambda x: x.get("time_utc", "")):
                    desc = ev.get("description", "").lower()
                    t_str = to_vn_time_str(ev.get("time_utc"))
                    if ("label created" in desc or "info received" in desc) and not label_at: label_at = t_str
                    if ("in transit" in desc or "accepted" in desc or "picked up" in desc) and not transit_at: transit_at = t_str
                
                ridx = row_map.get(num)
                sheet_label = str(records[ridx-2].get('Label_Created_At', ''))
                eff_label = label_at if label_at else sheet_label
                sla_val = calculate_sla_hours(eff_label, transit_at)
                
                if ridx:
                    updates.append({
                        'range': f'D{ridx}:H{ridx}',
                        'values': [[api_status, eff_label, transit_at, sla_val, now_vn]]
                    })
            
            if updates:
                sheet.batch_update(updates)
                st.success(f"Đã cập nhật dữ liệu cho {len(updates)} mã.")
        else:
            st.info("Tất cả các mã đều đã InTransit hoặc chưa Register.")
        status.update(label="Hoàn tất Tracking!", state="complete")
