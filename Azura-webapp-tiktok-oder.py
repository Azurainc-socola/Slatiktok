import streamlit as st
import json
import requests
import smtplib
import pandas as pd
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import gspread
from google.oauth2.service_account import Credentials

# ==========================================
# CẤU HÌNH GIAO DIỆN & THỜI GIAN
# ==========================================
st.set_page_config(page_title="Azura TikTok Sync", page_icon="🛍️", layout="wide")

VN_TZ = timezone(timedelta(hours=7))
today_vn = datetime.now(VN_TZ).date()

# ==========================================
# ĐỌC TOÀN BỘ SECRETS TỪ STREAMLIT
# ==========================================
try:
    # Tài khoản Portal Azura
    SECRET_AZURA_USER = st.secrets["AZURA_USER"]
    SECRET_AZURA_PASS = st.secrets["AZURA_PASS"]
    
    # Cấu hình Google Sheet & Email
    SECRET_SHEET_ID = st.secrets["GOOGLE_SHEET_ID"]
    SECRET_EMAIL_USER = st.secrets["EMAIL_USER"]
    SECRET_EMAIL_PASS = st.secrets["EMAIL_PASS"]
    
    # Chuỗi JSON cấu hình Google Cloud
    SECRET_GCP_JSON_STR = st.secrets["GCP_SERVICE_ACCOUNT_JSON"] 
except Exception as e:
    st.error(f"⚠️ Ứng dụng chưa được cấp quyền truy cập. Thiếu biến môi trường: {e}")
    st.stop()

# ==========================================
# CLASS XỬ LÝ LOGIC CHÍNH
# ==========================================
class AzuraTikTokStreamlit:
    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.base_url = "https://portal.aluffm.com"
        self.login_url = f"{self.base_url}/Account/Login"
        self.order_api = f"{self.base_url}/OnBehalfOrder/List"

    def login(self):
        payload = {"UserName": self.username, "Password": self.password, "RememberMe": "false"}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        resp = self.session.post(self.login_url, data=payload, headers=headers, allow_redirects=False)
        return resp.status_code in [200, 302]

    def fetch_orders(self, start_date_str, end_date_str, status_placeholder):
        all_matches = []
        page = 1
        stop_searching = False

        while not stop_searching:
            status_placeholder.text(f"⏳ Đang quét dữ liệu trang {page}...")
            params = {"page": page, "rows": 50}
            resp = self.session.get(self.order_api, params=params)
            
            if resp.status_code != 200:
                st.error(f"❌ Lỗi API khi tải trang {page}")
                break
                
            rows = resp.json().get("rows", [])
            if not rows:
                break

            for row in rows:
                order_date = row.get("createdAt", "")[:10]
                
                if order_date > end_date_str:
                    continue # Bỏ qua đơn mới hơn ngày kết thúc
                elif start_date_str <= order_date <= end_date_str:
                    if row.get("shippingPartnerString") == "Tiktok":
                        # Gom nhóm các JobID nếu có nhiều cái
                        designs = row.get("orderProductDesigns", [])
                        job_ids = [str(d.get("jobId")) for d in designs if d.get("jobId") is not None]
                        job_id_str = ", ".join(sorted(list(set(job_ids)))) if job_ids else ""

                        all_matches.append({
                            "Seller": row.get("customer", ""),
                            "Tracking": row.get("partnerBarcode", ""),
                            "Order_No": row.get("customerOrder", ""),
                            "Job_ID": job_id_str,
                            "Azura_ID": row.get("id", ""),
                            "Created_At": order_date
                        })
                elif order_date < start_date_str:
                    stop_searching = True # Dừng khi gặp đơn cũ hơn ngày bắt đầu
            
            page += 1
            if page > 50: # Chống treo lặp vô tận
                break
                
        return all_matches

    def update_sheet(self, data_list):
        try:
            creds_dict = json.loads(SECRET_GCP_JSON_STR)
            scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
            creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
            client = gspread.authorize(creds)
            sheet = client.open_by_key(SECRET_SHEET_ID).sheet1

            rows_to_append = []
            for item in data_list:
                row_data = [""] * 12 
                row_data[0] = item["Seller"]      # Cột A
                row_data[1] = item["Tracking"]    # Cột B
                row_data[8] = item["Order_No"]    # Cột I
                row_data[9] = item["Job_ID"]      # Cột J
                row_data[10] = item["Azura_ID"]   # Cột K
                row_data[11] = item["Created_At"] # Cột L
                rows_to_append.append(row_data)

            sheet.append_rows(rows_to_append, value_input_option="USER_ENTERED")
            return True, f"Đã ghi {len(rows_to_append)} dòng"
        except Exception as e:
            return False, str(e)

    def send_email(self, data_list, start_str, end_str, to_emails, cc_emails):
        count = len(data_list)
        has_job = sum(1 for d in data_list if d['Job_ID'])
        no_job = count - has_job
        date_range_txt = start_str if start_str == end_str else f"{start_str} đến {end_str}"
        sheet_url = f"https://docs.google.com/spreadsheets/d/{SECRET_SHEET_ID}"

        html = f"""
        <html><body>
            <h3>📊 Tổng kết quét đơn TikTok Shop</h3>
            <p>- Giai đoạn quét: <b>{date_range_txt}</b></p>
            <p>- Tổng số đơn Tiktok: <b>{count}</b></p>
            <ul>
                <li>Đã có JOB ID: <span style="color: green;">{has_job}</span></li>
                <li>Chưa có JOB ID: <span style="color: red;">{no_job}</span></li>
            </ul>
            <p>🔗 <a href="{sheet_url}">Truy cập Google Sheet tại đây</a></p>
        </body></html>
        """
        msg = MIMEMultipart()
        msg['From'] = SECRET_EMAIL_USER
        msg['To'] = to_emails
        if cc_emails: msg['Cc'] = cc_emails
        msg['Subject'] = f"[Azura TikTok] Báo cáo quét đơn ({date_range_txt})"
        msg.attach(MIMEText(html, 'html'))

        try:
            with smtplib.SMTP('smtp.gmail.com', 587) as server:
                server.starttls()
                server.login(SECRET_EMAIL_USER, SECRET_EMAIL_PASS.replace(" ", ""))
                all_receivers = [e.strip() for e in to_emails.split(',')]
                if cc_emails: all_receivers += [e.strip() for e in cc_emails.split(',')]
                server.sendmail(SECRET_EMAIL_USER, all_receivers, msg.as_string())
            return True, "Đã gửi Email thành công!"
        except Exception as e:
            return False, str(e)

