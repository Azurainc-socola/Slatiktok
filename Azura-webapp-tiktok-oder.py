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
# 1. CẤU HÌNH HỆ THỐNG & GIAO DIỆN
# ==========================================
st.set_page_config(page_title="Azura TikTok Sync Pro", page_icon="🛍️", layout="wide")

VN_TZ = timezone(timedelta(hours=7))
today_vn = datetime.now(VN_TZ).date()

# ĐỌC SECRETS (Bảo mật tuyệt đối - Không hiện lên UI)
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
            status_msg.text(f"⏳ Đang quét trang {page}...")
            resp = self.session.get(f"{self.base_url}/OnBehalfOrder/List", params={"page": page, "rows": 50})
            if resp.status_code != 200: break
            
            rows = resp.json().get("rows", [])
            if not rows: break

            for row in rows:
                created_at = row.get("createdAt", "")[:10]
                
                # Điều kiện dừng: Nếu đơn đã cũ hơn ngày bắt đầu thì không quét tiếp các trang sau
                if created_at < start_str:
                    status_msg.empty()
                    return all_data

                # Lọc: Đúng khoảng ngày và đúng Partner là Tiktok
                if start_str <= created_at <= end_str:
                    if row.get("shippingPartnerString") == "Tiktok":
                        # Xử lý Job ID (Gom nhiều Job ID thành chuỗi)
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
            if page > 100: break # Giới hạn an toàn
            
        status_msg.empty()
        return all_data

    def update_google_sheet(self, data_list):
        try:
            creds = Credentials.from_service_account_info(json.loads(GCP_JSON), 
                    scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
            client = gspread.authorize(creds)
            sheet = client.open_by_key(GS_ID).sheet1

            # Mapping dữ liệu vào đúng cột (A, B, I, J, K, L)
            final_rows = []
            for item in data_list:
                row = [""] * 12
                row[0] = item["Seller_Name"]    # Cột A
                row[1] = item["Tracking_Number"] # Cột B
                row[8] = item["Order_Number"]    # Cột I
                row[9] = item["Job_ID"]          # Cột J
                row[10] = item["AzuraID"]        # Cột K
                row[11] = item["Azura_Creat_At"] # Cột L
                final_rows.append(row)

            sheet.append_rows(final_rows, value_input_option="USER_ENTERED")
            return True, f"Thành công: Đã ghi {len(final_rows)} đơn vào Sheet."
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
            <p>- Tổng đơn tìm thấy: <b>{count}</b></p>
            <ul>
                <li>Đã có Job ID: {has_job}</li>
                <li>Chưa có Job ID: {count - has_job}</li>
            </ul>
            <p>📍 Dữ liệu đã được cập nhật vào Google Sheet.</p>
            <p>🔗 <a href="https://docs.google.com/spreadsheets/d/{GS_ID}">Mở file Google Sheet</a></p>
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
# 3. GIAO DIỆN NGƯỜI DÙNG (FOREND)
# ==========================================
st.title("🚀 AZURA TIKTOK SYNC - PHIÊN BẢN NÂNG CẤP")

with st.sidebar:
    st.header("📅 Lọc Theo Ngày")
    # Mặc định là ngày hiện tại
    date_pick = st.date_input("Chọn khoảng ngày", (today_vn, today_vn))
    
    if len(date_pick) == 2:
        start_d, end_d = date_pick
    else:
        start_d = end_d = date_pick[0]

    st.divider()
    st.header("📧 Gửi Báo Cáo")
    is_mail = st.checkbox("Gửi Email sau khi xong", value=True)
    if is_mail:
        mail_to = st.text_input("Người nhận (To)", placeholder="email1, email2...")
        mail_cc = st.text_input("Đồng gửi (CC)")

    st.divider()
    run_btn = st.button("🚀 BẮT ĐẦU CHẠY", type="primary", use_container_width=True)

if run_btn:
    engine = AzuraTikTokEngine()
    
    with st.status("🚀 Đang xử lý...", expanded=True) as status:
        st.write("🔑 Đang kết nối Azura Portal...")
        if not engine.login():
            st.error("❌ Đăng nhập Portal thất bại! Kiểm tra lại AZURA_USER/PASS trong Secrets.")
            st.stop()
            
        st.write(f"🔍 Đang tìm đơn Tiktok từ {start_d} đến {end_d}...")
        results = engine.fetch_orders(start_d, end_d)
        
        if not results:
            status.update(label="Hoàn tất - Không có đơn", state="complete")
            st.info("Không tìm thấy đơn hàng Tiktok nào trong khoảng ngày đã chọn.")
            st.stop()
            
        st.write(f"✅ Tìm thấy {len(results)} đơn. Đang ghi vào Google Sheet...")
        success, msg = engine.update_sheet(results)
        
        if success:
            st.write(f"✅ {msg}")
            if is_mail and mail_to:
                st.write("📧 Đang gửi email báo cáo...")
                if engine.send_email(results, str(start_d), str(end_d), mail_to, mail_cc):
                    st.write("✅ Đã gửi Email báo cáo thành công.")
        else:
            st.error(msg)
            
        status.update(label="🎉 Tất cả đã hoàn tất!", state="complete")

    # ==========================================
    # PHẦN HIỂN THỊ DỮ LIỆU TỐI ƯU (PREVIEW)
    # ==========================================
    st.divider()
    st.subheader(f"📊 Preview dữ liệu ({len(results)} đơn)")
    df = pd.DataFrame(results)
    
    # Chỉ load 50 dòng ra Web để tránh treo trình duyệt
    st.dataframe(df.head(50), use_container_width=True)
    
    if len(results) > 50:
        st.caption(f"*(Chỉ hiển thị 50 dòng đầu để tối ưu tốc độ. Toàn bộ {len(results)} đơn đã có trong Sheet)*")

    # Nút tải CSV nhanh
    csv = df.to_csv(index=False).encode('utf-8')
    st.download_button("⬇️ Tải file CSV kết quả", data=csv, file_name=f"tiktok_orders_{start_d}.csv", mime='text/csv')
