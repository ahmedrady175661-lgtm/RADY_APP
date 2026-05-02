# Flutter UI specification (handoff from web SPA)

This document describes the **user-facing structure, navigation, screens, and data fields** of the current web application (`static/index.html` + `static/_om_field.js`) so a Flutter client can **replicate the same flows and layout intent**. Backend routes, request bodies, and WebSocket payloads are defined in the FastAPI project; mirror those contracts in Dart, not the browser implementation details.

**Language & direction:** The product UI copy is **Arabic**, with **RTL** layout. Keep semantic order (navigation, primary actions) consistent with the web app unless you intentionally adapt for platform guidelines.

---

## 1. Global shell

### 1.1 Authentication gate

- Before login, a **full-screen lock** blocks the main app (`authLock`).
- Fields:
  - **Username** — `authUsername`
  - **Password** — `authPassword` (with optional show/hide)
  - **Device ID** — `authDeviceId` (web auto-generates/persists a device id if empty; Flutter should send a stable device identifier with login/refresh as the server expects)
- Actions: **Login**, **Clear session** (local session cleared; server logout may apply on full logout).
- After successful login, the main **`.wrap`** content is shown; `auth_session_v1` and tokens are handled like the web (see server `/auth/*`).

### 1.2 Header (always visible when authenticated)

- **App title:** “التسجيل” (branding; subtitle mentions License Plate Extractor · GPS · version).
- **Version badge** (e.g. v5.5).
- **Icon buttons:**
  - **Settings** — opens settings modal (`settingsModal`): short note + **Full logout** (clears server refresh token + local session).
  - **Theme** — toggles **dark / light** (`body.light`); persist `theme` in local storage equivalent.
  - **Instructions** — opens help modal (`instrModal`, `instrContent`).

### 1.3 Top navigation (tabs)

Order left-to-right in RTL (first item is visually rightmost):

| Tab key   | Web `id`     | Default visibility | Purpose (short)                                      |
|-----------|--------------|--------------------|-------------------------------------------------------|
| `tafrigh` | `navTafrigh` | Hidden initially   | Voice recording + GPS + queue + editable Excel grid |
| `field`   | `navField`   | **Active by default** | Two Excel files + Match + optional GPS ranking   |
| `check`   | `navCheck`   | Hidden initially   | One reference Excel + **Gemini Live** + session sheet |
| `gmaps`   | `navGmaps`   | Hidden initially   | Upload Excel → map pins (Google Maps JS on web)     |
| `admin`   | `navAdmin`   | **Hidden** unless `auth_is_admin === '1'` | User/group admin + API key pools + Gemini model list |

**Page containers:** `pageTafrigh`, `pageField`, `pageCheck`, `pageGmaps`, `pageAdmin` — only one main flow visible at a time; **Admin** is shown/hidden separately from the four consumer tabs.

**Leaving Check tab:** Web calls teardown for Live mic/WebSocket when switching away from `check` (Flutter should stop audio stream and close Live connection similarly).

---

## 2. Tab: التسجيل (Recording / “Tafrigh”) — `pageTafrigh`

### 2.1 Settings panel

| Element / id        | Type   | Role |
|---------------------|--------|------|
| `recorderName`      | text   | Default recorder name for new rows / export |
| `districtName`      | text   | Optional district applied across export |
| `sheetName`         | text   | Excel sheet name; syncs title bar `tafrighSheetTitlebar` |

### 2.2 Recording panel (`livePanel`)

