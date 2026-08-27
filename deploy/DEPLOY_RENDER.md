# CUnnect — Render (Free Tier) Deployment + SPEED Guide

Backend folder (`myproject`) Render pe, APK sabko share. ~30 min.

---

## STEP 1 — Code GitHub pe
1. github.com → **New repository** → naam `cunnect-backend` (Private theek hai) → Create (empty).
2. Windows PowerShell:
```powershell
cd C:\Users\adars\Downloads\cunnect_food_flutter\backend\myproject
git init
git add .
git commit -m "CUnnect backend"
git branch -M main
git remote add origin https://github.com/TUMHARA-USERNAME/cunnect-backend.git
git push -u origin main
```
(Pehli baar GitHub login popup aayega. `media` folder bhi push ho rahi hai = initial images.)

## STEP 2 — Local data export (users/vendors/menu/orders)
```powershell
python manage.py dumpdata --natural-foreign --natural-primary -e contenttypes -e auth.Permission -e sessions -e authtoken.token -e myapp.devicetoken -o deploy/data.json
git add deploy/data.json
git commit -m "data"
git push
```

## STEP 3 — Render Web Service
1. https://render.com → **Get Started** → GitHub se login → repo authorize.
2. **New → Web Service** → repo `cunnect-backend` chuno.
   ⭐ Service ka naam EXACTLY **`cunnect-backend`** rakhna — APK isi URL pe
   hardcode hai (`https://cunnect-backend.onrender.com`), users ko koi
   setting nahi deni padegi.
3. Settings:
   - **Root Directory:** khali chhodo (repo root = myproject)
   - **Build Command:** `pip install -r deploy/requirements_render.txt`
   - **Start Command:** `python manage.py migrate && daphne -b 0.0.0.0 -p $PORT myproject.asgi:application`
   - **Region:** ⭐ **Singapore (ap-southeast-1)** — India ke sabse paas = FAST
   - **Instance:** Free
4. **Create Web Service** → deploy hoga (~2-3 min). URL milega: `https://cunnect-backend.onrender.com`

## STEP 4 — Database (Postgres)
**Option A (sabse aasan): Render PostgreSQL**
1. **New → PostgreSQL** → name `cunnect-db`, region **Singapore**, free plan.
2. DB page → **Internal Database URL** copy karo.
3. Web Service → **Environment** → add:
   - `DATABASE_URL` = `postgresql://cunnect-db_user:PASS@cunnect-db.internal.../cunnect_db` (internal wala)
**Option B (permanent, 90-day limit nahi): Neon.tech**
1. neon.tech → free account → New Project (region **ap-south India**!) → connection string copy
   (`postgresql://user:pass@ep-....neon.tech/neondb?sslmode=require`)
2. Wahi `DATABASE_URL` env me daalo. (Recommended — Render PG free 90 din baad expire hota hai.)

## STEP 5 — Environment Variables (Web Service → Environment)
| Key | Value |
|---|---|
| `DJANGO_SECRET_KEY` | koi lamba random: `cu!nn3ct-S3cr3t-2026-xyz...` |
| `DJANGO_ALLOWED_HOSTS` | `*` |
| `DJANGO_DEBUG` | `false` |
| `DATABASE_URL` | upar wala |
| `CUNNECT_SMTP_USER` | tumhara SMTP email (OTP mails) |
| `CUNNECT_SMTP_APP_PASSWORD` | SMTP app password |

Save → Render **auto-redeploy** karega.

## STEP 6 — Data import (Render Shell)
1. Web Service page → **Shell** tab (free plan pe available).
2. ```bash
   python manage.py loaddata deploy/data.json
   ```
   Ab tumhare saare users/vendors/menu/orders live DB me. ✅

## STEP 7 — Test
Phone browser: `https://cunnect-backend.onrender.com/api/health/` → `{"status":"ok"}` 🎉

## STEP 8 — APK
App me server URL **hardcoded** hai (`https://cunnect-backend.onrender.com`) —
koi ⚙/setting option nahi, users bas install karke login krenge.
Seedha `flutter build apk` → **APK share karo** (WhatsApp/Drive). ✅

---

## ⚡ APP FAST RAKHNE KA PLAN
1. **Region Singapore** — India se ~60-90ms (US region = 250ms+ = SLOW).
2. **Keep-awake (sabse zaroori):** Render free service 15 min idle pe so jati hai
   (cold start ~30-50 sec). Fix: https://cron-job.org (free) → New Cronjob:
   - URL: `https://cunnect-backend.onrender.com/api/health/`
   - Schedule: **every 5 minutes** → service kabhi soyegi nahi = app hamesha turant khulegi.
3. **DEBUG=false + WhiteNoise** — static compressed+cached serve hoti hai.
4. **Postgres** — sqlite se concurrent users pe fast.
5. App side: images already optimized; polling light hai. Kuch aur nahi chahiye.

Pehli request thodi slow lag sakti hai (cold), cron lagane ke baad ye problem khatam.

---

## 📦 MEDIA PERMANENT — Cloudinary (FREE, koi card nahi)
Render ka disk har redeploy pe reset hota hai → uploads permanent rakhne ke liye:
1. https://cloudinary.com → **Free signup** (sirf email — card NAHI lagta).
2. Dashboard pe hi dikhega: **Cloud Name**, **API Key**, **API Secret**.
3. Render Web Service → Environment me:
   - `CLOUDINARY_CLOUD_NAME` = tumhara cloud name
   - `CLOUDINARY_API_KEY` = API key
   - `CLOUDINARY_API_SECRET` = API secret
4. Save → redeploy. Ab saari photos/banners Cloudinary CDN pe =
   permanent + India me fast. Free plan: ~25GB bandwidth/month — campus ke liye kaafi.

## FAQ
- **FCM push?** Render se chalegi (Firebase cloud call hai).
- **UMS scraping?** Render se hogi (outbound allowed).
- **WebSocket chat?** Single instance pe InMemory channel layer chalta hai.
- **Render PG 90 din?** Neon use karo (Option B) — permanent free.
- **Backup:** Render shell se `python manage.py dumpdata -o backup.json` time-time pe.
