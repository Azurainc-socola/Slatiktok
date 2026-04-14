import streamlit as st
import json
import requests
import smtplib
import io
import csv
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import gspread
from google.oauth2.service_account import Credentials

# ==========================================
# 1. THIẾT LẬP GIAO DIỆN & THỜI GIAN
# ==========================================
st.set_page_config(page_title="Azura TikTok Sync Ultra", page_icon="⚡", layout="centered")

VN_TZ = timezone(timedelta(hours=7))
today_vn = datetime.now(VN_TZ).date()

# ĐỌC SECRETS THEO BIẾN BẠN YÊU CẦU
try:
    # Azura Auth (Khớp với yêu cầu của bạn)
    AZ_USER = st.secrets["AZURA_USER"]
    AZ_PASS = st.secrets["AZURA_PASS"]
    
    # Các cấu hình khác
    GS_ID = st.secrets["GOOGLE_SHEET_ID"]
    GCP_JSON = st.secrets["GCP_SERVICE_ACCOUNT_JSON"]
    MAIL_USER = st.secrets["EMAIL_USER"]
    MAIL_PASS = st.secrets["EMAIL_PASS"]
except Exception as e:
    st.error(f"❌ Lỗi cấu hình Secret: Thiếu biến {e}. Hãy kiểm tra lại phần Settings > Secrets.")
    st.stop()

