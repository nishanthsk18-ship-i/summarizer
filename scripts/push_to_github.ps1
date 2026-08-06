# ==============================================================================
# scripts/push_to_github.ps1 — 1-Click GitHub Repository Upload Helper
# ==============================================================================
param(
    [string]$RepoUrl
)

if ([string]::IsNullOrWhiteSpace($RepoUrl)) {
    $RepoUrl = Read-Host "Enter your GitHub Repository URL (e.g. https://github.com/your-name/ai-media-summarizer.git)"
}

if ([string]::IsNullOrWhiteSpace($RepoUrl)) {
    Write-Host "❌ Error: No repository URL provided." -ForegroundColor Red
    exit 1
}

Write-Host "🚀 Preparing to push to GitHub..." -ForegroundColor Cyan
git branch -M main
git remote remove origin 2>$null
git remote add origin $RepoUrl

Write-Host "📦 Pushing code to $RepoUrl..." -ForegroundColor Yellow
git push -u origin main

if ($LASTEXITCODE -eq 0) {
    Write-Host "======================================================================" -ForegroundColor Green
    Write-Host "✅ GitHub push successful!" -ForegroundColor Green
    Write-Host "Now go to https://share.streamlit.io to deploy your app in 1 click!" -ForegroundColor Green
    Write-Host "======================================================================" -ForegroundColor Green
} else {
    Write-Host "❌ Push failed. Check your GitHub authentication or repository URL." -ForegroundColor Red
}
