# Render Deployment Guide - Whisper Model Auto-Download

## Problem Solved ✓

The Whisper model (~150MB) cannot be uploaded to GitHub due to size limits. This is now handled automatically!

## How It Works

1. **Model is NOT in Git** - Excluded via `.gitignore`
2. **Auto-downloads on deployment** - The `load_model()` function downloads the Whisper model from Hugging Face on first use
3. **Pre-loaded at startup** - `initialize_model_on_startup()` runs when Render deploys to avoid first-request timeout
4. **Cached in Render** - Model is saved to the `models/` directory and persists between requests

---

## Render Configuration

### 1. Build Command
```bash
pip install -r requirements.txt
```

### 2. Start Command
```bash
python app.py
```

### 3. Environment Variables

#### Required:
```
PORT=(auto-set by Render)
FLASK_SECRET_KEY=<generate-random-key>
WHISPER_MODEL=base
WHISPER_CACHE_DIR=models
```

#### Firebase (if using):
```
FIREBASE_CREDENTIALS=./firebase-admin-key.json
GOOGLE_APPLICATION_CREDENTIALS=./firebase-admin-key.json
```

#### Admin Account:
```
APP_ADMIN_EMAIL=your-email@example.com
APP_ADMIN_PASSWORD=your-secure-password
```

#### Google API (for AI Saarthi):
```
GOOGLE_API_KEY=your-google-api-key
```

### 4. Secret Files Tab

Add Firebase credentials:
- **Filename**: `firebase-admin-key.json`
- **Contents**: (paste your Firebase JSON)

### 5. System Packages

Render will automatically detect `packages.txt` and install:
- `ffmpeg` (required for audio processing)

---

## Important Notes

### Model Download Time
- **First deployment**: Takes 2-3 minutes to download Whisper model (~150MB)
- **Subsequent deploys**: May need to re-download if Render clears disk
- **During startup**: Model downloads before app accepts requests (prevents timeout)

### Disk Space
- Render Free Tier: 512MB RAM, limited disk
- Whisper `base` model: ~150MB
- Consider upgrading to paid tier if you encounter disk issues

### Model Sizes
You can change the model size via `WHISPER_MODEL` env var:
- `tiny` - 75MB (fastest, least accurate)
- `base` - 150MB (recommended for free tier)
- `small` - 500MB (requires paid tier)
- `medium` - 1.5GB (requires paid tier)
- `large` - 3GB (requires paid tier)

---

## Troubleshooting

### Error: "Port not detected"
✅ **Fixed** - `app.py` now binds to `0.0.0.0:PORT`

### Error: "Could not initialize Whisper model"
**Causes:**
1. Not enough disk space → Upgrade Render plan or use `tiny` model
2. Network timeout → Render should retry automatically
3. Missing ffmpeg → Check `packages.txt` is committed

**Solution:**
Check Render logs:
```
Pre-loading Whisper model during startup...
Loading Whisper model 'base' to models
✓ Whisper model loaded successfully
```

### Error: "Firebase authentication failed"
- Ensure `firebase-admin-key.json` is added to Render Secret Files
- Check `FIREBASE_CREDENTIALS` env var is set to `./firebase-admin-key.json`

### Model downloads on every request
- This means `initialize_model_on_startup()` failed
- Check Render startup logs for errors
- Model should only download once per deployment

---

## Local Development

For local development:
1. The model will auto-download to `models/base.pt` on first run
2. This file is in `.gitignore` and won't be committed
3. Subsequent runs will use the cached model

---

## Deployment Checklist

- [ ] `.gitignore` excludes `models/*.pt`
- [ ] `packages.txt` includes `ffmpeg`
- [ ] `requirements.txt` includes `torch` and `tiktoken`
- [ ] Render Build Command: `pip install -r requirements.txt`
- [ ] Render Start Command: `python app.py`
- [ ] Environment variables configured (see above)
- [ ] Firebase secret file added (if using Firebase)
- [ ] First deploy: Allow 3-5 minutes for model download

---

## Logs to Watch

During deployment, you should see:
```
Pre-loading Whisper model during startup...
Loading Whisper model 'base' to models
100%|██████████| 139M/139M [00:45<00:00, 3.24MB/s]
✓ Whisper model loaded successfully
 * Running on http://0.0.0.0:10000
```

If you see:
```
Could not pre-load model at startup: <error>
Model will be loaded on first transcription request
```
The app will still work, but the first transcription request will be slower.

---

## Performance Tips

1. **Use `tiny` or `base` model** on free tier
2. **Upgrade to Starter plan** ($7/month) for better performance and `small` model
3. **Monitor disk usage** in Render dashboard
4. **Check logs** after each deploy to confirm model loaded

---

## Summary

✅ Model auto-downloads from Hugging Face (not from Git)
✅ Pre-loaded at startup to avoid first-request timeout  
✅ Cached between requests on Render's disk  
✅ Works with free tier using `base` model  
✅ No manual upload needed  

Your app is ready to deploy! 🚀
