# Deploying: GitHub Actions (daily job) + Streamlit Community Cloud (dashboard)

```
GitHub Actions, weekdays 17:30 IST, then 00:15 and 08:30 IST the following night/morning (Tue-Sat)
  restore state (data branch) + price cache  ->  python -m nse_monitor run  ->  push state to `data` branch
                                                                                   |
Streamlit Community Cloud  <-- reads data/state.db (refreshes every 5 min) ---------+
  https://<your-app>.streamlit.app   (any browser, any device)
```

Your PC is not involved once this is set up.

## 1. Create the GitHub repository
1. On github.com, click **New repository**. Leave it **empty**: no README, no .gitignore. It can be
   *public*, which is simpler, or *private*, which needs one extra secret in step 4.
2. Push this folder:
   ```powershell
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```

## 2. Seed the cloud with your current tracking data (one time)
This publishes your existing tracking list and history so the cloud continues from them
instead of starting empty:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\seed_data_branch.ps1
```

## 3. Turn on the daily job
In the repository, open **Actions**. If asked, enable workflows, then choose **Daily NSE monitor → Run workflow**
to test it once. A normal run takes about 3–5 minutes. The first run takes about 10, because it downloads
the price history once. After that it runs by itself Mon–Fri at 17:30 IST, and Tue–Sat at 00:15 and 08:30 IST (covering the previous session). GitHub may start
scheduled runs 5–30 minutes late when it's busy.

Optional alerts: under **Settings → Secrets and variables → Actions**, add `TELEGRAM_BOT_TOKEN`
and `TELEGRAM_CHAT_ID`, or `NSE_MONITOR_WEBHOOK`.

## 4. Deploy the dashboard
1. Go to https://share.streamlit.io and sign in with GitHub. Click **Create app →
   Deploy a public app from GitHub**.
2. Set Repository to `<you>/<repo>`, Branch to `main`, and Main file to `streamlit_app.py`.
3. Optional: under **Advanced settings → Secrets**, paste the lines below. By default the app already reads
   `yash1709/strategy` (`DEFAULT_GITHUB_REPO` in `streamlit_app.py`); a secret is needed only for a fork
   or a private repository:
   ```toml
   GITHUB_REPO = "<you>/<repo>"
   # private repository only: a fine-grained token with read-only "Contents" on this repo
   # GITHUB_TOKEN = "github_pat_..."
   ```
4. Click **Deploy**. The link (`https://<name>.streamlit.app`) works in any browser.

## Good to know
* **Freshness:** the dashboard shows the latest trading day and when it was last updated. It re-reads the data
  every 5 minutes, and the **Refresh data** button forces an immediate re-read.
* **Sleeping apps:** Streamlit puts apps to sleep after a period with no visitors. The first visit after that
  takes about 30 seconds to wake it. The data itself keeps updating.
* **Inactive repositories:** GitHub pauses scheduled workflows in *public* repos after 60 days without
  commits to the default branch. It emails you first, and one click re-enables them.
* **Backups:** every run's `state.db` is also saved as a workflow artifact for 30 days.
* **Local scheduler:** once the cloud runs are confirmed, remove the Windows task so the two copies don't
  diverge or double-alert: `Unregister-ScheduledTask -TaskName "NSE RSI SMA50 Monitor"`.
