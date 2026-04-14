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
# THIẾT LẬP GIAO DIỆN & THỜI GIAN
# ==========================================
st.set_page_config(page_title="Azura TikTok Sync", page_icon="⚡", layout="centered")

VN_TZ = timezone(timedelta(hours=7))
today_vn = datetime.now(VN_TZ).date()

# ĐỌC SECRETS
try:
    AZ_USER = str(st.secrets["AZURA_USER"]).strip()
    AZ_PASS = str(st.secrets["AZURA_PASS"]).strip()
    GS_ID = str(st.secrets["GOOGLE_SHEET_ID"]).strip()
    MAIL_USER = str(st.secrets["EMAIL_USER"]).strip()
    MAIL_PASS = str(st.secrets["EMAIL_PASS"]).replace(" ", "").strip()
    GCP_JSON = str(st.secrets["GCP_SERVICE_ACCOUNT_JSON"]).strip()
except Exception as e:
    st.error(f"❌ Thiếu biến Secret: {e}")
    st.stop()

# ==========================================
# GIAO DIỆN STREAMLIT
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

# ==========================================
# LUỒNG XỬ LÝ CHÍNH (BÊ NGUYÊN TỪ MUG-APP.PY)
# ==========================================
if run_btn:
    if len(date_pick) == 2:
        start_date, end_date = date_pick
    else:
        start_date = end_date = date_pick[0]

    with st.status("🚀 Đang khởi chạy hệ thống...", expanded=True) as status:
        
        # ---------------------------------------------------------
        # 1. LOGIN PORTAL (COPY Y XÌ ĐÚC MUG-APP.PY)
        # ---------------------------------------------------------
        st.write("🌐 Đang đăng nhập hệ thống Portal...")
        session = requests.Session()
        cookie_str = ""
        
        try:
            r1 = session.get("https://portal.aluffm.com/Login", timeout=15)
            # Bắt token y hệt Mug-app
            match = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r1.text)
            if not match:
                st.error("❌ Lỗi: Không bắt được RequestVerificationToken.")
                st.stop()
            token = match.group(1)
            
            payload = {"UserName": AZ_USER, "Password": AZ_PASS, "__RequestVerificationToken": token, "RememberMe": "false"}
            session.post("https://portal.aluffm.com/Login", data=payload, headers={"Referer": "https://portal.aluffm.com/Login"}, allow_redirects=False)

            ck_dict = session.cookies.get_dict()
            if '.AspNetCore.Identity.Application' in ck_dict:
                cookie_str = "; ".join([f"{k}={v}" for k, v in ck_dict.items()])
                st.write("✅ Lấy Cookie thành công!")
            else:
                st.error("❌ Đăng nhập thất bại: Sai tài khoản hoặc mật khẩu.")
                st.stop()
        except Exception as e:
            st.error(f"❌ Lỗi mạng lúc đăng nhập: {e}")
            st.stop()

        # ---------------------------------------------------------
        # 2. QUÉT ĐƠN HÀNG (DÙNG REQUESTS MỚI + COOKIE NHƯ MUG-APP)
        # ---------------------------------------------------------
        start_str = start_date.strftime('%Y-%m-%d')
        end_str = end_date.strftime('%Y-%m-%d')
        st.write(f"🔍 Đang quét đơn hàng Tiktok từ {start_str} đến {end_str}...")
        
        all_data = []
        page = 1
        status_msg = st.empty()
        
        while True:
            status_msg.text(f"⏳ Đang quét trang {page}...")
            # Header và Params y hệt Mug-app (dùng pageNumber thay vì page)
            headers = {'Cookie': cookie_str, 'X-Requested-With': 'XMLHttpRequest'}
            res = requests.get("https://portal.aluffm.com/OnBehalfOrder/List", headers=headers, params={"pageSize": 50, "pageNumber": page}, timeout=20)
            
            if res.status_code != 200: break
            rows = res.json().get("rows", [])
            if not rows: break

            stop_page = False
            for row in rows:
                created_at = row.get("createdAt", "")[:10]
                
                # Check điều kiện dừng
                if created_at < start_str:
                    stop_page = True
                    break

                if start_str <= created_at <= end_str:
                    if row.get("shippingPartnerString") == "Tiktok":
                        # Gom Job ID
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
                        
            if stop_page: break
            page += 1
            if page > 300: break # Break an toàn
            
        status_msg.empty()
        
        if not all_data:
            status.update(label="Hoàn tất - Không có dữ liệu", state="complete")
            st.info("Không tìm thấy đơn hàng Tiktok nào.")
            st.stop()

        # ---------------------------------------------------------
        # 3. GHI GOOGLE SHEET
        # ---------------------------------------------------------
        st.write(f"📂 Tìm thấy {len(all_data)} đơn. Đang ghi Google Sheet...")
        try:
            creds = Credentials.from_service_account_info(
                json.loads(GCP_JSON), 
                scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
            )
            client = gspread.authorize(creds)
            sheet = client.open_by_key(GS_ID).sheet1

            final_rows = []
            for item in all_data:
                r = [""] * 12
                r[0], r[1], r[8], r[9], r[10], r[11] = item["Seller_Name"], item["Tracking_Number"], item["Order_Number"], item["Job_ID"], item["AzuraID"], item["Azura_Creat_At"]
                final_rows.append(r)

            sheet.append_rows(final_rows, value_input_option="USER_ENTERED")
            st.write(f"✅ Đã ghi thành công {len(final_rows)} dòng.")
            success_sheet = True
        except Exception as e:
            st.error(f"❌ Lỗi ghi Sheet: {e}")
            success_sheet = False

        # ---------------------------------------------------------
        # 4. GỬI EMAIL
        # ---------------------------------------------------------
        if success_sheet and is_mail and mail_to:
            st.write("📧 Đang gửi email báo cáo...")
            try:
                count = len(all_data)
                has_job = sum(1 for d in all_data if d['Job_ID'])
                msg = MIMEMultipart()
                msg['Subject'] = f"[Azura TikTok] Báo cáo đồng bộ ({start_str} - {end_str})"
                msg['From'] = MAIL_USER
                msg['To'] = mail_to
                
                body = f"<h3>📊 Kết quả quét Tiktok Shop</h3><p>Từ {start_str} đến {end_str}</p><ul><li>Tổng đơn: {count}</li><li>Đã có Job ID: {has_job}</li><li>Chưa có Job ID: {count - has_job}</li></ul><p>🔗 <a href='https://docs.google.com/spreadsheets/d/{GS_ID}'>Xem Google Sheet</a></p>"
                msg.attach(MIMEText(body, 'html'))
                
                server = smtplib.SMTP('smtp.gmail.com', 587)
                server.starttls()
                server.login(MAIL_USER, MAIL_PASS)
                server.sendmail(MAIL_USER, [x.strip() for x in mail_to.split(',')], msg.as_string())
                server.quit()
                st.write("✅ Email đã được gửi.")
            except Exception as e:
                st.error(f"❌ Lỗi gửi mail: {e}")

        status.update(label="🎉 HOÀN TẤT QUY TRÌNH!", state="complete")

    st.success(f"**Tổng kết:** Đã đồng bộ thành công **{len(all_data)}** đơn hàng.")
    
    # Nút tải CSV
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["Seller_Name", "Tracking_Number", "Order_Number", "Job_ID", "AzuraID", "Azura_Creat_At"])
    writer.writeheader()
    writer.writerows(all_data)
    st.download_button("⬇️ Tải file CSV", output.getvalue().encode('utf-8'), f"tiktok_sync_{start_str}.csv", "text/csv", use_container_width=True)
