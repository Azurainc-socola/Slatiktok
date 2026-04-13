import streamlit as st
import json
import requests
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import pytz
import gspread
from google.oauth2.service_account import Credentials

# ==========================================
# 1. CẤU HÌNH HỆ THỐNG
# ==========================================
VN_TZ = pytz.timezone('Asia/Ho_Chi_Minh')
REGISTER_URL = "https://api.17track.net/track/v2.4/register"
TRACK_INFO_URL = "https://api.17track.net/track/v2.4/gettrackinfo"
USPS_CARRIER_CODE = 21051
BATCH_SIZE = 40 

# Load Secrets
try:
    TRACK17_API_KEY = st.secrets["TRACK17_API_KEY"]
    SPREADSHEET_ID = st.secrets["SPREADSHEET_ID"]
    GCP_JSON_STR = st.secrets["GCP_JSON"]
    EMAIL_SENDER = st.secrets["EMAIL_SENDER"]
    EMAIL_APP_PASSWORD = st.secrets["EMAIL_APP_PASSWORD"]
except Exception:
    st.error("❌ Thiếu cấu hình Secrets (API Key, Sheet ID hoặc Email) trong Settings!")
    st.stop()

# ==========================================
# 2. CÁC HÀM TIỆN ÍCH
# ==========================================
def get_sheet_connection():
    creds_dict = json.loads(GCP_JSON_STR)
    creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).worksheet("Data")

def to_vn_time(iso_str):
    if not iso_str: return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        return dt.astimezone(VN_TZ).strftime("%Y-%m-%d %H:%M")
    except: return ""

def calculate_sla(label_at, transit_at):
    if not label_at: return 0
    try:
        fmt = "%Y-%m-%d %H:%M"
        t1 = VN_TZ.localize(datetime.strptime(label_at[:16], fmt))
        t2 = VN_TZ.localize(datetime.strptime(transit_at[:16], fmt)) if transit_at else datetime.now(VN_TZ)
        return int((t2 - t1).total_seconds() / 3600)
    except: return 0

def send_report(receiver, total, new_transit, sla24, sla48):
    msg = MIMEMultipart()
    msg['From'] = EMAIL_SENDER
    msg['To'] = receiver
    msg['Subject'] = f"🚀 [Azura SLA] Báo cáo quét vận đơn {datetime.now(VN_TZ).strftime('%H:%M %d/%m')}"
    
    html = f"""
    <h3>Kết quả phiên quét Azura</h3>
    <p>Thời gian: <b>{datetime.now(VN_TZ).strftime('%d/%m/%Y %H:%M')}</b></p>
    <ul>
        <li>Tổng số đơn vừa cập nhật: <b>{total}</b></li>
        <li>Đơn mới chuyển sang InTransit: <b style="color:green;">+{new_transit}</b></li>
    </ul>
    <h4>⚠️ Cảnh báo tồn kho (Chưa InTransit):</h4>
    <ul>
        <li>SLA > 24h: <b style="color:orange;">{sla24} đơn</b></li>
        <li>SLA > 48h: <b style="color:red;">{sla48} đơn</b></li>
    </ul>
    """
    msg.attach(MIMEText(html, 'html'))
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(EMAIL_SENDER, EMAIL_APP_PASSWORD)
            server.send_message(msg)
        return True
    except: return False

# ==========================================
# 3. GIAO DIỆN & LOGIC CHÍNH
# ==========================================
st.set_page_config(page_title="Azura SLA Admin", page_icon="⚡")
st.title("⚡ Azura SLA Control Panel")

# Khởi tạo hoặc lấy dữ liệu
try:
    sheet = get_sheet_connection()
    # FORMATTED_VALUE giúp lấy chuỗi, tránh lỗi "int too large"
    raw_data = sheet.get_all_values(value_render_option='FORMATTED_VALUE')
    headers = raw_data[0]
    data_rows = raw_data[1:]

    # Map cột linh hoạt
    cols = {h: i for i, h in enumerate(headers)}
    
    # Lọc danh sách cần xử lý
    reg_list = [i+2 for i, r in enumerate(data_rows) if r[cols['Register_Track']].lower() != 'done' and r[cols['Tracking_Number']]]
    
    track_list = []
    for i, r in enumerate(data_rows):
        stt = r[cols['17Track_Status']]
        if r[cols['Register_Track']].lower() == 'done' and stt not in ["InTransit", "Delivered", "Returned"]:
            track_list.append({"row": i+2, "num": r[cols['Tracking_Number']], "old_stt": stt, "old_label": r[cols['Label_Created_At']]})

    # Hiển thị số liệu
    m1, m2, m3 = st.columns(3)
    m1.metric("Tổng dòng", len(data_rows))
    m2.metric("Chưa Register", len(reg_list))
    m3.metric("Chờ InTransit", len(track_list))

