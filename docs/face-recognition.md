# Face identification (optional)

Uses **DeepFace** behind the optional **`[face]`** extra. Install:

```bash
uv pip install -e ".[face]"
# or: pip install 'spyoncino[face]'
```

Implementation: `spyoncino.postproc.face_identification` (see also constants aligned with this doc in source).

## Recipe: `postproc` → `face_identification`

| Key | Role |
|-----|------|
| `enabled` | Turn the face pipeline on or off. |
| `gallery_path` | On-disk gallery root (resolved with `data_root`; see [configuration.md](configuration.md)). |
| `detector_backend` | DeepFace face detector (e.g. `ssd`, `opencv`; heavier options need more deps). |
| `model_name` | Embedding model (e.g. `Facenet`). |
| `match_threshold` | Cosine match cutoff; **lower = stricter** (fewer false “known” matches). |
| `champion_frame_policy` | Which detection frame is used for the face crop (`area`, `confidence`, `combined`). |
| `recognition_cooldown_seconds_per_identity` / `unknown_prompt_cooldown_seconds` | Rate-limit repeated alerts. |

If faces are always “unknown”, try a slightly **higher** `match_threshold` (lower value = stricter matching), verify `gallery_path` points at populated identity folders, and confirm `[face]` installed without import errors in logs.

For path resolution and gallery layout vs `data_root`, see **[configuration.md](configuration.md)**.
