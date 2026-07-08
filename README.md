# Market Pattern Report

GitHub Pages version of the 4h market pattern report.

The report page is generated daily by GitHub Actions and published through GitHub Pages.
The generated `index.html` opens with a password gate. The repository stores only the SHA-256
password hash used by the browser-side check, not the plaintext password.

## Setup

1. Create a new GitHub repository.
2. Upload everything in this `github_pages_pattern_task` folder to that repository root.
3. In the GitHub repository, open `Settings -> Pages`.
4. Set `Build and deployment -> Source` to `GitHub Actions`.
5. Open `Actions -> Daily Market Pattern Pages -> Run workflow` once.

After the first successful run, the phone-readable page will be:

`https://<github-username>.github.io/<repo-name>/`

The daily report scans the static `futures_symbols_2026.txt` file.

The daily report workflow runs at `00:05 UTC`, which is `08:05 Beijing time`.
The symbols file is updated by a separate monthly workflow near Beijing month-end.
That workflow refuses to write or commit if the fetched list is empty or has fewer
than 400 symbols, and it only commits real list changes with the fixed message
`Update futures symbols`.
