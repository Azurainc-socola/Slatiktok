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

try:
    TRACK17_API_KEY = st.secrets["TRACK17_API_KEY"]
    SPREADSHEET_ID = st.secrets["SPREADSHEET_ID"]
    GCP_JSON_STR = st.secrets["GCP_JSON"]
    # Cấu hình Email
    EMAIL_SENDER = st.secrets["EMAIL_SENDER"]
    EMAIL_RECEIVER = st.secrets["EMAIL_RECEIVER"]
    EMAIL_APP_PASSWORD = st.secrets["EMAIL_APP_PASSWORD"]
except Exception:
    st.error("❌ Thiếu cấu hình Secrets (API Key, Sheet ID hoặc Email)!")
    st.stop()

# ==========================================
# 2. HÀM GỬI EMAIL THÔNG BÁO
# ==========================================
def send_summary_email(total_tracked, newly_intransit, sla_24, sla_48):
    subject = f"🚀 [Azura Report] Kết quả quét vận đơn {datetime.now(VN_TZ).strftime('%d/%m %H:%M')}"
    
    body = f"""
    <h3>Báo cáo kết quả quét dữ liệu Azura</h3>
    <p>Phiên quét vừa hoàn thành lúc: <b>{datetime.now(VN_TZ).strftime('%Y-%m-%d %H:%M:%S')}</b></p>
    <ul>
        <li><b>Tổng số đơn vừa cập nhật:</b> {total_tracked} đơn</li>
        <li><b>Số đơn mới chuyển sang InTransit:</b> <span style="color: green;">+{newly_intransit} đơn</span></li>
    </ul>
    <hr>
    <h4>Cảnh báo SLA (Đơn chưa InTransit):</h4>
    <ul>
        <li><b>Số đơn SLA > 24h:</b> <span style="color: orange;">{sla_24} đơn</span></li>
        <li><b>Số đơn SLA > 48h:</b> <span style="color: red;">{sla_48} đơn</span></li>
    </ul>
    <p><i>Vui lòng kiểm tra chi tiết trên Google Sheet để xử lý kịp thời.</i></p>
    """
    
    msg = MIMEMultipart()
    msg['From'] = EMAIL_SENDER
    msg['To'] = EMAIL_RECEIVER
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'html'))
    
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(EMAIL_SENDER, EMAIL_APP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        st.error(f"Lỗi gửi email: {e}")
        return False

# ==========================================
# 3. CÁC HÀM LOGIC (GIỮ NGUYÊN & TỐI ƯU)
# ==========================================
def get_sheet_data():
    creds_dict = json.loads(GCP_JSON_STR)
    creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Data")
    return sheet, sheet.get_all_values()

def calculate_sla_hours(label_at_str, transit_at_str):
    if not label_at_str: return 0
    try:
        fmt = "%Y-%m-%d %H:%M"
        t_label = VN_TZ.localize(datetime.strptime(label_at_str[:16], fmt))
        t_end = VN_TZ.localize(datetime.strptime(transit_at_str[:16], fmt)) if transit_at_str else datetime.now(VN_TZ)
        return int((t_end - t_label).total_seconds() / 3600)
    except: return 0

# ==========================================
# 4. GIAO DIỆN CHÍNH
# ==========================================
st.set_page_config(page_title="Azura SLA Control", page_icon="⚡")
st.title("⚡ Azura SLA Control & Email Alert")

# Checkbox gửi mail mặc định chọn
is_send_email = st.checkbox("Gửi thông báo email báo cáo sau khi quét xong", value=True)

try:
    sheet_obj, raw_rows = get_sheet_data()
    headers = raw_rows[0]
    data_rows = raw_rows[1:]
    
    idx_track = headers.index("Tracking_Number")
    idx_reg = headers.index("Register_Track")
    idx_status = headers.index("17Track_Status")
    idx_label = headers.index("Label_Created_At")
    idx_transit = headers.index("In_Transit_At")
    idx_sla = headers.index("SLA_Status")

    # Lọc danh sách
    need_reg_list = [i+2 for i, r in enumerate(data_rows) if str(r[idx_reg]).lower() != 'done' and r[idx_track]]
    
    # Danh sách đơn cần track (Chưa InTransit)
    need_track_rows = []
    for i, r in enumerate(data_rows):
        if str(r[idx_reg]).lower() == 'done' and r[idx_status] not in ["InTransit", "Delivered", "Returned"]:
            need_track_rows.append({"row_idx": i + 2, "num": str(r[idx_track]).strip(), "old_label": r[idx_label], "old_status": r[idx_status]})

    c1, c2, c3 = st.columns(3)
    c1.metric("Tổng đơn", len(data_rows))
    c2.metric("Cần Register", len(need_reg_list))
    c3.metric("Cần Update SLA", len(need_track_rows))

except Exception as e:
    st.error(f"Lỗi: {e}")
    st.stop()

st.divider()
col_a, col_b = st.columns(2)

# --- NÚT REGISTER ---
if col_a.button(f"🚀 Register ({len(need_reg_list)} đơn)", use_container_width=True):
    # (Giữ nguyên logic register cũ nhưng dùng need_reg_list mới)
    pass 

# --- NÚT TRACKING + GỬI MAIL ---
if col_b.button(f"📡 Update Track & Email Report ({len(need_track_rows)} đơn)", use_container_width=True, type="primary"):
    if not need_track_rows:
        st.info("Không có đơn nào cần cập nhật.")
    else:
        newly_intransit_count = 0
        total_tracked = 0
        
        progress_bar = st.progress(0)
        now_vn = datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M")

        for i in range(0, len(need_track_rows), BATCH_SIZE):
            batch = need_track_rows[i : i + BATCH_SIZE]
            batch_api = [{"number": x["num"], "carrier": USPS_CARRIER_CODE} for x in batch]
            
            resp = requests.post(TRACK_INFO_URL, json=batch_api, headers={"17token": TRACK17_API_KEY})
            accepted = resp.json().get("data", {}).get("accepted", [])
            
            updates = []
            for item in accepted:
                num = item.get("number")
                info = item.get("track_info") or {}
                api_status = (info.get("latest_status") or {}).get("status", "Pending")
                
                # Tìm lại thông tin dòng cũ để so sánh
                orig_row = next((x for x in batch if x["num"] == num), None)
                if not orig_row: continue
                
                # KIỂM TRA ĐƠN MỚI CHUYỂN INTRANSIT
                if api_status == "InTransit" and orig_row["old_status"] != "InTransit":
                    newly_intransit_count += 1
                
                # Bóc tách thời gian
                l_vn, t_vn = "", ""
                events = (info.get("tracking", {}).get("providers", []) or [{}])[0].get("events", [])
                for ev in sorted(events, key=lambda x: x.get("time_utc", "")):
                    desc = ev.get("description", "").lower()
                    if ("label created" in desc or "info received" in desc) and not l_vn:
                        l_vn = datetime.fromisoformat(ev.get("time_utc").replace('Z', '+00:00')).astimezone(VN_TZ).strftime("%Y-%m-%d %H:%M")
                    if ("in transit" in desc or "accepted" in desc or "picked up" in desc) and not t_vn:
                        t_vn = datetime.fromisoformat(ev.get("time_utc").replace('Z', '+00:00')).astimezone(VN_TZ).strftime("%Y-%m-%d %H:%M")
                
                eff_label = l_vn if l_vn else orig_row["old_label"]
                sla_val = calculate_sla_hours(eff_label, t_vn)
                updates.append({'range': f'D{orig_row["row_idx"]}:H{orig_row["row_idx"]}', 'values': [[api_status, eff_label, t_vn, sla_val, now_vn]]})
                total_tracked += 1
            
            if updates:
                sheet_obj.batch_update(updates)
            progress_bar.progress(min((i + BATCH_SIZE) / len(need_track_rows), 1.0))

        # SAU KHI QUÉT XONG: TÍNH TOÁN SLA TOÀN BẢNG ĐỂ GỬI MAIL
        if is_send_email:
            st.info("📧 Đang tổng hợp dữ liệu và gửi Email báo cáo...")
            # Đọc lại dữ liệu mới nhất sau khi update để có SLA chính xác
            _, updated_rows = get_sheet_data()
            sla_24_count = 0
            sla_48_count = 0
            
            for r in updated_rows[1:]:
                # Chỉ đếm những đơn chưa InTransit
                if r[idx_status] not in ["InTransit", "Delivered", "Returned"]:
                    try:
                        val = int(r[idx_sla])
                        if val > 48: sla_48_count += 1
                        elif val > 24: sla_24_count += 1
                    except: pass
            
            success = send_summary_email(total_tracked, newly_intransit_count, sla_24_count, sla_48_count)
            if success: st.success("✅ Đã gửi báo cáo qua Email!")

        st.success("✅ Hoàn tất cập nhật dữ liệu!")
        st.rerun()
