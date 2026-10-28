# Google Drive setup (one time, about 20 minutes)

The editor app keeps the stream VODs, the edited videos and a nightly backup of its database in
your Google Drive. This guide connects it. You only do this once. Use the Google account that has
the Google One plan (the big storage), because that is where the files will live.

You will need: that Google account, the editor's Google email address, and your admin login to the
site.

## Part 1: Make the Google app

1. Go to `https://console.cloud.google.com` and sign in with the Google One account.
2. At the top of the page, open the project picker and create a **new project**. Call it
   "DualBladeX Editor". Wait for it to be created, then make sure it is the selected project.
3. Open the menu and find **APIs & Services**, then the library of APIs. Search for
   **Google Drive API**, open it and press **Enable**.
4. In **APIs & Services**, open the **OAuth consent screen** (Google may now call this section
   "Google Auth Platform" or "Branding"; it is the same thing).
   - Choose **External** as the user type.
   - App name: "DualBladeX Editor". For the support email and developer contact, use your own email.
   - When it asks for **scopes**, add exactly one: the Google Drive scope that ends in `.../auth/drive.file`
     (described as "See, edit, create and delete only the specific Google Drive files you use with this app").
     Do not add any other scope.
5. **Publish the app.** On the consent screen page, find the publishing status (it says "Testing")
   and press the button to **Publish app**, then confirm. This step matters:
   - While the app is in Testing, Google cancels its login every 7 days, and uploads and deletes would
     silently stop working a week after setup.
   - Publishing does not need Google to review anything, because the `drive.file` scope is the
     low-risk kind. You may see a note that the app is "unverified". That is fine, it is only you
     using it.
6. Open **Credentials**, press to **create credentials**, and choose **OAuth client ID**.
   - Application type: **Web application**.
   - Name: anything, for example "DualBladeX backend".
   - Under **Authorized redirect URIs**, add this exact address, with nothing before or after it:
     `https://hub.dualbladex.org/auth/gdrive/callback`
   - Create it. Google shows a **Client ID** and a **Client secret**. Keep that window open, or copy
     both somewhere safe for the next part.

## Part 2: Tell the site

7. Log in to the site as admin and open the dashboard's **Settings** page. Find the **Editor app**
   section and fill in:
   - **Drive client ID**: the Client ID from step 6.
   - **Drive client secret**: the Client secret from step 6.
   - **Drive redirect URI**: `https://hub.dualbladex.org/auth/gdrive/callback` (it may already be filled in).
   - **Editor Google account**: the editor's Google email address. VODs and the brand kit folder are
     shared with this address.
   - Leave **Drive refresh token** empty. The next step fills it in by itself.
   Save the settings.

## Part 3: Connect

8. While still logged in as admin, open `https://hub.dualbladex.org/auth/gdrive/login` in your browser.
9. Google asks which account to use. Choose the Google One account and approve the access it asks
   for. You are sent back to the dashboard.
10. Open Google Drive. You should see a folder called **DualBladeX Editor**, containing three
    folders: **Projects**, **Brand kit** and **Backups**. The Brand kit folder is shared with the
    editor automatically.

If the folder does not appear, run step 8 again. It is safe to repeat. If Google says no refresh
token came back, open your Google account's security page, remove the "DualBladeX Editor" app from
the apps with access, and run step 8 again.

## What lives where

- **Projects**: one folder per stream VOD, named like `2026-10-24 Stream title`, with a subfolder for
  each tape. Edited videos are named `v1 - filename`, `v2 - filename`, and so on.
- **Backups**: a copy of the app's database every night around 4 AM. The newest 7 are kept.
- **Brand kit**: yours to fill with logos and overlays. The editor can read it.

Only files made by this app are visible to it. It cannot see the rest of your Drive.

## If the database is ever corrupted

The app's database (`app.db` on the Mac Mini) holds the projects, comments and logins. A corrupt
database does not stop the backend from starting: the overlays, chat commands and dashboard pages
keep working, but logging in and every editor-app call fail with a database error, and the log
complains about the database being corrupt or "malformed". When that happens, do this. The app
will not quietly start a fresh empty one, on purpose.

1. Stop the backend.
2. In Google Drive, open **DualBladeX Editor**, then **Backups**, and download the newest
   `app-YYYY-MM-DD.db` (the one with the latest date).
3. On the Mac Mini, put that file where the database lives (the **Database path** setting, or by
   default `app.db` next to `main.py`), replacing the broken one. Also delete any `app.db-wal` and
   `app.db-shm` files next to it.
4. Start the backend again.

You lose at most what happened since the last backup (up to a day). Drive itself still has all the videos.
