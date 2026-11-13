# News Studio Windows app

Launch `..\create_video_app.bat`. The launcher uses the already-built app when
available and builds it locally with .NET 10 on the first launch otherwise.
No external NuGet packages are required.

The original `..\create_video.bat` is unchanged and remains the console fallback.

## Sequential generation queue

When the latest production is complete, Ctrl+Shift+click the new-video button (or
**Start with settings**) to enter a generation count from 1 to 20. Queue items run
strictly one at a time with fast defaults enabled. A new item starts only after the
previous video's status is verified as complete; failures, cancellations, and manual
checkpoints stop the queue instead of skipping ahead.

While a queue is active, **Cancel Queue** prevents any remaining items from starting
but lets the active generation finish. **Stop safely** stops the active generation and
cancels the rest of the queue. Provider quota cards refresh once after the whole queue
ends rather than consuming refresh requests between items.

## What the app preserves

- All twelve production stages, cached-stage behavior, project locks, recovery,
  provider-cost approval, and preflight checks still come from `src\news_studio.py`.
- Every timed menu is sent to a Fluent content dialog with the same option keys,
  safe default, and countdown. **Cancel workflow** (or Escape) cancels the
  waiting bridge safely instead of selecting one of the production options.
- Manual-separator and optional AI-video checkpoints remain resumable. The app
  plays the configured Windows alert when a decision or manual action is needed.
- Network interruptions show a waiting state and automatically return to running
  when the pipeline's connectivity probe succeeds.
- Stopping the app terminates the current process tree; completed artifacts stay
  cached and the next GUI run recovers only a stale, non-running project lock.

## Settings safety

The Settings page is generated from `.env.example` and edits `.env` only after
Save changes is pressed. Secret-like keys are masked by default. Comments and key
order are retained, values are type-checked where possible, and a timestamped
`.env.backup-*` copy is created in `Automation/.env.backup/` before an atomic
replacement.

Settings are grouped by their actual pipeline role rather than relying only on
comment headings. Every category starts collapsed. The Windows-style **Find a
setting** field filters keys, categories, and descriptions, automatically expands
matching categories, and can be cleared with Escape or the clear button. UI
category names and descriptions are English even when source comments are not.

Use **Add new variable** for keys that are not in `.env.example`. The dialog
validates the variable name, can force secret masking, and stages the new row
until **Save changes** is pressed.

`NEWS_STUDIO_GUI_CHOICE_SOUND` and `NEWS_STUDIO_GUI_THEME` are app preferences
stored in `.env` when changed. The theme can be Light, Dark, or System.

## Quota and credit cards

The Production page caches provider checks for five minutes and has an explicit
Refresh button. ElevenLabs character balances plus Pexels, Pixabay, and Unsplash
request limits are shown only when their APIs expose those values. Restricted ElevenLabs keys are
validated separately against the voices endpoint, so a hidden balance is not
mistaken for an unusable voice key. OpenAI, Gemini, and YouTube display their
configuration state and an **Open** button to the provider's official usage or
quota dashboard rather than guessing a remaining balance.

A live Pexels, Pixabay, or Unsplash refresh consumes one list/search request for each
configured provider. No media is downloaded and no synthesis, generation, or upload
request is made by the quota panel.
