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

When a numeric metric arrives with `measurement_timestamp`, the integration imports it into recorder statistics (hourly buckets, cursor-based deduplication).

These are external Home Assistant long-term statistics, so `halthy:*` statistic series are hourly. Home Assistant's 5-minute short-term statistics apply to normal recorder-derived sensor statistics, not these historical imports. Use InfluxDB from the app for raw high-resolution history.

## Troubleshooting

- **Config flow does not open**: restart Home Assistant and check integration logs for config flow errors.
- **`401` / `403` on upload**: verify long-lived token and user ownership for configured username.
- **Entities not updating**: verify the app username exactly matches **Username** in the integration entry.
- **History gaps in dashboards**: confirm payload includes `measurement_timestamp`; use InfluxDB when you need raw high-resolution history instead of hourly HA external statistics.

## License

Halthy is available under the [MIT License](../../LICENSE).
