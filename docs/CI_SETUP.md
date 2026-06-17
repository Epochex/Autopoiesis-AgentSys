# CI Setup

The workflow file is prepared at:

```text
ci/github-workflows/python-phase15.yml
```

Copy it to `.github/workflows/python-phase15.yml` when pushing with a GitHub token that has `workflow` scope:

```bash
mkdir -p .github/workflows
cp ci/github-workflows/python-phase15.yml .github/workflows/python-phase15.yml
git add .github/workflows/python-phase15.yml
git commit -m "Add Python phase 1.5 CI"
git push origin main
```

The current token previously rejected pushes touching `.github/workflows/*`, so the workflow is stored outside `.github` until credentials are fixed.
