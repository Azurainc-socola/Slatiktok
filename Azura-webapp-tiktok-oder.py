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
st.set_page_config(page_title="Azura TikTok Sync Final", page_icon="⚡", layout="centered")

VN_TZ = timezone(timedelta(hours=7))
today_vn = datetime.now(VN_TZ).date()

# ĐỌC SECRETS - BẢO VỆ CHỐNG DẤU CÁCH (WHITESPACE PROTECTION)
try:
    AZ_USER = str(st.secrets["AZURA_USER"]).strip()
    AZ_PASS = str(st.secrets["AZURA_PASS"]).strip()
    
    GS_ID = str(st.secrets["GOOGLE_SHEET_ID"]).strip()
    
    MAIL_USER = str(st.secrets["EMAIL_USER"]).strip()
    # Xóa cả khoảng trắng ở giữa và đầu cuối cho App Password
    MAIL_PASS = str(st.secrets["EMAIL_PASS"]).replace(" ", "").strip()
    
    GCP_JSON = str(st.secrets["GCP_SERVICE_ACCOUNT_JSON"]).strip()
except Exception as e:
    st.error(f"❌ Lỗi cấu hình Secret: Thiếu biến {e}. Hãy kiểm tra lại phần Settings > Secrets.")
    st.stop()

# ==========================================
# 2. ENGINE XỬ LÝ (BACKEND) - COOKIE THỦ CÔNG
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
        login_headers = {
            "Content-Type": "application/x-www-form-urlencoded", 
            "User-Agent": self.headers["User-Agent"]
        }
        try:
            # Lấy cookie thủ công giống Mug-app.py để tránh mất session
            resp = requests.post(f"{self.base_url}/Account/Login", data=payload, headers=login_headers, allow_redirects=False, timeout=15)
            cookies_dict = resp.cookies.get_dict()
            if cookies_dict:
                self.headers["Cookie"] = "; ".join([f"{k}={v}" for k, v in cookies_dict.items()])
                return True, "Thành công"
            return False, "Portal không trả về Cookie. Kiểm tra lại User/Pass."
        except Exception as e:
            return False, str(e)

    def fetch_orders(self, start_date, end_date):
        all_data = []
        page = 1
        start_str = start_date.strftime('%Y-%m-%d')
        end_str = end_date.strftime('%Y-%m-%d')
        status_msg = st.empty()
        
        while True:
            status_msg.text(f"⏳ Đang thu thập dữ liệu trang {page}...")
            try:
                resp = requests.get(
                    f"{self.base_url}/OnBehalfOrder/List", 
                    params={"page": page, "rows": 50}, 
                    headers=self.headers, 
                    timeout=20
                )
                if resp.status_code != 200: break
                
                rows = resp.json().get("rows", [])
                if not rows: break

                for row in rows:
                    created_at = row.get("createdAt", "")[:10]
                    # Dừng quét ngay khi gặp đơn cũ hơn ngày bắt đầu để tối ưu RAM
                    if created_at < start_str:
                        status_msg.empty()
                        return all_data

                    if start_str <= created_at <= end_str:
                        if row.get("shippingPartnerString") == "Tiktok":
                            # Gom nhóm Job ID
                            designs = row.get("orderProductDesigns", [])
                            job_ids = [str(d.get("jobId")) for d in designs if d.get("jobId") is not None]
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
                if page > 300: break # Giới hạn an toàn
            except:
                break
        status_msg.empty()
        return all_data

    def update_sheet(self, data_list):
        try:
            creds = Credentials.from_service_account_info(
                json.loads(GCP_JSON), 
                scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
            )
            client = gspread.authorize(creds)
            sheet = client.open_by_key(GS_ID).sheet1

            # Mapping dữ liệu chuẩn cột A, B, I, J, K, L
            final_rows = []
            for item in data_list:
                row = [""] * 12
                row[0] = item["Seller_Name"]      # A
                row[1] = item["Tracking_Number"]  # B
                row[8] = item["Order_Number"]     # I
                row[9] = item["Job_ID"]           # J
                row[10] = item["AzuraID"]         # K
                row[11] = item["Azura_Creat_At"]  # L
                final_rows.append(row)

            sheet.append_rows(final_rows, value_input_option="USER_ENTERED")
            return True, f"✅ Đã cập nhật thành công {len(final_rows)} đơn vào Google Sheet."
        except Exception as e:
            return False, f"❌ Lỗi ghi Sheet: {str(e)}"

    def send_email(self, data_list, start_str, end_str, to_mail):
        try:
            count = len(data_list)
            has_job = sum(1 for d in data_list if d['Job_ID'])
            msg = MIMEMultipart()
            msg['Subject'] = f"[Azura TikTok] Báo cáo đồng bộ ({start_str} - {end_str})"
            msg['From'] = MAIL_USER
            msg['To'] = to_mail
            
            body = f"""
            <h3>📊 Kết quả quét đơn TikTok Shop</h3>
            <p>- Khoảng thời gian: <b>{start_str}</b> đến <b>{end_str}</b></p>
            <p>- Tổng đơn TikTok ghi nhận: <b>{count}</b></p>
            <ul>
                <li>Đã có Job ID: <span style="color: green;">{has_job}</span></li>
                <li>Chưa có Job ID: <span style="color: red;">{count - has_job}</span></li>
            </ul>
            <p>🔗 <a href='https://docs.google.com/spreadsheets/d/{GS_ID}'>Xem Google Sheet</a></p>
            """
            msg.attach(MIMEText(body, 'html'))
            
            with smtplib.SMTP('smtp.gmail.com', 587) as server:
                server.starttls()
                server.login(MAIL_USER, MAIL_PASS)
                server.sendmail(MAIL_USER, [x.strip() for x in to_mail.split(',')], msg.as_string())
            return True
        except:
            return False

