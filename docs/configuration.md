# Configuration reference

Deep dive for paths, detectors, retention, and backups. For a minimal setup, see the [README](../README.md) quick start and [`data/config/recipe.yaml`](../data/config/recipe.yaml).

## Config files

| File | Purpose |
|------|---------|
| `data/config/recipe.yaml` | Patrol timing, inputs, preproc/inference/postproc, interfaces, media retention — safe to version |
| `data/config/secrets.yaml` | Telegram, optional API keys, authentication — **never commit** (see [secrets-setup.md](secrets-setup.md)) |

## Recipe cheat sheet (paths, detector, face)

Defaults below match the sample **`data/config/recipe.yaml`**. Your file overrides them.

**Where things live (generic `data_root`)** — With default `data_root: "data"`, most relative paths resolve under **`data/`**: SQLite, media store, YOLO weights, etc. **`secrets_path`** is the exception: it is always relative to the **process working directory** (usually repo root), e.g. `data/config/secrets.yaml`, and is **not** joined with `data_root`. Set `data_root: null` only if you want the legacy layout (everything relative to cwd without a `data/` anchor). Implementation: `spyoncino.recipe_paths`.

**Face gallery vs `data_root`** — Face identities and DeepFace’s on-disk layout use **`postproc` → `gallery_path`**. That path is resolved with the same rules as other recipe paths: relative segments are under `data_root`. If you keep the sample value `gallery_path: "data/face_gallery"` while `data_root` is `"data"`, the leading `data/` segment is **deduplicated** so the gallery is **`data/face_gallery/`**, not `data/data/face_gallery`. Put identity folders and exemplars under that directory; SQLite still stores identity metadata in **`data/spyoncino.db`** (separate from image files).

| Recipe area | Main keys | Typical resolved location (default `data_root`) |
|-------------|-----------|-----------------------------------------------|
| Database | `sqlite_path` | `data/spyoncino.db` |
| Recordings index | `media.root` | `data/media/` |
| YOLO weights | `inference.*.params.weights` | e.g. `data/weights/yolov8n.pt` |
| Face gallery | `postproc` → `gallery_path` | e.g. `data/face_gallery/` |

### Object detection (YOLO)

`inference` → `detector` params:

| Key | Role |
|-----|------|
| `weights` | Path to `.pt` (under `data_root` when relative). Missing weights are filled from cache, cwd, or Ultralytics hub (see `spyoncino.inference.object_detection`). |
| `conf_threshold` / `iou_threshold` | Detection and NMS sensitivity. |
| `batch_size` | Frames per YOLO batch. |
| `alarmed_classes` | Label names that count as alarms (e.g. `person`). |

Inference uses **CUDA when PyTorch sees a GPU**, otherwise **CPU** (`object_detection.py`).

Optional face identification is documented in **[face-recognition.md](face-recognition.md)**.

## Troubleshooting (runbook)

| Symptom | What to check |
|--------|----------------|
| `Secrets file not found` | Path in `secrets_path` vs cwd; run `spyoncino` from repo root or use an absolute path. |
| YOLO errors / missing weights | Put a full `yolov8n.pt` (~6 MiB) in `data/weights/` or repo root; see `object_detection` weight resolution. GitHub 504 during download is transient—retry or copy the file manually. |
| Wrong detections | Lower or raise `conf_threshold`; adjust `iou_threshold`; confirm `alarmed_classes`. |
| Face always “unknown” | See [face-recognition.md](face-recognition.md) — `match_threshold`, `gallery_path`, and `[face]` extra. |
| Face pipeline not running | `enabled: true` under `face_identification`; check logs for DeepFace import errors. |
| CUDA not used | Install a CUDA build of PyTorch matching your GPU driver; otherwise inference falls back to CPU. |
| Media or DB “in the wrong place” | Confirm `data_root`, `sqlite_path`, and `media.root` in `data/config/recipe.yaml`; see table above. |

## Data layout, retention, logs

**What you need for the product to run:** Nothing beyond a valid recipe and secrets. **Paths** (`data_root`, SQLite, media, weights, gallery) are resolved in code (`spyoncino.recipe_paths`). **Retention** for stored media and for SQLite event/metrics rows is configured in `data/config/recipe.yaml` and enforced by the orchestrator.

### Retention (tune in `data/config/recipe.yaml`)

| Area | Recipe keys | Role |
|------|-------------|------|
| Media files on disk | `media.retention_days`, `media.max_total_mb`, `media.max_files_per_camera`, `media.retention_every_n_cycles` | Age/size limits; how often the orchestrator runs cleanup (`retention_every_n_cycles`). `0` disables where documented in YAML comments. |
| SQLite (`events` / `metrics`) | `event_log.retention_days`, `event_log.retention_every_n_cycles` | Prunes old analytics/timeline rows. |

**Logs and temp files:** There is no single recipe key for “log directory.” By default the app logs to **stderr / the console** (Python logging). **Temp / cache:** Ultralytics and DeepFace may use their own cache dirs (e.g. under your user profile); PyTorch may cache near the venv.

**Logs in production:** Capture process output however your host expects it: redirect stdout/stderr to files, run under **systemd** / **Windows Service** / **Task Scheduler** with logging, or put the process in **Docker** and use `docker logs`. The application does not rotate log files for you — use `logrotate`, service manager limits, or your platform’s log pipeline.

### Backup and restore (minimal)

| What | Why |
|------|-----|
| `data/config/recipe.yaml` | Pipeline definition |
| `data/config/secrets.yaml` | **Keep private** — store encrypted or offline; never commit |
| `data/spyoncino.db` | Events, metrics, identity/pending-face metadata |
| `data/media/` | If you need historical clips indexed by the app |
| `data/face_gallery/` | Identity exemplars (if you use face ID) |
| `data/weights/*.pt` | Optional if you do not want to re-download |

Reinstall deps with `uv sync` on the target machine, then run `spyoncino data/config/recipe.yaml` from the repo root (or your chosen working directory for `secrets_path`). For disaster-recovery runbooks and shipping notes, see **[ops.md](ops.md)** and **[architecture-backlog.md](architecture-backlog.md)**.
