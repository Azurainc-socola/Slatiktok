import streamlit as st
import json
import requests
import smtplib
import io
import csv
import re
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

# ĐỌC SECRETS - CÓ TÍNH NĂNG STRIP CHỐNG DẤU CÁCH THỪA
try:
    AZ_USER = str(st.secrets["AZURA_USER"]).strip()
    AZ_PASS = str(st.secrets["AZURA_PASS"]).strip()
    GS_ID = str(st.secrets["GOOGLE_SHEET_ID"]).strip()
    MAIL_USER = str(st.secrets["EMAIL_USER"]).strip()
    MAIL_PASS = str(st.secrets["EMAIL_PASS"]).replace(" ", "").strip()
    GCP_JSON = str(st.secrets["GCP_SERVICE_ACCOUNT_JSON"]).strip()
except Exception as e:
    st.error(f"❌ Lỗi cấu hình Secret: Thiếu biến {e}.")
    st.stop()

# ==========================================
# 2. ENGINE XỬ LÝ - BÊ 100% LOGIN TỪ MUG-APP
# ==========================================
class AzuraTikTokEngine:
    def __init__(self):
        self.base_url = "https://portal.aluffm.com"
        self.session = requests.Session()
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "X-Requested-With": "XMLHttpRequest"
        }

    def login(self):
        try:
            # BƯỚC 1: GET ĐỂ LẤY TOKEN ẨN (Y HỆT MUG-APP)
            r1 = self.session.get(f"{self.base_url}/Login", timeout=15)
            
            # TÌM TOKEN BẰNG REGEX (Y HỆT MUG-APP)
            match = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r1.text)
            token = match.group(1) if match else ""
            
            # BƯỚC 2: POST KÈM TOKEN VÀ REFERER (Y HỆT MUG-APP)
            payload = {
                "UserName": AZ_USER, 
                "Password": AZ_PASS, 
                "__RequestVerificationToken": token, 
                "RememberMe": "false"
            }
            self.session.post(
                f"{self.base_url}/Login", 
                data=payload, 
                headers={"Referer": f"{self.base_url}/Login"}, 
                allow_redirects=False
            )

            # BƯỚC 3: CHECK COOKIE ASP.NET (Y HỆT MUG-APP)
            ck_dict = self.session.cookies.get_dict()
            if '.AspNetCore.Identity.Application' in ck_dict:
                self.headers["Cookie"] = "; ".join([f"{k}={v}" for k, v in ck_dict.items()])
                return True, "Thành công"
            else:
                return False, "Đăng nhập thất bại: Không tìm thấy Cookie hệ thống."
        except Exception as e:
            return False, f"Lỗi mạng/hệ thống: {str(e)}"

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
                    # Dừng khi chạm đến ngày cũ hơn
                    if created_at < start_str:
                        status_msg.empty()
                        return all_data

                    if start_str <= created_at <= end_str:
                        if row.get("shippingPartnerString") == "Tiktok":
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
                if page > 300: break
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

            sheet.append_rows(final_rows, value_input_option="USER_ENTERED")
            return True, f"✅ Đã ghi {len(final_rows)} đơn vào Google Sheet."
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
            <p>- Ngày ghi nhận: <b>{start_str}</b> đến <b>{end_str}</b></p>
            <p>- Tổng đơn TikTok: <b>{count}</b></p>
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
# 3. GIAO DIỆN STREAMLIT
# ==========================================
st.title("⚡ AZURA TIKTOK SYNC")

with st.form("main_form"):
    col1, col2 = st.columns(2)
    with col1:
        date_pick = st.date_input("Chọn khoảng ngày", (today_vn, today_vn))
    with col2:
        is_mail = st.checkbox("Gửi Email báo cáo", value=True)
        mail_to = st.text_input("Gửi đến Email", placeholder="email1, email2...")
    
    run_btn = st.form_submit_button("🚀 BẮT ĐẦU CHẠY", use_container_width=True)

if run_btn:
    if len(date_pick) == 2:
        s_d, e_d = date_pick
    else:
        s_d = e_d = date_pick[0]

    engine = AzuraTikTokEngine()
    
    with st.status("🛠️ Đang xử lý...", expanded=True) as status:
        st.write("🔑 Đang đăng nhập hệ thống...")
        login_ok, login_msg = engine.login()
        
        if not login_ok:
            st.error(f"❌ Lỗi đăng nhập: {login_msg}")
            st.stop()
            
        st.write(f"🔍 Đang lấy dữ liệu từ {s_d} đến {e_d}...")
        results = engine.fetch_orders(s_d, e_d)
        
        if not results:
            status.update(label="Hoàn tất - Không có dữ liệu", state="complete")
            st.info("Không tìm thấy đơn hàng nào.")
            st.stop()
            
        st.write(f"📂 Đã tải {len(results)} đơn. Đang ghi Google Sheet...")
        success, sheet_msg = engine.update_sheet(results)
        st.write(sheet_msg)
        
        if success and is_mail and mail_to:
            st.write("📧 Đang gửi email báo cáo...")
            if engine.send_email(results, str(s_d), str(e_d), mail_to):
                st.write("✅ Email đã được gửi.")
            
        status.update(label="🎉 Hoàn thành!", state="complete")

    st.success(f"**Tổng kết:** Đã đồng bộ thành công **{len(results)}** đơn hàng.")
    
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["Seller_Name", "Tracking_Number", "Order_Number", "Job_ID", "AzuraID", "Azura_Creat_At"])
    writer.writeheader()
    writer.writerows(results)
    
    st.download_button("⬇️ Tải xuống CSV", output.getvalue().encode('utf-8'), f"tiktok_sync_{s_d}.csv", "text/csv", use_container_width=True)
