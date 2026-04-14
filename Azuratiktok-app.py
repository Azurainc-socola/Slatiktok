import os
import json
import requests
import smtplib
import re
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import gspread
from google.oauth2.service_account import Credentials

# ==========================================
# 1. CẤU HÌNH HỆ THỐNG & THỜI GIAN
# ==========================================
VN_TZ = timezone(timedelta(hours=7))
now_vn = datetime.now(VN_TZ)
TARGET_DATE = now_vn.strftime('%Y-%m-%d') 

# Lấy thông tin từ GitHub Secrets và dùng .strip() để chống lỗi khoảng trắng/xuống dòng
AZURA_USER = str(os.getenv("AZURA_USER", "")).strip()
AZURA_PASS = str(os.getenv("AZURA_PASS", "")).strip()
SHEET_ID = str(os.getenv("GOOGLE_SHEET_ID", "")).strip()
GCP_JSON = str(os.getenv("GCP_SERVICE_ACCOUNT_JSON", "")).strip()
EMAIL_USER = str(os.getenv("EMAIL_USER", "")).strip()
EMAIL_PASS = str(os.getenv("EMAIL_PASS", "")).strip()
EMAIL_RECEIVERS = str(os.getenv("EMAIL_RECEIVERS", "")).strip()

class AzuraTikTokAutomation:
    def __init__(self):
        self.session = requests.Session()
        self.base_url = "https://portal.aluffm.com"
        self.login_url = f"{self.base_url}/Login"
        self.order_api = f"{self.base_url}/OnBehalfOrder/List"
        self.sheet_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
        self.cookie_str = ""

    def login(self):
        """Đăng nhập lấy Cookie hệ thống"""
        try:
            print("🌐 Đang kết nối hệ thống Portal để lấy Token...")
            r1 = self.session.get(self.login_url, timeout=15)
            match = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r1.text)
            
            if not match:
                print("❌ Lỗi: Không bắt được RequestVerificationToken.")
                return False
            
            token = match.group(1)
            payload = {
                "UserName": AZURA_USER, 
                "Password": AZURA_PASS, 
                "__RequestVerificationToken": token, 
                "RememberMe": "false"
            }
            headers = {"Referer": self.login_url}
            self.session.post(self.login_url, data=payload, headers=headers, allow_redirects=False)

            ck_dict = self.session.cookies.get_dict()
            if '.AspNetCore.Identity.Application' in ck_dict:
                self.cookie_str = "; ".join([f"{k}={v}" for k, v in ck_dict.items()])
                self.session.headers.update({
                    'Cookie': self.cookie_str, 
                    'X-Requested-With': 'XMLHttpRequest'
                })
                print("✅ Đăng nhập Azura Portal thành công.")
                return True
            else:
                print("❌ Đăng nhập thất bại: Kiểm tra lại tài khoản/mật khẩu.")
                return False
        except Exception as e:
            print(f"❌ Lỗi mạng lúc đăng nhập: {e}")
            return False

    def fetch_tiktok_orders(self):
        """Quét đơn hàng và xử lý lệch múi giờ UTC -> VN"""
        all_matches = []
        page = 1
        stop_searching = False

        print(f"🚀 Bắt đầu quét đơn hàng ngày: {TARGET_DATE} (Giờ VN)...")

        while not stop_searching:
            params = {"pageSize": 50, "pageNumber": page}
            try:
                resp = self.session.get(self.order_api, params=params, timeout=20)
                if resp.status_code != 200: break
                
                data = resp.json()
                rows = data.get("rows", [])
                if not rows: break

                for row in rows:
                    created_at_raw = row.get("createdAt", "")
                    order_date_vn = ""
                    
                    if created_at_raw:
                        # Convert UTC string to Vietnam Time
                        # API format: 2024-05-20T10:00:00Z
                        dt_utc = datetime.strptime(created_at_raw[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                        dt_vn = dt_utc.astimezone(VN_TZ)
                        order_date_vn = dt_vn.strftime('%Y-%m-%d')

                    if row.get("shippingPartnerString") == "Tiktok":
                        if order_date_vn == TARGET_DATE:
                            row['date_vn_formatted'] = order_date_vn
                            all_matches.append(self.process_row_data(row))
                        elif order_date_vn < TARGET_DATE:
                            # Nếu đã gặp đơn cũ hơn ngày cần tìm thì có thể dừng (do đơn sắp xếp mới nhất lên đầu)
                            stop_searching = True
                
                # Kiểm tra đơn cuối cùng của trang để quyết định có sang trang tiếp không
                last_row_raw = rows[-1].get("createdAt", "")
                if last_row_raw:
                    last_dt_vn = datetime.strptime(last_row_raw[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).astimezone(VN_TZ)
                    if last_dt_vn.strftime('%Y-%m-%d') < TARGET_DATE:
                        stop_searching = True

                page += 1
                if page > 200: break # Giới hạn an toàn
            except Exception as e:
                print(f"❌ Lỗi dữ liệu trang {page}: {e}")
                break
                
        return all_matches

    def process_row_data(self, row):
        """Mapping dữ liệu theo cột Sheet (A, B, I, J, K, L)"""
        designs = row.get("orderProductDesigns", [])
        job_ids = [str(d.get("jobId")) for d in designs if d.get("jobId") is not None]
        job_id_str = ", ".join(sorted(list(set(job_ids)))) if job_ids else ""

        return {
            "A": row.get("customer", ""),
            "B": row.get("partnerBarcode", ""),
            "I": row.get("customerOrder", ""),
            "J": job_id_str,
            "K": row.get("id", ""),
            "L": row.get("date_vn_formatted", "") # Sử dụng ngày đã convert sang giờ VN
        }

    def update_google_sheet(self, data_list):
        """Ghi dữ liệu vào Google Sheet"""
        if not data_list: return 0
        if not GCP_JSON:
            print("❌ Lỗi: Thiếu cấu hình GCP_SERVICE_ACCOUNT_JSON trong Secrets.")
            return -1
        
        try:
            creds_dict = json.loads(GCP_JSON)
            scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
            creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
            client = gspread.authorize(creds)
            sheet = client.open_by_key(SHEET_ID).sheet1

            rows_to_append = []
            for item in data_list:
                row_data = [""] * 12 
                row_data[0] = item["A"] # Cột A
                row_data[1] = item["B"] # Cột B
                row_data[8] = item["I"] # Cột I
                row_data[9] = item["J"] # Cột J
                row_data[10] = item["K"] # Cột K
                row_data[11] = item["L"] # Cột L
                rows_to_append.append(row_data)

            sheet.append_rows(rows_to_append, value_input_option="USER_ENTERED")
            return len(rows_to_append)
        except Exception as e:
            print(f"❌ Lỗi Google Sheet: {e}")
            return -1

    def send_email_report(self, count, details):
        """Gửi email tổng kết đơn hàng"""
        if not EMAIL_USER or not EMAIL_PASS or not EMAIL_RECEIVERS:
            print("⚠️ Bỏ qua gửi Email do thiếu cấu hình Secrets.")
            return

        has_job = sum(1 for d in details if d.get('J'))
        no_job = count - has_job

        subject = f"[Azura TikTok] Báo cáo quét đơn ngày {TARGET_DATE}"
        body = f"""
        <html>
        <body style="font-family: Arial, sans-serif;">
            <h3 style="color: #2e6c80;">📊 Tổng kết quét đơn TikTok Shop</h3>
            <p>- Ngày ghi nhận (Giờ VN): <b>{TARGET_DATE}</b></p>
            <p>- Tổng số đơn Tiktok tìm thấy: <b>{count}</b></p>
            <ul>
                <li>Đã có JOB ID: <span style="color: green; font-weight: bold;">{has_job}</span></li>
                <li>Chưa có JOB ID: <span style="color: red; font-weight: bold;">{no_job}</span></li>
            </ul>
            <p>📍 Dữ liệu đã được cập nhật vào Google Sheet.</p>
            <p>🔗 <a href="{self.sheet_url}">Mở Google Sheet của bạn</a></p>
            <br>
            <p style="font-size: 0.8em; color: gray;"><i>Tin nhắn tự động từ GitHub Actions Workflow</i></p>
        </body>
        </html>
        """
        
        msg = MIMEMultipart()
        msg['From'] = EMAIL_USER
        msg['To'] = EMAIL_RECEIVERS
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'html'))

        try:
            with smtplib.SMTP('smtp.gmail.com', 587) as server:
                server.starttls()
                server.login(EMAIL_USER, EMAIL_PASS.replace(" ", ""))
                # Hỗ trợ gửi cho danh sách nhiều email cách nhau bởi dấu phẩy
                receiver_list = [r.strip() for r in EMAIL_RECEIVERS.split(',')]
                server.sendmail(EMAIL_USER, receiver_list, msg.as_string())
            print("✅ Đã gửi email báo cáo thành công.")
        except Exception as e:
            print(f"❌ Lỗi gửi email: {e}")

# ==========================================
# 3. CHƯƠNG TRÌNH CHÍNH
# ==========================================
if __name__ == "__main__":
    bot = AzuraTikTokAutomation()
    
    # Kiểm tra cấu hình bắt buộc
    if not all([AZURA_USER, AZURA_PASS, SHEET_ID]):
        print("❌ Lỗi: Thiếu các biến môi trường quan trọng (User, Pass, SheetID).")
    else:
        if bot.login():
            orders = bot.fetch_tiktok_orders()
            if orders:
                added_count = bot.update_google_sheet(orders)
                if added_count > 0:
                    print(f"✅ Đã thêm mới {added_count} đơn vào Sheet.")
                    bot.send_email_report(len(orders), orders)
                else:
                    print("ℹ️ Không có dữ liệu nào được ghi thêm vào Sheet.")
            else:
                print(f"ℹ️ Không tìm thấy đơn hàng Tiktok nào trong ngày {TARGET_DATE}.")
                # Gửi báo cáo 0 đơn để người dùng yên tâm tool vẫn chạy
                bot.send_email_report(0, [])
