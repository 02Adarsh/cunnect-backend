# CUnnect — Oracle Cloud Free Tier Deployment (APK share karne ke liye)

Backend = `myproject` folder (Django + Channels). Ye guide follow karo — ~20 min.

---

## STEP 1 — Oracle Free account
1. https://cloud.oracle.com → **Sign Up** (Free tier). Card lagta hai verification ke liye,
   lekin **Always Free** resources pe charge NAHI hota.
2. Home region choose karo (Mumbai `ap-mumbai-1` milta hai to best — latency kam).

## STEP 2 — VM banana
1. Console → **Compute → Instances → Create Instance**.
2. Name: `cunnect`.
3. Image: **Ubuntu 24.04 LTS** (aarch64).
4. Shape: **VM.Standard.A1.Flex** (Ampere, 4 OCPU + 24 GB RAM — Always Free).
5. SSH key: **Generate a key pair** → dono files download karo (`cunnect.key` + `.pub`).
6. Create → instance RUNNING hone do → **Public IP** copy kar lo (maan lo `140.238.x.x`).

## STEP 3 — Port 8000 kholo (do jagah!)
**(a) Oracle VCN:**
1. Instance page → **Subnet** link → subnet ki **Security List** → **Add Ingress Rules**.
2. Source CIDR `0.0.0.0/0`, Protocol TCP, Destination Port **8000** → Add.
   (Port 22 pehle se khula hota hai.)

**(b) VM ka apna firewall (Ubuntu):** SSH ke baad (STEP 4) ye chalana:
```bash
sudo iptables -I INPUT 6 -p tcp --dport 8000 -j ACCEPT
sudo apt install -y netfilter-persistent
sudo netfilter-persistent save
```

## STEP 4 — Code upload (Windows PowerShell se)
```powershell
cd C:\Users\adars\Downloads\cunnect_food_flutter\backend
scp -i $env:USERPROFILE\Downloads\cunnect.key -r myproject ubuntu@140.238.x.x:~/
```
(IP apna lagao. Pehli baar `yes` bolna.)

## STEP 5 — VM setup (SSH)
```powershell
ssh -i $env:USERPROFILE\Downloads\cunnect.key ubuntu@140.238.x.x
```
VM ke andar:
```bash
sudo apt update && sudo apt install -y python3-venv python3-pip
python3 -m venv venv
source venv/bin/activate
cd myproject
pip install -r deploy/requirements.txt
python manage.py migrate          # db.sqlite3 pehle se data ke saath aaya hai
```

## STEP 6 — Service (auto-start + crash pe restart)
```bash
sudo cp deploy/cunnect.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cunnect
sudo systemctl status cunnect     # active (running) dikhna chahiye
curl -s http://localhost:8000/ | head -c 200   # response aaye to OK
```
Logs dekhne ho: `sudo journalctl -u cunnect -f`

## STEP 7 — Phone se test
Mobile browser me kholo: `http://140.238.x.x:8000/` — page/response aaya to backend LIVE hai. 🎉

## STEP 8 — App ka URL set karo (do options)
- **Option A (best — APK me fixed):** `lib/services/api_client.dart` me line:
  ```dart
  LocalStore.get('cunnect_base_url') ?? 'http://localhost:8000';
  ```
  ki jagah `'http://140.238.x.x:8000'` likho → `flutter build apk` → ye APK sabko bhejo.
- **Option B:** APK jaisi hai waisi bhejo; har user login screen ke **⚙ button** se
  server address `http://140.238.x.x:8000` daal sakta hai.

## STEP 9 — APK share
`app-release.apk` WhatsApp/Drive/Telegram pe bhejo. FCM push notifications Firebase ke
through aati hai — server IP se farak nahi padta. UMS scraping bhi VM se hogi (outbound free).

---

## Notes / FAQ
- **Free tier limits:** A1 Flex 4 OCPU/24 GB + 200 GB storage — campus-scale ke liye kaafi.
- **Data backup:** `scp ubuntu@IP:~/myproject/db.sqlite3 .` haftewar le lete raho.
- **Domain + HTTPS (optional):** free domain (e.g. DuckDNS) + `certbot --nginx` —
  tab nginx proxy 443→8000 lagana. Abhi zaroorat nahi; app cleartext allow karta hai.
- **Restart/change:** code update karna ho → dobara `scp` → `sudo systemctl restart cunnect`.
- **Admin panel:** `python manage.py createsuperuser` → `http://IP:8000/admin/`.