except Exception as e:
    st.error(f"Lỗi kết nối Sheet: {e}")
    st.stop()

st.divider()

# Giao diện gửi Email
st.subheader("📩 Cấu hình báo cáo")
c_mail1, c_mail2 = st.columns([1, 2])
with c_mail1:
    is_mail = st.checkbox("Gửi báo cáo sau khi quét", value=True)
with c_mail2:
    mail_to = st.text_input("Email nhận:", value=st.secrets.get("EMAIL_RECEIVER", ""), placeholder="ceo@azura.com")

st.divider()
btn_reg, btn_track = st.columns(2)

# --- XỬ LÝ REGISTER ---
if btn_reg.button(f"🚀 Register toàn bộ {len(reg_list)} đơn", use_container_width=True):
    if not reg_list: st.info("Không có mã mới.")
    else:
        bar = st.progress(0)
        for i in range(0, len(reg_list), BATCH_SIZE):
            rows = reg_list[i:i+BATCH_SIZE]
            batch_api = [{"number": data_rows[r-2][cols['Tracking_Number']], "carrier": USPS_CARRIER_CODE} for r in rows]
            requests.post(REGISTER_URL, json=batch_api, headers={"17token": TRACK17_API_KEY})
            
            updates = [{'range': f'C{r}:D{r}', 'values': [["done", "Pending"]]} for r in rows]
            sheet.batch_update(updates)
            bar.progress(min((i + BATCH_SIZE) / len(reg_list), 1.0))
        st.success("✅ Register hoàn tất!")
        st.rerun()

# --- XỬ LÝ TRACKING ---
if btn_track.button(f"📡 Update & Báo cáo {len(track_list)} đơn", use_container_width=True, type="primary"):
    if not track_list: st.info("Không có đơn cần cập nhật.")
    else:
        new_in_transit = 0
        total_up = 0
        bar = st.progress(0)
        now_vn = datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M")

        for i in range(0, len(track_list), BATCH_SIZE):
            batch = track_list[i:i+BATCH_SIZE]
            batch_api = [{"number": x['num'], "carrier": USPS_CARRIER_CODE} for x in batch]
            
            res = requests.post(TRACK_INFO_URL, json=batch_api, headers={"17token": TRACK17_API_KEY}).json()
            accepted = res.get("data", {}).get("accepted", [])
            
            updates = []
            for item in accepted:
                num = item.get("number")
                info = item.get("track_info") or {}
                new_stt = (info.get("latest_status") or {}).get("status", "Pending")
                
                orig = next(x for x in batch if x['num'] == num)
                if new_stt == "InTransit" and orig['old_stt'] != "InTransit":
                    new_in_transit += 1
                
                l_vn, t_vn = "", ""
                events = (info.get("tracking", {}).get("providers", []) or [{}])[0].get("events", [])
                for ev in sorted(events, key=lambda x: x.get("time_utc", "")):
                    desc = ev.get("description", "").lower()
                    t_str = to_vn_time(ev.get("time_utc"))
                    if ("label created" in desc or "info received" in desc) and not l_vn: l_vn = t_str
                    if ("in transit" in desc or "accepted" in desc or "picked up" in desc) and not t_vn: t_vn = t_str
                
                eff_label = l_vn if l_vn else orig['old_label']
                sla = calculate_sla(eff_label, t_vn)
                updates.append({'range': f'D{orig["row"]}:H{orig["row"]}', 'values': [[new_stt, eff_label, t_vn, sla, now_vn]]})
                total_up += 1
            
            if updates: sheet.batch_update(updates)
            bar.progress(min((i + BATCH_SIZE) / len(track_list), 1.0))

        # GỬI EMAIL BÁO CÁO
        if is_mail and mail_to:
            with st.spinner("📧 Đang tổng hợp SLA & Gửi email..."):
                # Đọc lại dữ liệu mới nhất để đếm SLA toàn bảng
                new_raw = sheet.get_all_values(value_render_option='FORMATTED_VALUE')
                s24, s48 = 0, 0
                for r in new_raw[1:]:
                    if r[cols['17Track_Status']] not in ["InTransit", "Delivered", "Returned"]:
                        try:
                            val = int(r[cols['SLA_Status']])
                            if val > 48: s48 += 1
                            elif val > 24: s24 += 1
                        except: pass
                send_report(mail_to, total_up, new_in_transit, s24, s48)
                st.success(f"📧 Đã gửi báo cáo đến {mail_to}")
        
        st.success(f"✅ Đã cập nhật xong {total_up} mã!")
        st.rerun()
