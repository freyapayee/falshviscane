# VISCANE farmer Android app

Open `android/` in Android Studio. The Kotlin WebView connects to
`djangoviscane_final/`, the Django backend. Internet and a running backend are
required; Python, the database and the classifier are not bundled in the APK.

## Latest local verification (3 October 2026)

- Android Studio configuration switched from local Gradle 8.9 to the 8.11.1 wrapper.
- `assembleDebug`, `testDebugUnitTest` and `lintDebug` passed with JDK 17.
- All 3 navigation tests passed. Lint reported 0 errors and 3 warnings:
  newer Gradle available, WebView JavaScript usage and launcher icon shape.
- `apksigner verify` passed for the generated debug APK.
- Django `manage.py check` passed.
- Debug APK installed on the connected Pixel 8 API 37 emulator. The farmer
  welcome page loads at `http://10.0.2.2:5000/`; Settings displays its Server URL
  below the toolbar. Both `10.0.2.2` and `172.18.128.1` host requests return HTTP 200
  after correcting the backend's local host list and restarting Django.
- Emulator screenshots are in `djangoviscane_final/.local/android-readiness/`.
  Camera, prediction and physical-device acceptance remain pending.
  No signed production artifact was generated.

## Local development

Use JDK 17, SDK platform 36 and Build Tools 35.0.0. The wrapper pins Gradle
8.11.1; the project uses AGP 8.10.1 and Kotlin 2.1.20. Minimum Android: API 24.

### Resolve the Android Studio sync error first

The screenshot's Gradle 8.9 error comes from Android Studio using a local
distribution instead of the wrapper. `android/.idea/gradle.xml` now selects the
wrapper. Close and reopen the `android/` project so Studio reloads that setting.
In **File > Settings > Build, Execution, Deployment > Build Tools > Gradle**,
select **Wrapper / gradle-wrapper.properties** as the Gradle distribution and
JDK 17 as the Gradle JDK, then **File > Sync Project with Gradle Files**.
The wrapper URL must end in `gradle-8.11.1-bin.zip`.
After sync, select the `app` run configuration and a device. If it is missing,
use **Run > Edit Configurations > + > Android App**, module `app`, and launch
the default activity.

