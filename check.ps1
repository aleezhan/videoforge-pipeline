param(
    [Parameter(Position = 0)][string]$Task = "check",
    [Parameter(Position = 1)][string]$Base = ""
)

# Developer task runner (PowerShell). CI runs the same checks on Linux:
# see .github/workflows/ci.yml.

function Stamp {
    # Print which commit (and how many uncommitted files) a full run refers to.
    $dirty = @(git status --porcelain | Where-Object { $_ }).Count
    Write-Host ("commit " + (git rev-parse --short HEAD) +
                ", uncommitted files: $dirty, " + (Get-Date -Format "yyyy-MM-dd HH:mm"))
}

switch ($Task) {
    # Full suite, one line per test (the suite is long; dots give no progress),
    # with the ten slowest tests listed at the end.
    "test"  { Stamp; uv run pytest -v --durations=10 }
    "quick" {
        # Only tests affected by the current diff, without render tests.
        # tools/affected_tests.py prints:
        #   edited  <file>  the test file itself changed: run it fully, render included
        #   reached <file>  the change reaches it through imports: run without render
        #   ALL             shared files changed (templates, workflows, conftest): all, no render
        # This speeds up the inner loop; it does NOT replace `check`.
        # Optional argument: the base to diff against, e.g. `.\check.ps1 quick origin/main`.
        # pytest exit code 5 ("nothing collected") is not a failure here.
        $rows = @(uv run python tools/affected_tests.py $Base | Where-Object { $_ })
        if ($LASTEXITCODE -ne 0) { exit 1 }
        if ($rows.Count -eq 0) {
            Write-Host "no code changes - nothing to check"
            exit 0
        }
        $edited  = @($rows | Where-Object { $_ -like "edited *" }  | ForEach-Object { $_.Substring(7) })
        $reached = @($rows | Where-Object { $_ -like "reached *" } | ForEach-Object { $_.Substring(8) })
        $files = @($edited + $reached)
        if ($rows -contains "ALL") {
            Write-Host "shared files changed - whole suite without render tests"
            uv run pytest -v --ff -m "not render"
        }
        else {
            Write-Host ("affected test files: " + $files.Count + " of " +
                        (Get-ChildItem tests -Filter 'test_*.py').Count + ", without render")
            uv run pytest -v --ff -m "not render" @files
        }
        if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 5) { exit $LASTEXITCODE }
        if ($edited.Count -gt 0) {
            Write-Host ("render tests of edited files (" + $edited.Count + ")")
            uv run pytest -v --ff -m render @edited
            if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 5) { exit $LASTEXITCODE }
        }
        Write-Host "remaining render tests run in the full .\check.ps1 check"
        exit 0
    }
    "lint"  { uv run ruff check . ; if ($LASTEXITCODE -eq 0) { uv run ruff format --check . } }
    "fix"   { uv run ruff check --fix . ; uv run ruff format . }
    "types" { uv run mypy videoforge }
    "ui"    {
        # Control panel frontend: type check + build into ui/dist (served by `videoforge ui`).
        # Separate from `check` because it needs Node.js; the API works without it.
        Push-Location ui
        if (-not (Test-Path node_modules)) { npm install }
        npm run build
        Pop-Location
    }
    "check" {
        # Everything that must be green before a commit.
        uv run ruff check .
        if ($LASTEXITCODE -ne 0) { exit 1 }
        uv run ruff format --check .
        if ($LASTEXITCODE -ne 0) { exit 1 }
        uv run mypy videoforge
        if ($LASTEXITCODE -ne 0) { exit 1 }
        Stamp
        uv run pytest -v --durations=10
    }
    default { Write-Host "Tasks: test, quick, lint, fix, types, check, ui" }
}
exit $LASTEXITCODE
