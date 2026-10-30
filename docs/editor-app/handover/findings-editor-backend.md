# Editor backend audit (confirmed by controller where noted)
C1 backup.py:52-59 uploads app.db incl. plaintext `sessions` tokens to Drive; drive.file token (media-link/download, both roles) can list+download app-*.db -> session hijack to admin. Also token is read-write (can DELETE files bypassing two-step). Fix: strip sessions in the backup copy (DELETE FROM sessions on dest before upload) AND/OR hash session tokens (sha256) in core/auth.py. Longer term: don't hand the raw token out.
I2 CONFIRMED by controller: commits 594d667 (net_time auto clock reset) and 2c8f3c3 (past-due delete clamp) are on origin/editor-app-plan-5a only; PR #10 merged at aba87de before them. core/net_time.py missing on main. Fix: cherry-pick both onto the fix branch.
I3 editor_service.py:153-161 start_iteration_upload lacks deletions.ensure_tape_not_deleting (before and after awaits); renew/complete have it. Possibly refuse when tape files_deleted_at set.
I4 editor_service.py:236-238 vod_ready lets share_with raise -> VOD never ready, PC retries forever. Make share best-effort (log warning) like complete_iteration_upload 213-218.
M5 notifier.py:92 ready digest names soft-deleted/deleting projects.
M6 media_link doesn't check status: deleted VOD/version -> 502 "Drive had a problem"; refuse unless ready/deleting.
M7 gdrive.py:96 expires_in reported ~600s short (margin subtracted).
M8 editor_comments.py:154 start_over compares next_version()-1 incl uploading/abandoned rows; should use latest READY version.
M9 uploader_api.py:105-108 create_project + meta_set not one transaction.
M10 gdrive_oauth.py:52-54 brand kit shared once; email change never re-shares; reconnect other account keeps stale folder ids; ensure_layout not under _folder_lock from backup/callback.
M11 editor_comments.py:193-210 search returns deleted projects' comments.
M12 Unverified: project delete allowed while vod_status uploading.
M13 stream/twitch_client.py:63 default 300s timeout on stream_info (uploader /live can hang).
