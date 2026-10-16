# <img width="30" height="30" alt="Halthy icon" src="brand/icon.png" /> Halthy Home Assistant Integration

Halthy is a Home Assistant custom integration that receives health metrics from the Halthy app and creates per-user entities with stable unique identifiers.

> [!NOTE]
> **Halthy for iPhone is available in beta.**
>
> [Join the TestFlight](https://testflight.apple.com/join/8KWJZZj2) to test the app on an iPhone running iOS 18.5 or later. Beta builds may contain bugs and expire after 90 days. Please send feedback through TestFlight or [GitHub Issues](https://github.com/Mosher23/Halthy/issues).

Project documentation, privacy information, and security reporting instructions are available in the repository root: [`../../README.md`](../../README.md), [`../../PRIVACY.md`](../../PRIVACY.md), and [`../../SECURITY.md`](../../SECURITY.md). For support, use the [GitHub issue tracker](https://github.com/Mosher23/Halthy/issues).

## What This Integration Provides

- Receives app uploads at `POST /api/halthy/push`
- Creates and updates `sensor.*` and `image.*` entities in Home Assistant
- Supports multi-user setup via one config entry per username
- Provides diagnostic and control entities for sync monitoring
- Supports optional logbook activity logging
- Imports numeric samples with timestamps into Home Assistant recorder statistics
- Includes a bundled workout card with route map navigation, calendar archive, and multi-workout day selection
- Creates a read-only Home Assistant workout calendar for each configured person

## Installation

### Stable release and app compatibility

Version 0.2.1 is a stable integration release. The iPhone app is distributed
separately through TestFlight. Existing token-authenticated API uploads remain
supported; native sign-in and paired webhooks require a compatible updated app.

The app discovers profiles owned by the signed-in Home Assistant account. Optional
webhook pairing is scoped to that account, profile, device and issuing grant.
Webhook URLs are secrets: do not publish or share them. Pairing lookup and removal
require authenticated requests; lookup never returns the secret webhook URL.
Commands, imports and other control operations continue to use authentication.

Compatible apps also use scoped workout-map inspection/transaction endpoints and
archive-only repair. An archive-only write does not replace the displayed map or
advance sync diagnostics. Durable receipts protect retries; they are not backups
and cannot undo remote writes. Back up Home Assistant before upgrading or rolling
back. Do not manually activate map-management endpoints as a setup step.

Validation includes real HA 2025.6.3 authentication, loopback HTTP and synthetic
users/grants in `tests/test_http_authentication.py`. This is not a full supported-
version matrix or a substitute for checking your own deployment after upgrading.

### HACS (recommended)

1. Open **HACS**.
2. Add repository `https://github.com/Mosher23/Halthy` as a custom integration repository if needed.
3. Install **Halthy**.
4. Restart Home Assistant.

### Fix an installation from `v0.1.0-beta`

The `v0.1.0-beta` release archive could install Halthy under an incorrect nested directory. If your installation contains `custom_components/halthy/custom_components/halthy`, remove Halthy in HACS, delete the remaining `custom_components/halthy` directory, install `v0.1.1-beta` or newer, and restart Home Assistant. Removing the HACS files does not remove the person configurations stored by Home Assistant.

### Manual

1. Copy `custom_components/halthy` into `<config>/custom_components/`.
2. Restart Home Assistant.

## Setup in Home Assistant UI

1. Go to **Settings -> Devices & Services -> Add Integration**.
2. Select **Halthy**.
3. Enter:
   - **Username**: must match the username configured in the app
   - **Display Name** (optional): shown as device name in Home Assistant
4. Repeat for each person.

### Sleep times

For UTC-looking timestamps, absolute date formatting, missing statistics, or HACS icon placeholders, see [Troubleshooting](https://github.com/Mosher23/Halthy/blob/main/docs/troubleshooting.md).

Select a sleep metric for upload in the updated iOS app to create three timestamp sensors per user:

- **Go to bed time** (`sensor.<username>_go_to_bed_time`): start of the matching recorded in-bed interval. Missing in-bed data is shown as **unknown**, never estimated.
- **Fall asleep time** (`sensor.<username>_fall_asleep_time`): first recorded asleep stage.
- **Wake up time** (`sensor.<username>_wake_up_time`): end of the final asleep stage, not the time you left bed.

All three use the same latest main sleep session from the past 48 hours. Stages and gaps up to 90 minutes are grouped, and overlapping samples are merged. Sessions lasting at least two hours take priority over short naps; if none exists, the app falls back to sessions lasting at least 20 minutes. These are recorded boundaries, not live sleep detection.

Full timestamps are displayed using Home Assistant's timestamp handling. Values refresh on the next app upload even when sleep duration is unchanged. A new session without in-bed data clears the previous bedtime to unknown; an unreadable or missing session leaves existing values unchanged. Both the app and integration must be updated, and the app needs access to sleep data.

### Height and Weight sensor settings

Height is reported in centimetres by default. Body mass is displayed as **Weight**. Both sensors suggest one decimal place and support Home Assistant's unit conversion and display precision settings: open the sensor, select the settings cog, and adjust **Unit of measurement** and **Display precision**. Your choices are kept across uploads. Existing entity IDs stay the same, including `sensor.<username>_body_mass`.

### Digital health sensors

On an eligible iPhone with Digital health data-access permission, select the metrics in the Halthy app's Digital health settings. This is separate from HealthKit permission. The feature requires iOS 26.4 or later and an eligible EU device and Apple Account.

| Sensor | Value |
|---|---|
| `sensor.<username>_screen_time` | Current day's screen time in minutes |
| `sensor.<username>_longest_activity_session` | Current day's longest activity session in minutes |
| `sensor.<username>_first_pickup_time` | Time of the first pickup, shown in Home Assistant's local time |
| `sensor.<username>_pickups_without_app_use` | Current day's count of pickups without app use |

The app sends the current-day snapshot to Home Assistant and can export the past seven days to InfluxDB. These are device-level aggregates: no app identities or website history are sent. Update both the app and integration, then run a sync. Deselecting a Digital health metric removes its sensor on the next selection-pruning upload.

### Midnight rollover

Daily activity, energy, dietary totals, event counts, workout totals, screen time,
longest activity session and pickups without app use reset to **0 at 00:00**.
First pickup time becomes **unknown** until today's first pickup is reported.
Sleep-session results, medical measurements and last-workout timestamps are not
daily counters and retain their last result.

The integration performs rollover without an iPhone upload and catches up after
a restart or reload. It uses each snapshot's `reporting_time_zone`; older app
payloads fall back to Home Assistant's timezone. Calendar days, including daylight
saving changes, are used rather than fixed 24-hour intervals. Daily uploads uses
Home Assistant's timezone.

`daily_reset: true` and `daily_source_fresh: false` distinguish a calendar reset
from a fresh phone measurement. `last_pushed` retains the actual phone receipt
time. Resetting a daily value does not increment Daily uploads or change Last full
sync. Previous-day retries and historical daily snapshots can update historical
statistics but cannot replace today's live total. Existing entity IDs are kept.

Update the app too: category sensors now count today's events (not the latest
single event), stood hours exclude idle records, and symptom counts exclude
records marked not present. Test/observation category counts count logged records,
not positive clinical results. Complete empty daily counter reads report zero;
query failures and unavailable sleep/medical readings do not become zero.

Existing graphs retain their previously recorded values after an update. The
midnight fix applies to new state history after the integration is restarted;
it does not move an old morning reset back to midnight or rewrite old statistics.

### Device software version

Device info shows the installed integration's software version as **Integration
<version>**, not the iPhone's iOS version or the app's TestFlight build. The version
comes from Home Assistant's loaded integration manifest and refreshes on restart
or integration reload. Device and entity identifiers are preserved.

## Built-in Workout Card

Halthy includes a bundled Lovelace card: `custom:halthy-workout-card`.

- The module is served by the integration and auto-registered on startup.
- You can usually add the card directly to your dashboard without manual resource setup.
- Manual fallback resource URL: `/halthy/halthy-workout-card.js`
- The main card can browse archived workout maps with transparent left/right overlay buttons.
- The right overlay is hidden when the newest workout is selected.
- The calendar popup is larger, has a separated calendar section, highlights workout days, and shows a selector for multiple workouts on the same day.

Minimal config:

```yaml
type: custom:halthy-workout-card
user: your_username
show_heart_rate_zones: true
```

The visual card editor detects configured Halthy users and proposes them for selection. Heart-rate zones are shown by default; turn off **Show heart rate zones** in the visual editor or set `show_heart_rate_zones: false` in YAML to hide them.

## Workout Calendar

Each Halthy config entry creates `calendar.<username>_workouts`. Add this entity to Home Assistant's Calendar dashboard to browse workouts at their actual start and end times.

- Workouts with and without route maps are supported.
- Multiple workouts on one day are separate events.
- HealthKit UUIDs prevent duplicates and allow later uploads to update an event.
- Event descriptions include available summary values such as duration, distance, active energy, average heart rate, cadence, and speed.
- Calendar metadata and route images use the configured per-person retention limit to keep Home Assistant storage bounded.

Unlock the iPhone and perform a foreground upload or **Force upload** once after upgrading the integration and app. The app uploads the available HealthKit workout history in bounded batches. Existing route archive metadata is migrated automatically during integration setup.

The calendar is read-only; workout changes must be made in Apple Health or the app that originally recorded the workout.

## Workout Image Archive

When the app uploads a workout route image, Halthy archives it in Home Assistant media storage.

- Folder: `/config/media/halthy/workouts/<app_username>/`
- Filename format: `<workout_timestamp>_<workout_fingerprint>.<ext>`
- Timestamp source: `measurement_timestamp` with workout timestamp fallbacks

Same-workout replacement:

- Uploading a newer image for the same workout replaces older archived files for that workout.
- Workout matching uses a stable `workout_uuid` when available, with metadata fingerprint fallback.
- Newly archived images store small metadata sidecars so the card can show readable workout types, summary chips, and zone data.
- Existing archived images without metadata still display, but may fall back to a generic `Workout` title.
- The card reads the archive through authenticated endpoints:
  - `GET /api/halthy/workouts`
  - `GET /api/halthy/workout_image`

Workout image entity attributes include:

- `archive_local_url`
- `archive_media_source_id`
- `archive_file_name`
- `archive_workout_timestamp`
- `archive_replaced_file_count`
- `heart_rate_zones`
- `cycling_power_zones`

## Integration Options

Open **Settings -> Devices & Services -> Halthy -> Configure**.

### Username and Display Name

You can change the configured **Username** and **Display Name** for an existing entry.

- The username is the routing key used by uploads, commands, services, and workout archive lookup.
- After changing it in Home Assistant, update the username in the iOS app to match.
- Halthy moves archived workout files to the new username folder during reload and updates stored archive references.
- Sensor and image entity IDs are migrated when the target ID is available. Configuration controls retain stable internal IDs.
- Recorder statistic IDs contain the username, so a rename starts new `halthy:*` series without deleting the old history.

### Historical Statistics

Enable or disable **Import historical statistics** independently for each configured person.

- Enabled by default.
- When disabled, current `sensor.*` entities continue updating, but no new `halthy:*` historical statistic rows are imported.
- Existing statistics are not deleted when the option is disabled.

### Temperature Unit

- Home Assistant unit system
- Always Celsius
- Always Fahrenheit

### Activity Log Mode

Native API and paired-webhook uploads report `sync_completed: true` after a
successful current-metric collection. This refreshes **Last full sync** and
**Daily uploads** without pruning other sensors. Historical pages and retries
do not count as additional full-sync sessions. Historical backfill can still be
pending after a current-metric sync completes. The app checks the integration's
`sync_completion_supported` capability before sending this metadata.

| Mode | Behavior |
|---|---|
| `Off` | No logbook entries |
| `Session summary` | One summary log per sync session |
| `Per-entity verbose` | Log entry for each updated/removed entity |

### Stored Workouts and Images

Sets the maximum number of workout calendar records and route images retained for this person. The default is 250 and the supported range is 25 to 2,000. Oldest data is removed first.

## Entities Created

### Data entities

- Sensors: `sensor.<app_username>_<metric_key>`
- Workout image: `image.<app_username>_workout`

### Diagnostic entities

- `sensor.<app_username>_last_update`
- `sensor.<app_username>_last_full_sync`
- `sensor.<app_username>_daily_upload_count`

### Control/config entities

- `select.<app_username>_force_upload_interval`
- `button.<app_username>_force_upload`
- `button.<app_username>_force_influx_backfill`

## Services

- `halthy.force_upload`
- `halthy.force_influx_backfill`

Both services accept optional `app_username` to target one configured user.

## API Endpoints (used by the app)

- `POST /api/halthy/push`
- `GET /api/halthy/command`
- `POST /api/halthy/command_ack`
- `GET /api/halthy/workouts`
- `GET /api/halthy/workout_image`

## Recorder Statistics Behavior

> [!IMPORTANT]
> Home Assistant states represent current values. Full sample history is not stored as state snapshots by default.

When a numeric metric arrives with `measurement_timestamp`, the integration imports it into recorder statistics. Samples received in separate upload batches are deduplicated by metric, timestamp, and value and combined into hourly buckets. The sample store is `/config/.storage/halthy_statistics.sqlite`; include it in Home Assistant backups. It grows as more samples are uploaded.

In the app's Home Assistant settings, choose **Home Assistant history** and tap **Backfill Home Assistant history**. The app resumes incremental readings from saved HealthKit anchors and fills selected sleep and cumulative metrics with historical daily snapshots over later syncs. Long ranges take multiple syncs.

These are external Home Assistant long-term statistics, so `halthy:*` statistic series are hourly. Home Assistant's 5-minute short-term statistics apply to normal recorder-derived sensor statistics, not these historical imports. Use InfluxDB from the app for raw high-resolution history.

## Troubleshooting

- **Config flow does not open**: restart Home Assistant and check integration logs for config flow errors.
- **`401` / `403` on upload**: verify long-lived token and user ownership for configured username.
- **Entities not updating**: verify the app username exactly matches **Username** in the integration entry.
- **History gaps in dashboards**: confirm payload includes `measurement_timestamp`; use InfluxDB when you need raw high-resolution history instead of hourly HA external statistics.

## License

Halthy is available under the [MIT License](../../LICENSE).
