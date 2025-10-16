# Render Configuration Checklist

## Critical Settings to Fix Model Re-downloading

### 1. Environment Variables (Render Dashboard → Environment)

```bash
# Essential for memory optimization
WHISPER_MODEL=tiny
WHISPER_CACHE_DIR=/opt/render/project/src/models

# Flask settings
FLASK_SECRET_KEY=<your-secret-key>
PORT=10000

# Admin account
APP_ADMIN_EMAIL=your-email@example.com
APP_ADMIN_PASSWORD=your-secure-password
```

### 2. Why Model Downloads Every Time

**Root Cause**: The model is stored in memory (`_MODEL` global variable), not on disk.

**What's happening**:
- ❌ If startup pre-loading failed → model loads on EVERY request
- ✅ If startup pre-loading worked → model loads ONCE and stays in RAM

### 3. Check Your Render Logs

After deploying, look for:

#### ✅ Good (Model loads once at startup):
```
Pre-loading Whisper model during startup...
Loading Whisper model 'tiny' to /opt/render/project/src/models
100%|███████████████████████████████████████| 73M/73M [00:02<00:00, 35.2MiB/s]
✓ Whisper model ready
 * Running on http://0.0.0.0:10000
```

Then on each transcription request:
```
Using cached Whisper model from memory  ← Should see this!
```

#### ❌ Bad (Model downloads every time):
```
Could not pre-load model at startup: <some error>
Model will be loaded on first transcription request
```

Then on EACH transcription:
```
Loading Whisper model 'tiny' to /opt/render/project/src/models
100%|███████████████████████████████████████| 73M/73M [00:03<00:00, 21.5MiB/s]
```

### 4. Memory Requirements with tiny model

- Model download: ~73MB disk temporarily
- Model in RAM: ~150-200MB
- Flask + Python: ~50-100MB
- **Total**: ~250-350MB (fits in 512MB free tier ✓)

### 5. If Model Still Downloads Every Time

**Possible causes**:

1. **Render is restarting your app** (check for crashes in logs)
2. **Multiple worker processes** (free tier should use 1 worker)
3. **Startup timeout** → model didn't finish loading

**Solutions**:

#### A. Increase startup time tolerance
Render gives ~60 seconds for startup. The `tiny` model should download in 2-3 seconds.

#### B. Use persistent disk (Paid tier only)
Free tier has ephemeral disk, but RAM persistence works fine if app doesn't crash.

#### C. Check for app crashes
```bash
# In Render logs, look for:
Application Error
Worker timeout
Out of Memory
```

### 6. Deployment Commands

Make sure these are correct in Render:

**Build Command**:
```bash
pip install -r requirements.txt
```

**Start Command**:
```bash
python app.py
```

### 7. Test After Deploying

1. Watch Render logs during deployment
2. Wait for "✓ Whisper model ready" message
3. Try transcribing → should NOT show download progress
4. Try transcribing again → should say "Using cached Whisper model from memory"

---

## Quick Fix Summary

1. ✅ Use `tiny` model (already changed in code)
2. ✅ Enable startup loading (already changed in code)  
3. ⚠️ Set `WHISPER_MODEL=tiny` in Render environment
4. ⚠️ Verify no startup crashes in logs
5. ⚠️ Confirm "Using cached model" appears after first transcription

Deploy with:
```powershell
git add .
git commit -m "Fix model re-downloading: use tiny model + startup loading"
git push origin main
```

Then monitor Render logs carefully!
