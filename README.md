# Instagram Downloader

A Python script for downloading Instagram photos and videos:

- **Posts, reels and carousels**: paste a link and get everything in it, every photo and video of a carousel included.
- **Photos only, videos only, or both.**
- **Resolution choice**: highest available (the default), choose from a list for each post, or cap it at a maximum such as 1080, 720 or 480.
- **Several links at once**: paste many links, or give a `.txt` file with links. Each link is downloaded in turn, and failed ones are saved to a file so you can retry them.
- **Stories**: from one user, from everyone you follow, or from a story link.
- **Highlights**: all of a user's highlights or just the ones you pick, or a highlight link.
- **Saved posts**: export the links of all your saved posts (or one collection) to `.txt` and `.csv`, and optionally download them all.

It uses only the Python standard library, so there's nothing to `pip install`.

## Requirements

- **Python 3.9 or newer.**
- **ffmpeg** (optional, recommended). Instagram often keeps the best video quality, for example 720p
  instead of 480p, as separate video and audio streams, and ffmpeg joins them without re-encoding.
  Without it you get the best single-file version and a tip when a better one exists. To install on Windows:

  ```bash
  winget install Gyan.FFmpeg
  ```

## Quick start

```bash
python instagram_downloader.py
```

This opens the menu:

```
  1. Download posts / reels / carousels (paste one or more links)
  2. Download stories of a user
  3. Download stories of everyone I follow
  4. Download highlights of a user
  5. Export the links of my saved posts
  6. Settings
  7. Log in
  8. Log out
  0. Exit
```

You can also paste a link straight at the menu prompt to download it.

When you paste links in option 1, put one per line or separate them with spaces, then press Enter on an
empty line. Before the download starts you can press Enter to use your default options, or type `o` to change
them for this download: what to get (everything, photos only or videos only) and which quality.

Public posts and reels work without logging in. Stories, highlights, saved posts and private accounts you
follow need you to log in, and downloads are more reliable when you're logged in.

## Logging in

The script never asks for your password. It imports the session of a browser where you're already logged in
to instagram.com (menu option 7):

1. **Firefox (automatic).** Reads the login from Firefox. Log in to instagram.com in Firefox first.
2. **A cookies file.** Export your cookies with a browser extension such as *Get cookies.txt LOCALLY*
   (`cookies.txt` format) or *Cookie-Editor* (JSON format), then give the script the file.
3. **Paste the `sessionid` cookie.** This works with any browser, including Chrome and Edge, whose cookies
   other programs can't read anymore:
   1. Open https://www.instagram.com and make sure you're logged in.
   2. Press **F12**, open the **Application** tab (**Storage** in Firefox), then
      **Cookies > https://www.instagram.com**.
   3. Copy the **Value** of the `sessionid` row and paste it into the script (the input stays hidden).

The session is saved in `%APPDATA%\InstagramDownloader\session.json` on Windows
(`~/.config/instagram-downloader/` elsewhere). **Treat it like a password**: anyone who has it can use your
account. **Log out** (option 8) deletes it. Your browser stays logged in. If Instagram ends the session
(for example after you log out in the browser), log in again here.

## Quality and resolutions

Instagram stores each photo and video in several sizes:

| Choice | What you get |
|---|---|
| **Highest available** (default) | The largest version. At equal resolution, the higher-bitrate one. |
| **Let me choose for each post** | A list of every version, such as `720x1280, 1.8 Mbps, ~3.1 MB`. Carousels ask once for all photos and once for all videos. |
| **Maximum resolution** (e.g. `720`) | The largest version whose shorter side is at most that many pixels. If every version is bigger, you get the smallest one. |

Files downloaded below the best quality get the size in their name, for example `..._480x854.mp4`, so they
don't clash with the full-quality download. To see what's available without downloading anything, use `--list`.

## Where the files go

The default download folder is `Downloads\Instagram`. You can change it in Settings.