# ==========================================
# 3. GIAO DIỆN (UI) - TỐI ƯU HIỆU NĂNG
# ==========================================
st.title("⚡ AZURA TIKTOK SYNC (FINAL PRO)")
st.markdown("Hệ thống đồng bộ TikTok - Portal - Google Sheet.")

with st.form("main_form"):
    col1, col2 = st.columns(2)
    with col1:
        date_pick = st.date_input("Chọn ngày", (today_vn, today_vn))
    with col2:
        is_mail = st.checkbox("Gửi Email báo cáo", value=True)
        mail_to = st.text_input("Email nhận", placeholder="email1, email2...")
    
    run_btn = st.form_submit_button("🚀 BẮT ĐẦU CHẠY", use_container_width=True)

if run_btn:
    if len(date_pick) == 2:
        s_d, e_d = date_pick
    else:
        s_d = e_d = date_pick[0]

    engine = AzuraTikTokEngine()
    
    with st.status("🛠️ Đang xử lý dữ liệu...", expanded=True) as status:
        st.write("🔑 Đang đăng nhập hệ thống nội bộ...")
        login_ok, login_msg = engine.login()
        if not login_ok:
            st.error(f"❌ Lỗi đăng nhập: {login_msg}")
            st.stop()
            
        st.write(f"🔍 Quét đơn Tiktok từ {s_d} đến {e_d}...")
        results = engine.fetch_orders(s_d, e_d)
        
        if not results:
            status.update(label="Hoàn tất - Không có đơn mới", state="complete")
            st.info("Không tìm thấy đơn hàng TikTok nào trong thời gian này.")
            st.stop()
            
        st.write(f"📂 Đã tải xong {len(results)} đơn. Đang đẩy lên Google Sheet...")
        success, sheet_msg = engine.update_sheet(results)
        st.write(sheet_msg)
        
        if success and is_mail and mail_to:
            st.write("📧 Đang gửi email tổng hợp...")
            if engine.send_email(results, str(s_d), str(e_d), mail_to):
                st.write("✅ Đã gửi Email thành công.")
            
        status.update(label="🎉 Tiến trình hoàn tất!", state="complete")

    st.success(f"**Tổng kết:** Đã đồng bộ thành công **{len(results)}** đơn hàng.")
    
    # TẢI DỮ LIỆU CSV (DÙNG MODULE CSV LÕI CHO NHẸ RAM)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["Seller_Name", "Tracking_Number", "Order_Number", "Job_ID", "AzuraID", "Azura_Creat_At"])
    writer.writeheader()
    writer.writerows(results)
    
    st.download_button(
        label="⬇️ Tải xuống dữ liệu vừa quét (CSV)",
        data=output.getvalue().encode('utf-8'),
        file_name=f"tiktok_sync_{s_d}.csv",
        mime='text/csv',
        use_container_width=True
    )
