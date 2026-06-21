# Deploying media-ingestor to TrueNAS SCALE

Flow: `git push` → GitHub Actions builds the image → GHCR → Watchtower on the
NAS auto-pulls it. After the one-time setup below, **`git push` is the whole
deploy**.

---

## 1. Put the code on GitHub (one time)

**Option A — with the `gh` CLI (fastest):**
```powershell
scoop install gh
gh auth login                       # pick GitHub.com → HTTPS → login in browser
cd C:\Code\footage-ingestor
gh repo create christianprando/media-ingestor --public --source=. --push
```

**Option B — manually:** create an empty repo `media-ingestor` at github.com
(no README), then:
```powershell
cd C:\Code\footage-ingestor
git remote add origin https://github.com/christianprando/media-ingestor.git
git push -u origin main
```

The push triggers the **build** workflow. Watch it under the repo's **Actions**
tab; in ~2–3 min it publishes `ghcr.io/christianprando/media-ingestor:latest`.

## 2. Make the image public (one time)

So the NAS can pull without credentials:
GitHub → your profile → **Packages** → `media-ingestor` → **Package settings**
→ **Change visibility** → **Public**.
(The image holds no secrets — config is mounted separately.)

## 3. Prepare storage on TrueNAS (one time)

In the **main-pool/photos** dataset (filesystem path `/mnt/main-pool/photos`)
create these folders — they sit alongside your existing data, which the
container never touches:
```
/mnt/main-pool/photos/incoming
/mnt/main-pool/photos/originals
/mnt/main-pool/photos/derived
```
Then under `incoming/` create one folder per device:
```
incoming/osmo-pocket-4
incoming/canon
incoming/phone-christian
incoming/phone-natalie
```

Create the config file at `/mnt/main-pool/photos/.media-ingestor/config.toml` with:

```toml
[paths]
incoming  = "/data/incoming"
originals = "/data/originals"
derived   = "/data/derived"

[sources."osmo-pocket-4"]
publish = true
[sources."canon"]
publish = true
[sources."phone-christian"]
publish = false
[sources."phone-natalie"]
publish = false

[ingest]
delete_source_after_verify = true
prune_empty_dirs = true

[organize]
date_source = "metadata"      # exiftool is bundled in the image
use_filename_date = true
```

## 4. Create the Custom App (one time)

TrueNAS → **Apps** → **Discover Apps** → top-right **Custom App** →
**Install via YAML**. Paste [`docker-compose.yml`](docker-compose.yml) **as-is**
— the paths are already set for `main-pool/photos`. Deploy. The `media-ingestor`
container starts `watch`ing; `watchtower` keeps it updated.

## 5. Verify

- App **logs** should show `watching /data/incoming ... every 30s`.
- If you see `exiftool not found` — you shouldn't (it's in the image); if you do,
  the build didn't include it.
- Drop a test clip into `incoming/canon/` and watch it appear under
  `originals/YYYY/MM/`.

---

## Day-to-day after setup

```
edit code → pytest → git push
```
That's it. Watchtower pulls the new image within ~5 min (or hit **Update** on the
app). Roll back by pointing the app's image tag at a previous `sha-…` tag.

## Notes

- **File ownership:** the container runs as root, so archived files are
  root-owned. If another app (e.g. Immich) needs a specific owner, set `user:
  "UID:GID"` in the compose and make sure that user can write `/data`.
- **Backups:** the archive (`originals/`) still needs its own off-site backup —
  RAID and YouTube are not backups.