# ==========================================
# 2. ENGINE XỬ LÝ (BACKEND) - LẤY COOKIE THỦ CÔNG
# ==========================================
class AzuraTikTokEngine:
    def __init__(self):
        self.base_url = "https://portal.aluffm.com"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01"
        }

    def login(self):
        payload = {"UserName": AZ_USER, "Password": AZ_PASS, "RememberMe": "false"}
        login_headers = {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": self.headers["User-Agent"]}
        try:
            resp = requests.post(f"{self.base_url}/Account/Login", data=payload, headers=login_headers, allow_redirects=False, timeout=15)
            # Lấy cookie thủ công giống Mug-app
            cookies_dict = resp.cookies.get_dict()
            if cookies_dict:
                self.headers["Cookie"] = "; ".join([f"{k}={v}" for k, v in cookies_dict.items()])
                return True
            return False
        except:
            return False

    def fetch_orders(self, start_date, end_date):
        all_data = []
        page = 1
        start_str = start_date.strftime('%Y-%m-%d')
        end_str = end_date.strftime('%Y-%m-%d')
        status_msg = st.empty()
        
        while True:
            status_msg.text(f"⏳ Đang thu thập dữ liệu trang {page}...")
            try:
                resp = requests.get(f"{self.base_url}/OnBehalfOrder/List", params={"page": page, "rows": 50}, headers=self.headers, timeout=20)
                if resp.status_code != 200: break
                rows = resp.json().get("rows", [])
                if not rows: break

                for row in rows:
                    created_at = row.get("createdAt", "")[:10]
                    if created_at < start_str: # Dừng quét nếu quá ngày bắt đầu
                        status_msg.empty()
                        return all_data

                    if start_str <= created_at <= end_str:
                        if row.get("shippingPartnerString") == "Tiktok":
                            designs = row.get("orderProductDesigns", [])
                            job_ids = [str(d.get("jobId")) for d in designs if d.get("jobId")]
                            job_id_str = ", ".join(sorted(list(set(job_ids))))

                            all_data.append({
                                "Seller_Name": row.get("customer", ""),
                                "Tracking_Number": row.get("partnerBarcode", ""),
                                "Order_Number": row.get("customerOrder", ""),
                                "Job_ID": job_id_str,
                                "AzuraID": row.get("id", ""),
                                "Azura_Creat_At": created_at
                            })
                page += 1
                if page > 300: break
            except:
                break
        status_msg.empty()
        return all_data

    def update_sheet(self, data_list):
        try:
            creds = Credentials.from_service_account_info(json.loads(GCP_JSON), 
                    scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
            client = gspread.authorize(creds)
            sheet = client.open_by_key(GS_ID).sheet1

            final_rows = []
            for item in data_list:
                row = [""] * 12
                row[0], row[1], row[8], row[9], row[10], row[11] = item["Seller_Name"], item["Tracking_Number"], item["Order_Number"], item["Job_ID"], item["AzuraID"], item["Azura_Creat_At"]
                final_rows.append(row)

            sheet.append_rows(final_rows, value_input_option="USER_ENTERED")
            return True, f"✅ Đã ghi {len(final_rows)} dòng vào Sheet."
        except Exception as e:
            return False, f"❌ Lỗi Sheet: {str(e)}"

    def send_email(self, data_list, start_str, end_str, to_mail):
        try:
            count = len(data_list)
            has_job = sum(1 for d in data_list if d['Job_ID'])
            msg = MIMEMultipart()
            msg['Subject'] = f"[Azura TikTok] Báo cáo đơn hàng ({start_str} - {end_str})"
            msg['From'], msg['To'] = MAIL_USER, to_mail
            
            body = f"<h3>📊 Kết quả quét đơn TikTok</h3><p>Từ ngày {start_str} đến {end_str}</p><ul><li>Tổng đơn: {count}</li><li>Đã có Job ID: {has_job}</li><li>Chưa có Job ID: {count - has_job}</li></ul><p>🔗 <a href='https://docs.google.com/spreadsheets/d/{GS_ID}'>Mở Google Sheet</a></p>"
            msg.attach(MIMEText(body, 'html'))
            
            with smtplib.SMTP('smtp.gmail.com', 587) as server:
                server.starttls()
                server.login(MAIL_USER, MAIL_PASS.replace(" ", ""))
                server.sendmail(MAIL_USER, [x.strip() for x in to_mail.split(',')], msg.as_string())
            return True
        except:
            return False

# ==========================================
# 3. GIAO DIỆN (UI) - KHÔNG LOAD FULL DATA
# ==========================================
st.title("⚡ AZURA TIKTOK SYNC (ULTRA LIGHT)")

with st.form("main_form"):
    col1, col2 = st.columns(2)
    with col1:
        date_pick = st.date_input("Chọn khoảng ngày", (today_vn, today_vn))
    with col2:
        mail_to = st.text_input("Gửi báo cáo đến email", placeholder="email1, email2...")
    
    submit = st.form_submit_button("🚀 BẮT ĐẦU CHẠY", use_container_width=True)

if submit:
    if len(date_pick) == 2:
        s_d, e_d = date_pick
    else:
        s_d = e_d = date_pick[0]

    engine = AzuraTikTokEngine()
    
    with st.status("🛠️ Đang xử lý...", expanded=True) as status:
        st.write("🔑 Đang đăng nhập hệ thống...")
        if not engine.login():
            st.error("❌ Đăng nhập thất bại! Kiểm tra lại AZURA_USER/PASS trong Secret.")
            st.stop()
            
        st.write("🔍 Đang lấy dữ liệu Tiktok từ Portal...")
        results = engine.fetch_orders(s_d, e_d)
        
        if not results:
            status.update(label="Hoàn tất - Không có đơn mới.", state="complete")
            st.info("Không tìm thấy đơn hàng nào trong khoảng ngày này.")
            st.stop()
            
        st.write(f"📂 Tìm thấy {len(results)} đơn. Đang cập nhật Google Sheet...")
        success, msg = engine.update_sheet(results)
        st.write(msg)
        
        if success and mail_to:
            st.write("📧 Đang gửi email thông báo...")
            engine.send_email(results, str(s_d), str(e_d), mail_to)
            st.write("✅ Email đã được gửi.")
            
        status.update(label="🎉 Hoàn tất chương trình!", state="complete")

    # PHẦN TẢI FILE (DÙNG CSV MODULE CHO NHẸ)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["Seller_Name", "Tracking_Number", "Order_Number", "Job_ID", "AzuraID", "Azura_Creat_At"])
    writer.writeheader()
    writer.writerows(results)
    
    st.download_button(
        label="⬇️ Tải xuống bản sao dữ liệu (CSV)",
        data=output.getvalue().encode('utf-8'),
        file_name=f"azura_tiktok_{s_d}.csv",
        mime='text/csv',
        use_container_width=True
    )
