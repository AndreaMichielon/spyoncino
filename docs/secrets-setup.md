# Secrets configuration

Telegram credentials and other sensitive values live in **`data/config/secrets.yaml`**. That file is **gitignored** and must not be committed.

Paths below are relative to the **repository root** (the process working directory when Spyoncino runs).

## File shape

Create `data/config/secrets.yaml` with at least:

```yaml
telegram:
  token: "YOUR_TELEGRAM_BOT_TOKEN"  # REQUIRED: from @BotFather
  chat_id: null                       # Optional: auto-detected or set manually

authentication:
  setup_password: "your_strong_password"  # First-time /setup
  superuser_id: null                      # Auto-set during /setup
  user_whitelist: []                      # Managed via bot commands
```

## Setup

### 1. Create the file

From the repo root:

```bash
cp data/config/secrets.yaml.example data/config/secrets.yaml
# then edit data/config/secrets.yaml with your token and password
```

If you prefer, create `data/config/secrets.yaml` manually using the structure above.

### 2. Telegram bot token

1. In Telegram, open `@BotFather`, run `/newbot`, and follow the prompts.
2. Copy the token (format `123456789:ABCdef...`).
3. Set `telegram.token` in `data/config/secrets.yaml`.

### 3. Chat ID (optional)

- **Auto (recommended):** leave `chat_id: null`, start the bot, send `/start` — the bot can pick up your chat id.
- **Manual:** use `/whoami` (or similar) and set `chat_id` to your numeric id.

### 4. Confirm git ignore

```bash
grep "secrets.yaml" .gitignore
```

You should see entries such as `data/config/secrets.yaml`.

## Recipe

Point the Telegram interface at the secrets file, for example:

```yaml
interfaces:
  - name: "Telegram Notifications"
    class: "telegram"
    params:
      secrets_path: "data/config/secrets.yaml"
      memory_manager: null
      config:
        notify_on_detection: ["text", "gif"]
        notify_on_preproc: []
        notify_on_face: []
        gif: { fps: 10, duration: 3 }
        video: { fps: 10, duration: 3, format: mp4 }
        max_file_size_mb: 50.0
        notification_rate_limit: 5
```

`secrets_path` is resolved from the **current working directory**, not `data_root`.

## Security practices

1. Never commit `secrets.yaml`.
2. Use a strong `setup_password`.
3. On Linux/macOS: `chmod 600 data/config/secrets.yaml`.
4. Keep backups encrypted if you store them anywhere else.
5. Rotate the bot token via @BotFather if it leaks.

## Troubleshooting

| Symptom | What to check |
|--------|----------------|
| Secrets file not found | File exists at `data/config/secrets.yaml`; `secrets_path` in the recipe matches how you run the app (cwd). |
| No `telegram` section | YAML structure and indentation under `telegram:`. |
| No `token` | `telegram.token` is set and quoted. |
| Bot silent | Token correct, bot not blocked, `chat_id` set or auto-detected. |

Restart the application after changing `secrets.yaml`.

## Example

```yaml
telegram:
  token: "1234567890:ABCdefGHIjklMNOpqrsTUVwxyz1234567890"
  chat_id: 123456789

authentication:
  setup_password: "MySecurePassword123!"
  superuser_id: 123456789
  user_whitelist: [123456789]
```
