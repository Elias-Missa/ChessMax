$ErrorActionPreference = "Stop"
$pgn = "data/raw/lichess_db_standard_rated_2026-04.pgn.zst"
$url = "https://database.lichess.org/standard/lichess_db_standard_rated_2026-04.pgn.zst"

# Resumable download. -C - tells curl to continue from wherever the local file left off.
# --retry 50 / --retry-delay 10 handle transient network drops without giving up.
Write-Host "[$(Get-Date -Format o)] Resuming download from $((Get-Item $pgn).Length) bytes"
curl.exe -C - -L --retry 50 --retry-delay 10 --retry-connrefused -o $pgn $url
if ($LASTEXITCODE -ne 0) {
    Write-Host "[$(Get-Date -Format o)] Download exited with $LASTEXITCODE"
    exit $LASTEXITCODE
}

Write-Host "[$(Get-Date -Format o)] Download complete. Starting mining."
python -m pipeline.mine_quiet $pgn --target-count 5000 --depth 12 --threads 8 --hash-mb 512 --batch-size 25 --progress-every 100 --seed 42
Write-Host "[$(Get-Date -Format o)] Mining exited with $LASTEXITCODE"