- **Max duration:** 5 minutes (`MAX_REC_SECS = 300`).
- **Main record button** — `recBtn` / `recIcon`; toggles record/stop.
- **GPS pin button** — `gpsPinBtn` (enabled while recording): adds one GPS sample in **manual** mode.
- **After stop:** choice strip `recChoices`: Continue recording, New recording, Add to queue, Direct send.
- **Timer** — `timer`; **progress bar** — `recProgress` / `recBar`.
- **Audio preview** — `liveAudioWrap`: play/pause `livePP`, scrub `livePW`, time `liveTT`, speed `liveSpd` / `liveSpdV`, element `liveAudio`.
- **GPS mode chips** — `mA` = **auto** (default), `mM` = **manual**; `autoOpts` shows interval `gpsInt` (seconds) for auto mode.
- **Live GPS readout** — `gLat`, `gLng`, `gAcc`, `gCnt`; status `gDot` + `gTxt`; log `gLog`.

**Important (web):** Audio is **captured in-app only** (no file picker for audio). Flutter: use platform audio capture + same max duration and queue semantics.

### 2.3 Queue

- List host: `queueList`; count badge: `qBadge`.
- **Send all pending** — `sendAllBtn` (disabled when nothing pending).
- Each queue item: thumbnail, name, duration, GPS summary, status badge (`pending` / `processing` / `done` / `error`), actions: **download audio**, **send** (single), **remove** (two-tap confirm on web).

### 2.4 Status bar

- `statusBar` / `spin` / `statusTxt` — global messages for recording pipeline.

### 2.5 Data table (editable, Excel-like shell)

- Toolbar: **Export Excel** (`exportExcel`), **Attach append file** (`xlsAppendIn`), **Clear all** (`clearTable`), row count `rowCount`.
- Append file name row: `xlsAppendFname`, remove `xlsAppendRemoveBtn`.
- Sheet chrome: `tafrighXlsSheet`, title `tafrighSheetTitlebar`, scroll `tafrigh-xls-scroll`, table `tafrighDataTable`, body `excelBody`.

**Row model (keys used in web `tableRows`):**

| Column (Arabic header in UI) | Field key(s) |
|------------------------------|--------------|
| رقم اللوحة                   | `full_plate` or legacy `plate` |
| GPS                          | `gps` (read-only on grid; filled from capture / server) |
| تاريخ التسجيل                | `recording_date` |
| الحي                         | `district_name` |
| الشارع                       | `street_name` |
| ملاحظات                      | `notes` or legacy `location_details` |
| نوع السيارة                  | `vehicle_type` |
| اسم المسجّل                  | `recorder_name` |
| موقع الشارع                  | `street_location` if set, else derived from mid GPS among row set (web helper `streetLocForRow`) |

- Rows persist under local key **`plateTable_v5`** (JSON array).

---

## 3. Tab: الفرز (Field match) — `pageField` + `_om_field.js`

Logic lives mainly in **`static/_om_field.js`**. Element ids are prefixed with **`om`** so Flutter can map 1:1.

### 3.1 Optional Postgres / group panel (hidden by default)

- `omPgStoragePanel` — shown when server reports Postgres-backed large storage for check/field flows.
- `omGroupBanner`, `omStoredImportsList`, `omPgImportHint`, hidden checkbox `omUseStoredLargeCb` (internal).

### 3.2 Two-column file grid

**Large file (“data source”) — left card**

- Drop zone: `omDropLarge`; file input: `omLargeFileIn`.
- File name: `omLargeFname`; remove: `omRemoveLargeBtn`.
- Password: `omLargePw` + toggle + **Confirm** `omConfirmLargePw` (triggers header detection).
- Plate column on large file is fixed in hidden input `omLargeCol` (default Arabic header “رقم اللوحة”).
- Optional **export column checkboxes** for match output: `omCheckLargeExportDrop` / `omLargeExportList`.

**Small file (“search list”) — right card**

- Drop zone: `omDropSmall`; file input: `omSmallFileIn`.
- File name: `omSmallFname`; remove: `omRemoveSmallBtn`.
- Plate column dropdown: `omSmallCol` + badge `omSmallColBadge`.
- Small file export columns: `omCheckSmallExportDrop` / `omSmallExportList`.
- **Alternative to small file:** textarea `omSmallPlatesText` — one plate per line; if non-empty, it **replaces** the small file.

### 3.3 Status