AGP compatibility reference:
[Android Developers](https://developer.android.com/build/releases/agp-8-10-0-release-notes).

From the repository root:

```powershell
.\.venv-django\Scripts\python.exe -m pip install -r djangoviscane_final/requirements-windows.lock.txt
cd djangoviscane_final
$env:VISCANE_FARMER_ONLY = 'true'
..\.venv-django\Scripts\python.exe manage.py migrate
..\.venv-django\Scripts\python.exe manage.py runserver 0.0.0.0:5000 --noreload
```

In another terminal:

```powershell
cd android
$env:GRADLE_USER_HOME = Join-Path (Get-Location) '.gradle-user'
.\gradlew.bat assembleDebug testDebugUnitTest lintDebug
```

Debug APK: `android/app/build/outputs/apk/debug/app-debug.apk`.
Emulator URL: `http://10.0.2.2:5000/`. Debug Settings can use
`http://YOUR-PC-LAN-IP:5000/` for a phone on the same Wi-Fi. Allow inbound
TCP 5000 in the PC firewall for that test. Debug and release have separate IDs.

If the app shows **DisallowedHost / HTTP 400**, edit the backend's
`djangoviscane_final/.env.local` (the root `.env.example` is for the Flask app):

```text
DJANGO_DEBUG=true
DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost,10.0.2.2
```

Append your PC LAN IP when testing over Wi-Fi. Host entries must omit the scheme
and port. Restart Django after changing this file, including when using
`--noreload`. Run only one development server on port 5000. Keep production's
host list limited to its deployed hostname.

### Install and test the APK locally

1. Start the Django server using the commands above and keep that terminal open.
2. Start an emulator through Android Studio's **Device Manager**, or connect an
   Android phone by USB, enable Developer options / USB debugging, and accept
   the computer's debugging authorization on the phone.
3. From the repository root, install and launch the debug APK:

   ```powershell
   $adb = Join-Path $env:LOCALAPPDATA 'Android\Sdk\platform-tools\adb.exe'
   & $adb devices -l
   & $adb install -r .\android\app\build\outputs\apk\debug\app-debug.apk
   & $adb shell am start -n com.viscane.app.debug/com.viscane.app.MainActivity
   ```

   If multiple devices are connected, add `-s DEVICE_SERIAL` after `$adb` to
   each install, shell and reverse command.
4. An emulator uses the default `http://10.0.2.2:5000/`. For a USB phone, run
   `& $adb reverse tcp:5000 tcp:5000`, then set the app's **Settings > Server URL**
   to `http://127.0.0.1:5000/` and return to the app. Repeat reverse after reconnecting.
   For Wi-Fi testing, use the PC LAN IP instead; `localhost` on a phone refers
   to the phone itself. Rebuild/reinstall is unnecessary when changing this debug setting.
5. Complete the physical-device acceptance checklist below. Confirm an actual
   prediction reaches the configured predictor; a page loading alone is insufficient.
   HTTP debug testing covers native photo capture, but test live camera preview
   again with the production HTTPS origin.

Lint report: `android/app/build/reports/lint-results-debug.html`.
Unit test report: `android/app/build/reports/tests/testDebugUnitTest/index.html`.
If the app crashes, capture `& $adb logcat -d -s AndroidRuntime`.

## Production farmer server

Deploy a dedicated Django instance behind an HTTPS reverse proxy with:

```text
DJANGO_DEBUG=false
VISCANE_SECRET_KEY=<unique generated secret>
DJANGO_ALLOWED_HOSTS=<farmer hostname>
DJANGO_CSRF_TRUSTED_ORIGINS=https://<farmer hostname>
VISCANE_FARMER_ONLY=true
DATABASE_URL=<production database URL>
SCAN_PREDICT_ENDPOINT=<private or HTTPS predictor URL>
```

Keep administrator features on a separate protected Django instance with
`VISCANE_FARMER_ONLY=false` and a separate hostname. They can share the
production database. Keep session cookies host-scoped. The farmer instance
rejects administrator routes even without the APK user-agent marker. The marker
only shapes presentation and provides an additional client restriction.

If the trusted proxy strips and sets X-Forwarded-Proto itself, set
`DJANGO_TRUST_PROXY=true`. Otherwise leave it false. Production enables HTTPS
redirects and secure cookies; configure TLS/proxy routing before testing.
Use a production WSGI/ASGI server, persistent upload storage, backups and uptime
monitoring. Provision administrator accounts through the protected admin instance.
Enable `DJANGO_HSTS_INCLUDE_SUBDOMAINS=true` only when every subdomain uses HTTPS.
Enable `DJANGO_HSTS_PRELOAD=true` only when intentionally preparing the domain
for browser preload enrollment. Their default false values produce two advisory
deployment-check warnings, rather than imposing an unverified domain policy.

```powershell
..\.venv-django\Scripts\python.exe manage.py check --deploy
..\.venv-django\Scripts\python.exe manage.py collectstatic --noinput
```

Serve collected `/static/` assets through the proxy, but deny
`/static/uploads/cv_scans/` explicitly before the general static rule. Existing
scan files are served through the authenticated image endpoint. New scans are
stored outside public assets in `djangoviscane_final/.private/`; set
`VISCANE_PRIVATE_UPLOAD_ROOT` to persistent private storage in production and
include it in backups. Never expose that directory through the proxy. Keep
classifier credentials on Django and secure the predictor hop with TLS or a
private network.

## Release

The release build has no Server URL or external-browser menu. It disables HTTP,
backups and WebView debugging. Supply your real HTTPS origin:

```powershell
.\gradlew.bat assembleRelease -PfarmerBaseUrl=https://YOUR-FARMER-HOST/
```

This creates an unsigned release APK. Use Android Studio's Generate Signed App
Bundle or APK flow with your private release keystore. Preserve the keystore for
updates and never commit its passwords. Set farmerBaseUrl in Gradle properties
or on the command line. Google Play distribution requires a signed AAB and the
applicable Play Console requirements. Target API 36 is configured.

### Prepare the production artifacts

1. Complete local device testing, then deploy and verify the real HTTPS farmer
   server using the production section above. Confirm login, prediction, private
   images and admin-route blocking on that host.
2. Keep `applicationId` as `com.viscane.app`. Before each update, increase
   `versionCode` in `android/app/build.gradle` above the last published value and
   set the intended `versionName`.
3. For Android Studio builds, add `farmerBaseUrl=https://YOUR-REAL-FARMER-HOST/`
   to `android/gradle.properties`, replacing the placeholder. This is the public
   server origin, not a credential. Sync the project. A command-line `-P` value
   applies only to that command and is not saved for the signing wizard.
4. From `android/`, validate and build against the same real origin:

   ```powershell
   .\gradlew.bat lintRelease assembleRelease bundleRelease -PfarmerBaseUrl=https://YOUR-REAL-FARMER-HOST/
   ```

   These builds have no release signing configuration. Treat their outputs as
   unsigned artifacts until you sign them; they are not ready for distribution.
5. In Android Studio choose **Build > Generate Signed App Bundle or APK**.
   Select **APK** for direct phone installation, or **Android App Bundle** for
   Google Play. Select module `app`, use an existing release/upload keystore or
   create one, choose `release`, and finish. Keep the keystore and credentials
   backed up outside the repository. Reuse the appropriate key for updates.
   The wizard shows the destination of the signed artifact.
6. Install the signed release APK and repeat the acceptance checklist against
   production. Its package is `com.viscane.app`; debug uses `com.viscane.app.debug`.
   Verify there is no Server URL menu, HTTP is blocked and login persists across
   app restarts. For Play, upload the signed AAB to internal testing and test the
   Play-installed build before promoting a release.
7. Complete the Play Console's applicable listing, privacy policy, Data safety,
   content rating and testing requirements, then publish only after acceptance.
   Review the current Console requirements for your account before submission.

Signing reference:
[Android Developers](https://developer.android.com/studio/publish/app-signing).
The production hostname, private signing key and device acceptance results are
required to finish the production release; none can be inferred from a debug build.

## Camera and mobile layout

### Dashboard camera dialog update

The dashboard's Open Camera dialog uses a centered, height-limited panel,
rectangular Upload Picture / Take Photo controls and explicit loading/unavailable
states. Live preview exposes Capture, then Retake after a photo. Escape and
Android Back close the dialog; camera tracks are released after cancellation.

Desktop and Android share `templates/homepage.html` and
`static/css/camera-dialog.css`, served by Django. Restart Django and reload the
dashboard to pick up the template. Use Android Studio's **Run app** to install
the native Back change, or install the rebuilt
`android/app/build/outputs/apk/debug/app-debug.apk`.

Verification: debug build, 3 navigation tests and lint passed (0 errors,
3 warnings). The rebuilt APK was installed on the emulator. A layout-only
WebView preview confirmed that the panel and controls fit within the screen,
with horizontal centering within 1 CSS pixel. Inline dashboard JavaScript syntax
was checked. Screenshot:
`djangoviscane_final/.local/android-readiness/emulator-camera-layout.png`.
The final camera browser verifier now passes all 8 checks: layout at 1440x900,
320x740, 390x844, 768x1024 and 844x390; focus/Escape behavior; cancellation while
camera permission is pending; fake live capture with mocked prediction and Retake;
and restoring upload controls after a prediction error. No JavaScript page errors
were observed. Screenshots and results:
`djangoviscane_final/.local/camera-verification/`.
The hidden preview image now uses `display: none` after Retake, and the CSS
URL version is bumped to avoid stale WebView styling.

Run the camera-specific verifier from the repository root when available:

```powershell
.\.venv-django\Scripts\python.exe djangoviscane_final/scripts/verify_dashboard_camera.py
```

It uses a disposable database and mocked predictions. Test permission denial,
native capture, cancellation and HTTPS live preview on a physical phone too.

The `/scan/new` scanner opens in an upload-first state with **Choose photo** as
the primary action and **Use camera** as the optional action. It explains photo
quality and supported formats instead of opening a live camera automatically.
Buttons have 48px touch targets. In Android, **Use camera** opens native capture;
in browsers it opens a live preview on request. Once the preview is ready, that
button becomes **Take photo**, so there are never two camera actions. **Close
camera** stops the stream and returns to upload. Permission denial keeps gallery
upload available. **Change photo** returns to the upload screen after analysis.
The scanner stylesheet uses `?v=6` to refresh the WebView's cached CSS.

On 3 October 2026, Django checks and 65 browser checks passed, covering six
viewport sizes, gallery upload with a mocked predictor, native-camera file
input selection, explicit browser preview, camera closure and permission denial.
Current screenshots are `scan-upload-desktop.png`, `scan-new.png` and
`scan-live-preview.png` under `djangoviscane_final/.local/android-readiness/`.

These changes live in `djangoviscane_final/templates/scan_new.html` and
`djangoviscane_final/static/css/scan-page.css`; Android Studio's app loads them
from Django, so no APK rebuild is needed for this layout update. Restart Django
when using `--noreload`, then reload the scanner or reopen the app. For deployed
servers, collect and serve the updated static files as usual.

The earlier v5 fallback layout was visually verified in a Pixel 8 API 37 emulator.
That screenshot (`.local/android-readiness/scan-layout-emulator.png`) predates
the upload-first revision. Native capture and a real prediction still require
device acceptance testing; the emulator is currently disconnected.

Choose photo selects the gallery; Use camera launches Android capture using a
FileProvider URI. Live preview requires a trusted HTTPS origin. Native capture
remains available for HTTP debug testing. Permission denial permits gallery use.
The WebView checks the permission origin and handles system bars, cutouts and
keyboard insets. Farmer-only navigation applies to phones, tablets and landscape.

## Verification and release acceptance

```powershell
cd djangoviscane_final
..\.venv-django\Scripts\python.exe manage.py test core --noinput
..\.venv-django\Scripts\python.exe scripts/verify_farmer_mobile.py
```

The browser verifier uses a disposable database and checks login/profile CSRF,
admin blocking, page errors and overflow at 320, 360, 390, 412, 768 and landscape
sizes. Results/screenshots: `djangoviscane_final/.local/android-readiness/`.
It does not certify native camera, keyboard behavior or the production host.

Before distributing the signed APK, test on a physical Android phone:

- Registration/login, language switching, profile/password updates and logout.
- Take Photo, gallery, permission denial, cancellation, rotation and large images.
- Prediction, calculation, saved results, recommendations, history and feedback.
- Offline/slow network, retry, Back, resume, enlarged text and keyboard visibility.
- Administrator routes and redirects remain blocked at all widths.
- The production farmer hostname rejects admin paths from ordinary browsers too.

Release URL, TLS/proxy setup, signing, device tests, legacy-static blocking and
backup operations must be completed in the target environment before release.