# ==========================================
# GIAO DIỆN CHÍNH
# ==========================================
st.title("🛍️ AZURA TIKTOK ORDER SYNC")

with st.sidebar:
    st.header("📅 Chọn Thời Gian")
    date_range = st.date_input("Từ ngày - Đến ngày", (today_vn, today_vn))
    if len(date_range) == 2:
        start_date, end_date = date_range
    else:
        start_date = end_date = date_range[0]

    st.divider()
    st.header("📧 Cấu Hình Email")
    enable_email = st.checkbox("Bật gửi Email báo cáo", value=False)
    if enable_email:
        email_to = st.text_input("Gửi đến (To)", placeholder="a@gmail.com, b@gmail.com")
        email_cc = st.text_input("Đồng gửi (CC)")

    run_btn = st.button("🚀 BẮT ĐẦU QUÉT", type="primary", use_container_width=True)

if run_btn:
    start_str = start_date.strftime('%Y-%m-%d')
    end_str = end_date.strftime('%Y-%m-%d')

    with st.status("Đang tiến hành xử lý...", expanded=True) as status:
        st.write("🔑 Đang đăng nhập hệ thống nội bộ...")
        
        # Truyền user/pass từ st.secrets vào class
        bot = AzuraTikTokStreamlit(SECRET_AZURA_USER, SECRET_AZURA_PASS)
        
        if not bot.login():
            status.update(label="Đăng nhập thất bại!", state="error")
            st.error("Tài khoản trong Secret không chính xác hoặc Portal đang lỗi.")
            st.stop()

        st.write(f"🔍 Đang quét đơn từ {start_str} đến {end_str}...")
        status_text = st.empty()
        orders = bot.fetch_orders(start_str, end_str, status_text)
        status_text.empty()
        
        if not orders:
            status.update(label="Hoàn tất - Không có dữ liệu", state="complete")
            st.info("Không tìm thấy đơn hàng TikTok nào trong thời gian này.")
            st.stop()
            
        st.write(f"✅ Tìm thấy **{len(orders)}** đơn hàng hợp lệ.")

        st.write("📝 Đang ghi dữ liệu lên Google Sheet...")
        success, msg = bot.update_sheet(orders)
        if success:
            st.write(f"✅ {msg}")
        else:
            st.error(f"❌ Lỗi ghi Sheet: {msg}")

        if enable_email and email_to:
            st.write("📧 Đang gửi Email báo cáo...")
            e_success, e_msg = bot.send_email(orders, start_str, end_str, email_to, email_cc)
            if e_success:
                st.write(f"✅ {e_msg}")
            else:
                st.error(f"❌ Lỗi gửi Mail: {e_msg}")
        
        status.update(label="🎉 Tiến trình hoàn tất!", state="complete")

    st.subheader(f"📊 Dữ liệu đã quét ({len(orders)} đơn)")
    st.dataframe(pd.DataFrame(orders), use_container_width=True)
