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
# 1. CẤU HÌNH HỆ THỐNG
# ==========================================
VN_TZ = pytz.timezone('Asia/Ho_Chi_Minh')
REGISTER_URL = "https://api.17track.net/track/v2.4/register"
TRACK_INFO_URL = "https://api.17track.net/track/v2.4/gettrackinfo"
USPS_CARRIER_CODE = 21051

try:
    TRACK17_API_KEY = st.secrets["TRACK17_API_KEY"]
    SPREADSHEET_ID = st.secrets["SPREADSHEET_ID"]
    GCP_JSON_STR = st.secrets["GCP_JSON"]
except Exception as e:
    st.error("❌ Thiếu cấu hình Secrets trong Settings!")
    st.stop()

# ==========================================
# 2. CÁC HÀM XỬ LÝ
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
        t_label = VN_TZ.localize(datetime.strptime(label_at_str[:16], fmt))
        t_end = VN_TZ.localize(datetime.strptime(transit_at_str[:16], fmt)) if transit_at_str else datetime.now(VN_TZ)
        return int((t_end - t_label).total_seconds() / 3600)
    except: return ""

# ==========================================
# 3. GIAO DIỆN NGƯỜI DÙNG (UI)
# ==========================================
st.set_page_config(page_title="Azura SLA Management", page_icon="🚀", layout="wide")
st.title("🚚 Azura 17Track Management App")

try:
    sheet = get_google_sheet()
    
    # 🔥 FIX TRIỆT ĐỂ: Ép Google trả về dạng TEXT nguyên bản, không convert số
    # value_render_option='FORMATTED_VALUE' sẽ lấy đúng những gì hiển thị trên ô
    records = sheet.get_all_records(value_render_option='FORMATTED_VALUE') 
    
    df = pd.DataFrame(records)
    
    # Hiển thị Dashboard
    if not df.empty:
        c1, c2, c3 = st.columns(3)
        c1.metric("Tổng đơn", len(df))
        c2.metric("Chưa Register", len(df[df['Register_Track'] != 'done']))
        c3.metric("Chờ InTransit", len(df[~df['17Track_Status'].isin(['InTransit', 'Delivered', 'Returned'])]))
        
        st.write("### Danh sách vận đơn")
        st.dataframe(df.head(100), use_container_width=True)
    else:
        st.warning("Sheet trống hoặc không có dữ liệu.")

except Exception as e:
    st.error(f"❌ Lỗi kết nối dữ liệu: {e}")
    st.info("Mẹo: Nếu vẫn lỗi, hãy bôi đen cột Tracking Number trong Google Sheet -> Định dạng -> Số -> Văn bản thuần túy.")
    st.stop()

st.divider()

# --- KHU VỰC NÚT BẤM ---
col_reg, col_track = st.columns(2)

if col_reg.button("🔥 CHẠY REGISTER (Mã mới)", use_container_width=True):
    with st.spinner("Đang đăng ký mã mới..."):
        to_reg = []
        updates = []
        for idx, row in enumerate(records, start=2):
            num = str(row.get('Tracking_Number', '')).strip()
            reg_status = str(row.get('Register_Track', '')).strip().lower()
            
            if reg_status != 'done' and num:
                to_reg.append({"number": num, "carrier": USPS_CARRIER_CODE})
                updates.append({'range': f'C{idx}:D{idx}', 'values': [["done", "Pending"]]})
                if len(to_reg) >= 40: break
        
        if to_reg:
            headers = {"Content-Type": "application/json", "17token": TRACK17_API_KEY}
            requests.post(REGISTER_URL, json=to_reg, headers=headers)
            sheet.batch_update(updates)
            st.success(f"✅ Đã Register xong {len(to_reg)} mã.")
            st.rerun()
        else: st.warning("Không có mã mới cần Register.")

if col_track.button("📡 CHẠY GET TRACK INFO (Cập nhật SLA)", use_container_width=True):
    with st.spinner("Đang cập nhật hành trình..."):
        to_track = []
        row_map = {}
        for idx, row in enumerate(records, start=2):
            num = str(row.get('Tracking_Number', '')).strip()
            reg_done = str(row.get('Register_Track', '')).strip().lower() == 'done'
            status = str(row.get('17Track_Status', '')).strip()
            
            if reg_done and status not in ["InTransit", "Delivered", "Returned"]:
                to_track.append({"number": num, "carrier": USPS_CARRIER_CODE})
                row_map[num] = idx
                if len(to_track) >= 40: break
        
        if to_track:
            headers = {"Content-Type": "application/json", "17token": TRACK17_API_KEY}
            resp = requests.post(TRACK_INFO_URL, json=to_track, headers=headers)
            data_resp = resp.json().get("data", {})
            accepted = data_resp.get("accepted", [])
            
            updates = []
            now_vn = datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M")
            
            for item in accepted:
                num = item.get("number")
                info = item.get("track_info") or {}
                api_status = (info.get("latest_status") or {}).get("status", "Pending")
                
                label_vn, transit_vn = "", ""
                events = (info.get("tracking", {}).get("providers", []) or [{}])[0].get("events", [])
                for ev in sorted(events, key=lambda x: x.get("time_utc", "")):
                    desc = ev.get("description", "").lower()
                    t_str = to_vn_time_str(ev.get("time_utc"))
                    if ("label created" in desc or "info received" in desc) and not label_vn: label_vn = t_str
                    if ("in transit" in desc or "accepted" in desc or "picked up" in desc) and not transit_vn: transit_vn = t_str
                
                ridx = row_map.get(num)
                # Lấy dữ liệu cũ từ sheet bằng DataFrame để tránh lỗi truy cập index
                sheet_label = str(df.iloc[ridx-2]['Label_Created_At']) if ridx else ""
                eff_label = label_vn if label_vn else sheet_label
                sla_val = calculate_sla_hours(eff_label, transit_vn)
                
                updates.append({
                    'range': f'D{ridx}:H{ridx}',
                    'values': [[api_status, eff_label, transit_vn, sla_val, now_vn]]
                })
            
            if updates:
                sheet.batch_update(updates)
                st.success("✅ Đã cập nhật xong.")
                st.rerun()