- `omFieldStatus` / `omFieldSpin` / `omFieldStatusTxt`.

### 3.4 Primary action

- **Match** — `omMatchBtn` (disabled until large + (small file or text) ready per JS rules).

### 3.5 Match result block — `omResultBox`

- Stats: `omRMatched`, `omRPlates`, `omRUnmatched`.
- Column summary: `omRLargeCol`, `omRSmallCol`.
- Truncation note: `omMatchTruncNote`.
- Preview: Excel-style host `omMatchPreviewHost` **or** classic table `omMatchTableWrap` with `omMatchThead` / `omMatchTbody`.
- **Open result** — `omDlBtn` (download/open blob).
- **Clear saved match** on device — clears IndexedDB/local restore for field match.

### 3.6 GPS section (after match, if large file has GPS column)

- Wrapper: `omGpsMatchSection` (hidden until relevant).
- User origin: `omGpsMyLat`, `omGpsMyLon`, refresh `omGpsLocBtn`, status `omGpsLocDot` / `omGpsLocTxt`.
- Progress: `omGpsProgress`, `omGpsProgFill`, `omGpsProgLbl`.

### 3.7 GPS result block — `omGpsResultBox`

- Stats: `omGpsRSucc`, `omGpsRFail`, `omGpsRNearest`.
- Table wrap `omGpsResultTableWrap`, body `omGpsResultTableBody`.
- **Download** sorted-by-distance Excel — button calls `omDownloadGpsResult()`.

### 3.8 Manual column hint

- `omFieldHeadersHint` / `omFieldHeadersContent` — shown when auto plate column detection fails.

---

## 4. Tab: التشيك (Live check) — `pageCheck`

### 4.1 Reference Excel

- Upload: `dropLarge` / `largeFileIn`; name `largeFname`; remove `removeLargeBtn`.
- Password is stored in hidden `largePw` on web (simplified vs Field tab).
- **Plate column** select: `largeCol` + `largeColBadge`.
- Hint panel: `checkHeadersHint` / `checkHeadersContent`.

**Server:** Large workbook is uploaded to temp storage + Live session; column is sent over WebSocket when set.

### 4.2 Gemini Live block

- Connection dot + label: `checkLiveDot`, `checkLiveWsTxt`.
- Live transcript area: `checkLiveTranscript`.
- **Toggle listen** — `checkLiveToggleBtn` (starts/stops mic; web streams PCM base64 over WS).
- **Plates returned by model — “found in sheet” column only:** scroll host `checkLivePlateColHit` (green “hit” rows). There is **no** separate “miss” list column in the Live UI; misses are reflected in the **session table** / export behavior below.
- **Clear list** — `checkLiveClearPlates`.

### 4.3 Session sheet panel (`checkSessionSheetPanel`)

Mirrors recording export semantics for a **per-session** grid:

- Sheet/export title: `checkSessionSheetName` → title bar `checkSessionSheetTitlebar`.
- Defaults for new rows: `checkSessionDefaultDistrict`, `checkSessionDefaultStreet`, `checkSessionDefaultRecorder`.
- Table: `checkSessionXlsSheet`, `checkSessionDataTable`, body `checkSessionExcelBody`, row count `checkSessionRowCount`.

**Column headers (Arabic):** رقم اللوحة، GPS، تاريخ التسجيل، الحي، الشارع، اسم المسجّل + delete column.

- **Append file for export** — `checkSessionAppendIn`, `checkSessionAppendFname`, `checkSessionAppendRemoveBtn`.
- **Export session Excel** — `exportCheckSessionExcel`.
- **Clear session rows** — `clearCheckSessionRows`.

**Local persistence key:** `checkSessionSheet_v1` (session grid data).

### 4.4 Product rules (Live plate outcomes) — replicate in Flutter

These are **UX + data** rules the web implements; align Flutter with the same contract the backend sends (`found`, `moving`, etc.):