```
Instagram/
  <username>/
    2024-05-01_C7abcDEF123.jpg              single photo or video
    2024-05-01_C7abcDEF123_1.jpg            carousel item 1, 2, 3...
    stories/2024-05-01_14-30-05_<id>.mp4
    highlights/<highlight title>/2024-03-02_09-10-11_<id>.jpg
  saved_posts_2024-05-01_143000.txt         saved-post links, one per line
  saved_posts_2024-05-01_143000.csv         the same with user, type, date and caption
  failed_links_2024-05-01_143512.txt        links to retry
```

Files that already exist are skipped, so you can re-run the same list safely. The file dates are set to the
date the content was posted. You can also save captions as `.txt` files (turn it on in Settings).

To download your saved posts later, or to retry failed links, paste the path of the `.txt` file into option 1.

## Command-line use

Run without arguments for the menu, or use options. `python instagram_downloader.py --help` lists all of them.
Some examples:

```bash
# one or more posts / reels / carousels
python instagram_downloader.py https://www.instagram.com/p/XXXXXXXXXXX/ https://www.instagram.com/reel/YYYYYYYYYYY/

# links from a file, videos only, at most 720p, into another folder
python instagram_downloader.py -f links.txt -t videos -q 720 -o D:\Instagram

# choose the resolution for each post / only list what's available
python instagram_downloader.py -q ask https://www.instagram.com/p/XXXXXXXXXXX/
python instagram_downloader.py --list https://www.instagram.com/p/XXXXXXXXXXX/

# stories and highlights (need login)
python instagram_downloader.py --stories user1 user2
python instagram_downloader.py --following-stories
python instagram_downloader.py --highlights user1

# saved posts (need login)
python instagram_downloader.py --export-saved
python instagram_downloader.py --export-saved --download-saved -t photos
python instagram_downloader.py --export-saved --collection https://www.instagram.com/you/saved/recipes/17900000000000000/

# logging in / out
python instagram_downloader.py --login firefox
python instagram_downloader.py --cookies cookies.txt
python instagram_downloader.py --login sessionid
python instagram_downloader.py --logout
```

`-t/--type`, `-q/--quality`, `-o/--output` and `--captions` apply only to that run. Your saved settings don't
change.

## Being careful with your account

Instagram limits how fast an account can make requests, and automated downloading is against its terms of
use. To keep your account safe, the script waits a few seconds between requests to Instagram (4 to 8 seconds
by default; change it in Settings). If Instagram asks it to slow down, it waits and retries, and it stops a
batch instead of pushing on. Avoid downloading thousands of posts in one go. Download only content you're
allowed to keep, such as your own posts or posts for personal offline viewing, and respect the creators'
rights.

## Troubleshooting

| Message | What to do |
|---|---|
| *Instagram is limiting logged-out visitors* | Log in, or wait a few minutes. |
| *Instagram is rate-limiting your account* | Take a break (15+ minutes), then retry the saved `failed_links_*.txt`. Consider a bigger delay in Settings. |
| *Your Instagram session has expired* | Log in again (option 7). To keep going with public posts only, log out. |
| *Instagram wants to verify your account* | Open instagram.com in your browser, complete the check, then log in here again. |
| *Post not found* | The post was deleted, or it's from a private account you don't follow (or you're not logged in). |
| Video saved *without sound* | Install ffmpeg (see Requirements). |

## Development

```bash
python -m unittest
```

The tests run offline. One test merges real video and audio and needs ffmpeg; it's skipped if ffmpeg is missing.

| Module | Purpose |
|---|---|
| `instagram_downloader.py` | Entry point |
| `igdl/cli.py` | Menu, prompts and command-line options |
| `igdl/api.py` | Instagram web API: posts, stories, highlights, saved posts, rate limiting, errors |
| `igdl/media.py` | Turns API data into photos and videos with all their versions, and picks the version to download |
| `igdl/download.py` | Saves files, names them, and merges video and audio with ffmpeg |
| `igdl/auth.py` | Imports the login from Firefox, cookie files or a pasted `sessionid`, and stores the session |
| `igdl/links.py` | Recognizes the different kinds of Instagram links |
| `igdl/net.py` | Small HTTP client built on `urllib` |
| `igdl/settings.py` | Settings stored in the user's config folder |

Instagram changes its private web API from time to time. If something stops working, the likely places to
look are the endpoint and header constants at the top of `igdl/api.py`.
