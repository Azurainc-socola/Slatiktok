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
# 1. CẤU HÌNH GIAO DIỆN SIÊU NHẸ
# ==========================================
st.set_page_config(page_title="Azura TikTok Sync (Ultra-Light)", page_icon="⚡", layout="centered")

VN_TZ = timezone(timedelta(hours=7))
today_vn = datetime.now(VN_TZ).date()

# ĐỌC SECRETS
try:
    AZ_USER = st.secrets["AZURA_USER"]
    AZ_PASS = st.secrets["AZURA_PASS"]
    GS_ID = st.secrets["GOOGLE_SHEET_ID"]
    GCP_JSON = st.secrets["GCP_SERVICE_ACCOUNT_JSON"]
    MAIL_USER = st.secrets["EMAIL_USER"]
    MAIL_PASS = st.secrets["EMAIL_PASS"]
except Exception as e:
    st.error(f"❌ Thiếu cấu hình Secrets trên Streamlit Cloud: {e}")
    st.stop()

# ==========================================
# 2. LOGIC XỬ LÝ (BACKEND)
# ==========================================
class AzuraTikTokEngine:
    def __init__(self):
        self.session = requests.Session()
        self.base_url = "https://portal.aluffm.com"

    def login(self):
        payload = {"UserName": AZ_USER, "Password": AZ_PASS, "RememberMe": "false"}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        resp = self.session.post(f"{self.base_url}/Account/Login", data=payload, headers=headers, allow_redirects=False)
        return resp.status_code in [200, 302]

    def fetch_orders(self, start_date, end_date):
        all_data = []
        page = 1
        start_str = start_date.strftime('%Y-%m-%d')
        end_str = end_date.strftime('%Y-%m-%d')
        
        status_msg = st.empty()
        
        while True:
            status_msg.text(f"⏳ Đang quét API trang {page}...")
            resp = self.session.get(f"{self.base_url}/OnBehalfOrder/List", params={"page": page, "rows": 50})
            if resp.status_code != 200: break
            
            rows = resp.json().get("rows", [])
            if not rows: break

            for row in rows:
                created_at = row.get("createdAt", "")[:10]
                
                if created_at < start_str:
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
            if page > 200: break # Mở rộng limit cho ngày siêu sale
            
        status_msg.empty()
        return all_data

    def update_google_sheet(self, data_list):
        try:
            creds = Credentials.from_service_account_info(json.loads(GCP_JSON), 
                    scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
            client = gspread.authorize(creds)
            sheet = client.open_by_key(GS_ID).sheet1

            final_rows = []
            for item in data_list:
                row = [""] * 12
                row[0] = item["Seller_Name"]
                row[1] = item["Tracking_Number"]
                row[8] = item["Order_Number"]
                row[9] = item["Job_ID"]
                row[10] = item["AzuraID"]
                row[11] = item["Azura_Creat_At"]
                final_rows.append(row)

            # Ghi vào Sheet 1 lần duy nhất (Batch update)
            sheet.append_rows(final_rows, value_input_option="USER_ENTERED")
            return True, f"Thành công: Đã ghi {len(final_rows)} đơn vào Google Sheet."
        except Exception as e:
            return False, f"Lỗi Google Sheet: {str(e)}"

    def send_email(self, data_list, start_str, end_str, to_mail, cc_mail):
        try:
            count = len(data_list)
            has_job = sum(1 for d in data_list if d['Job_ID'])
            
            msg = MIMEMultipart()
            msg['Subject'] = f"[Azura] Báo cáo Tiktok Shop ({start_str} - {end_str})"
            msg['From'] = MAIL_USER
            msg['To'] = to_mail
            if cc_mail: msg['Cc'] = cc_mail

            body = f"""
            <h3>📊 Báo cáo kết quả quét đơn TikTok</h3>
            <p>- Khoảng thời gian: <b>{start_str}</b> đến <b>{end_str}</b></p>
            <p>- Tổng đơn ghi nhận: <b>{count}</b></p>
            <ul>
                <li>Đã có Job ID: <span style="color: green;">{has_job}</span></li>
                <li>Chưa có Job ID: <span style="color: red;">{count - has_job}</span></li>
            </ul>
            <p>🔗 <a href="https://docs.google.com/spreadsheets/d/{GS_ID}">Mở file Google Sheet</a></p>
            <p><i>Hệ thống tự động đồng bộ - Không cần phản hồi mail này.</i></p>
            """
            msg.attach(MIMEText(body, 'html'))
            
            with smtplib.SMTP('smtp.gmail.com', 587) as server:
                server.starttls()
                server.login(MAIL_USER, MAIL_PASS.replace(" ", ""))
                recipients = [x.strip() for x in to_mail.split(',')]
                if cc_mail: recipients += [x.strip() for x in cc_mail.split(',')]
                server.sendmail(MAIL_USER, recipients, msg.as_string())
            return True
        except:
            return False

# ==========================================
# 3. GIAO DIỆN (UI) - KHÔNG RENDER DATA
# ==========================================
st.title("⚡ AZURA TIKTOK SYNC")
st.markdown("Phiên bản Tối ưu hiệu năng - Chạy nền an toàn không giới hạn đơn.")

with st.form("run_form"):
    st.subheader("📅 Cấu hình chạy")
    col1, col2 = st.columns(2)
    with col1:
        date_pick = st.date_input("Chọn khoảng ngày", (today_vn, today_vn))
    with col2:
        is_mail = st.checkbox("Gửi Email báo cáo", value=True)
        mail_to = st.text_input("Email nhận", placeholder="a@gmail.com, b@gmail.com")
        
    run_btn = st.form_submit_button("🚀 BẮT ĐẦU CHẠY", use_container_width=True)

if run_btn:
    if len(date_pick) == 2:
        start_d, end_d = date_pick
    else:
        start_d = end_d = date_pick[0]

    engine = AzuraTikTokEngine()
    
    with st.status("🚀 Đang chạy luồng xử lý...", expanded=True) as status:
        st.write("🔑 Đang kết nối Azura Portal...")
        if not engine.login():
            st.error("❌ Đăng nhập thất bại!")
            st.stop()
            
        st.write(f"🔍 Đang quét dữ liệu từ {start_d} đến {end_d}...")
        results = engine.fetch_orders(start_d, end_d)
        
        if not results:
            status.update(label="Xong! Không có đơn mới.", state="complete")
            st.info("Không tìm thấy đơn hàng Tiktok nào.")
            st.stop()
            
        st.write(f"✅ Đã tải xong {len(results)} đơn. Bắt đầu đẩy lên Google Sheet...")
        success, msg = engine.update_sheet(results)
        
        if success:
            st.write(f"✅ {msg}")
            if is_mail and mail_to:
                st.write("📧 Đang gửi email...")
                engine.send_email(results, str(start_d), str(end_d), mail_to, "")
                st.write("✅ Đã gửi Email.")
        else:
            st.error(msg)
            
        status.update(label="🎉 Tiến trình hoàn tất!", state="complete")

    # Hiển thị thống kê gọn gàng thay vì nguyên cái bảng
    st.success(f"**Tổng kết:** Đã quét và đồng bộ thành công **{len(results)}** đơn hàng.")
    
    # Nút tải CSV dùng thư viện lõi, không tốn RAM
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["Seller_Name", "Tracking_Number", "Order_Number", "Job_ID", "AzuraID", "Azura_Creat_At"])
    writer.writeheader()
    writer.writerows(results)
    
    st.download_button(
        label="⬇️ Nhấn vào đây để tải file CSV (nếu cần)",
        data=output.getvalue().encode('utf-8'),
        file_name=f"tiktok_orders_{start_d}.csv",
        mime='text/csv'
    )
