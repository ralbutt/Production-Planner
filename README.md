# Rapid Assembly — Production Planner

## Deploying to Render (step by step, no coding needed)

### 1. GitHub
1. Go to **github.com** → sign in or create free account
2. Click the **+** button (top right) → **New repository**
3. Name it `rapid-assembly`, set to **Private**, click **Create repository**
4. On the next screen click **uploading an existing file**
5. Unzip the downloaded file on your computer
6. Drag **all the files and folders** into the GitHub upload area
7. Scroll down, click **Commit changes**

### 2. Render
1. Go to **render.com** → sign up with your GitHub account
2. Click **New +** → **Web Service**
3. Click **Connect** next to your `rapid-assembly` repository
4. Fill in these settings:
   - **Name:** rapid-assembly (or anything you like)
   - **Region:** Europe (Frankfurt)
   - **Branch:** main
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app`
   - **Instance Type:** Free
5. Click **Advanced** → **Add Environment Variable**
   - Key: `SECRET_KEY`
   - Value: type any long random phrase, e.g. `RapidAssembly2024xK9qmPlan`
6. Click **Create Web Service**
7. Wait ~3 minutes for it to build and deploy
8. Click the URL shown (e.g. `https://rapid-assembly.onrender.com`)
9. First visit shows a setup page — create Denisa's account

### 3. Important — keeping your data
Render's free tier resets the database when the service restarts (roughly every day).

**To keep data permanently:** upgrade to the $7/month "Starter" plan and add a Disk:
- In your service settings → **Disks** → **Add Disk**
- Mount path: `/data`
- Then set the environment variable `DATABASE_URL` = `/data/rapid_assembly.db`

### 4. The display screen
Open `https://your-app.onrender.com/display` on a browser on your workshop screen.
No login needed — it auto-refreshes every 60 seconds.

### 5. Weekly routine
1. Export Works Orders from Emax → **Import** page → upload
2. Export Sales Delivery Lines from Emax → **Import** page → upload
3. **Schedule** page → tick readiness checklist → **Auto-Plan** (or plan manually)
4. Check **Sales Orders** page — everything should show On Track