| Condition | UI | Session / Excel row | Alert sound/vibration (hit-style) |
|-----------|----|---------------------|-----------------------------------|
| Plate **in** reference sheet (`found == true`), not moving | Show in hit list | **Write** session row | Yes (hit notify) |
| Plate **not** in sheet (`found == false`), not moving | No hit-list row | **Write** session row | Session “new row” feedback only (no double hit beep) |
| Plate marked **moving** (`moving == true`) | Show in hit list if `found == true`; otherwise still surfaced as needed | **Never** write session row; remove existing row for that normalized plate if any | **No** hit beep/vibrate for moving |
| `found` unknown / null | Do not sync session | — | — |

### 4.5 Check status

- `checkStatus` / `checkSpin` / `checkStatusTxt`.

---

## 5. Tab: Google Maps — `pageGmaps`

- Upload **.xlsx only** — `gmDropZone` / `gmXlsxIn`; name `gmFileName`; remove `gmRemoveBtn`.
- GPS column select: `gmGpsCol` — options **`GPS`** or **`موقع الشارع`** (disabled in UI if column missing in file).
- Label columns (multi-select checkboxes): `gmLabelColsDrop` / `gmLabelColsList` (auto-ticked: plate, vehicle type, recording date, notes when present).
- **Load pins** — `gmLoadBtn`; **My location** — `gmMyLocBtn`; stats `gmStats`.
- Status: `gmStatus` / `gmSpin` / `gmStatusTxt`.
- Map: `gmMap` (web uses Google Maps JavaScript API; Flutter would use Maps SDK + parsed points from same backend parse endpoint).

---

## 6. Tab: Admin — `pageAdmin`

Only for **admin** users (`navAdmin` visible).

Sections (top to bottom):

1. **User Gemini picks** — `userGeminiModelBar`: `userRestModel`, `userLiveModel` (lists from `/api/config/gemini-models`).
2. **Create user** — `admNewUser`, `admNewPass`, `admUserRowsLimit`, `admIsAdmin`, `admNewGroupId`, buttons create + refresh; message `adminMsg`.
3. **Groups** — `admNewGroupName`, `admNewGroupRowsLimit`, create group; table `adminGroupsBody`.
4. **Users table** — `adminUsersBody` (assign group, row limits, activate, reset device, delete).
5. **API key pools** (Gemini / ORS / Gmaps) — add inputs `admPoolInGemini`, `admPoolInOrs`, `admPoolInGmaps`; tables `admPoolGeminiBody`, `admPoolOrsBody`, `admPoolGmapsBody`; hint `admPoolRedisHint`, status `admKeyStatus`.
6. **Gemini model registry** — `admGmChannel`, `admGmModelId`, `admGmLabel`, `admGmSort`, add button, table `adminGmModelsBody`.

**Separate admin HTML:** `GET /admin/check-storage` serves **`static/admin-check-storage.html`** (Postgres check-storage inspection) — not embedded in `index.html`.

---

## 7. Settings modal (`settingsModal`)

- Explains that logout returns to login screen.
- **Full logout** — revokes refresh token on server + clears local auth flags.

---

## 8. Flutter implementation notes

1. **RTL:** Set `Directionality.rtl` and use Arabic strings from the web as the source of truth for labels unless you localize further.
2. **Ids:** Treat **`id` values above as stable API** between your Flutter widgets and any shared documentation for QA.
3. **Field tab script:** Port flows by reading **`_om_field.js`** next to this file (function names prefixed `om*`).
4. **Networking:** Use the same authenticated cookie/session model or adapt to your auth scheme; all `/api/*`, `/auth/*`, `/admin/*`, and **`/ws/check-live`** details live in the Python routers.
5. **Files not required for consumer UI clone:** `admin-check-storage.html` is operator tooling only.

---

## 9. Document version

- Generated to match the **v5.5** web shell structure (`static/index.html` + `static/_om_field.js`). If HTML ids change, update this file in the same commit.
