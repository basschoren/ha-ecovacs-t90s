# Ecovacs DEEBOT T90S for Home Assistant

Unofficial Home Assistant custom integration for Ecovacs DEEBOT robots, made
for the **DEEBOT T90S PRO OMNI** (Care Kit). It combines in one config entry
and one device per robot:

- everything the built-in Home Assistant **Ecovacs** integration offers:
  vacuum, map image, sensors, buttons, selects, numbers, switches, the last
  job event and the `raw_get_positions` action;
- the **camera live view** of
  [ECOVACS Live View](https://github.com/imcdo17-cyber/home-assistant-ecovacs-live):
  camera, start/stop buttons, a live view switch and a status sensor;
- built-in support for the **DEEBOT T90S PRO OMNI** (device class `833gj5`),
  which `deebot-client` does not know yet;
- **start, pause, stop and room cleaning that work on the T90 family**, which
  rejects the commands `deebot-client` sends (see below);
- **furniture and carpets on the map**;
- a **local fallback through Matter**: if the robot is also paired over
  Matter, start/stop/dock/rooms keep working while the Ecovacs cloud is down;
- more settings: mop wash water temperature, mop drying duration, cleaning
  speed, edge mopping, TrueDetect sensitivity, do not disturb, voice
  assistant, mop-only mode, automatic firmware updates, buttons to stop
  washing/drying the mops, saved scenarios, robot position, the map of the
  last clean job;
- actions `ecovacs_t90s.clean_zone` (rectangles and rooms in one job),
  `ecovacs_t90s.start_scenario` and the debug action
  `ecovacs_t90s.raw_command`.

You log in once; the robot control and the live view share that login.

> Status: phase 2 (version 0.2.0). Verified on a real T90S so far: setup,
> start (`clean_V2`), return to dock, state updates, the furniture answer and
> the answers of the settings listed below. Everything else is tested with
> mocked APIs only; see "What is verified".

## Requirements

- Home Assistant 2026.11 or newer.
- An Ecovacs account with an MQTT (JSON) robot. Old XMPP robots are not
  supported; use the built-in Ecovacs integration for those.
- For the live view: the Live View PIN that protects the robot camera in the
  Ecovacs app.
- For the local fallback (optional): the robot paired to Home Assistant with
  the **Matter** integration.

## Installation

### HACS (custom repository)

1. HACS > menu > **Custom repositories**.
2. Add `https://github.com/basschoren/ha-ecovacs-t90s` with category
   **Integration**.
3. Install **Ecovacs DEEBOT T90S** and restart Home Assistant.

### Manual

Copy `custom_components/ecovacs_t90s/` to `/config/custom_components/` and
restart Home Assistant.

## Configuration

**Settings > Devices & services > Add integration > Ecovacs DEEBOT T90S**

1. Choose **Cloud** (or **Self-hosted** if you run your own Ecovacs-compatible
   server; the live view needs the Ecovacs cloud).
2. Enter your Ecovacs username, password and country, and optionally the Live
   View PIN.
3. If Ecovacs asks to verify Home Assistant as a new device, enter the code it
   e-mails you.

**Configure** (options) lets you set or change the Live View PIN, the time
after which a live view stops automatically (default 10 minutes, 0 = never)
and whether furniture is drawn on the map (default on). Changing the options
does not reconnect the robot.

### Migrating from Ecovacs + ECOVACS Live View

This integration uses its own domain (`ecovacs_t90s`), so it can run next to
the built-in `ecovacs` integration and `ecovacs_live` while you compare them.
It gets its own devices and entity IDs; nothing is migrated automatically.
When you are done:

- remove the `ecovacs_live` config entry (two live view logins for one
  robot are not useful);
- remove the helper that patched `deebot-client` for class `833gj5`; this
  integration does that itself;
- remove the built-in `ecovacs` config entry if you no longer need it.

## Start, pause, stop and rooms

The T90 family answers the `clean` command of `deebot-client` with
`20003 rcp not support`, so the built-in integration cannot start these
robots. For mapped device classes (like `833gj5`) this integration sends
`clean_V2` first and falls back to `clean` when a robot answers `20003`; the
variant that worked is remembered. Payloads (`body.data`):

| Action | Command | Payload |
| --- | --- | --- |
| start | `clean_V2` | `{"act": "start", "content": {"total": 0, "donotClean": 0, "count": 0, "type": "auto"}}` |
| pause | `clean_V2` | `{"act": "pause"}` |
| resume (start while paused) | `clean_V2` | `{"act": "resume"}` |
| stop | `clean_V2` | `{"act": "stop"}` |
| rooms 2 then 1 | `clean_V2` | `{"act": "start", "content": {"type": "freeClean", "value": "1,2;1,1"}}` |
| rectangle + room 1 | `clean_V2` | `{"act": "start", "content": {"type": "freeClean", "value": "3,null,1410,2990,-360,1210;1,1"}}` |
| scenario | `clean_V2` | `{"act": "start", "content": {"type": "qcClean", "value": "<scenario id>"}}` |
| return to dock | `charge` | `{"act": "go"}` |
| locate | `playSound` | `{"count": 1, "sid": 30}` |

Fallbacks on `20003`: `clean` with `{"act": "start", "type": "auto"}` (or
`pause`/`resume`/`stop`), rooms as `{"act": "start", "type": "spotArea",
"content": "2,1", "count": <clean count>}`, one rectangle as `customArea`.

Commands now report failures: a rejected command raises an error with the
robot's code instead of silently doing nothing. After an accepted command the
vacuum shows the expected state at once and asks for the real state 10
seconds later, in case no push arrives (the T90S does not support
`getCleanInfo`; its state comes from `getChargeState`, `getWorkState` and
pushes).

Rooms without a name in the app show as "Room 3" ("Kamer 3" in Dutch) in the
`rooms` attribute and in the room list of **Clean area**.

### `ecovacs_t90s.clean_zone`

```yaml
action: ecovacs_t90s.clean_zone
target:
  entity_id: vacuum.my_robot
data:
  zones: [[1410, 2990, -360, 1210]]   # one or more [x1, y1, x2, y2] in mm
  rooms: [1]                          # optional, cleaned after the zones
```

Coordinates are map millimetres, the same frame as the robot position (enable
the **Position X/Y** sensors to read them).

### `ecovacs_t90s.start_scenario`

Starts a cleaning scenario saved in the Ecovacs app by its id. If the robot
reports saved scenarios, a **Scenario** select and a **Start scenario** button
are created as well.

## Furniture on the map

The robot knows the outline of furniture and carpets
(`getFurnitureInfo_V2`); the map image draws them as a layer on top: carpets
hatched with a dashed outline, other furniture as a light grey shape with its
name, or its type number while the name is unknown. Only type 20 (carpet) is
known for sure; 8 is a bed in the data of deebot-client PR #2030. The T90S
also reports types 2, 4, 1005, 1006, 1014 and 1015, whose meaning is not
documented anywhere yet. If you find out which is which, please open an
issue.

The furniture is read when the active map is known or changes, when the map
outline is sent again, and when the robot pushes `onFurnitureInfo_V2`. The map
image lists it in the `furniture` attribute (also in the diagnostics). Turn
the drawing off with **Configure > Show furniture on the map**.

## Offline use with Matter (local fallback)

If the robot is also paired to Home Assistant over Matter, the integration
finds that Matter device by its serial number (no configuration needed) and
shows it in the **Local fallback** sensor (*available*, *unavailable*, *not
found*). When the robot cannot be reached through the Ecovacs cloud (the
**Cloud connection** sensor is off) or a command fails with a connection
error, the vacuum entity of this integration sends **start, pause, stop,
return to dock and locate** to the Matter vacuum and shows the Matter
vacuum's state instead of going unavailable. **Clean area** works too, if you
mapped the same Home Assistant areas to rooms for both vacuums (area mapping
of the **Clean area** action).

What you keep and lose while the cloud is down:

- kept: start, pause, stop, return to dock, locate, rooms (Clean area), the
  state of the robot (through the Matter vacuum). The Matter device itself
  also keeps its own battery sensor and clean mode select;
- lost: the map, the camera live view, station controls, all settings and
  every other entity of this integration (they become unavailable), zones
  and scenarios, fan speed.

How "cloud down" is detected: `deebot-client` checks every 60 seconds whether
the robot answers through the cloud; in addition every command of this
integration that cannot reach the cloud, or that the cloud reports as robot
offline or unanswered, marks the connection as down at once. It counts as up
again with the next MQTT message of the robot, an answered command or a
check every 30 seconds. With a Matter twin, a command waits at most 15
seconds for the cloud before it goes to Matter.

This has not been tested with a real outage yet. Limitation: if Home
Assistant starts while the Ecovacs cloud is unreachable, this integration
cannot set up (it retries automatically); use the Matter vacuum directly until
then. The integration does not depend on `matter`; without a Matter twin
nothing changes.

## Debug action: `ecovacs_t90s.raw_command`

Sends a JSON command through `deebot-client` and returns the raw answer. It is
meant for exploring undocumented commands. A wrong command can change robot
settings, so be careful with `set...` commands.

```yaml
action: ecovacs_t90s.raw_command
target:
  entity_id: vacuum.my_robot      # or device_id of the robot
data:
  command: getCleanInfo_V2
  params: {}                      # optional JSON object or list
  timeout: 30                     # optional, seconds (1-300)
response_variable: answer
```

The response always contains `success`, `command`, `params`, `device_class`
and `elapsed_seconds`, plus either `response` (the raw API answer, also when
the robot reports an error) or `error` and `message` (timeout, API or
connection errors). Select exactly one robot.

## Extra settings and how they are guarded

At setup the integration asks the robot for each extra setting. An entity is
only created when the robot answers with the expected data; a robot that
answers `20003 rcp not support` gets no entity (logged at debug level), and a
robot that does not answer is asked again later. Setup never waits for these
questions.

| Entity | Read with | Changed with |
| --- | --- | --- |
| Mop wash water temperature (cold/warm/hot/maximum) | `getWashInfo` | `setWashInfo {"mode": 0..3}` |
| Mop drying duration (2/3/4 h) | `getDryingDuration` | `setDryingDuration {"duration": 120/180/240}` |
| Cleaning speed (standard/deep/quick) | `getCustomAreaMode` | `setCustomAreaMode {"sweepMode": 0/1/2}` |
| Edge mopping (off/standard/extreme) | `getMopExpandState` | `setMopExpandState {"enable", "level": 1/0}` |
| TrueDetect sensitivity (standard/high) | `getTrueDetect` | `setTrueDetect {"enable": 1, "level": 1/0}` |
| Do not disturb (keeps the times of the app) | `getBlock` | `setBlock {"enable", "start", "end"}` |
| Scenario select + Start scenario button | `getQuickCommand {"type": "1,2"}` | `clean_V2 qcClean` |
| Voice assistant, Mop only, Automatic firmware updates | `deebot-client` | `deebot-client` |
| Active map (only with more than one map) | `getCachedMapInfo` | `setCachedMapInfo {"act": "userchangemap", "userUsing": 1, "mid"}` |
| Stop washing / drying the mops | – | `stationAction {"act": 4, "type": 4/2}` |
| Position X/Y, Heading (disabled by default) | `getPos` / pushes | – |
| Last clean map (image from the Ecovacs clean log) | clean logs | – |
| Cloud connection, Local fallback (diagnostic) | – | – |

The carpet strategy (`getCarpetInfo`) is not supported by the T90S and was
left out.

## What is verified

On a DEEBOT T90S PRO OMNI (fw 1.50.62), by the owner:

- setup, all entities on one device, no errors in the log (phase 1 code);
- `clean` is rejected with `20003`; `clean_V2` start (payload above) starts
  the robot; `charge {"act": "go"}` docks it; state changes arrive by push;
- `getCleanInfo` and `getCleanInfo_V2` are rejected with `20003`;
  `getChargeState`, `getStationState`, `getStats` and `getPos` work;
- the answers of `getFurnitureInfo_V2`, `getWashInfo`, `getDryingDuration`,
  `getMopExpandState`, `getCustomAreaMode`, `getTrueDetect`, `getBlock`,
  `getQuickCommand` and `getCachedMapInfo` (used as test data);
- the Matter twin exists with the same serial number.

Not yet verified on the robot (taken from captures of the T90 PRO OMNI and
other models in deebot-client issues #2027, #2028, #555, PR #1860, PR #2030
and ecovacs-deebot.js): `clean_V2` pause, resume and stop, `freeClean` rooms
and rectangles, `qcClean`, every `set...` command above, the station stop
buttons, the furniture drawing on the real map, the Matter fallback during a
real outage, the clean log image.

## Supporting another robot model

If `deebot-client` does not know your robot, the log shows
`Device class "xxxxxx" not recognized`. If the robot is a variant of a
supported model, add a line to `EXTRA_DEVICE_CLASSES` in
`custom_components/ecovacs_t90s/device_classes.py`:

```python
EXTRA_DEVICE_CLASSES = {
    "833gj5": "twunby",  # DEEBOT T90S PRO OMNI (Care Kit) -> DEEBOT T90 PRO OMNI
    "xxxxxx": "yyyyyy",  # your model -> supported model with the same features
}
```

The mapping is skipped automatically once `deebot-client` supports a class
natively. `deebot-client` has no public API for this, so the integration fills
its internal capability cache; if that cache changes in a future release, a
warning is logged and the robot is reported as unsupported instead of breaking
setup. Diagnostics show the status of each mapping.

## Privacy and diagnostics

The repository contains no account data or robot identifiers. Your credentials,
the renewable Ecovacs account token and the Live View PIN are stored in the
Home Assistant config entry, like other integrations do. Diagnostics redact
the username, password, tokens, client device ID, PIN, robot IDs, serial
numbers and robot names; they include the state of the extra features
(probes, settings, furniture, cloud connection, local fallback). Debug logs of
`deebot-client` can contain tokens; review them before sharing.

## Known limitations

- Most new features are not verified on a real robot yet, see "What is
  verified".
- Stop: a T90 PRO OMNI of another hardware class (twunby) ignored
  `clean_V2 {"act": "stop"}` in deebot-client PR #1860; whether the T90S does
  is not known yet. **Return to dock** always works.
- The integration cannot start while the Ecovacs cloud is unreachable (see
  the Matter section).
- The live view uses private Ecovacs/AWS Kinesis APIs and can break when
  Ecovacs changes them.
- Robots with class `833gj5` use the capabilities of the T90 PRO OMNI plus the
  extra settings above; other T90S features are not exposed yet.

## Development

The tests use `pytest-homeassistant` of the same version as Home Assistant and
mock all Ecovacs APIs:

```bash
pip install pytest-homeassistant==2026.11.2 deebot-client==18.7.0
pytest tests
```

On Windows put `tests/windows_stubs` on `PYTHONPATH` first. Lint with
`ruff check` and `ruff format` (configuration in `ruff.toml`).

## Credits and license

Licensed under the Apache License 2.0, see [LICENSE](LICENSE). Contains code
from the Home Assistant core `ecovacs` integration (Apache-2.0) and from
ECOVACS Live View (MIT); see [NOTICE](NOTICE). Not affiliated with ECOVACS or
the Home Assistant project.

## Nederlands

Onofficiële Home Assistant-integratie voor Ecovacs DEEBOT-robots, gemaakt voor
de **DEEBOT T90S PRO OMNI**. Eén configuratie en één apparaat per robot met
alles van de ingebouwde Ecovacs-integratie, plus het live camerabeeld van
ECOVACS Live View en ondersteuning voor de T90S (apparaatklasse `833gj5`).

- **Installeren:** via HACS als aangepaste repository
  (`https://github.com/basschoren/ha-ecovacs-t90s`, categorie Integratie) of
  door `custom_components/ecovacs_t90s/` naar `/config/custom_components/` te
  kopiëren. Herstart daarna Home Assistant.
- **Instellen:** voeg **Ecovacs DEEBOT T90S** toe, kies **Cloud** en log één
  keer in met je Ecovacs-account en land. De Live View-pincode is optioneel en
  kan later via **Configureren** worden ingesteld, net als na hoeveel minuten
  live view automatisch stopt en of meubels op de kaart staan.
- **Starten en kamers:** de T90-reeks weigert het `clean`-commando van
  `deebot-client`; deze integratie stuurt `clean_V2` (en valt terug op `clean`
  als een robot dat weigert). Kamers zonder naam heten "Kamer 3".
- **Meubels op de kaart:** tapijten gearceerd, andere meubels grijs met naam of
  typenummer. De lijst staat in het attribuut `furniture` van de kaart.
- **Offline met Matter:** is de robot ook via Matter gekoppeld, dan vindt de
  integratie die op serienummer. Is de Ecovacs-cloud onbereikbaar, dan gaan
  starten, pauzeren, stoppen, terug naar het station, lokaliseren en
  kamers (Ruimte schoonmaken) via Matter en toont de stofzuiger de status van
  Matter. Kaart, camera, stationsbediening en instellingen zijn dan niet
  beschikbaar. Nog niet getest tijdens een echte storing.
- **Extra instellingen:** watertemperatuur en droogduur van de dweilen,
  schoonmaaksnelheid, randdweilen, TrueDetect-gevoeligheid, niet storen,
  spraakassistent, alleen dweilen, automatische updates, knoppen om wassen of
  drogen te stoppen, scenario's, positie en de kaart van de laatste beurt.
  Alleen wat de robot ondersteunt wordt aangemaakt.
- **Acties:** `ecovacs_t90s.clean_zone` (rechthoeken in mm en kamers in één
  taak), `ecovacs_t90s.start_scenario` en `ecovacs_t90s.raw_command` (stuurt
  een willekeurig JSON-commando en geeft het onbewerkte antwoord terug).
- **Status:** start, terug naar het station en de antwoorden van de
  instellingen zijn op een echte T90S gecontroleerd; de rest nog niet.
