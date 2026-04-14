import os
import json
import requests
import smtplib
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
# Theo PRD: Chạy lúc 23:15 hàng ngày để quét đơn của ngày hôm đó
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
        self.login_url = f"{self.base_url}/Account/Login"
        self.order_api = f"{self.base_url}/OnBehalfOrder/List"
        self.sheet_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"

    def login(self):
        """Đăng nhập lấy Cookie"""
        payload = {
            "UserName": AZURA_USER,
            "Password": AZURA_PASS,
            "RememberMe": "false"
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        response = self.session.post(self.login_url, data=payload, headers=headers, allow_redirects=False)
        if response.status_code in [200, 302]:
            print("✅ Đăng nhập Azura Portal thành công.")
            return True
        else:
            print(f"❌ Đăng nhập thất bại. Status: {response.status_code}")
            return False

    def fetch_tiktok_orders(self):
        """Lọc đơn hàng Tiktok theo ngày chỉ định"""
        all_matches = []
        page = 1
        stop_searching = False

        print(f"🚀 Bắt đầu quét đơn hàng ngày: {TARGET_DATE}...")

        while not stop_searching:
            params = {"page": page, "rows": 50}
            resp = self.session.get(self.order_api, params=params)
            if resp.status_code != 200:
                print(f"⚠️ Lỗi khi gọi API trang {page}")
                break
            
            data = resp.json()
            rows = data.get("rows", [])
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
                        # nếu đã sang ngày cũ hơn thì có thể dừng (tùy portal)
                        # Ở đây ta tiếp tục quét hết các trang để đảm bảo không sót
                        pass
            
            # Nếu trang hiện tại toàn đơn cũ hơn ngày cần tìm, có thể dừng để tối ưu
            last_order_in_page = rows[-1].get("createdAt", "")[:10]
            if last_order_in_page < TARGET_DATE:
                stop_searching = True
            
            page += 1
            if page > 20: # Giới hạn an toàn tránh loop vô tận
                break
                
        return all_matches

    def process_row_data(self, row):
        """Mapping dữ liệu theo đúng cột Google Sheet (A, B, I, J, K, L)"""
        # Xử lý Job ID: Gom nhiều jobId thành 1 chuỗi, cách nhau bởi dấu phẩy
        designs = row.get("orderProductDesigns", [])
        job_ids = [str(d.get("jobId")) for d in designs if d.get("jobId") is not None]
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
            scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
            creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
            client = gspread.authorize(creds)
            sheet = client.open_by_key(SHEET_ID).sheet1 # Mặc định Sheet đầu tiên

            # Chuẩn bị mảng 2 chiều để ghi batch (tối ưu hơn ghi từng dòng)
            # Vì yêu cầu ghi vào cột cụ thể, ta sẽ lấy toàn bộ dòng và chèn vào
            rows_to_append = []
            for item in data_list:
                # Tạo một dòng 12 cột (A đến L)
                row_data = [""] * 12 
                row_data[0] = item["A"] # Cột A
                row_data[1] = item["B"] # Cột B
                row_data[8] = item["I"] # Cột I (Index 8)
                row_data[9] = item["J"] # Cột J (Index 9)
                row_data[10] = item["K"] # Cột K (Index 10)
                row_data[11] = item["L"] # Cột L (Index 11)
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
            <p><i>Auto-generated by Azura VibeCoder Assistant</i></p>
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
                bot.send_report = bot.send_email_report(len(orders), orders)
            else:
                print("ℹ️ Không có dữ liệu mới được thêm.")
        else:
            print(f"ℹ️ Không tìm thấy đơn hàng Tiktok nào trong ngày {TARGET_DATE}.")
            # Vẫn gửi mail thông báo 0 đơn nếu cần
            bot.send_email_report(0, [])
