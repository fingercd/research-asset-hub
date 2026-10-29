# Video PDF recovery downloader

This narrow tool consumes `2026-08-31-video-missing-40-recovery-map.json` and
downloads only its 40 verified official PDF URLs into a core-domain staging
directory. It allowlists arXiv, OpenReview, and CVF; limits responses to 200 MiB;
requires parseable PDF bytes; computes SHA-256/page counts; and compares the
expected title with text from the first two pages.

It never writes to or removes the Desktop acquisition source. Existing staged
files are revalidated and reused without overwrite.

```powershell
.\.venv\Scripts\python.exe tools\paper_recovery\download_missing_video.py `
  --map work\validation\2026-08-31-video-missing-40-recovery-map.json `
  --output-dir 'D:\ResearchLibrary\VLM框架3DV压缩\work\intake-20260831\recovered-pdfs' `
  --report work\validation\2026-08-31-video-missing-40-download.json `
  --jobs 4
```

`downloaded_needs_title_review` is not a successful identity verdict; those
files require comparison with the retained MinerU markdown before integration.

If OpenReview rejects the machine's plain HTTP route with 403 while the user's
connected browser can open the official forum, recover only the mapped indexes
through WebBridge. The helper exports PDF bytes in 256 KiB chunks; it never
exports browser cookies or headers and closes its dedicated browser session.

```powershell
.\.venv\Scripts\python.exe tools\paper_recovery\download_openreview_via_webbridge.py `
  --map work\validation\2026-08-31-video-missing-40-recovery-map.json `
  --output-dir 'D:\ResearchLibrary\VLM框架3DV压缩\work\intake-20260831\recovered-pdfs' `
  --indexes 11 24 37 `
  --report work\validation\2026-08-31-openreview-browser-recovery.json
```
