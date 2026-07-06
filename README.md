# Binance Pattern Report

GitHub Pages version of the Binance 4h pattern report.

The report page is generated daily by GitHub Actions and published through GitHub Pages.
The generated `index.html` opens with a password gate. The repository stores only the SHA-256
password hash used by the browser-side check, not the plaintext password.

## Setup

1. Create a new GitHub repository.
2. Upload everything in this `github_pages_pattern_task` folder to that repository root.
3. In the GitHub repository, open `Settings -> Pages`.
4. Set `Build and deployment -> Source` to `GitHub Actions`.
5. Open `Actions -> Daily Binance Pattern Pages -> Run workflow` once.

After the first successful run, the phone-readable page will be:

`https://<github-username>.github.io/<repo-name>/`

The workflow schedule is `01:00 UTC`, which is `09:00 Beijing time`.
