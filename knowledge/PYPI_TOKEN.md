# Tightening PyPI publish credentials

Right now, PyPI uploads for `freeride-gateway` use an **account-wide token** generated when the project didn't yet exist. That token can publish *any* package from your account — too broad. Time to lock it down.

Two paths. **Trusted publishing is the better one** (no tokens at all, ever, for future releases). Project-scoped token is the fallback if you don't want to wire up GitHub Actions yet.

---

## Option A (recommended): Trusted Publishing via GitHub Actions OIDC

PyPI lets your GitHub Actions workflow upload directly using GitHub's OIDC identity. No token, no secret to leak, nothing to rotate.

### One-time setup (~5 min)

1. Go to <https://pypi.org/manage/project/freeride-gateway/settings/publishing/>
2. Click **"Add a new pending publisher"**
3. Fill in:
   - **PyPI Project Name:** `freeride-gateway`
   - **Owner:** `Shaivpidadi`
   - **Repository name:** `FreeRideV3`
   - **Workflow filename:** `release.yml`
   - **Environment name:** `pypi` *(this exact string — used by the workflow below)*
4. Click **"Add"**

### Add the workflow file

Create `.github/workflows/release.yml` in the repo:

```yaml
name: release

on:
  push:
    tags:
      - "v*.*.*"      # triggers on tags like v0.3.0, v0.3.0a3, v1.0.0

jobs:
  publish:
    runs-on: ubuntu-latest
    environment: pypi          # must match what was set in PyPI publisher config
    permissions:
      id-token: write          # required for trusted publishing OIDC
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - run: pip install --upgrade build
      - run: python -m build
      - run: ls -la dist/

      - name: Publish to PyPI
        uses: pypa/gh-action-pypi-publish@release/v1
        # No `with: password:` — it uses the OIDC token automatically.
```

Commit + push that.

### From now on, releases are tag-driven

```bash
# Bump version in freeride/__init__.py first, then:
git tag v0.3.0
git push origin main --tags
```

The tag push triggers the workflow → builds → publishes to PyPI. No tokens were touched.

### Revoke the old token

After the first trusted-publish run succeeds:
1. <https://pypi.org/manage/account/token/>
2. Find the broad-scope token (the one used for the manual upload of 0.3.0a1 / a2)
3. Click **Revoke**

---

## Option B (fallback): project-scoped API token

If you don't want to use GitHub Actions for now, you can keep using `twine upload` manually but with a token that **only** publishes to `freeride-gateway`. Smaller blast radius if it ever leaks.

### Steps

1. <https://pypi.org/manage/account/token/>
2. **Add API token**
3. Token name: `freeride-gateway uploads (2026)`
4. **Scope: "Project: freeride-gateway"** *(the dropdown — pick this, not "Entire account")*
5. Copy the token (shown once)
6. Save it somewhere local you trust (1Password, a `.env` file gitignored, etc.)

### Use it

```bash
TWINE_USERNAME="__token__" \
TWINE_PASSWORD="pypi-AgEN..." \
twine upload dist/*
```

### Revoke the old token

Same as Option A — revoke the broad-scope token at <https://pypi.org/manage/account/token/> after the new one works.

---

## Which to pick

- **Option A** if you're going to ship more than one release. The setup pays for itself by the second release.
- **Option B** if you just want to ship 0.3.0 and figure out automation later. Fully reversible — you can switch to Option A any time and just stop using the token.

Either way, **revoke the original broad-scope token** afterward. That's the actual security win.
