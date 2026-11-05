---
name: os-up
description: Start the AI Team OS services in one step — API + Dashboard
---

# /os-up — One-Step Service Startup

Start the AI Team OS API server and Dashboard frontend.

## Steps

### 1. Check the API service
```bash
curl -s --max-time 2 http://localhost:8000/api/teams
```
- If it returns JSON → the API is already running; skip to step 3
- If it times out/fails → go to step 2

### 2. Start the API service (in the background)
```bash
# Run in the project root (the directory containing pyproject.toml)
python -m uvicorn aiteam.api.app:create_app --host 0.0.0.0 --port 8000 --factory &
```
Wait 3 seconds, then verify:
```bash
curl -s --max-time 3 http://localhost:8000/api/teams
```

### 3. Check the Dashboard
```bash
curl -s --max-time 2 http://localhost:5173
```
- If it returns HTML → the Dashboard is already running; skip to step 5
- If it times out/fails → go to step 4

### 4. Start the Dashboard (in the background)
```bash
# Run in the project's dashboard subdirectory
cd dashboard
npm run dev &
```
Wait 5 seconds, then verify:
```bash
curl -s --max-time 3 http://localhost:5173
```

### 5. Report status

Show:
```
AI Team OS service status:
- API: http://localhost:8000 ✅/❌
- Dashboard: http://localhost:5173 ✅/❌
- API docs: http://localhost:8000/docs
- WebSocket: ws://localhost:8000/ws
```

## Notes
- Start services in the background (`&`) so the current session is not blocked
- Check before starting to avoid duplicate instances
- The Dashboard runs with npm run dev (development mode); stop it with Ctrl+C or 'q'
- If startup fails, suggest checking dependencies: `pip install -e ".[all]"` and `cd dashboard && npm install`
