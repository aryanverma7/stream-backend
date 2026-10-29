# Discord bot setup

The site can send Discord direct messages (DMs) to you and to your editor: a new VOD, a new version to review, comment summaries, delete reminders. It does this through a small bot of your own. One-time setup, about ten minutes.

1. **Create the bot.** Go to https://discord.com/developers/applications and click New Application (name it e.g. "DualBladeX Editor"). In the left menu click Bot, then Reset Token, then Copy. Discord shows the token once. Treat it like a password; if it ever leaks, reset it and paste the new one. Leave the three Privileged Gateway Intents off. Then make the bot private: in the left menu click Installation, set Install Link to **None** and save (Discord refuses to make a bot private while that link exists), then go back to Bot and switch Public Bot off. Step 2's link still works for your own server.
2. **Add the bot to your server.** Click OAuth2, then URL Generator. Tick the scope "bot" and no permissions. Open the URL it gives you and authorize the bot into your server. Your editor must be in the same server.
3. **Copy both user IDs.** In Discord, User Settings, Advanced, turn on Developer Mode. Right-click yourself, Copy User ID. Right-click the editor in the member list, Copy User ID.
4. **Allow DMs.** You and the editor: right-click the server icon, Privacy Settings, allow Direct Messages from server members.
5. **Fill in the site.** Admin dashboard, Settings, "Editor app":
   - Discord bot token (`discord_bot_token`)
   - Streamer Discord user ID (`streamer_discord_user_id`)
   - Editor Discord user ID (`editor_discord_user_id`)
   - then switch on Discord notifications (`discord_notifications_enabled`)

   If you added these earlier with "+ Add setting" under these exact keys, they are picked up automatically.
6. **What each DM means.**
   - VOD or clip ready (to the editor).
   - A new version to review (to the streamer).
   - Comment digests (to the other person), sent after 10 quiet minutes so a burst of comments is one message.
   - Delete reminders (to the editor), hourly between 12:00 and 22:00 Mac Mini time, starting 3 hours after he clicks Download, until he deletes the file.
   - "The site is back online" (to the editor) after an outage longer than 10 minutes.

## If something is wrong

- The admin storage view (`GET /api/editor/storage`, shown in the Plan 5 UI) has a `discord` block: pending and dropped counts and the last error.
- Messages queued while notifications are OFF are skipped, not sent later when you switch them on.
- A message can arrive twice in a rare case: a power cut or a timeout just as Discord accepted it.
