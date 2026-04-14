import os
import json
import requests
import smtplib
import re # Đã thêm thư viện re để bắt Token
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ==========================================
# 1. CẤU HÌNH HỆ THỐNG & THỜI GIAN
# ==========================================
VN_TZ = timezone(timedelta(hours=7))
now_vn = datetime.now(VN_TZ)
TARGET_DATE = now_vn.strftime('%Y-%m-%d') 

# Lấy thông tin từ GitHub Secrets
AZURA_USER = os.getenv("AZURA_USER")
AZURA_PASS = os.getenv("AZURA_PASS")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GCP_JSON = os.getenv("GCP_SERVICE_ACCOUNT_JSON")
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
EMAIL_RECEIVERS = os.getenv("EMAIL_RECEIVERS")

class AzuraTikTokAutomation:
    def __init__(self):
        self.session = requests.Session()
        self.base_url = "https://portal.aluffm.com"
        self.login_url = f"{self.base_url}/Login" # Đã sửa URL giống file chuẩn
        self.order_api = f"{self.base_url}/OnBehalfOrder/List"
        self.sheet_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
        self.cookie_str = "" # Lưu trữ cookie

    def login(self):
        """Đăng nhập lấy Cookie giống 100% logic của WebApp Streamlit"""
        try:
            print("🌐 Đang kết nối hệ thống Portal để lấy Token...")
            # 1. GET request để lấy trang login và bắt RequestVerificationToken
            r1 = self.session.get(self.login_url, timeout=15)
            match = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r1.text)
            
            if not match:
                print("❌ Lỗi: Không bắt được RequestVerificationToken.")
                return False
            
            token = match.group(1)
            
            # 2. POST request kèm token để đăng nhập
            payload = {
                "UserName": AZURA_USER, 
                "Password": AZURA_PASS, 
                "__RequestVerificationToken": token, 
                "RememberMe": "false"
            }
            headers = {"Referer": self.login_url}
            
            self.session.post(self.login_url, data=payload, headers=headers, allow_redirects=False)

            # 3. Trích xuất cookie từ session
            ck_dict = self.session.cookies.get_dict()
            if '.AspNetCore.Identity.Application' in ck_dict:
                self.cookie_str = "; ".join([f"{k}={v}" for k, v in ck_dict.items()])
                
                # Cập nhật Header cho toàn bộ Session để dùng cho lúc fetch data
                self.session.headers.update({
                    'Cookie': self.cookie_str, 
                    'X-Requested-With': 'XMLHttpRequest'
                })
                print("✅ Đăng nhập Azura Portal thành công và đã lưu Cookie.")
                return True
            else:
                print("❌ Đăng nhập thất bại: Sai tài khoản, mật khẩu hoặc hệ thống từ chối.")
                return False
                
        except Exception as e:
            print(f"❌ Lỗi mạng lúc đăng nhập: {e}")
            return False

    def fetch_tiktok_orders(self):
        """Lọc đơn hàng Tiktok theo ngày chỉ định"""
        all_matches =[]
        page = 1
        stop_searching = False

        print(f"🚀 Bắt đầu quét đơn hàng ngày: {TARGET_DATE}...")

        while not stop_searching:
            # Đã sửa lại Params giống file chuẩn: pageSize và pageNumber
            params = {"pageSize": 50, "pageNumber": page}
            
            try:
                resp = self.session.get(self.order_api, params=params, timeout=20)
                if resp.status_code != 200:
                    print(f"⚠️ Lỗi khi gọi API trang {page}. Status: {resp.status_code}")
                    break
                
                data = resp.json()
                rows = data.get("rows",[])
                if not rows:
                    break

                for row in rows:
                    created_at_raw = row.get("createdAt", "")
                    # Format: 2026-04-14T05:02:16Z -> lấy 10 ký tự đầu
                    order_date = created_at_raw[:10]
                    
                    # Chỉ lấy đơn có partner là Tiktok
                    if row.get("shippingPartnerString") == "Tiktok":
                        if order_date == TARGET_DATE:
                            all_matches.append(self.process_row_data(row))
                        elif order_date < TARGET_DATE:
                            # Vì đơn hàng thường sắp xếp mới nhất lên đầu, 
                            # nếu đã sang ngày cũ hơn thì có thể dừng
                            pass
                
                # Nếu trang hiện tại toàn đơn cũ hơn ngày cần tìm, có thể dừng để tối ưu
                last_order_in_page = rows[-1].get("createdAt", "")[:10]
                if last_order_in_page < TARGET_DATE:
                    stop_searching = True
                
                page += 1
                if page > 100: # Giới hạn an toàn (file chuẩn là 300)
                    break
            except Exception as e:
                print(f"❌ Lỗi khi tải dữ liệu trang {page}: {e}")
                break
                
        return all_matches

    def process_row_data(self, row):
        """Mapping dữ liệu theo đúng cột Google Sheet (A, B, I, J, K, L)"""
        designs = row.get("orderProductDesigns", [])
        job_ids =[str(d.get("jobId")) for d in designs if d.get("jobId") is not None]
        job_id_str = ", ".join(sorted(list(set(job_ids)))) if job_ids else ""

        return {
            "A": row.get("customer", ""),               # Seller_Name
            "B": row.get("partnerBarcode", ""),         # Tracking_Number
            "I": row.get("customerOrder", ""),          # Order_number
            "J": job_id_str,                            # Job ID
            "K": row.get("id", ""),                     # AzuraID
            "L": row.get("createdAt", "")[:10]          # Azura_Creat_At (YYYY-MM-DD)
        }

    def update_google_sheet(self, data_list):
        """Ghi dữ liệu vào Google Sheet"""
        if not data_list:
            return 0
        
        try:
            creds_dict = json.loads(GCP_JSON)
            scope =["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
            creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
            client = gspread.authorize(creds)
            sheet = client.open_by_key(SHEET_ID).sheet1

            rows_to_append =[]
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
        """Gửi email tổng kết"""
        if not EMAIL_USER or not EMAIL_PASS: return

        has_job = sum(1 for d in details if d['J'])
        no_job = count - has_job

        subject = f"[Azura TikTok] Báo cáo quét đơn ngày {TARGET_DATE}"
        body = f"""
        <html>
        <body>
            <h3>📊 Tổng kết quét đơn TikTok Shop</h3>
            <p>- Ngày ghi nhận: <b>{TARGET_DATE}</b></p>
            <p>- Tổng số đơn Tiktok tìm thấy: <b>{count}</b></p>
            <ul>
                <li>Đã có JOB ID: <span style="color: green;">{has_job}</span></li>
                <li>Chưa có JOB ID: <span style="color: red;">{no_job}</span></li>
            </ul>
            <p>📍 Dữ liệu đã được ghi tiếp vào Google Sheet.</p>
            <p>🔗 Xem tại: <a href="{self.sheet_url}">Link Google Sheet</a></p>
            <br>
            <p><i>Auto-generated by GitHub Actions</i></p>
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
                server.sendmail(EMAIL_USER, EMAIL_RECEIVERS.split(','), msg.as_string())
            print("✅ Đã gửi email báo cáo.")
        except Exception as e:
            print(f"❌ Lỗi gửi email: {e}")

# ==========================================
# CHƯƠNG TRÌNH CHÍNH
# ==========================================
if __name__ == "__main__":
    bot = AzuraTikTokAutomation()
    if bot.login():
        orders = bot.fetch_tiktok_orders()
        if orders:
            added_count = bot.update_google_sheet(orders)
            if added_count > 0:
                print(f"✅ Đã thêm {added_count} đơn vào Sheet.")
                bot.send_email_report(len(orders), orders)
            else:
                print("ℹ️ Không có dữ liệu mới được thêm.")
        else:
            print(f"ℹ️ Không tìm thấy đơn hàng Tiktok nào trong ngày {TARGET_DATE}.")
            # ĐÃ FIX: Đóng ngoặc đúng chuẩn và thêm list rỗng [] cho biến details
            bot.send_email_report(0,[])
