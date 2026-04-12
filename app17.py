import os
import json
import time
import requests
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

TRACK17_API_KEY = os.getenv("TRACK17_API_KEY")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
GCP_JSON_STR = os.getenv("GCP_JSON")

TRACK17_URL = "https://api.17track.net/track/v2.4/gettrackinfo"
USPS_CARRIER_CODE = 21051  

def get_google_sheet():
    if not GCP_JSON_STR:
        raise ValueError("❌ Lỗi: Thiếu GCP_JSON trong Env Var.")
    creds_dict = json.loads(GCP_JSON_STR)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    )
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).worksheet("Data")

def calculate_sla(label_at, transit_at):
    if not label_at or not transit_at: return ""
    try:
        t1 = datetime.fromisoformat(label_at.replace('Z', '+00:00'))
        t2 = datetime.fromisoformat(transit_at.replace('Z', '+00:00'))
        hours = (t2 - t1).total_seconds() / 3600
        if hours <= 24: return "✅ EXCELLENT (<24h)"
        if hours <= 48: return "⚡ GOOD (<48h)"
        return f"⚠️ DELAY ({int(hours)}h)"
    except:
        return "N/A"

def run_sync():
    print(f"🚀 [USPS Mode - DEBUG] Bắt đầu quét lúc: {datetime.now()}")
    
    try:
        sheet = get_google_sheet()
        records = sheet.get_all_records()
    except Exception as e:
        print(f"❌ Lỗi truy cập Sheet: {e}")
        return

    tracking_list = []
    row_mapping = {}

    for idx, row in enumerate(records, start=2):
        num = str(row.get('Tracking_Number', '')).strip()
        if num and len(num) > 10:
            tracking_list.append({"number": num, "carrier": USPS_CARRIER_CODE})
            row_mapping[num] = idx

    if not tracking_list:
        print("📭 Không có mã nào hợp lệ để quét.")
        return

    headers = {"Content-Type": "application/json", "17token": TRACK17_API_KEY}
    updates = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    for i in range(0, len(tracking_list), 40):
        batch = tracking_list[i:i+40]
        print(f"📦 Batch {i//40 + 1}: Đang check {len(batch)} mã...")
        
        try:
            resp = requests.post(TRACK17_URL, json=batch, headers=headers)
            res_data = resp.json()
            
            # --- 📸 CAMERA AN NINH: IN TOÀN BỘ JSON TRẢ VỀ ---
            print("\n🕵️ DỮ LIỆU TỪ 17TRACK TRẢ VỀ LÀ:")
            print(json.dumps(res_data, indent=2))
            print("----------------------------------\n")
            
            if res_data.get("code") != 0:
                print(f"❌ Lỗi API: {res_data.get('msg')}")
                continue

            # Xử lý các mã bị từ chối (Rejected)
            rejected = res_data.get("data", {}).get("rejected", [])
            for r in rejected:
                print(f"⚠️ 17Track từ chối mã {r.get('number')} - Lý do: {r.get('error', {}).get('message')}")

            # Xử lý các mã được nhận (Accepted)
            accepted = res_data.get("data", {}).get("accepted", [])
            for item in accepted:
                num = item.get("number")
                
                # FIX: Không dùng 'continue' nữa, nếu rỗng thì cho biến thành {}
                info = item.get("track_info") or {}
                stt_obj = info.get("latest_status") or {}
                
                current_stt = stt_obj.get("status", "Not Found")

                label_at = ""
                transit_at = ""
                
                providers = info.get("tracking", {}).get("providers", []) if info else []
                events = providers[0].get("events", []) if providers else []

                for ev in sorted(events, key=lambda x: x.get("time_utc", "")):
                    desc = ev.get("description", "").lower()
                    time_utc = ev.get("time_utc", "")
                    if not time_utc: continue

                    if ("label created" in desc or "info received" in desc or "shipping info received" in desc) and not label_at:
                        label_at = time_utc
                    if ("in transit" in desc or "accepted" in desc or "picked up" in desc) and not transit_at:
                        transit_at = time_utc

                sla_val = calculate_sla(label_at, transit_at)

                ridx = row_mapping.get(num)
                if ridx:
                    updates.append({
                        'range': f'C{ridx}:G{ridx}',
                        'values': [[current_stt, label_at, transit_at, sla_val, now_str]]
                    })

            time.sleep(0.5)

        except Exception as e:
            print(f"⚠️ Lỗi Batch: {e}")

    if updates:
        print(f"📝 Đang ghi {len(updates)} dòng...")
        sheet.batch_update(updates)
        print("✅ Done!")
    else:
        print("ℹ️ Không có gì để cập nhật.")

if __name__ == "__main__":
    run_sync()
