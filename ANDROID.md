# Android (Android Studio)

This repo is a Flask web app (server-rendered HTML). The Android project in `android/` is a WebView wrapper so the app works the same UI/flows on mobile.

## 1) Run the Flask server

From the repo root:

```powershell
python app.py
```

By default Flask listens on port `5000`.

## 2) Open the Android project

- Open Android Studio
- `File -> Open...` and select the `android/` folder

## 3) Point the app to your server

The app loads a configurable **Server URL**:

- Android Emulator default: `http://10.0.2.2:5000/`
- Physical phone on same Wi‑Fi: `http://<YOUR_PC_LAN_IP>:5000/`

In the Android app: menu `Settings` -> set **Server URL**.

## Notes

- If using a phone, allow Windows Firewall inbound access to port `5000`.
- The Android app enables cleartext HTTP for local development (`usesCleartextTraffic=true`).

## Camera note (important)

- `getUserMedia()` (live camera preview in the browser/WebView) requires a **secure context** (HTTPS or `http://localhost`).
  - If you open the Flask server using a LAN IP like `http://192.168.x.x:5000/` or `http://10.x.x.x:5000/`, Chrome will block the live preview and the UI will fall back to a file picker.
- In the Android wrapper app, the **Open Camera** flow uses the native Android camera intent as a fallback so you can still take a photo even when using plain HTTP during development.
